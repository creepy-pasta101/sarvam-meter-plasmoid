# SPDX-License-Identifier: MIT
"""Local HTTP daemon serving the widget's status JSON (SPEC section 11).

Routes (nothing else exists, and nothing can mutate state):

===========  ==============  ==============================================
method       path            behaviour
===========  ==============  ==============================================
GET          /v1/status      status payload, ``Cache-Control: no-store``
GET          /v1/health      ``{"ok": true, "version": ..., "uptime_seconds": N}``
POST         /v1/refresh     force a cliproxy *and* dashboard re-poll
GET          /v1/config      sanitised config, secrets redacted
anything     anything else   ``404`` ``not_found`` / ``405`` ``method_not_allowed``
===========  ==============  ==============================================

Both source snapshots (CLIProxyAPI and the Sarvam dashboard) are refreshed on
background threads behind locks; a slow or unreachable source never delays
``/v1/status``, which always answers from the cached snapshots.  The dashboard
snapshot additionally has a TTL of ``dashboard.poll_seconds``.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from .status import build_status

try:  # pragma: no cover - U1 owns __init__.py
    from . import __version__ as VERSION
except Exception:  # noqa: BLE001
    VERSION = "0.1.0"

STATUS_PATH = "/v1/status"
HEALTH_PATH = "/v1/health"
REFRESH_PATH = "/v1/refresh"
CONFIG_PATH = "/v1/config"

#: path -> the single method it accepts
ROUTES = {
    STATUS_PATH: "GET",
    HEALTH_PATH: "GET",
    REFRESH_PATH: "POST",
    CONFIG_PATH: "GET",
}

CONTENT_TYPE = "application/json; charset=utf-8"
#: Upper bound on how much of a request body is drained and discarded.
MAX_DRAIN_BYTES = 1 * 1024 * 1024

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
    def _drain_body(self) -> None:
        """Consume any request body so it cannot poison the next request."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        remaining = max(0, min(length, MAX_DRAIN_BYTES))
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def _dispatch(self, method: str) -> None:
        started = time.time()
        path = urlsplit(self.path).path or "/"
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/") or "/"
        self._drain_body()
        try:
            status_code, payload = self._route(method.upper(), path)
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

    def _route(self, method: str, path: str) -> Tuple[int, dict]:
        allowed = ROUTES.get(path)
        if allowed is None:
            return 404, {"error": {"message": "not found", "code": "not_found"}}
        # HEAD is answered wherever GET is, without a body (the writer skips it).
        if method == "HEAD" and allowed == "GET":
            method = "GET"
        if method != allowed:
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
            return 200, self.daemon.config_payload()
        return 404, {"error": {"message": "not found", "code": "not_found"}}  # pragma: no cover


class Daemon:
    """Owns the HTTP server, the cached snapshots and the ledger handle."""

    def __init__(
        self,
        config: Any,
        ledger: Any = None,
        logger: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self.config = config
        self.logger = logger
        self._started_at = time.time()
        self._snapshot: Optional[dict] = None
        self._snapshot_at: Optional[float] = None
        self._refresh_lock = threading.Lock()
        self._dashboard_snapshot: Optional[dict] = None
        self._dashboard_at: Optional[float] = None
        self._dashboard_lock = threading.Lock()
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
        section = getattr(self.config, "cliproxy", None)
        interval = int(getattr(section, "quota_fetch_interval_seconds", 300) or 300)
        if interval < 1:
            interval = 1
        while not self._stop.is_set():
            if bool(getattr(section, "quota_fetch", True)):
                try:
                    self.refresh_cliproxy()
                except Exception as exc:  # noqa: BLE001 - keep the thread alive
                    self._emit("sarvam-meter: cliproxy refresh failed (%s)" % (exc,))
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
        interval = self.dashboard_ttl
        while not self._stop.is_set():
            try:
                self.refresh_dashboard()
            except Exception as exc:  # noqa: BLE001 - keep the thread alive
                self._emit("sarvam-meter: dashboard refresh failed (%s)" % (exc,))
            if self._stop.wait(interval):
                break

    def refresh_dashboard(self) -> dict:
        """Poll the dashboard and cache its snapshot (serialised by a lock)."""
        with self._dashboard_lock:
            snapshot = self._fetch_dashboard_snapshot()
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
