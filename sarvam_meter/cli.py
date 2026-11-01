# SPDX-License-Identifier: MIT
"""Command line interface for ``sarvam-meter`` (SPEC section 12).

Exit codes: ``0`` ok, ``1`` runtime error, ``2`` configuration or usage error.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import socket
import stat
import sys
import tempfile
import threading
from typing import Any, List, Optional, Sequence, Tuple

from . import client as client_module
from .client import ClientError
from .config import (ConfigError, load_config, resolve_config_path,
                     write_default_config)
from .ledger import Ledger
from .rates import UNITS, cost_for, rate_card, rate_known
from .status import build_status

PROG = "sarvam-meter"

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_CONFIG = 2

_VERSION_FALLBACK = "0.1.0"


def _version() -> str:
    try:  # pragma: no cover - U1 owns __init__.py
        from . import __version__ as version

        return str(version)
    except Exception:  # noqa: BLE001
        return _VERSION_FALLBACK


# ---------------------------------------------------------------------------
# output helpers
# ---------------------------------------------------------------------------


def _out(message: str) -> None:
    sys.stdout.write(message + "\n")


def _err(message: str) -> None:
    sys.stderr.write("%s: %s\n" % (PROG, message))


def _as_float(text: str) -> Optional[float]:
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value


def _format_quantity(value: float) -> str:
    """``1000000.0`` -> ``"1000000"``; ``0.5`` -> ``"0.5"``."""
    if value == int(value):
        return "%d" % int(value)
    return ("%g" % value)


def _short_ts(value: Any) -> str:
    """``2026-11-01T10:00:00Z`` -> ``2026-11-01T10:00Z`` (display only)."""
    if not isinstance(value, str) or not value:
        return ""
    if value.endswith(":00Z") and len(value) > 6:
        return value[:-4] + "Z"
    return value


def _config_path(args: argparse.Namespace) -> Optional[str]:
    return getattr(args, "config", None)


def _load(args: argparse.Namespace) -> Any:
    return load_config(_config_path(args))


def _open_ledger(config: Any) -> Ledger:
    return Ledger(str(getattr(config, "db_path", "")))


def _close_ledger(ledger: Any) -> None:
    closer = getattr(ledger, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# status rendering
# ---------------------------------------------------------------------------


def _sarvam_line(status: dict) -> str:
    sarvam = status.get("sarvam") or {}
    currency = sarvam.get("currency") or "INR"
    balance = sarvam.get("balance") or {}
    spend = sarvam.get("spend") or {}
    calls = (sarvam.get("calls") or {}).get("today", 0)
    today = float(spend.get("today") or 0.0)
    month = float(spend.get("month") or 0.0)
    if balance.get("known"):
        head = "Sarvam  %s %.2f left" % (currency, float(balance.get("remaining") or 0.0))
    else:
        head = "Sarvam  %s spend only" % (currency,)
    return "%s  (spent %.2f today, %.2f this month, %d calls)" % (
        head,
        today,
        month,
        int(calls or 0),
    )


def _bucket_line(name: str, plan: str, bucket: dict) -> str:
    window = str(bucket.get("window") or "-")
    head = name
    if plan:
        head = "%s  %s" % (name, plan)
    head = "%s %s window" % (head, window)
    remaining = bucket.get("remaining_fraction")
    if isinstance(remaining, bool) or not isinstance(remaining, (int, float)):
        state = "unknown"
    else:
        state = "%d%% left" % int(round(float(remaining) * 100.0))
    reset = _short_ts(bucket.get("reset_at"))
    tail = ", resets %s" % (reset,) if reset else ""
    return "%s %s%s" % (head, state, tail)


def _account_name(account: dict) -> str:
    for key in ("label", "name", "id", "auth_index"):
        value = account.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    provider = account.get("provider")
    if isinstance(provider, str) and provider.strip():
        return provider.strip().capitalize()
    return "Account"


def _cliproxy_lines(status: dict) -> List[str]:
    cliproxy = status.get("cliproxy") or {}
    if not cliproxy.get("configured"):
        return []
    lines: List[str] = []
    if not cliproxy.get("reachable"):
        lines.append("Cliproxy  unreachable")
    for account in cliproxy.get("accounts") or []:
        if not isinstance(account, dict):
            continue
        name = _account_name(account)
        plan = account.get("plan")
        plan = str(plan).strip() if isinstance(plan, str) else ""
        buckets = [b for b in (account.get("buckets") or []) if isinstance(b, dict)]
        if not buckets:
            detail = account.get("error")
            if not isinstance(detail, str) or not detail:
                detail = "no quota data"
            lines.append("%s  %s" % (name, detail))
            continue
        for bucket in buckets:
            lines.append(_bucket_line(name, plan, bucket))
    return lines


def render_status(status: dict) -> List[str]:
    """Human readable lines: one line per source."""
    lines = [_sarvam_line(status)]
    lines.extend(_cliproxy_lines(status))
    return lines


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    explicit = _config_path(args)
    # --if-missing is what the systemd unit's ExecStartPre runs, so it must never
    # overwrite a config the user has edited -- and it must never crash either:
    # `explicit` is None when --config was not passed, and the file that matters
    # is the one write_default_config would resolve.  (This is exactly the bug
    # that made `systemctl --user enable --now sarvam-meter` fail.)
    if getattr(args, "if_missing", False):
        resolved = resolve_config_path(explicit)
        if os.path.isfile(resolved):
            _err("config already present at %s" % (resolved,))
            return EXIT_OK
    written = write_default_config(explicit)
    _out(written)
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    config = _load(args)
    ledger = _open_ledger(config)
    try:
        status = build_status(config, ledger, _cliproxy_snapshot(config))
    finally:
        _close_ledger(ledger)

    if getattr(args, "json", False):
        _out(json.dumps(status, indent=2, sort_keys=False))
        return EXIT_OK

    for line in render_status(status):
        _out(line)
    for entry in status.get("errors") or []:
        if isinstance(entry, dict):
            _err(
                "%s: %s (%s)"
                % (entry.get("source"), entry.get("message"), entry.get("code"))
            )
    return EXIT_OK


def cmd_balance_set(args: argparse.Namespace) -> int:
    amount = _as_float(args.amount)
    if amount is None or amount < 0:
        _err("invalid amount %r: expected a finite number >= 0" % (args.amount,))
        return EXIT_CONFIG
    config = _load(args)
    ledger = _open_ledger(config)
    try:
        currency = str(getattr(getattr(config, "sarvam", None), "currency", "INR") or "INR")
        ledger.set_balance(amount, currency)
    finally:
        _close_ledger(ledger)
    _out("Balance set to %s %.2f" % (currency, amount))
    return EXIT_OK


def cmd_balance_show(args: argparse.Namespace) -> int:
    config = _load(args)
    ledger = _open_ledger(config)
    try:
        balance = ledger.get_balance()
    finally:
        _close_ledger(ledger)
    if not isinstance(balance, dict) or balance.get("starting") is None:
        _out("No starting balance set.")
        return EXIT_OK
    _out(
        "%s %.2f (set at %s)"
        % (
            balance.get("currency") or "INR",
            float(balance.get("starting") or 0.0),
            balance.get("set_at") or "-",
        )
    )
    return EXIT_OK


def cmd_record(args: argparse.Namespace) -> int:
    quantity = _as_float(args.quantity)
    if quantity is None or quantity < 0:
        _err("invalid quantity %r: expected a finite number >= 0" % (args.quantity,))
        return EXIT_CONFIG
    config = _load(args)
    api = str(args.api)
    model = args.model or None
    unit = str(args.unit)
    if unit not in UNITS:
        _err("invalid unit %r: expected one of %s" % (unit, ", ".join(UNITS)))
        return EXIT_CONFIG
    overrides = getattr(getattr(config, "sarvam", None), "rate_overrides", None)
    card = rate_card(overrides) if overrides else rate_card()
    known = rate_known(api, model, card)
    try:
        cost = cost_for(api, model, quantity, unit, card)
    except ValueError as exc:
        _err("cannot price %s/%s: %s" % (api, model or "-", exc))
        return EXIT_CONFIG
    if not math.isfinite(cost):
        # An infinite row would poison every SUM(cost) and blank the widget.
        _err("cannot price %s/%s: the cost overflows" % (api, model or "-"))
        return EXIT_CONFIG
    currency = str(getattr(getattr(config, "sarvam", None), "currency", "INR") or "INR")
    ledger = _open_ledger(config)
    try:
        ledger.record_event(
            source="manual",
            api=api,
            model=model,
            quantity=quantity,
            unit=unit,
            cost=round(float(cost), 6),
            estimated=not known,
        )
    finally:
        _close_ledger(ledger)
    label = api if not model else "%s/%s" % (api, model)
    _out(
        "Recorded %s %s %s = %s %.6f"
        % (label, _format_quantity(quantity), unit, currency, round(float(cost), 6))
    )
    return EXIT_OK


# -- session file ------------------------------------------------------------

#: The only header names the session file may contain (case-insensitive).
SESSION_KEYS = ("cookie", "authorization")


def _session_path(config: Any) -> str:
    section = getattr(config, "dashboard", None)
    return str(getattr(section, "session_file", "") or "")


def _session_mode(path: str) -> Optional[int]:
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return None


def _reject_newline(name: str, value: str) -> Optional[int]:
    """Refuse header values that would smuggle a second header line."""
    if "\n" in value or "\r" in value:
        _err("%s must not contain a newline" % (name,))
        return EXIT_CONFIG
    return None


def _write_session_file(path: str, headers: dict) -> None:
    """Write *headers* atomically as ``0600`` inside a ``0700`` directory."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    if not os.path.isdir(directory):
        os.makedirs(directory, mode=0o700, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:  # pragma: no cover - e.g. a foreign filesystem
            pass
    payload = json.dumps(headers, indent=2) + "\n"
    handle_fd, tmp_path = tempfile.mkstemp(prefix=".session-", dir=directory)
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:  # pragma: no cover
            pass
        raise


def cmd_session_set(args: argparse.Namespace) -> int:
    cookie_file = getattr(args, "cookie_file", None)
    cookie = getattr(args, "cookie", None)
    if cookie_file:
        try:
            with open(os.path.expanduser(cookie_file), "r", encoding="utf-8") as handle:
                cookie = handle.read()
        except OSError as exc:
            _err("cannot read cookie file %s: %s" % (cookie_file, exc.strerror or exc))
            return EXIT_RUNTIME
    if cookie is None:
        _err("one of --cookie or --cookie-file is required")
        return EXIT_CONFIG
    cookie = cookie.strip()
    if not cookie:
        _err("cookie must not be empty")
        return EXIT_CONFIG
    refused = _reject_newline("cookie", cookie)
    if refused is not None:
        return refused

    headers = {"Cookie": cookie}
    authorization = getattr(args, "authorization", None)
    if authorization is not None and str(authorization).strip():
        authorization = str(authorization).strip()
        refused = _reject_newline("authorization", authorization)
        if refused is not None:
            return refused
        headers["Authorization"] = authorization

    config = _load(args)
    path = _session_path(config)
    if not path:
        _err("dashboard.session_file is not set")
        return EXIT_CONFIG
    _write_session_file(path, headers)
    _out("Session written to %s (mode 0600)" % (path,))
    return EXIT_OK


def cmd_session_show(args: argparse.Namespace) -> int:
    config = _load(args)
    path = _session_path(config)
    if not path:
        _err("dashboard.session_file is not set")
        return EXIT_CONFIG
    mode = _session_mode(path)
    if mode is None:
        _out("Session file: %s" % (path,))
        _out("Mode: missing")
        _out("Cookie: absent")
        _out("Authorization: absent")
        return EXIT_OK
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except OSError as exc:
        _err("cannot read %s: %s" % (path, exc.strerror or exc))
        return EXIT_RUNTIME
    except ValueError:
        _err("cannot read %s: invalid JSON" % (path,))
        return EXIT_RUNTIME
    if not isinstance(document, dict):
        _err("cannot read %s: not a JSON object" % (path,))
        return EXIT_RUNTIME
    present = {
        str(key).strip().lower()
        for key in document
        if isinstance(key, str) and str(document.get(key) or "").strip()
    }
    _out("Session file: %s" % (path,))
    _out("Mode: %04o" % (mode,))
    for name in SESSION_KEYS:
        _out("%s: %s" % (name.capitalize(), "present" if name in present else "absent"))
    return EXIT_OK


# -- dashboard ---------------------------------------------------------------


def _dashboard_snapshot(config: Any, ledger: Any) -> dict:
    """Poll the dashboard once (never raises; the caller reads ``error``)."""
    try:
        from .daemon import fetch_dashboard_snapshot

        return fetch_dashboard_snapshot(config, ledger)
    except Exception as exc:  # noqa: BLE001 - a command must never traceback
        return {
            "configured": bool(
                getattr(getattr(config, "dashboard", None), "enabled", False)
            ),
            "reachable": False,
            "as_of": None,
            "org_id": None,
            "currency": "INR",
            "window": {"start": None, "end": None},
            "spend": {"window": 0.0, "today": 0.0},
            "by_model": [],
            "error": {
                "source": "dashboard",
                "code": "unreachable",
                "message": str(exc) or exc.__class__.__name__,
            },
        }


def _dashboard_lines(block: dict, base_url: str) -> List[str]:
    if not block.get("configured"):
        return ["Dashboard  not configured"]
    head = "Dashboard  %s  %s" % (
        base_url or "-",
        "reachable" if block.get("reachable") else "unreachable",
    )
    lines = [head]
    if block.get("reachable"):
        window = block.get("window") or {}
        spend = block.get("spend") or {}
        lines.append(
            "%s %.2f spent %s..%s (%s %.2f today)"
            % (
                block.get("currency") or "INR",
                float(spend.get("window") or 0.0),
                window.get("start") or "-",
                window.get("end") or "-",
                block.get("currency") or "INR",
                float(spend.get("today") or 0.0),
            )
        )
        for entry in (block.get("by_model") or [])[:3]:
            if isinstance(entry, dict):
                lines.append(
                    "  %s  %.2f"
                    % (entry.get("model") or "-", float(entry.get("cost") or 0.0))
                )
    error = block.get("error")
    if isinstance(error, dict):
        lines.append("dashboard: %s" % (error.get("code") or "error",))
    return lines


def cmd_dashboard(args: argparse.Namespace) -> int:
    config = _load(args)
    ledger = _open_ledger(config)
    try:
        snapshot = _dashboard_snapshot(config, ledger)
        block = build_status(
            config, ledger, None, dashboard_snapshot=snapshot
        )["sarvam"]["dashboard"]
    finally:
        _close_ledger(ledger)

    if getattr(args, "json", False):
        _out(json.dumps(block, indent=2, sort_keys=False))
    else:
        section = getattr(config, "dashboard", None)
        for line in _dashboard_lines(
            block, str(getattr(section, "base_url", "") or "")
        ):
            _out(line)
    if block.get("configured") and not block.get("reachable"):
        return EXIT_RUNTIME
    return EXIT_OK


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the status daemon: the socket listener and nothing else.

    There is no recording proxy to start any more -- it was removed -- so this
    command only binds the daemon's ``AF_UNIX`` socket and waits for a signal.
    """
    from .daemon import Daemon

    config = _load(args)
    ledger = _open_ledger(config)
    # Pass the resolved path too: the widget's settings page writes through
    # config.set, and that must edit the file this process actually loaded.
    daemon = Daemon(
        config,
        ledger=ledger,
        config_path=_config_path(args),
        socket_path=getattr(args, "socket", None),
    )
    try:
        bound_path = daemon.start()
    except OSError as exc:
        _err(
            "cannot bind daemon socket %s: %s"
            % (daemon.socket_path, exc.strerror or exc)
        )
        _close_ledger(ledger)
        return EXIT_RUNTIME
    _err("daemon listening on %s" % (bound_path,))
    if getattr(args, "print_status", False):
        _out(json.dumps(daemon.status(), indent=2, sort_keys=False))
    stop = threading.Event()

    def _request_stop(signum, frame):  # noqa: ARG001 - signal handler signature
        stop.set()

    installed = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            installed[signum] = signal.signal(signum, _request_stop)
        except (ValueError, OSError):  # not the main thread, or unsupported
            pass
    try:
        stop.wait()
    except KeyboardInterrupt:  # pragma: no cover - handler not installed
        pass
    finally:
        for signum, handler in installed.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):  # pragma: no cover - defensive
                pass
        daemon.shutdown()
        _close_ledger(ledger)
    return EXIT_OK


# -- query (talk to a running daemon over its socket) ------------------------


class _UsageError(Exception):
    """A ``query`` invocation the user has to fix (exit code 2 by default)."""

    def __init__(self, message: str, code: int = EXIT_CONFIG) -> None:
        super().__init__(message)
        self.code = code


#: ``query <name>`` -> the op it sends, for the ops that take no arguments.
_QUERY_SIMPLE_OPS = {
    "status": "status",
    "health": "health",
    "refresh": "refresh",
    "config": "config.get",
    "session": "session.get",
}


def _query_socket(args: argparse.Namespace) -> str:
    """The socket path a ``query`` invocation should use.

    An explicit ``--socket`` wins; otherwise the path is resolved from the
    config the daemon would have loaded (so ``--config`` selects the same
    socket the daemon started from that file would bind).
    """
    explicit = getattr(args, "socket", None)
    if explicit is not None and str(explicit).strip():
        return os.path.expanduser(str(explicit).strip())
    config = None
    try:
        config = _load(args)
    except ConfigError:
        # A broken or missing config must not stop a plain ``query status``:
        # the default location is still the right guess.
        config = None
    return client_module.socket_path(config)


def _query_request(args: argparse.Namespace) -> Tuple[str, dict]:
    """``(op, arguments)`` for the requested query, or raise :class:`_UsageError`."""
    command = getattr(args, "query_command", None)
    if command in _QUERY_SIMPLE_OPS:
        return _QUERY_SIMPLE_OPS[command], {}
    if command == "set-config":
        text = str(getattr(args, "config_json", "") or "")
        if text.strip() == "-":
            text = sys.stdin.read()
        if not text.strip():
            raise _UsageError(
                "set-config expects a JSON object, e.g. "
                "'{\"dashboard\": {\"poll_seconds\": 60}}'"
            )
        try:
            document = json.loads(text)
        except ValueError:
            raise _UsageError(
                "set-config expects a JSON object, e.g. "
                "'{\"dashboard\": {\"poll_seconds\": 60}}'"
            ) from None
        if not isinstance(document, dict):
            raise _UsageError("set-config expects a JSON object")
        return "config.set", {"config": document}
    if command == "set-session":
        body = {}
        cookie = getattr(args, "cookie", None)
        cookie_file = getattr(args, "cookie_file", None)
        if cookie_file:
            try:
                with open(os.path.expanduser(cookie_file), "r", encoding="utf-8") as handle:
                    cookie = handle.read()
            except OSError as exc:
                raise _UsageError(
                    "cannot read cookie file %s: %s" % (cookie_file, exc.strerror or exc),
                    EXIT_RUNTIME,
                ) from None
        if cookie is not None and str(cookie).strip():
            body["cookie"] = str(cookie).strip()
        authorization = getattr(args, "authorization", None)
        if authorization is not None and str(authorization).strip():
            body["authorization"] = str(authorization).strip()
        if not body:
            raise _UsageError(
                "one of --cookie, --cookie-file or --authorization is required"
            )
        return "session.set", body
    raise _UsageError("a query op is required")


def cmd_query(args: argparse.Namespace) -> int:
    """``sarvam-meter query <op>``: one request, the result as JSON on stdout.

    Exit ``0`` with the result on stdout, ``1`` when the daemon refuses or
    cannot be reached (``sarvam-meter: <message>`` on stderr), ``2`` for a bad
    op or bad usage.
    """
    try:
        op, payload = _query_request(args)
        path = _query_socket(args)
    except _UsageError as exc:
        _err(str(exc))
        return exc.code
    try:
        result = client_module.call(
            op,
            socket_path=path,
            timeout=float(getattr(args, "timeout", 10.0) or 10.0),
            **payload
        )
    except ClientError as exc:
        # The daemon's own message: it never carries a stored value.
        _err(str(exc))
        return EXIT_RUNTIME
    _out(json.dumps(result, indent=2, sort_keys=False))
    return EXIT_OK


# -- doctor -----------------------------------------------------------------


def _check(name: str, ok: bool, detail: str = "") -> Tuple[str, bool]:
    if ok:
        # A passing check may still have something worth saying, e.g. "not
        # configured (run: ...)" -- dropping it would hide the one command the
        # user needs on a fresh install.
        _out("OK   %s%s" % (name, (": " + detail) if detail else ""))
    else:
        _out("FAIL %s%s" % (name, (": " + detail) if detail else ""))
    return name, ok


def _socket_is_free(path: str) -> Tuple[bool, str]:
    """``daemon socket free``: nothing answers on *path* and it can be bound.

    A stale socket file left by a crashed daemon is *free* -- the next start
    unlinks it -- while a file that is not a socket at all, or a socket a live
    daemon answers on, is not.
    """
    expanded = os.path.expanduser(str(path))
    try:
        info = os.lstat(expanded)
    except FileNotFoundError:
        info = None
    except OSError as exc:
        return False, "cannot stat %s (%s)" % (expanded, exc.strerror or exc)
    if info is not None:
        if not stat.S_ISSOCK(info.st_mode):
            return False, "%s exists and is not a socket" % (expanded,)
        if client_module.is_running(expanded, timeout=1.0):
            return False, "%s in use (a daemon is already listening)" % (expanded,)
    directory = os.path.dirname(os.path.abspath(expanded)) or "."
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe_path = ""
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        # Bind a throwaway name in the real directory: that proves the socket
        # can be created there without touching the daemon's own path.
        probe_path = os.path.join(
            directory, ".sarvam-meter-probe-%d.sock" % (os.getpid(),)
        )
        try:
            os.unlink(probe_path)
        except OSError:
            pass
        probe.bind(probe_path)
        probe.listen(1)
        return True, ""
    except OSError as exc:
        return False, "cannot bind in %s (%s)" % (directory, exc.strerror or exc)
    finally:
        probe.close()
        if probe_path:
            try:
                os.unlink(probe_path)
            except OSError:
                pass


def _dir_writable(path: str) -> Tuple[bool, str]:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    try:
        os.makedirs(directory, exist_ok=True)
        handle, probe = tempfile.mkstemp(prefix=".sarvam-meter-probe-", dir=directory)
        os.close(handle)
        os.unlink(probe)
        return True, ""
    except OSError as exc:
        return False, "%s (%s)" % (directory, exc.strerror or exc)


def _cliproxy_snapshot(config: Any) -> Optional[dict]:
    """Poll CLIProxyAPI once when it is enabled; ``None`` when disabled.

    Never raises and never mutates anything -- it only reads the management API,
    exactly like the daemon's background refresh does.
    """
    section = getattr(config, "cliproxy", None)
    if not bool(getattr(section, "enabled", False)):
        return None
    try:
        from .daemon import fetch_cliproxy_snapshot

        return fetch_cliproxy_snapshot(config)
    except Exception:  # noqa: BLE001
        return None


def _cliproxy_reachable(config: Any) -> Tuple[bool, str]:
    snapshot = _cliproxy_snapshot(config)
    if not isinstance(snapshot, dict):
        return False, "no snapshot"
    if snapshot.get("reachable"):
        return True, ""
    errors = snapshot.get("errors") or []
    detail = ""
    if errors and isinstance(errors[0], dict):
        detail = str(errors[0].get("message") or errors[0].get("code") or "")
    return False, detail or "unreachable"


def _dashboard_session_check(config: Any) -> Tuple[bool, str]:
    """``dashboard session``: the file exists, is ``0600`` and holds only headers.

    Scraping is the default source now, so "no session captured yet" is a setup
    step, not a fault: doctor reports it as OK with the command to run.  A file
    that exists but is loose-permissioned, unreadable or carries unexpected keys
    *is* a failure, because that one is silently unsafe or broken.
    """
    section = getattr(config, "dashboard", None)
    enabled = bool(getattr(section, "enabled", False))
    path = str(getattr(section, "session_file", "") or "")
    if not path:
        return False, "dashboard.session_file is not set"
    try:
        if not os.path.isfile(path):
            # Not configured yet.  `doctor` exists to tell you what to fix, and
            # this one has a one-line fix that the message names.
            return True, "not configured (run: sarvam-meter session set --cookie-file PATH)"
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o077:
            return False, "mode %04o, expected 0600" % (mode,)
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
        if not isinstance(document, dict):
            return False, "session file is not a JSON object"
        unexpected = sorted(
            str(key)
            for key in document
            if not isinstance(key, str) or key.strip().lower() not in SESSION_KEYS
        )
        if unexpected:
            # Key *names* only; a value is never read out of the file.
            return False, "unexpected keys: %s" % (", ".join(unexpected),)
        return True, ""
    except (OSError, ValueError) as exc:
        return False, "cannot read session file (%s)" % (exc.__class__.__name__,)
    except Exception as exc:  # noqa: BLE001 - a check must never traceback
        return False, "cannot check session file (%s)" % (exc.__class__.__name__,)


def _dashboard_reachable(config: Any) -> Tuple[bool, str]:
    """``dashboard reachable``: one poll of the dashboard source."""
    try:
        snapshot = _dashboard_snapshot(config, None)
    except Exception as exc:  # noqa: BLE001 - never traceback out of doctor
        return False, str(exc) or exc.__class__.__name__
    if not isinstance(snapshot, dict):
        return False, "no snapshot"
    if snapshot.get("reachable"):
        return True, ""
    error = snapshot.get("error")
    if isinstance(error, dict):
        code = str(error.get("code") or "error")
        message = str(error.get("message") or "")
        if code == "no_session":
            # Same setup step as above; not a fault.
            return True, "not configured (no session file)"
        return False, (code + (": " + message if message else ""))
    return False, "unreachable"


#: The applet id, and the two files that carry the settings contract.
PLASMOID_ID = "io.github.creepy_pasta101.sarvammeter"


def _plasmoid_dirs() -> List[str]:
    """Every directory a plasmoid package could be loaded from, best first.

    A user-installed copy wins over the packaged one, which is exactly the thing
    that makes a stale page impossible to explain from the package alone.
    """
    data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    roots: List[str] = []
    if data_home:
        roots.append(os.path.expanduser(data_home))
    else:
        roots.append(os.path.join(os.path.expanduser("~"), ".local", "share"))
    for entry in (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":"):
        if entry.strip():
            roots.append(os.path.expanduser(entry.strip()))
    return [os.path.join(root, "plasma", "plasmoids", PLASMOID_ID) for root in roots]


def _config_page_check() -> tuple:
    """Is the installed settings page the one this version expects?

    Plasma sets two initial properties per KConfigXT key, `cfg_<key>` and
    `cfg_<key>Default`. A page missing either one never receives its values, and
    the only symptom is console noise plus a dialog full of fallbacks -- so
    compare what is installed against what is declared, and say which file won.
    """
    installed = next((path for path in _plasmoid_dirs() if os.path.isdir(path)), None)
    if installed is None:
        return True, "not installed (running from a checkout?)"
    page_path = os.path.join(installed, "contents", "config", "configGeneral.qml")
    xml_path = os.path.join(installed, "contents", "config", "main.xml")
    if not os.path.isfile(page_path) or not os.path.isfile(xml_path):
        return False, "no config page under %s" % (installed,)
    try:
        with open(xml_path, "r", encoding="utf-8") as handle:
            xml_text = handle.read()
        with open(page_path, "r", encoding="utf-8") as handle:
            page_text = handle.read()
    except OSError as exc:
        return False, "cannot read %s (%s)" % (installed, exc.strerror or exc)

    entries = []
    for match in re.finditer(r'<entry\s+name="([^"]+)"', xml_text):
        entries.append(match.group(1))
    if not entries:
        return False, "%s declares no keys" % (xml_path,)

    missing: List[str] = []
    for name in entries:
        if not re.search(r"property\s+\w+\s+cfg_%s\b" % re.escape(name), page_text):
            missing.append("cfg_" + name)
        if not re.search(r"property\s+\w+\s+cfg_%sDefault\b" % re.escape(name), page_text):
            missing.append("cfg_" + name + "Default")
    if missing:
        return False, "%s is stale: no %s (reinstall, or kpackagetool6 --upgrade)" % (
            installed, ", ".join(missing))
    return True, installed


def cmd_doctor(args: argparse.Namespace) -> int:
    results: List[bool] = []

    try:
        config = _load(args)
    except ConfigError as exc:
        results.append(_check("config loads", False, str(exc))[1])
        for name in (
            "data dir writable",
            "db opens",
            "plasmoid config page",
            "daemon socket free",
            "cliproxy reachable",
            "dashboard session",
            "dashboard reachable",
        ):
            results.append(_check(name, False, "not checked (no config)")[1])
        return EXIT_RUNTIME

    results.append(_check("config loads", True)[1])

    db_path = str(getattr(config, "db_path", ""))
    ok, detail = _dir_writable(db_path)
    results.append(_check("data dir writable", ok, detail)[1])

    ledger = None
    try:
        ledger = _open_ledger(config)
        # A read-only database opens and reads fine but rejects every write, so
        # probe with a real write and clean it up again.
        probe_key = "doctor:probe"
        ledger.set_kv(probe_key, "1")
        ledger.set_kv(probe_key, None)
        results.append(_check("db opens", True)[1])
    except Exception as exc:  # noqa: BLE001
        results.append(_check("db opens", False, str(exc) or exc.__class__.__name__)[1])
    finally:
        if ledger is not None:
            _close_ledger(ledger)

    try:
        ok, detail = _config_page_check()
    except Exception as exc:  # noqa: BLE001 - a check must never traceback
        ok, detail = False, str(exc) or exc.__class__.__name__
    results.append(_check("plasmoid config page", ok, detail)[1])

    ok, detail = _socket_is_free(client_module.socket_path(config))
    results.append(_check("daemon socket free", ok, detail)[1])

    cliproxy_cfg = getattr(config, "cliproxy", None)
    if bool(getattr(cliproxy_cfg, "enabled", False)):
        ok, detail = _cliproxy_reachable(config)
        results.append(_check("cliproxy reachable", ok, detail)[1])

    dashboard_cfg = getattr(config, "dashboard", None)
    try:
        ok, detail = _dashboard_session_check(config)
    except Exception as exc:  # noqa: BLE001 - a check must never traceback
        ok, detail = False, str(exc) or exc.__class__.__name__
    results.append(_check("dashboard session", ok, detail)[1])

    if bool(getattr(dashboard_cfg, "enabled", False)):
        try:
            ok, detail = _dashboard_reachable(config)
        except Exception as exc:  # noqa: BLE001 - a check must never traceback
            ok, detail = False, str(exc) or exc.__class__.__name__
        results.append(_check("dashboard reachable", ok, detail)[1])

    return EXIT_OK if all(results) else EXIT_RUNTIME


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Sarvam AI credit usage and CLIProxyAPI quota meter.",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="command")

    serve = subparsers.add_parser("serve", help="run the status daemon")
    serve.add_argument("--config", default=None, metavar="PATH")
    serve.add_argument(
        "--socket",
        default=None,
        metavar="PATH",
        help="socket path to bind (default: $XDG_RUNTIME_DIR/sarvam-meter/daemon.sock)",
    )
    serve.add_argument(
        "--port",
        type=int,
        default=None,
        metavar="N",
        help="ignored: the daemon no longer listens on TCP",
    )
    serve.add_argument("--print-status", action="store_true", dest="print_status")
    serve.set_defaults(func=cmd_serve)

    status = subparsers.add_parser("status", help="print current usage")
    status.add_argument("--config", default=None, metavar="PATH")
    status.add_argument("--json", action="store_true", dest="json")
    status.set_defaults(func=cmd_status)

    balance = subparsers.add_parser("balance", help="manage the starting balance")
    balance_sub = balance.add_subparsers(dest="balance_command", metavar="command")

    balance_set = balance_sub.add_parser("set", help="set the starting balance")
    balance_set.add_argument("amount", metavar="<amount>")
    balance_set.add_argument("--config", default=None, metavar="PATH")
    balance_set.set_defaults(func=cmd_balance_set)

    balance_show = balance_sub.add_parser("show", help="show the starting balance")
    balance_show.add_argument("--config", default=None, metavar="PATH")
    balance_show.set_defaults(func=cmd_balance_show)

    record = subparsers.add_parser("record", help="record a manual usage event")
    record.add_argument("--api", required=True, metavar="<api>")
    record.add_argument("--quantity", required=True, metavar="<n>")
    record.add_argument("--unit", required=True, metavar="<unit>")
    record.add_argument("--model", default=None, metavar="M")
    record.add_argument("--config", default=None, metavar="PATH")
    record.set_defaults(func=cmd_record)

    session = subparsers.add_parser("session", help="manage the dashboard session")
    session_sub = session.add_subparsers(dest="session_command", metavar="command")

    session_set = session_sub.add_parser("set", help="write the dashboard session file")
    source = session_set.add_mutually_exclusive_group(required=True)
    source.add_argument("--cookie", default=None, metavar="VALUE")
    source.add_argument("--cookie-file", default=None, dest="cookie_file", metavar="PATH")
    session_set.add_argument("--authorization", default=None, metavar="VALUE")
    session_set.add_argument("--config", default=None, metavar="PATH")
    session_set.set_defaults(func=cmd_session_set)

    session_show = session_sub.add_parser("show", help="show the session file state")
    session_show.add_argument("--config", default=None, metavar="PATH")
    session_show.set_defaults(func=cmd_session_show)

    dashboard = subparsers.add_parser("dashboard", help="print the dashboard snapshot")
    dashboard.add_argument("--config", default=None, metavar="PATH")
    dashboard.add_argument("--json", action="store_true", dest="json")
    dashboard.set_defaults(func=cmd_dashboard)

    query = subparsers.add_parser(
        "query", help="ask a running daemon over its socket"
    )
    query.add_argument(
        "--socket",
        default=None,
        metavar="PATH",
        help="socket path (default: resolved the way the daemon resolves it)",
    )
    query.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help="config file used to resolve the socket path",
    )
    query.add_argument(
        "--timeout", type=float, default=10.0, metavar="SECONDS",
        help="how long to wait for the daemon to answer",
    )
    query_sub = query.add_subparsers(
        dest="query_command", metavar="op", required=True
    )
    for name, summary in (
        ("status", "print the status payload"),
        ("health", "print the daemon's health"),
        ("refresh", "force a re-poll of every source, then print the status"),
        ("config", "print the sanitised config"),
        ("session", "print the dashboard session state (header names only)"),
    ):
        leaf = query_sub.add_parser(name, help=summary)
        # SUPPRESS keeps a value given *before* the op name from being clobbered
        # by the subparser's own default.
        leaf.add_argument("--socket", default=argparse.SUPPRESS, metavar="PATH")
        leaf.add_argument("--config", default=argparse.SUPPRESS, metavar="PATH")
        leaf.add_argument(
            "--timeout", type=float, default=argparse.SUPPRESS, metavar="SECONDS"
        )
        leaf.set_defaults(func=cmd_query)

    query_set_config = query_sub.add_parser(
        "set-config", help="apply a writable-subset config update"
    )
    query_set_config.add_argument(
        "config_json",
        metavar="<json>",
        help="a JSON object of writable keys, or - to read it from stdin",
    )
    query_set_config.add_argument(
        "--socket", default=argparse.SUPPRESS, metavar="PATH"
    )
    query_set_config.add_argument(
        "--config", default=argparse.SUPPRESS, metavar="PATH"
    )
    query_set_config.add_argument(
        "--timeout", type=float, default=argparse.SUPPRESS, metavar="SECONDS"
    )
    query_set_config.set_defaults(func=cmd_query)

    query_set_session = query_sub.add_parser(
        "set-session", help="store a captured dashboard session"
    )
    session_source = query_set_session.add_mutually_exclusive_group()
    session_source.add_argument("--cookie", default=None, metavar="VALUE")
    session_source.add_argument(
        "--cookie-file", default=None, dest="cookie_file", metavar="PATH"
    )
    query_set_session.add_argument("--authorization", default=None, metavar="VALUE")
    query_set_session.add_argument(
        "--socket", default=argparse.SUPPRESS, metavar="PATH"
    )
    query_set_session.add_argument(
        "--config", default=argparse.SUPPRESS, metavar="PATH"
    )
    query_set_session.add_argument(
        "--timeout", type=float, default=argparse.SUPPRESS, metavar="SECONDS"
    )
    query_set_session.set_defaults(func=cmd_query)

    doctor = subparsers.add_parser("doctor", help="check the installation")
    doctor.add_argument("--config", default=None, metavar="PATH")
    doctor.set_defaults(func=cmd_doctor)

    init = subparsers.add_parser("init", help="write the default config")
    init.add_argument("--config", default=None, metavar="PATH")
    init.add_argument("--if-missing", action="store_true", dest="if_missing",
                      help="do nothing when the config already exists")
    init.set_defaults(func=cmd_init)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point.  Returns the process exit code (never raises SystemExit)."""
    parser = build_parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        args = parser.parse_args(arguments)
    except SystemExit as exc:  # argparse: --help -> 0, usage error -> 2
        code = exc.code
        if code is None:
            return EXIT_OK
        if isinstance(code, int):
            return code
        return EXIT_CONFIG

    handler = getattr(args, "func", None)
    if handler is None:
        parser.print_usage(sys.stderr)
        _err("a command is required")
        return EXIT_CONFIG

    try:
        return int(handler(args) or EXIT_OK)
    except ConfigError as exc:
        _err(str(exc))
        return EXIT_CONFIG
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return EXIT_RUNTIME
    except OSError as exc:
        _err(str(exc) or exc.__class__.__name__)
        return EXIT_RUNTIME
    except Exception as exc:  # noqa: BLE001 - never traceback at the user
        _err(str(exc) or exc.__class__.__name__)
        return EXIT_RUNTIME


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
