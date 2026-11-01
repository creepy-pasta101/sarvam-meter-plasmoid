# SPDX-License-Identifier: MIT
"""Command line interface for ``sarvam-meter`` (SPEC section 12).

Exit codes: ``0`` ok, ``1`` runtime error, ``2`` configuration or usage error.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import socket
import sys
import tempfile
import threading
from typing import Any, List, Optional, Sequence, Tuple

from .config import ConfigError, load_config, write_default_config
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
    path = write_default_config(_config_path(args))
    _out(path)
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


def _with_port(config: Any, port: Optional[int], section: str = "daemon") -> Any:
    """Return a config whose ``section`` port is overridden (frozen dataclasses)."""
    if port is None:
        return config
    target = getattr(config, section)
    try:
        import dataclasses

        return dataclasses.replace(
            config, **{section: dataclasses.replace(target, port=int(port))}
        )
    except Exception:  # noqa: BLE001 - non-dataclass config (tests)
        setattr(target, "port", int(port))
        return config


def cmd_serve(args: argparse.Namespace) -> int:
    from .daemon import Daemon

    config = _with_port(_load(args), getattr(args, "port", None), section="daemon")
    ledger = _open_ledger(config)
    proxy = None
    proxy_port = None
    if getattr(args, "with_proxy", False):
        from .proxy import ProxyServer

        proxy = ProxyServer(config, ledger)
        try:
            # Bind in this thread first: a taken port must fail loudly, not kill
            # a background thread and leave the user thinking the proxy runs.
            proxy_port = proxy.port
        except OSError as exc:
            _err(
                "cannot bind recording proxy on %s:%s: %s"
                % (config.proxy.host, config.proxy.port, exc)
            )
            _close_ledger(ledger)
            return EXIT_RUNTIME
        threading.Thread(
            target=proxy.serve_forever, name="sarvam-meter-proxy", daemon=True
        ).start()
    daemon = Daemon(config, ledger=ledger)
    try:
        port = daemon.start()
    except OSError as exc:
        _err("cannot bind daemon on %s:%s: %s" % (daemon.host, config.daemon.port, exc))
        if proxy is not None:
            proxy.shutdown()
        _close_ledger(ledger)
        return EXIT_RUNTIME
    _err(
        "daemon listening on http://%s:%d (proxy %s)"
        % (daemon.host, port, ("enabled on port %s" % proxy_port) if proxy is not None else "disabled")
    )
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
        if proxy is not None:
            proxy.shutdown()
        _close_ledger(ledger)
    return EXIT_OK


def cmd_proxy(args: argparse.Namespace) -> int:
    from .proxy import ProxyServer

    config = _with_port(_load(args), getattr(args, "port", None), section="proxy")
    ledger = _open_ledger(config)
    server = ProxyServer(config, ledger)
    _err(
        "recording proxy listening on http://%s:%d"
        % (config.proxy.host, getattr(server, "port", config.proxy.port))
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - interactive
        pass
    finally:
        shutdown = getattr(server, "shutdown", None)
        if callable(shutdown):
            try:
                shutdown()
            except Exception:  # noqa: BLE001
                pass
        _close_ledger(ledger)
    return EXIT_OK


# -- doctor -----------------------------------------------------------------


def _check(name: str, ok: bool, detail: str = "") -> Tuple[str, bool]:
    if ok:
        _out("OK   %s" % (name,))
    else:
        _out("FAIL %s%s" % (name, (": " + detail) if detail else ""))
    return name, ok


def _port_is_free(host: str, port: int) -> Tuple[bool, str]:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.settimeout(2.0)
        # Both servers bind with allow_reuse_address, so a port in TIME_WAIT is
        # genuinely free for them; the probe must not disagree.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, int(port)))
        return True, ""
    except OSError as exc:
        return False, "port %s in use (%s)" % (port, exc.strerror or exc)
    finally:
        probe.close()


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


def cmd_doctor(args: argparse.Namespace) -> int:
    results: List[bool] = []

    try:
        config = _load(args)
    except ConfigError as exc:
        results.append(_check("config loads", False, str(exc))[1])
        for name in (
            "data dir writable",
            "db opens",
            "daemon port free",
            "proxy port free",
            "cliproxy reachable",
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

    daemon_cfg = config.daemon
    ok, detail = _port_is_free(str(daemon_cfg.host), int(daemon_cfg.port))
    results.append(_check("daemon port free", ok, detail)[1])

    proxy_cfg = getattr(config, "proxy", None)
    if bool(getattr(proxy_cfg, "enabled", False)):
        ok, detail = _port_is_free(str(proxy_cfg.host), int(proxy_cfg.port))
        results.append(_check("proxy port free", ok, detail)[1])

    cliproxy_cfg = getattr(config, "cliproxy", None)
    if bool(getattr(cliproxy_cfg, "enabled", False)):
        ok, detail = _cliproxy_reachable(config)
        results.append(_check("cliproxy reachable", ok, detail)[1])

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
    serve.add_argument("--with-proxy", action="store_true", dest="with_proxy")
    serve.add_argument("--port", type=int, default=None, metavar="N")
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

    proxy = subparsers.add_parser("proxy", help="run the recording proxy")
    proxy.add_argument("--config", default=None, metavar="PATH")
    proxy.add_argument("--port", type=int, default=None, metavar="N")
    proxy.set_defaults(func=cmd_proxy)

    doctor = subparsers.add_parser("doctor", help="check the installation")
    doctor.add_argument("--config", default=None, metavar="PATH")
    doctor.set_defaults(func=cmd_doctor)

    init = subparsers.add_parser("init", help="write the default config")
    init.add_argument("--config", default=None, metavar="PATH")
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
