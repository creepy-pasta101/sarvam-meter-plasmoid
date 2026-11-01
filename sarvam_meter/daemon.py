# SPDX-License-Identifier: MIT
"""Local daemon serving the widget's status JSON over a Unix socket.

There is no TCP listener anywhere in this process.  The daemon binds one
``AF_UNIX`` stream socket -- ``$XDG_RUNTIME_DIR/sarvam-meter/daemon.sock`` by
default, ``~/.cache/sarvam-meter/daemon.sock`` when ``XDG_RUNTIME_DIR`` is
unset, and ``daemon.socket_path`` when the config sets it -- at mode ``0600``
inside a ``0700`` directory.  Only processes that can open that file can talk
to it, which is the point: a loopback port is reachable by every local process.

Protocol: one JSON object per line in each direction.

================================  ==============================================
request                           behaviour
================================  ==============================================
``{"op": "status"}``              the status payload (was ``GET /v1/status``)
``{"op": "health"}``              ``{"ok": true, "version": ..., "uptime_seconds": N}``
``{"op": "refresh"}``             force a cliproxy *and* dashboard re-poll, return status
``{"op": "config.get"}``          sanitised config, secrets redacted
``{"op": "config.set", ...}``     rewrite the *writable* subset: ``{"config": {...}}``
``{"op": "session.get"}``         whether a dashboard session is stored (names only)
``{"op": "session.set", ...}``    store a captured ``Cookie``/``Authorization``
anything else                     ``{"ok": false, "error": {"code": "unknown_op", ...}}``
================================  ==============================================

Every reply is either ``{"ok": true, "result": <json>}`` or
``{"ok": false, "error": {"code": "<code>", "message": "<text>"}}``.  A line
that is not a JSON object is ``bad_request``; a line longer than
:data:`MAX_LINE_BYTES` is ``too_large`` and closes that connection.  The client
(``sarvam_meter.client``) opens a connection per request, but a client that
keeps one open may send several ops down it: replies come back in order.

The two mutating ops are ``config.set`` -- what the plasmoid's settings page
calls -- and ``session.set`` -- what the plasmoid's sign-in webview calls once
the user has signed in to the Sarvam dashboard.

``config.set``: only the keys listed in :data:`WRITABLE_SECTIONS` may be
changed, values are validated by the same rules :func:`config.load_config`
uses, and the file is replaced atomically at mode ``0600``.

``session.set``: the request carries the raw ``Cookie`` and/or
``Authorization`` header value the webview captured.  Both are bounded
(:data:`SESSION_FIELD_LIMITS`), rejected if they could inject a header
(CR/LF/NUL), and written to ``dashboard.session_file`` atomically at mode
``0600`` under the canonical key names :func:`dashboard.load_session` reads
(``{"Cookie": ..., "Authorization": ...}``).  A stored value is never echoed
back, never logged and never appears in an error message: ``session.get``
reports only the header *names*, the path and the file mode.

Both source snapshots (CLIProxyAPI and the Sarvam dashboard) are refreshed on
background threads behind locks; a slow or unreachable source never delays
``status``, which always answers from the cached snapshots.  The dashboard
snapshot additionally has a TTL of ``dashboard.poll_seconds``.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .client import socket_path as resolve_socket_path

from .config import (
    DEFAULT_CONFIG_PATH,
    ENV_CONFIG_PATH,
    ConfigError,
    default_config_dict,
    load_config,
)
from .status import build_status

try:  # pragma: no cover - U1 owns __init__.py
    from . import __version__ as VERSION
except Exception:  # noqa: BLE001
    VERSION = "0.1.0"

#: Every op the daemon answers, in the order the documentation lists them.
OPS: Tuple[str, ...] = (
    "status",
    "health",
    "refresh",
    "config.get",
    "config.set",
    "session.get",
    "session.set",
)

#: How long ``accept()`` waits before the loop checks its stop flag.
ACCEPT_POLL_SECONDS = 0.2
#: Upper bound on one request line.  A longer line is answered ``too_large``
#: and that connection is closed (the rest of it cannot be trusted).
MAX_LINE_BYTES = 256 * 1024
#: How long a connection may stay silent before it is closed.  One thread per
#: connection, so an idle client must never hold a thread for ever.
IDLE_TIMEOUT_SECONDS = 10.0
#: Upper bound on a ``config.set`` payload (a settings document is tiny).
MAX_CONFIG_BODY_BYTES = 64 * 1024
#: Upper bound on a ``session.set`` payload (16 KiB cookie + 8 KiB bearer).
MAX_SESSION_BODY_BYTES = 64 * 1024

#: Sections and keys ``config.set`` may change.  Everything else in the
#: document (``cliproxy.management_key``, ``sarvam.*``, ``daemon.port``, ...) is
#: deliberately not writable over the socket.
WRITABLE_SECTIONS: Dict[str, Tuple[str, ...]] = {
    "dashboard": ("enabled", "base_url", "poll_seconds", "session_file"),
    "cliproxy": ("enabled", "base_url", "api_version"),
    "daemon": ("refresh_seconds",),
}

#: Sections that exist in the config document but are read-only over the socket.  A
#: key sent inside one of these is named in full (``sarvam.starting_balance is
#: not writable``), so the settings page can point at the offending row.
READ_ONLY_SECTIONS = frozenset(["sarvam"])

#: The only keys ``session.set`` accepts, and how long each value may be.
SESSION_FIELD_LIMITS: Dict[str, int] = {
    "cookie": 16 * 1024,
    "authorization": 8 * 1024,
}
#: Canonical order (and spelling) of the header names a session file holds.
#: Mirrors ``dashboard.SESSION_HEADERS``; the *file* keys are capitalised, the
#: keys of a request body and of ``load_session()`` are lower case.
SESSION_HEADER_NAMES: Tuple[str, ...] = ("cookie", "authorization")
#: request-body key -> the key the session file (and ``load_session``) uses.
SESSION_FILE_KEYS: Dict[str, str] = {
    "cookie": "Cookie",
    "authorization": "Authorization",
}
#: Mode the session file is written with, as reported by both ops.
SESSION_FILE_MODE = "0600"
#: Reported mode when there is no session file (no permissions exist to report).
SESSION_NO_FILE_MODE = "0000"
#: Characters that would let a captured value inject a header or truncate a
#: value; a request carrying one is rejected before anything is written.
SESSION_FORBIDDEN_CHARS = "\r\n\x00"

_SECRET_KEYS = frozenset(
    [
        "api_key",
        "apikey",
        "api_subscription_key",
        "api-subscription-key",
        "authorization",
        "access_token",
        "management_key",
        "password",
        "refresh_token",
        "secret",
        "token",
        "x-management-key",
    ]
)


def _redact_tree(value: Any) -> Any:
    """Recursively replace secret-looking values with ``"***"`` (``""`` if unset)."""
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in _SECRET_KEYS:
                out[key] = "***" if item else ""
            else:
                out[key] = _redact_tree(item)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact_tree(item) for item in value]
    return value


class ConfigUpdateError(Exception):
    """A ``config.set`` request was rejected and **nothing** was written."""

    code = "invalid_config"

    def __init__(self, message: str, code: str = "invalid_config") -> None:
        super().__init__(message)
        self.code = code


def _invalid_config(message: str) -> dict:
    """The ``error`` shape for a rejected config update."""
    return {"error": {"message": message, "code": "invalid_config"}}


class SessionUpdateError(Exception):
    """A ``session.set`` request was rejected and **nothing** was written.

    The message names the offending *field* (``cookie``/``authorization``) or
    the path -- never a value, not even a prefix of one.
    """

    code = "invalid_session"

    def __init__(self, message: str, code: str = "invalid_session") -> None:
        super().__init__(message)
        self.code = code


def _invalid_session(message: str) -> dict:
    """The ``error`` shape for a rejected session capture."""
    return {"error": {"message": message, "code": "invalid_session"}}


def resolve_config_path(path: Optional[str] = None) -> str:
    """The config file the daemon reads and writes.

    Same resolution order as :func:`config.load_config` -- explicit path, then
    ``$SARVAM_METER_CONFIG``, then ``~/.config/sarvam-meter/config.json`` -- so
    the write endpoint edits the file the daemon was started from.
    """
    if path is not None and str(path).strip():
        return os.path.expanduser(str(path))
    env_path = os.environ.get(ENV_CONFIG_PATH, "")
    if env_path.strip():
        return os.path.expanduser(env_path.strip())
    return DEFAULT_CONFIG_PATH


def _section(obj: Any, names: List[str]) -> Dict[str, Any]:
    return {name: getattr(obj, name, None) for name in names}


def sanitized_config(config: Any) -> dict:
    """Config as a JSON-safe dict with every secret redacted."""
    document = {
        "version": getattr(config, "version", 1),
        "db_path": getattr(config, "db_path", ""),
        "daemon": _section(
            getattr(config, "daemon", None),
            ["socket_path", "refresh_seconds", "request_timeout_seconds"],
        ),
        "sarvam": _section(
            getattr(config, "sarvam", None),
            ["enabled", "currency", "starting_balance", "rate_overrides"],
        ),
        "cliproxy": _section(
            getattr(config, "cliproxy", None),
            [
                "enabled",
                "base_url",
                "management_key",
                "api_version",
                "quota_fetch",
                "quota_fetch_interval_seconds",
                "include_api_key_usage",
                "allow_remote",
            ],
        ),
        "dashboard": _section(
            getattr(config, "dashboard", None),
            ["enabled", "base_url", "session_file", "poll_seconds", "product"],
        ),
    }
    return _redact_tree(document)


# --------------------------------------------------------------------------
# writable-subset updates (config.set)
# --------------------------------------------------------------------------


def merge_writable_subset(document: dict, subset: dict) -> dict:
    """Return a deep copy of *document* with *subset* merged over it.

    Every writable value is a scalar, so a per-section update is exactly the
    right merge: keys the request does not mention keep their current value and
    no section is ever replaced wholesale.
    """
    merged = copy.deepcopy(document)
    for section, values in subset.items():
        current = merged.get(section)
        if not isinstance(current, dict):
            current = {}
        else:
            current = dict(current)
        current.update(copy.deepcopy(values))
        merged[section] = current
    return merged


def check_writable_subset(subset: Any) -> dict:
    """Reject any section or key that is not in :data:`WRITABLE_SECTIONS`.

    The whole request is rejected on the first offending key -- nothing is ever
    applied partially -- and the message names the key.
    """
    if not isinstance(subset, dict):
        raise ConfigUpdateError("request body must be a JSON object")
    for section in sorted(subset, key=lambda item: str(item)):
        if not isinstance(section, str):
            raise ConfigUpdateError("%s is not writable" % (section,))
        values = subset[section]
        if section not in WRITABLE_SECTIONS:
            if section in READ_ONLY_SECTIONS and isinstance(values, dict) and values:
                key = sorted(values, key=lambda item: str(item))[0]
                raise ConfigUpdateError("%s.%s is not writable" % (section, key))
            raise ConfigUpdateError("%s is not writable" % (section,))
        if not isinstance(values, dict):
            raise ConfigUpdateError("%s: must be a JSON object" % (section,))
        allowed = WRITABLE_SECTIONS[section]
        for key in sorted(values, key=lambda item: str(item)):
            if not isinstance(key, str) or key not in allowed:
                raise ConfigUpdateError("%s.%s is not writable" % (section, key))
    return subset


def decode_config_request(request: Any) -> dict:
    """Decode a request body (``dict``, ``bytes`` or ``str``) to a dict.

    A malformed body produces a fixed message: the body is never echoed back,
    so a value the caller sent (a key, a session path) cannot bounce out of an
    error response.
    """
    if isinstance(request, dict):
        return request
    if isinstance(request, (bytes, bytearray)):
        try:
            request = bytes(request).decode("utf-8")
        except UnicodeDecodeError:
            raise ConfigUpdateError("request body must be UTF-8 encoded JSON") from None
    if isinstance(request, str):
        if not request.strip():
            raise ConfigUpdateError("request body must be a JSON object")
        try:
            request = json.loads(request)
        except ValueError:
            raise ConfigUpdateError("request body is not valid JSON") from None
    if not isinstance(request, dict):
        raise ConfigUpdateError("request body must be a JSON object")
    return request


def write_config_atomic(path: str, document: dict) -> None:
    """Write *document* to *path* atomically, with mode ``0600``.

    The temporary file lives in the target's own directory so the final step is
    a rename (``os.replace``), never a copy; it is chmodded to ``0600`` before a
    single byte is written, and it is fsynced before the rename.  A crash
    mid-write therefore leaves either the old file or the new one, never a
    truncated or world-readable one.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    if not os.path.isdir(directory):
        os.makedirs(directory, mode=0o700, exist_ok=True)
    payload = json.dumps(document, indent=2) + "\n"
    handle_fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".config-", suffix=".tmp")
    try:
        os.fchmod(handle_fd, 0o600)
        handle = os.fdopen(handle_fd, "w", encoding="utf-8")
    except BaseException:
        try:
            os.close(handle_fd)
        except OSError:  # pragma: no cover
            pass
        try:
            os.unlink(tmp_path)
        except OSError:  # pragma: no cover
            pass
        raise
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:  # pragma: no cover
            pass
        raise


# --------------------------------------------------------------------------
# the dashboard session (session.get / session.set)
# --------------------------------------------------------------------------


def decode_session_request(request: Any) -> dict:
    """Decode a request body (``dict``, ``bytes`` or ``str``) to a dict.

    Every failure message is fixed: the body is never echoed back, so a value
    the caller sent can never bounce out of an error response.
    """
    if isinstance(request, dict):
        return request
    if isinstance(request, (bytes, bytearray)):
        try:
            request = bytes(request).decode("utf-8")
        except UnicodeDecodeError:
            raise SessionUpdateError(
                "request body must be UTF-8 encoded JSON"
            ) from None
    if isinstance(request, str):
        if not request.strip():
            raise SessionUpdateError("request body must be a JSON object")
        try:
            request = json.loads(request)
        except ValueError:
            raise SessionUpdateError("request body is not valid JSON") from None
    if not isinstance(request, dict):
        raise SessionUpdateError("request body must be a JSON object")
    return request


def check_session_request(request: Any) -> Dict[str, str]:
    """Validate a session capture and return ``{lower-name: value}``.

    Only ``cookie`` and ``authorization`` may appear; any other key rejects the
    whole request (nothing is ever stored partially).  Each value is bounded by
    :data:`SESSION_FIELD_LIMITS`, must be a string, must not contain CR, LF or
    NUL, and is stripped the way :func:`dashboard.load_session` strips it, so
    what is written is exactly what the poller will send.  An empty value counts
    as *not supplied*; at least one header must remain.

    Raises :class:`SessionUpdateError`; no message ever carries a value.
    """
    document = decode_session_request(request)
    for key in sorted(document, key=lambda item: str(item)):
        if not isinstance(key, str) or key not in SESSION_FIELD_LIMITS:
            raise SessionUpdateError("%s is not a session field" % (key,))

    session: Dict[str, str] = {}
    for name in SESSION_HEADER_NAMES:
        if name not in document:
            continue
        value = document[name]
        if not isinstance(value, str):
            raise SessionUpdateError("%s: must be a string" % (name,))
        for char in SESSION_FORBIDDEN_CHARS:
            if char in value:
                raise SessionUpdateError(
                    "%s: must not contain CR, LF or NUL" % (name,)
                )
        limit = SESSION_FIELD_LIMITS[name]
        if len(value.encode("utf-8")) > limit:
            raise SessionUpdateError("%s: must be at most %d bytes" % (name, limit))
        stripped = value.strip()
        if stripped:
            session[name] = stripped

    if not session:
        raise SessionUpdateError(
            "at least one of cookie or authorization is required"
        )
    return session


def write_session_atomic(path: str, session: Dict[str, str]) -> None:
    """Write a captured *session* to *path* atomically, with mode ``0600``.

    The document uses the canonical key names :func:`dashboard.load_session`
    reads (``{"Cookie": ..., "Authorization": ...}``) and omits a header that
    was not supplied.  The temporary file lives in the target's own directory so
    the final step is a rename, is chmodded to ``0600`` before a single byte is
    written, and is fsynced before the rename; the parent directory is created
    at ``0700`` when missing.  A crash mid-write therefore leaves either the old
    session or the new one, never a truncated or world-readable one.
    """
    expanded = os.path.expanduser(str(path))
    directory = os.path.dirname(os.path.abspath(expanded)) or "."
    if not os.path.isdir(directory):
        os.makedirs(directory, mode=0o700, exist_ok=True)
    document = dict(
        (SESSION_FILE_KEYS[name], session[name])
        for name in SESSION_HEADER_NAMES
        if name in session
    )
    payload = json.dumps(document, indent=2) + "\n"
    handle_fd, tmp_path = tempfile.mkstemp(
        dir=directory, prefix=".session-", suffix=".tmp"
    )
    try:
        os.fchmod(handle_fd, 0o600)
        handle = os.fdopen(handle_fd, "w", encoding="utf-8")
    except BaseException:
        try:
            os.close(handle_fd)
        except OSError:  # pragma: no cover
            pass
        try:
            os.unlink(tmp_path)
        except OSError:  # pragma: no cover
            pass
        raise
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, expanded)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:  # pragma: no cover
            pass
        raise


def _session_header_names() -> Tuple[str, ...]:
    """The header names a session file may carry (``dashboard.SESSION_HEADERS``)."""
    try:
        from .dashboard import SESSION_HEADERS
    except Exception:  # noqa: BLE001 - dashboard.py may legitimately be absent
        return SESSION_HEADER_NAMES
    names = tuple(str(name).strip().lower() for name in SESSION_HEADERS)
    return names or SESSION_HEADER_NAMES


def read_session_header_names(path: str) -> List[str]:
    """The header *names* stored in a session file, never a value.

    Read through :func:`dashboard.load_session` when the module is importable,
    so this agrees exactly with what the next poll will send; otherwise the file
    is parsed with the same rules (a non-empty string under an accepted name).
    Any unreadable or malformed file simply has no usable headers.
    """
    names = _session_header_names()
    expanded = os.path.expanduser(str(path))
    try:
        from .dashboard import load_session
    except Exception:  # noqa: BLE001 - dashboard.py may legitimately be absent
        load_session = None  # type: ignore[assignment]
    if load_session is not None:
        try:
            session = load_session(expanded)
        except Exception:  # noqa: BLE001 - a bad file holds no header names
            return []
        if not isinstance(session, dict):  # pragma: no cover - defensive
            return []
        return [name for name in names if session.get(name)]
    try:
        with open(expanded, "rb") as handle:
            document = json.loads(handle.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return []
    if not isinstance(document, dict):
        return []
    present = set()
    for key, value in document.items():
        name = str(key).strip().lower()
        if name in names and isinstance(value, str) and value.strip():
            present.add(name)
    return [name for name in names if name in present]


def validate_config_document(document: dict) -> Any:
    """Run *document* through :func:`config.load_config` without touching the
    real file, and return the resulting :class:`Config`.

    The document is written to a throwaway directory (mode ``0700``, file
    ``0600``) and loaded from there, so the validation rules are exactly
    ``load_config``'s -- the same merge over
    :func:`config.default_config_dict`, the same bounds -- while a rejected
    document never reaches the live config file.  The scratch copy holds
    whatever the real file holds (including ``cliproxy.management_key``), so it
    is deleted before returning, success or not, and is never world-readable.
    """
    scratch = tempfile.mkdtemp(prefix="sarvam-meter-config-")
    try:
        probe = os.path.join(scratch, "config.json")
        write_config_atomic(probe, document)
        return load_config(probe)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _make_cliproxy_client(config: Any) -> Any:
    """Construct a :class:`CliproxyClient` (imported lazily; may not exist yet)."""
    from .cliproxy import CliproxyClient

    section = config.cliproxy
    timeout = float(getattr(getattr(config, "daemon", None), "request_timeout_seconds", 10) or 10)
    return CliproxyClient(
        str(getattr(section, "base_url", "")),
        str(getattr(section, "management_key", "")),
        api_version=str(getattr(section, "api_version", "v8") or "v8"),
        timeout=timeout,
        allow_remote=bool(getattr(section, "allow_remote", False)),
        include_api_key_usage=bool(getattr(section, "include_api_key_usage", False)),
    )


def fetch_cliproxy_snapshot(config: Any) -> dict:
    """Poll CLIProxyAPI once and return a snapshot dict.

    Never raises: an unreachable or misbehaving CLIProxyAPI becomes an
    ``errors`` entry.  Returns a "configured: false" snapshot when the source is
    disabled (callers pass it straight to :func:`build_status`).
    """
    section = getattr(config, "cliproxy", None)
    configured = bool(getattr(section, "enabled", False))
    base: Dict[str, Any] = {
        "configured": configured,
        "reachable": False,
        "api_version": str(getattr(section, "api_version", "v8") or "v8"),
        "accounts": [],
        "errors": [],
    }
    if not configured:
        return base
    try:
        client = _make_cliproxy_client(config)
        snapshot = client.snapshot()
    except Exception as exc:  # noqa: BLE001 - never raise out of a refresh
        base["errors"] = [
            {
                "source": "cliproxy",
                "code": "unreachable",
                "message": str(exc) or exc.__class__.__name__,
            }
        ]
        return base
    if not isinstance(snapshot, dict):
        base["errors"] = [
            {
                "source": "cliproxy",
                "code": "bad_response",
                "message": "cliproxy snapshot was not an object",
            }
        ]
        return base
    merged = dict(snapshot)
    merged.setdefault("configured", True)
    merged.setdefault("errors", [])
    merged.setdefault("accounts", [])
    merged.setdefault("reachable", False)
    merged.setdefault("api_version", base["api_version"])
    return merged


#: Default window used when no starting balance has been set yet.
def _dashboard_dates(ledger: Any) -> Tuple[str, str]:
    """Return ``(since_date, today)`` as ``YYYY-MM-DD`` strings.

    ``since_date`` is the date part of ``balance.set_at`` (SPEC 17.5): the window
    the dashboard is asked about starts when the starting balance was recorded.
    Without a balance (or without a readable ledger) the window falls back to the
    first day of the current month, so the daemon still has a bounded query.
    """
    now = datetime.now(timezone.utc).astimezone()
    today = now.strftime("%Y-%m-%d")
    set_at = None
    try:
        balance = ledger.get_balance() if ledger is not None else None
        if isinstance(balance, dict):
            candidate = balance.get("set_at")
            if isinstance(candidate, str):
                set_at = candidate
    except Exception:  # noqa: BLE001 - a broken ledger must not break the refresh
        set_at = None
    if set_at:
        head = set_at.strip()[:10]
        try:
            datetime.strptime(head, "%Y-%m-%d")
        except ValueError:
            head = ""
        if head and head <= today:
            return head, today
    return now.replace(day=1).strftime("%Y-%m-%d"), today


def _empty_dashboard_snapshot(configured: bool) -> dict:
    return {
        "configured": bool(configured),
        "reachable": False,
        "as_of": None,
        "org_id": None,
        "currency": "INR",
        "window": {"start": None, "end": None},
        "spend": {"window": 0.0, "today": 0.0, "month": 0.0},
        "balance": {
            "known": False,
            "amount": None,
            "currency": "INR",
            "as_of": None,
            "state": "",
            "billing_model": "",
            "unit": "",
        },
        "by_model": [],
        "usage": [],
        "error": None,
    }


def fetch_dashboard_snapshot(config: Any, ledger: Any = None) -> dict:
    """Poll the Sarvam dashboard once and return its snapshot dict.

    Never raises.  A disabled source, a missing session file, an unreachable
    dashboard or a bad response all become a snapshot with ``error`` set, so the
    caller can pass the result straight to :func:`build_status`.  Session values
    never appear in the returned dict or in an error message.
    """
    section = getattr(config, "dashboard", None)
    configured = bool(getattr(section, "enabled", False))
    base = _empty_dashboard_snapshot(configured)
    if not configured:
        return base
    since_date, today = _dashboard_dates(ledger)
    base["window"] = {"start": since_date, "end": today}
    try:
        # Imported lazily, exactly like the cliproxy client: ``dashboard.py`` is
        # owned by another unit and may legitimately be absent.
        from .dashboard import DashboardClient, DashboardError, load_session
    except Exception as exc:  # noqa: BLE001 - no dashboard module: report, do not raise
        base["error"] = {
            "source": "dashboard",
            "code": "unreachable",
            "message": str(exc) or exc.__class__.__name__,
        }
        return base

    try:
        session = load_session(str(getattr(section, "session_file", "") or ""))
        client = DashboardClient(
            str(getattr(section, "base_url", "")),
            session,
            timeout=float(
                getattr(
                    getattr(config, "daemon", None), "request_timeout_seconds", 10
                )
                or 10
            ),
            product=str(
                getattr(section, "product", "sarvam-api-platform")
                or "sarvam-api-platform"
            ),
        )
        snapshot = client.snapshot(since_date=since_date, today=today)
    except DashboardError as exc:
        base["error"] = {
            "source": "dashboard",
            "code": str(getattr(exc, "code", None) or "unreachable"),
            "message": str(exc) or exc.__class__.__name__,
        }
        return base
    except Exception as exc:  # noqa: BLE001 - never raise out of a refresh
        base["error"] = {
            "source": "dashboard",
            "code": "unreachable",
            "message": str(exc) or exc.__class__.__name__,
        }
        return base
    if not isinstance(snapshot, dict):
        base["error"] = {
            "source": "dashboard",
            "code": "bad_response",
            "message": "dashboard snapshot was not an object",
        }
        return base
    merged = _empty_dashboard_snapshot(True)
    merged.update(snapshot)
    merged["configured"] = bool(snapshot.get("configured", True))
    merged["reachable"] = bool(snapshot.get("reachable"))
    if not isinstance(merged.get("window"), dict):
        merged["window"] = base["window"]
    return merged


class _BrokenLedger:
    """Stand-in used when the ledger cannot be opened (edge case: unwritable DB).

    Every attribute access raises, so :func:`build_status` reports a
    ``ledger`` / ``read_failed`` error and still serves a zeroed payload.
    """

    db_path = None

    def __init__(self, error: Any) -> None:
        self._error = error

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)

        def _fail(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("ledger unavailable: %s" % (self._error,))

        return _fail


# --------------------------------------------------------------------------
# the socket server (the only listener this process has)
# --------------------------------------------------------------------------


def _ok_reply(result: Any) -> dict:
    """The ``ok: true`` envelope."""
    return {"ok": True, "result": result}


def _error_reply(code: str, message: str) -> dict:
    """The ``ok: false`` envelope."""
    return {"ok": False, "error": {"code": str(code), "message": str(message)}}


def ensure_socket_directory(path: str) -> str:
    """Create the socket's parent directory at mode ``0700`` when it is missing.

    An existing directory is left exactly as it is: it may be the user's
    ``$XDG_RUNTIME_DIR`` itself, and tightening the mode of a directory this
    process does not own would be a nasty surprise.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    if not os.path.isdir(directory):
        os.makedirs(directory, mode=0o700, exist_ok=True)
        try:
            # ``mkdir`` honours the umask; this makes 0700 certain.
            os.chmod(directory, 0o700)
        except OSError:  # pragma: no cover - e.g. a foreign filesystem
            pass
    return directory


def socket_is_live(path: str, timeout: float = 0.5) -> bool:
    """True when something accepts a connection on the socket *path*."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(timeout)
        probe.connect(path)
        return True
    except OSError:
        return False
    finally:
        try:
            probe.close()
        except OSError:  # pragma: no cover
            pass


def prepare_socket_path(path: str) -> None:
    """Make *path* bindable: create the directory, drop a **stale** socket file.

    A file that is not a socket is never removed -- deleting something this
    process did not create would be a destructive surprise -- and a socket that
    still answers means another daemon owns the path, so it is left alone and
    the caller's ``bind()`` fails loudly instead of stealing the socket.
    """
    ensure_socket_directory(path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:  # pragma: no cover - defensive
        raise OSError("cannot stat %s (%s)" % (path, exc.strerror or exc)) from None
    if not stat.S_ISSOCK(info.st_mode):
        raise OSError("%s exists and is not a socket" % (path,))
    if socket_is_live(path):
        raise OSError("another daemon is already listening on %s" % (path,))
    try:
        os.unlink(path)
    except FileNotFoundError:  # pragma: no cover - raced with another daemon
        return
    except OSError as exc:  # pragma: no cover - defensive
        raise OSError(
            "cannot remove the stale socket %s (%s)" % (path, exc.strerror or exc)
        ) from None


class SocketServer:
    """A ``SOCK_STREAM`` Unix-domain server speaking newline-delimited JSON.

    One thread per connection (the same concurrency the removed HTTP server
    had), an accept loop that polls with a short timeout so
    :meth:`shutdown` never blocks, and a per-connection idle timeout so a
    client that connects and says nothing cannot hold a thread for ever.
    """

    def __init__(
        self,
        path: str,
        dispatch: Callable[[bytes], dict],
        idle_timeout: float = IDLE_TIMEOUT_SECONDS,
        logger: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self.path = path
        try:
            self.idle_timeout = float(idle_timeout)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            self.idle_timeout = IDLE_TIMEOUT_SECONDS
        if self.idle_timeout <= 0:
            self.idle_timeout = IDLE_TIMEOUT_SECONDS
        self._dispatch = dispatch
        self._logger = logger
        self._socket: Optional[socket.socket] = None
        self._identity: Optional[Tuple[int, int]] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._accept_thread: Optional[threading.Thread] = None
        self._threads: List[threading.Thread] = []
        self._connections: List[socket.socket] = []

    # -- setup -----------------------------------------------------------
    @property
    def server_address(self) -> str:
        """The bound path (the analogue of an HTTP server's address)."""
        return self.path

    @property
    def thread(self) -> Optional[threading.Thread]:
        """The accept thread, once :meth:`start` has been called."""
        return self._accept_thread

    def bind(self) -> str:
        """Create the socket, ``chmod`` it to ``0600`` and start listening."""
        prepare_socket_path(self.path)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(self.path)
            os.chmod(self.path, 0o600)
            info = os.lstat(self.path)
            server.listen(64)
            # A short timeout keeps the accept loop responsive to ``_stop``.
            server.settimeout(ACCEPT_POLL_SECONDS)
        except BaseException:
            try:
                server.close()
            except OSError:  # pragma: no cover
                pass
            raise
        self._socket = server
        #: Identity of the file we created, so shutdown only unlinks our own.
        self._identity = (info.st_dev, info.st_ino)
        return self.path

    def start(self) -> None:
        """Start the accept loop on a background thread (binds if needed)."""
        if self._socket is None:
            self.bind()
        if self._accept_thread is None or not self._accept_thread.is_alive():
            self._stop.clear()
            self._accept_thread = threading.Thread(
                target=self.serve_forever,
                name="sarvam-meter-socket",
                daemon=True,
            )
            self._accept_thread.start()

    # -- the accept loop -------------------------------------------------
    def serve_forever(self) -> None:
        server = self._socket
        if server is None:
            raise RuntimeError("the socket server is not bound")
        self._stop.clear()
        while not self._stop.is_set():
            try:
                connection, _address = server.accept()
            except socket.timeout:
                continue
            except OSError:
                # The listening socket was closed under us, or the stop flag
                # is set: either way this loop is finished.
                if self._stop.is_set() or self._socket is None:
                    break
                continue
            thread = threading.Thread(
                target=self._serve_connection,
                args=(connection,),
                name="sarvam-meter-client",
                daemon=True,
            )
            with self._lock:
                self._threads = [item for item in self._threads if item.is_alive()]
                self._threads.append(thread)
            thread.start()

    # -- one connection --------------------------------------------------
    def _serve_connection(self, connection: socket.socket) -> None:
        with self._lock:
            self._connections.append(connection)
        try:
            connection.settimeout(self.idle_timeout)
            buffer = b""
            while not self._stop.is_set():
                try:
                    chunk = connection.recv(65536)
                except socket.timeout:
                    self._emit(
                        "idle connection closed after %.1fs" % (self.idle_timeout,)
                    )
                    return
                except OSError:
                    return
                if not chunk:
                    return
                buffer += chunk
                while True:
                    newline = buffer.find(b"\n")
                    if newline < 0:
                        break
                    line = buffer[:newline]
                    buffer = buffer[newline + 1:]
                    if not self._serve_line(connection, line):
                        return
                if len(buffer) > MAX_LINE_BYTES:
                    # No newline yet and already over the limit: the rest of
                    # the line cannot be trusted, so close the connection.
                    self._emit("request line too large; closing the connection")
                    self._send(
                        connection,
                        _error_reply("too_large", "request line is too large"),
                    )
                    return
        finally:
            with self._lock:
                try:
                    self._connections.remove(connection)
                except ValueError:  # pragma: no cover - defensive
                    pass
            try:
                connection.close()
            except OSError:  # pragma: no cover
                pass

    def _serve_line(self, connection: socket.socket, line: bytes) -> bool:
        """Answer one request line; ``False`` means "close this connection"."""
        if len(line) > MAX_LINE_BYTES:
            self._emit("request line too large; closing the connection")
            self._send(
                connection, _error_reply("too_large", "request line is too large")
            )
            return False
        try:
            reply = self._dispatch(line)
        except Exception as exc:  # noqa: BLE001 - never leak a traceback
            reply = _error_reply(
                "internal_error", "internal error: %s" % (exc.__class__.__name__,)
            )
        return self._send(connection, reply)

    def _send(self, connection: socket.socket, reply: dict) -> bool:
        try:
            payload = (json.dumps(reply) + "\n").encode("utf-8")
        except (TypeError, ValueError):  # pragma: no cover - a payload must be JSON
            payload = (
                json.dumps(_error_reply("internal_error", "reply was not JSON")) + "\n"
            ).encode("utf-8")
        try:
            connection.sendall(payload)
        except OSError:
            return False
        return True

    # -- teardown --------------------------------------------------------
    def shutdown(self) -> None:
        """Ask the accept loop to stop and wait for it."""
        self._stop.set()
        thread = self._accept_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    def server_close(self) -> None:
        """Close the listening socket, every connection, and unlink the file."""
        self._stop.set()
        server = self._socket
        self._socket = None
        if server is not None:
            try:
                server.close()
            except OSError:  # pragma: no cover
                pass
        with self._lock:
            connections = list(self._connections)
            self._connections = []
        for connection in connections:
            try:
                connection.close()
            except OSError:  # pragma: no cover
                pass
        self._unlink()

    def _unlink(self) -> None:
        """Remove the socket file, but only if it is still the one we created.

        Nothing is removed when this server never bound (``identity`` is
        ``None``) or when the path now holds a different file: a second daemon
        that took the path over must keep its socket.
        """
        identity = self._identity
        self._identity = None
        if identity is None:
            return
        try:
            info = os.lstat(self.path)
        except OSError:
            return
        if (info.st_dev, info.st_ino) != identity:
            return
        try:
            os.unlink(self.path)
        except OSError:  # pragma: no cover - defensive
            pass

    def _emit(self, line: str) -> None:
        logger = self._logger
        if logger is None:
            return
        try:
            logger(line)
        except Exception:  # pragma: no cover - a logger must never break serving
            pass


# --------------------------------------------------------------------------
# the ops (each one is exactly the handler the removed HTTP route called)
# --------------------------------------------------------------------------


def _encoded_size(value: Any) -> int:
    """The byte length of *value* once encoded as JSON (``0`` when it cannot be)."""
    try:
        return len(json.dumps(value).encode("utf-8"))
    except (TypeError, ValueError):  # pragma: no cover - a decoded body always can
        return 0


def _op_status(daemon: "Daemon", request: dict) -> Any:
    return daemon.status()


def _op_health(daemon: "Daemon", request: dict) -> Any:
    return daemon.health()


def _op_refresh(daemon: "Daemon", request: dict) -> Any:
    daemon.refresh_cliproxy()
    daemon.refresh_dashboard()
    return daemon.status()


def _op_config_get(daemon: "Daemon", request: dict) -> Any:
    return daemon.config_payload()


def _op_config_set(daemon: "Daemon", request: dict) -> Any:
    """``config.set``: ``{"op": "config.set", "config": {...}}``."""
    if "config" not in request:
        raise ConfigUpdateError('request must carry a "config" object')
    subset = request["config"]
    if _encoded_size(subset) > MAX_CONFIG_BODY_BYTES:
        raise ConfigUpdateError("request body is too large")
    return daemon.apply_config(subset)


def _op_session_get(daemon: "Daemon", request: dict) -> Any:
    return daemon.session_payload()


def _op_session_set(daemon: "Daemon", request: dict) -> Any:
    """``session.set``: the request *is* the body (``op`` aside).

    Everything except ``op`` is handed to :meth:`Daemon.apply_session`
    unchanged, so an unknown key is rejected with exactly the message the HTTP
    route produced (``x is not a session field``) and nothing is written.
    """
    body = dict((key, value) for key, value in request.items() if key != "op")
    if _encoded_size(body) > MAX_SESSION_BODY_BYTES:
        raise SessionUpdateError("request body is too large")
    return daemon.apply_session(body)


#: op name -> the handler.  This table is the whole surface of the daemon.
OP_HANDLERS: Dict[str, Callable[["Daemon", dict], Any]] = {
    "status": _op_status,
    "health": _op_health,
    "refresh": _op_refresh,
    "config.get": _op_config_get,
    "config.set": _op_config_set,
    "session.get": _op_session_get,
    "session.set": _op_session_set,
}


class Daemon:
    """Owns the socket server, the cached snapshots and the ledger handle."""

    def __init__(
        self,
        config: Any,
        ledger: Any = None,
        logger: Optional[Callable[[str], Any]] = None,
        config_path: Optional[str] = None,
        socket_path: Optional[str] = None,
        idle_timeout: Optional[float] = None,
    ) -> None:
        self.config = config
        #: The file ``config.set`` rewrites (and reloads from).
        self.config_path = resolve_config_path(config_path)
        self.logger = logger
        #: Where this daemon's socket lives: an explicit ``--socket``, else the
        #: path resolved exactly like :func:`sarvam_meter.client.socket_path`.
        self._socket_path = (
            os.path.expanduser(str(socket_path).strip())
            if socket_path is not None and str(socket_path).strip()
            else resolve_socket_path(config)
        )
        #: How long a connection may stay silent before it is closed.
        if idle_timeout is None:
            idle_timeout = getattr(
                getattr(config, "daemon", None),
                "idle_timeout_seconds",
                IDLE_TIMEOUT_SECONDS,
            )
        try:
            self.idle_timeout = float(idle_timeout)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            self.idle_timeout = IDLE_TIMEOUT_SECONDS
        if self.idle_timeout <= 0:  # pragma: no cover - defensive
            self.idle_timeout = IDLE_TIMEOUT_SECONDS
        self._started_at = time.time()
        self._snapshot: Optional[dict] = None
        self._snapshot_at: Optional[float] = None
        self._refresh_lock = threading.Lock()
        self._dashboard_snapshot: Optional[dict] = None
        self._dashboard_at: Optional[float] = None
        self._dashboard_lock = threading.Lock()
        #: Held across the whole read-modify-write of a config update.
        self._config_lock = threading.Lock()
        #: Held across the atomic write of a session file (``session.set``).
        self._session_lock = threading.Lock()
        #: Bumped on every config swap; a fetch that straddles a swap is dropped.
        self._config_generation = 0
        self._stop = threading.Event()
        self._serving = threading.Event()
        self._server: Optional[SocketServer] = None
        self._thread: Optional[threading.Thread] = None
        self._refresh_thread: Optional[threading.Thread] = None
        self._dashboard_thread: Optional[threading.Thread] = None
        #: The optional D-Bus service handle (``None`` when no binding is
        #: installed or no session bus is reachable).  See
        #: :mod:`sarvam_meter.dbus_service`.
        self._dbus_service: Any = None
        if ledger is None:
            ledger = self._open_ledger()
        self.ledger = ledger

    # -- setup -----------------------------------------------------------
    def _open_ledger(self) -> Any:
        try:
            from .ledger import Ledger

            return Ledger(str(getattr(self.config, "db_path", "")))
        except Exception as exc:  # noqa: BLE001 - serve degraded instead
            self._emit("sarvam-meter: ledger unavailable (%s); serving without it" % (exc,))
            return _BrokenLedger(exc)

    def _make_socket_server(self) -> SocketServer:
        return SocketServer(
            self.socket_path,
            self.handle_line,
            idle_timeout=self.idle_timeout,
            logger=self._emit,
        )

    def _ensure_server(self) -> SocketServer:
        """The bound (not yet serving) socket server; binds on first use."""
        if self._server is None:
            self._server = self._make_socket_server()
            self._server.bind()
        return self._server

    @property
    def socket_path(self) -> str:
        """The path this daemon binds (resolved once, at construction)."""
        return self._socket_path

    @property
    def server(self) -> Optional[SocketServer]:
        """The socket server, once it has been bound (``None`` before that)."""
        return self._server

    @property
    def family(self) -> int:
        """The address family of the listening socket (always ``AF_UNIX``)."""
        return socket.AF_UNIX

    # -- lifecycle -------------------------------------------------------
    def start(self) -> str:
        """Bind the socket, start the background threads, return the path.

        The socket is bound and serving before the optional D-Bus service is
        published, and that step cannot fail: a machine without a D-Bus binding
        (or without a session bus) simply logs one line and keeps the socket.
        """
        server = self._ensure_server()
        self._start_refresh_thread()
        self._start_dashboard_thread()
        self._serving.set()
        server.start()
        self._thread = server.thread
        self._start_dbus_service()
        return self.socket_path

    def serve_forever(self) -> None:
        """Blocking serve loop (binds on first use)."""
        server = self._ensure_server()
        self._start_refresh_thread()
        self._start_dashboard_thread()
        self._serving.set()
        self._thread = threading.current_thread()
        self._start_dbus_service()
        try:
            server.serve_forever()
        finally:
            self._stop_dbus_service()

    def shutdown(self) -> None:
        """Stop serving, close every connection and unlink the socket file."""
        # Release the bus name before the socket goes away: no new D-Bus call
        # may land while the daemon is tearing its own state down.
        self._stop_dbus_service()
        self._stop.set()
        server = self._server
        if server is not None:
            try:
                server.shutdown()
            except Exception:  # pragma: no cover
                pass
            try:
                server.server_close()
            except Exception:  # pragma: no cover
                pass
        # Forget it: a later ``start()`` binds a fresh socket instead of
        # trying to serve on a closed one.
        self._server = None
        current = threading.current_thread()
        for thread in (self._thread, self._refresh_thread, self._dashboard_thread):
            if thread is not None and thread is not current and thread.is_alive():
                thread.join(timeout=5.0)
        self._thread = None
        self._refresh_thread = None
        self._dashboard_thread = None
        # Last, so no in-flight op can be using it.  A Ledger that is never
        # closed leaves SQLite to emit "ResourceWarning: unclosed database" at
        # some arbitrary later moment (Python 3.13+); in a test suite that noise
        # lands inside whichever test happens to be asserting on stderr next.
        ledger = getattr(self, "ledger", None)
        close_all = getattr(ledger, "close_all", None)
        if callable(close_all):
            try:
                close_all()
            except Exception:  # noqa: BLE001 - shutting down regardless
                pass

    # -- the optional D-Bus service ---------------------------------------
    def _start_dbus_service(self) -> None:
        """Publish ``org.kde.sarvam.Meter`` when a D-Bus binding is installed.

        Every failure mode ends here: no binding, no session bus, a bus name
        another daemon owns, a binding that raises on import.  None of them may
        prevent the socket from serving, so the whole thing is wrapped and the
        only trace of it is one log line.
        """
        if self._dbus_service is not None:
            return
        try:
            from . import dbus_service
        except Exception as exc:  # noqa: BLE001 - never a hard dependency
            self._emit(
                "sarvam-meter: D-Bus service not started (%s); "
                "the Unix socket remains the transport" % (exc.__class__.__name__,)
            )
            return
        try:
            self._dbus_service = dbus_service.start(self, logger=self.logger)
        except Exception as exc:  # noqa: BLE001 - a bus problem must not stop us
            self._dbus_service = None
            self._emit(
                "sarvam-meter: D-Bus service not started (%s); "
                "the Unix socket remains the transport" % (exc.__class__.__name__,)
            )

    def _stop_dbus_service(self) -> None:
        """Release the bus name, if this daemon published one."""
        handle = self._dbus_service
        self._dbus_service = None
        if handle is None:
            return
        try:
            handle.stop()
        except Exception:  # noqa: BLE001 - teardown is best effort
            pass

    @property
    def dbus_service(self) -> Any:
        """The published D-Bus handle, or ``None`` (mostly for tests/doctor)."""
        return self._dbus_service

    # -- cliproxy snapshot ----------------------------------------------
    def _start_refresh_thread(self) -> None:
        if not bool(getattr(getattr(self.config, "cliproxy", None), "enabled", False)):
            return
        if self._refresh_thread is not None and self._refresh_thread.is_alive():
            return
        self._stop.clear()
        self._refresh_thread = threading.Thread(
            target=self._refresh_loop,
            name="sarvam-meter-refresh",
            daemon=True,
        )
        self._refresh_thread.start()

    def _refresh_loop(self) -> None:
        while not self._stop.is_set():
            # Re-read the current config every cycle: a new interval (or a
            # disabled source) takes effect without restarting the daemon.
            section = getattr(self.config, "cliproxy", None)
            if bool(getattr(section, "quota_fetch", True)):
                try:
                    self.refresh_cliproxy()
                except Exception as exc:  # noqa: BLE001 - keep the thread alive
                    self._emit("sarvam-meter: cliproxy refresh failed (%s)" % (exc,))
            interval = int(getattr(section, "quota_fetch_interval_seconds", 300) or 300)
            if interval < 1:
                interval = 1
            if self._stop.wait(interval):
                break

    def refresh_cliproxy(self) -> dict:
        """Re-poll CLIProxyAPI and cache the snapshot (serialised by a lock)."""
        with self._refresh_lock:
            snapshot = self._fetch_snapshot()
            self._snapshot = snapshot
            self._snapshot_at = time.time()
        return snapshot

    def _fetch_snapshot(self) -> dict:
        return fetch_cliproxy_snapshot(self.config)

    @property
    def snapshot(self) -> Optional[dict]:
        """The cached snapshot (``None`` until the first successful poll)."""
        return self._snapshot

    # -- dashboard snapshot (TTL cache) ----------------------------------
    @property
    def dashboard_ttl(self) -> int:
        """Seconds a cached dashboard snapshot stays fresh (``dashboard.poll_seconds``)."""
        section = getattr(self.config, "dashboard", None)
        try:
            ttl = int(getattr(section, "poll_seconds", 300) or 300)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            ttl = 300
        return ttl if ttl >= 1 else 1

    def _start_dashboard_thread(self) -> None:
        if not bool(getattr(getattr(self.config, "dashboard", None), "enabled", False)):
            return
        if self._dashboard_thread is not None and self._dashboard_thread.is_alive():
            return
        self._stop.clear()
        self._dashboard_thread = threading.Thread(
            target=self._dashboard_refresh_loop,
            name="sarvam-meter-dashboard",
            daemon=True,
        )
        self._dashboard_thread.start()

    def _dashboard_refresh_loop(self) -> None:
        while not self._stop.is_set():
            if not bool(getattr(getattr(self.config, "dashboard", None), "enabled", False)):
                # Disabled by a settings update: stop polling until re-enabled.
                return
            try:
                self.refresh_dashboard()
            except Exception as exc:  # noqa: BLE001 - keep the thread alive
                self._emit("sarvam-meter: dashboard refresh failed (%s)" % (exc,))
            # ``dashboard_ttl`` reads the *current* config, so a new
            # ``dashboard.poll_seconds`` applies from the next wait onward.
            if self._stop.wait(self.dashboard_ttl):
                break

    def refresh_dashboard(self) -> dict:
        """Poll the dashboard and cache its snapshot (serialised by a lock).

        A fetch that straddles a config update (the generation changed while it
        was in flight) is returned to the caller but *not* cached: it was made
        with the old settings and must not outlive them.
        """
        with self._dashboard_lock:
            generation = self._config_generation
            snapshot = self._fetch_dashboard_snapshot()
            if generation == self._config_generation:
                self._dashboard_snapshot = snapshot
                self._dashboard_at = time.time()
        return snapshot

    def _fetch_dashboard_snapshot(self) -> dict:
        return fetch_dashboard_snapshot(self.config, self.ledger)

    @property
    def dashboard_snapshot(self) -> Optional[dict]:
        """The cached dashboard snapshot; never triggers a fetch (never blocks)."""
        return self._dashboard_snapshot

    def dashboard_snapshot_is_fresh(self) -> bool:
        """True while the cached dashboard snapshot is younger than its TTL."""
        snapshot = self._dashboard_snapshot
        at = self._dashboard_at
        if snapshot is None or at is None:
            return False
        return (time.time() - at) < self.dashboard_ttl

    # -- payloads --------------------------------------------------------
    def status(self) -> dict:
        """Current status payload, built from the cached snapshots.

        Never blocks: both sources are served from their caches, which the
        background threads keep warm.
        """
        return build_status(
            self.config,
            self.ledger,
            self._snapshot,
            dashboard_snapshot=self._dashboard_snapshot,
        )

    def health(self) -> dict:
        uptime = max(0.0, time.time() - self._started_at)
        return {
            "ok": True,
            "version": VERSION,
            "uptime_seconds": int(uptime),
        }

    def config_payload(self) -> dict:
        """Read-only, sanitised config."""
        return sanitized_config(self.config)

    # -- dashboard session (the plasmoid's sign-in webview) ---------------
    def session_path(self) -> str:
        """The session file the poller reads (``dashboard.session_file``)."""
        section = getattr(self.config, "dashboard", None)
        return str(getattr(section, "session_file", "") or "").strip()

    def session_payload(self) -> dict:
        """``session.get``: configured?, path, file mode, header *names*.

        Never reads a value out: the header names come from
        :func:`read_session_header_names`, which keeps only the keys.  ``mode``
        is the file's actual mode when it exists and :data:`SESSION_NO_FILE_MODE`
        (``"0000"``) when there is nothing to stat.
        """
        path = self.session_path()
        expanded = os.path.expanduser(path) if path else ""
        payload: Dict[str, Any] = {
            "configured": False,
            "path": expanded,
            "mode": SESSION_NO_FILE_MODE,
            "headers": [],
        }
        if not expanded:
            return payload
        try:
            info = os.stat(expanded)
        except OSError:
            return payload
        if not stat.S_ISREG(info.st_mode):
            return payload
        payload["configured"] = True
        payload["mode"] = "%04o" % stat.S_IMODE(info.st_mode)
        payload["headers"] = read_session_header_names(expanded)
        return payload

    def apply_session(self, request: Any) -> dict:
        """Validate and persist a captured dashboard session.

        *request* is the request body: a ``dict``, or its raw ``bytes``/``str``
        JSON.  Only ``cookie`` and ``authorization`` may appear, both are
        bounded and checked for CR/LF/NUL, and the file is replaced atomically
        at mode ``0600`` with the canonical key names the dashboard client reads.

        On success the cached dashboard snapshot is dropped -- under the same
        lock a poll holds, so a snapshot fetched with the *old* session can never
        be re-cached after this returns -- and the summary of what was stored is
        returned.  No restart is needed for the new file to be used: the poller
        calls :func:`dashboard.load_session` on every tick, so the next TTL tick
        of a running-but-failing dashboard thread picks the new session up.

        Raises :class:`SessionUpdateError` for any rejection -- in which case the
        session file is left byte-identical and nothing in memory changes.
        """
        session = check_session_request(request)
        path = self.session_path()
        if not path:
            raise SessionUpdateError("dashboard.session_file is not set")
        expanded = os.path.expanduser(path)
        with self._session_lock:
            try:
                write_session_atomic(expanded, session)
            except OSError as exc:
                raise SessionUpdateError(
                    "cannot write dashboard session file %s (%s)"
                    % (expanded, type(exc).__name__)
                ) from None
        self._drop_dashboard_snapshot()
        stored = [name for name in SESSION_HEADER_NAMES if name in session]
        self._emit(
            "sarvam-meter: dashboard session stored (%s) in %s"
            % (", ".join(stored), expanded)
        )
        return {"stored": stored, "path": expanded, "mode": SESSION_FILE_MODE}

    def _drop_dashboard_snapshot(self) -> None:
        """Forget the cached dashboard snapshot (it was fetched with old inputs)."""
        with self._dashboard_lock:
            self._dashboard_snapshot = None
            self._dashboard_at = None

    # -- config writes (the plasmoid's settings page) ---------------------
    def _read_config_document(self) -> dict:
        """The config document currently on disk (defaults when it is absent)."""
        path = self.config_path
        try:
            with open(path, "r", encoding="utf-8") as handle:
                document = json.load(handle)
        except FileNotFoundError:
            # The daemon can run from an in-memory config; the first successful
            # update then creates the file from the documented defaults.
            return default_config_dict()
        except OSError as exc:
            raise ConfigUpdateError(
                "cannot read config %s: %s" % (path, exc.strerror or exc)
            ) from None
        except ValueError:
            raise ConfigUpdateError("invalid JSON in %s" % (path,)) from None
        if not isinstance(document, dict):
            raise ConfigUpdateError("config %s: top level must be a JSON object" % (path,))
        return document

    def apply_config(self, request: Any) -> dict:
        """Validate, persist and reload a partial config update.

        *request* is the request body: a ``dict``, or its raw ``bytes``/``str``
        JSON.  Only :data:`WRITABLE_SECTIONS` may appear in it; the subset is
        merged over the document on disk and the result is validated by the same
        rules ``load_config`` applies.  On success the file is replaced
        atomically at mode ``0600``, the in-memory config is swapped in one
        assignment, the cached dashboard snapshot is dropped, and the sanitised
        config is returned.

        Raises :class:`ConfigUpdateError` for any rejection -- in which case the
        config file is left byte-identical and nothing in memory changes.
        """
        subset = check_writable_subset(decode_config_request(request))
        with self._config_lock:
            document = self._read_config_document()
            merged = merge_writable_subset(document, subset)
            try:
                validate_config_document(merged)
            except ConfigError as exc:
                raise ConfigUpdateError(str(exc)) from None
            write_config_atomic(self.config_path, merged)
            try:
                reloaded = load_config(self.config_path)
            except ConfigError as exc:  # pragma: no cover - just validated
                raise ConfigUpdateError(str(exc)) from None
            self._install_config(reloaded)
        self._emit(
            "sarvam-meter: config updated (%s)"
            % (
                ", ".join(
                    "%s.%s" % (section, key)
                    for section in sorted(subset)
                    for key in sorted(subset[section])
                )
                or "no changes",
            )
        )
        return self.config_payload()

    def _install_config(self, config: Any) -> None:
        """Swap in a validated config and invalidate everything derived from it.

        ``self.config`` is rebound in a single assignment, so a reader on another
        thread sees either the whole old config or the whole new one.  The cached
        dashboard snapshot was fetched with the old settings and is dropped; the
        generation bump also stops an in-flight fetch from re-caching itself.
        """
        self.config = config
        self._config_generation += 1
        self._dashboard_snapshot = None
        self._dashboard_at = None
        if self._serving.is_set() and not self._stop.is_set():
            # A source this update enabled starts polling without a restart.
            # Only a daemon that is actually serving owns background threads.
            self._start_refresh_thread()
            self._start_dashboard_thread()

    # -- logging ---------------------------------------------------------
    def _emit(self, line: str) -> None:
        logger = self.logger
        if logger is None:
            sys.stderr.write(line + "\n")
            sys.stderr.flush()
            return
        info = getattr(logger, "info", None)
        if callable(info):
            info(line)
            return
        try:
            logger(line)
        except Exception:  # pragma: no cover
            pass

    def _log_op(self, op: str, reply: dict, elapsed: float) -> None:
        """One stderr line per request: the op and the outcome, nothing else.

        The request line is never logged -- ``session.set`` carries a live
        cookie -- so only the op name, the outcome code and the duration do.
        """
        if reply.get("ok") is True:
            outcome = "ok"
        else:
            error = reply.get("error")
            outcome = (
                str(error.get("code") or "error")
                if isinstance(error, dict)
                else "error"
            )
        self._emit(
            "%s %s -> %s (%.1f ms)"
            % (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                op or "-",
                outcome,
                elapsed * 1000.0,
            )
        )


    # -- the wire (one JSON object per line) ------------------------------
    def handle_line(self, line: bytes) -> dict:
        """Decode, dispatch and log one request line; always returns a reply."""
        started = time.time()
        reply, op = self._dispatch_line(line)
        self._log_op(op, reply, time.time() - started)
        return reply

    def _dispatch_line(self, line: bytes) -> Tuple[dict, str]:
        """``(reply, op)`` for one request line -- never raises."""
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            return _error_reply(
                "bad_request", "request must be UTF-8 encoded JSON"
            ), ""
        if not text.strip():
            return _error_reply("bad_request", "request must be a JSON object"), ""
        try:
            request = json.loads(text)
        except ValueError:
            return _error_reply("bad_request", "request is not valid JSON"), ""
        if not isinstance(request, dict):
            return _error_reply("bad_request", "request must be a JSON object"), ""
        raw_op = request.get("op")
        if not isinstance(raw_op, str) or not raw_op.strip():
            return _error_reply(
                "bad_request", 'request must carry an "op" string'
            ), ""
        op = raw_op.strip()
        handler = OP_HANDLERS.get(op)
        if handler is None:
            # The op name is the caller's own word, never a stored value; it is
            # bounded so a hostile client cannot flood the error path.
            return _error_reply("unknown_op", "unknown op: %s" % (op[:64],)), op
        try:
            result = handler(self, request)
        except ConfigUpdateError as exc:
            return _error_reply(exc.code, str(exc)), op
        except SessionUpdateError as exc:
            return _error_reply(exc.code, str(exc)), op
        except Exception as exc:  # noqa: BLE001 - never leak a traceback
            return _error_reply(
                "internal_error", "internal error: %s" % (exc.__class__.__name__,)
            ), op
        return _ok_reply(result), op


def make_server(
    config: Any, ledger: Any = None, socket_path: Optional[str] = None
) -> Tuple[SocketServer, Daemon]:
    """Create a bound (not yet serving) socket server plus its :class:`Daemon`."""
    daemon = Daemon(config, ledger=ledger, socket_path=socket_path)
    server = daemon._ensure_server()
    return server, daemon
