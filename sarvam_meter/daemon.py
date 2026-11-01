# SPDX-License-Identifier: MIT
"""Local HTTP daemon serving the widget's status JSON (SPEC section 11).

Routes (nothing else exists):

===========  ==============  ==============================================
method       path            behaviour
===========  ==============  ==============================================
GET          /v1/status      status payload, ``Cache-Control: no-store``
GET          /v1/health      ``{"ok": true, "version": ..., "uptime_seconds": N}``
POST         /v1/refresh     force a cliproxy *and* dashboard re-poll
GET          /v1/config      sanitised config, secrets redacted
POST         /v1/config      rewrite the *writable* subset of the config
GET          /v1/session     whether a dashboard session is stored (names only)
POST         /v1/session     store a captured dashboard ``Cookie``/``Authorization``
anything     anything else   ``404`` ``not_found`` / ``405`` ``method_not_allowed``
===========  ==============  ==============================================

The two mutating routes are ``POST /v1/config`` -- what the plasmoid's settings
page calls -- and ``POST /v1/session`` -- what the plasmoid's sign-in webview
calls once the user has signed in to the Sarvam dashboard.

``POST /v1/config``: only the keys listed in :data:`WRITABLE_SECTIONS` may be
changed, values are validated by the same rules :func:`config.load_config`
uses, and the file is replaced atomically at mode ``0600``.

``POST /v1/session``: the body carries the raw ``Cookie`` and/or
``Authorization`` header value the webview captured.  Both are bounded
(:data:`SESSION_FIELD_LIMITS`), rejected if they could inject a header
(CR/LF/NUL), and written to ``dashboard.session_file`` atomically at mode
``0600`` under the canonical key names :func:`dashboard.load_session` reads
(``{"Cookie": ..., "Authorization": ...}``).  A stored value is never echoed
back, never logged and never appears in an error message: ``GET /v1/session``
reports only the header *names*, the path and the file mode.

Both source snapshots (CLIProxyAPI and the Sarvam dashboard) are refreshed on
background threads behind locks; a slow or unreachable source never delays
``/v1/status``, which always answers from the cached snapshots.  The dashboard
snapshot additionally has a TTL of ``dashboard.poll_seconds``.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

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

STATUS_PATH = "/v1/status"
HEALTH_PATH = "/v1/health"
REFRESH_PATH = "/v1/refresh"
CONFIG_PATH = "/v1/config"
SESSION_PATH = "/v1/session"

#: path -> the methods it accepts
ROUTES = {
    STATUS_PATH: ("GET",),
    HEALTH_PATH: ("GET",),
    REFRESH_PATH: ("POST",),
    CONFIG_PATH: ("GET", "POST"),
    SESSION_PATH: ("GET", "POST"),
}

#: Sections and keys ``POST /v1/config`` may change.  Everything else in the
#: document (``cliproxy.management_key``, ``sarvam.*``, ``daemon.port``, ...) is
#: deliberately not writable over HTTP.
WRITABLE_SECTIONS: Dict[str, Tuple[str, ...]] = {
    "dashboard": ("enabled", "base_url", "poll_seconds", "session_file"),
    "cliproxy": ("enabled", "base_url", "api_version"),
    "proxy": ("enabled",),
    "daemon": ("refresh_seconds",),
}

#: Sections that exist in the config document but are read-only over HTTP.  A
#: key sent inside one of these is named in full (``sarvam.starting_balance is
#: not writable``), so the settings page can point at the offending row.
READ_ONLY_SECTIONS = frozenset(["sarvam"])

CONTENT_TYPE = "application/json; charset=utf-8"
#: Upper bound on how much of a request body is drained and discarded.
MAX_DRAIN_BYTES = 1 * 1024 * 1024
#: Upper bound on a ``POST /v1/config`` body (a settings document is tiny).
MAX_CONFIG_BODY_BYTES = 64 * 1024
#: Upper bound on a ``POST /v1/session`` body (16 KiB cookie + 8 KiB bearer).
MAX_SESSION_BODY_BYTES = 64 * 1024

#: The only keys ``POST /v1/session`` accepts, and how long each value may be.
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
#: Mode the session file is written with, as reported by both routes.
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
    """A ``POST /v1/config`` request was rejected and **nothing** was written."""

    code = "invalid_config"

    def __init__(self, message: str, code: str = "invalid_config") -> None:
        super().__init__(message)
        self.code = code


def _invalid_config(message: str) -> dict:
    """The ``400`` body shape for a rejected config update."""
    return {"error": {"message": message, "code": "invalid_config"}}


class SessionUpdateError(Exception):
    """A ``POST /v1/session`` request was rejected and **nothing** was written.

    The message names the offending *field* (``cookie``/``authorization``) or
    the path -- never a value, not even a prefix of one.
    """

    code = "invalid_session"

    def __init__(self, message: str, code: str = "invalid_session") -> None:
        super().__init__(message)
        self.code = code


def _invalid_session(message: str) -> dict:
    """The ``400`` body shape for a rejected session capture."""
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
            ["host", "port", "refresh_seconds", "request_timeout_seconds"],
        ),
        "proxy": _section(
            getattr(config, "proxy", None),
            ["enabled", "host", "port", "upstream_base", "max_body_bytes"],
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
# writable-subset updates (POST /v1/config)
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
# the dashboard session (GET/POST /v1/session)
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


class _ThreadingServer(ThreadingHTTPServer):
    """Threaded HTTP server with the socket flags the spec requires."""

    daemon_threads = True
    allow_reuse_address = True


class _Handler(BaseHTTPRequestHandler):
    """One handler instance per request; ``daemon`` is injected by subclassing."""

    server_version = "sarvam-meter/" + VERSION
    protocol_version = "HTTP/1.1"
    daemon: "Daemon" = None  # type: ignore[assignment]

    # -- logging ---------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
        """Silence the default stderr access log; :class:`Daemon` logs instead."""
        return

    # -- verbs -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("HEAD")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._dispatch("OPTIONS")

    # -- plumbing --------------------------------------------------------
    def _read_body(self) -> bytes:
        """Read the request body (bounded), so it cannot poison the next request."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        remaining = max(0, min(length, MAX_DRAIN_BYTES))
        chunks: List[bytes] = []
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _dispatch(self, method: str) -> None:
        started = time.time()
        path = urlsplit(self.path).path or "/"
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/") or "/"
        body = self._read_body()
        try:
            status_code, payload = self._route(method.upper(), path, body)
        except Exception as exc:  # noqa: BLE001 - never leak a traceback
            status_code = 500
            payload = {
                "error": {
                    "message": "internal error: %s" % (exc.__class__.__name__,),
                    "code": "internal_error",
                }
            }
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status_code)
            self.send_header("Content-Type", CONTENT_TYPE)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            # One request per connection: an unread request body must never be
            # parsed as the start of the next request on a keep-alive socket.
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            if method.upper() != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            pass
        self.daemon._log(method.upper(), path, status_code, time.time() - started)

    def _route(self, method: str, path: str, body: bytes = b"") -> Tuple[int, dict]:
        allowed = ROUTES.get(path)
        if allowed is None:
            return 404, {"error": {"message": "not found", "code": "not_found"}}
        # HEAD is answered wherever GET is, without a body (the writer skips it).
        if method == "HEAD" and "GET" in allowed:
            method = "GET"
        if method not in allowed:
            return 405, {
                "error": {"message": "method not allowed", "code": "method_not_allowed"}
            }
        if path == STATUS_PATH:
            return 200, self.daemon.status()
        if path == HEALTH_PATH:
            return 200, self.daemon.health()
        if path == REFRESH_PATH:
            self.daemon.refresh_cliproxy()
            self.daemon.refresh_dashboard()
            return 200, self.daemon.status()
        if path == CONFIG_PATH:
            if method == "POST":
                return self._config_update(body)
            return 200, self.daemon.config_payload()
        if path == SESSION_PATH:
            if method == "POST":
                return self._session_update(body)
            return 200, self.daemon.session_payload()
        return 404, {"error": {"message": "not found", "code": "not_found"}}  # pragma: no cover

    def _config_update(self, body: bytes) -> Tuple[int, dict]:
        """``POST /v1/config``: apply a writable-subset update, return the config.

        A rejection is a ``400`` naming the offending key and changes nothing on
        disk; a success is the same sanitised document ``GET /v1/config`` serves.
        The request body is never echoed back.
        """
        if len(body) > MAX_CONFIG_BODY_BYTES:
            return 400, _invalid_config("request body is too large")
        try:
            payload = self.daemon.apply_config(body)
        except ConfigUpdateError as exc:
            return 400, {"error": {"message": str(exc), "code": exc.code}}
        return 200, payload

    def _session_update(self, body: bytes) -> Tuple[int, dict]:
        """``POST /v1/session``: store the captured session, return what was stored.

        A rejection is a ``400`` naming the offending field and changes nothing
        on disk; a success is ``{"stored": [...], "path": ..., "mode": ...}``.
        The request body is never echoed back and no value is ever returned.
        """
        if len(body) > MAX_SESSION_BODY_BYTES:
            return 400, _invalid_session("request body is too large")
        try:
            payload = self.daemon.apply_session(body)
        except SessionUpdateError as exc:
            return 400, {"error": {"message": str(exc), "code": exc.code}}
        return 200, payload


class Daemon:
    """Owns the HTTP server, the cached snapshots and the ledger handle."""

    def __init__(
        self,
        config: Any,
        ledger: Any = None,
        logger: Optional[Callable[[str], Any]] = None,
        config_path: Optional[str] = None,
    ) -> None:
        self.config = config
        #: The file ``POST /v1/config`` rewrites (and reloads from).
        self.config_path = resolve_config_path(config_path)
        self.logger = logger
        self._started_at = time.time()
        self._snapshot: Optional[dict] = None
        self._snapshot_at: Optional[float] = None
        self._refresh_lock = threading.Lock()
        self._dashboard_snapshot: Optional[dict] = None
        self._dashboard_at: Optional[float] = None
        self._dashboard_lock = threading.Lock()
        #: Held across the whole read-modify-write of a config update.
        self._config_lock = threading.Lock()
        #: Held across the atomic write of a session file (``POST /v1/session``).
        self._session_lock = threading.Lock()
        #: Bumped on every config swap; a fetch that straddles a swap is dropped.
        self._config_generation = 0
        self._stop = threading.Event()
        self._serving = threading.Event()
        self._server: Optional[_ThreadingServer] = None
        self._thread: Optional[threading.Thread] = None
        self._refresh_thread: Optional[threading.Thread] = None
        self._dashboard_thread: Optional[threading.Thread] = None
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

    def _make_http_server(self) -> _ThreadingServer:
        section = getattr(self.config, "daemon", None)
        host = str(getattr(section, "host", "127.0.0.1") or "127.0.0.1")
        port = int(getattr(section, "port", 8787) or 0)
        handler = type("SarvamMeterHandler", (_Handler,), {"daemon": self})
        server = _ThreadingServer((host, port), handler)
        return server

    def _ensure_server(self) -> _ThreadingServer:
        if self._server is None:
            self._server = self._make_http_server()
        return self._server

    @property
    def port(self) -> int:
        """The actual bound port (useful when configured with ``0``)."""
        if self._server is None:
            return 0
        try:
            return int(self._server.server_address[1])
        except Exception:  # pragma: no cover
            return 0

    @property
    def host(self) -> str:
        if self._server is None:
            section = getattr(self.config, "daemon", None)
            return str(getattr(section, "host", "127.0.0.1") or "127.0.0.1")
        return str(self._server.server_address[0])

    # -- lifecycle -------------------------------------------------------
    def start(self) -> int:
        """Bind the socket, start the background threads, return the port."""
        server = self._ensure_server()
        self._start_refresh_thread()
        self._start_dashboard_thread()
        if self._thread is None or not self._thread.is_alive():
            self._serving.set()
            self._thread = threading.Thread(
                target=server.serve_forever,
                name="sarvam-meter-http",
                daemon=True,
            )
            self._thread.start()
        return self.port

    def serve_forever(self) -> None:
        """Blocking serve loop (binds on first use)."""
        server = self._ensure_server()
        self._start_refresh_thread()
        self._start_dashboard_thread()
        self._serving.set()
        server.serve_forever()

    def shutdown(self) -> None:
        self._stop.set()
        server = self._server
        if server is not None:
            if self._serving.is_set():
                try:
                    server.shutdown()
                except Exception:  # pragma: no cover
                    pass
            try:
                server.server_close()
            except Exception:  # pragma: no cover
                pass
        current = threading.current_thread()
        for thread in (self._thread, self._refresh_thread, self._dashboard_thread):
            if thread is not None and thread is not current and thread.is_alive():
                thread.join(timeout=5.0)
        self._thread = None
        self._refresh_thread = None
        self._dashboard_thread = None

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
        """``GET /v1/session``: configured?, path, file mode, header *names*.

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

    def _log(self, method: str, path: str, status_code: int, elapsed: float) -> None:
        """One stderr line per request.  Path only: no query string, no headers."""
        self._emit(
            "%s %s %s -> %d (%.1f ms)"
            % (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                method,
                path,
                status_code,
                elapsed * 1000.0,
            )
        )


def make_server(config: Any, ledger: Any = None) -> Tuple[ThreadingHTTPServer, Daemon]:
    """Create a bound (not yet serving) server plus its :class:`Daemon`."""
    daemon = Daemon(config, ledger=ledger)
    server = daemon._ensure_server()
    return server, daemon
