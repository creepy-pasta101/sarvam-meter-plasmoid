# SPDX-License-Identifier: MIT
"""Optional D-Bus service for the sarvam-meter daemon (unit B2).

WHY THIS EXISTS
The plasmoid needs to hand the daemon one secret -- the dashboard session
cookie -- and a Unix socket write is not the only way to do that: a D-Bus method
call lets a *service* receive a value, so the value never has to appear in the
daemon's own command line.  The daemon therefore publishes

    bus name   org.kde.sarvam.Meter
    object     /org/kde/sarvam/Meter
    interface  org.kde.sarvam.Meter

with six methods, each of which returns a JSON **string** (D-Bus has no
dictionary-of-anything type, and a string keeps the reply identical to what the
socket sends):

================================  ============================================
method                            returns
================================  ============================================
``Status() -> s``                 ``{"ok": true, "result": <status>}``
``Refresh() -> s``                the status after a forced re-poll
``GetConfig() -> s``              the sanitised config (secrets redacted)
``SetConfig(s json) -> s``        the sanitised config after a writable update
``GetSession() -> s``             whether a session is stored (header *names*)
``SetSession(s cookie, s auth)``  what was stored -- never a value
================================  ============================================

Every method is a thin wrapper over :data:`sarvam_meter.daemon.OP_HANDLERS`,
i.e. exactly the handlers the Unix socket dispatches to.  Nothing is
reimplemented here: validation, redaction and the "never echo a secret" rule all
live in :mod:`sarvam_meter.daemon` and this module inherits them.

NEVER A HARD DEPENDENCY
``sarvam-meter`` is a standard-library package with no install-time
dependencies, and a D-Bus binding is not on every machine.  This module
therefore imports cleanly with **none** of ``dbus`` (dbus-python), ``jeepney``
or ``dasbus`` installed: :func:`available_binding` simply returns ``None`` and
:func:`start` logs one line saying so and returns ``None``.  The daemon treats
that as "no D-Bus today" and keeps serving the socket -- the socket remains the
transport no matter what happens here.

The first binding that imports wins, in the documented order: dbus-python,
then jeepney, then dasbus.  (dbus-python and dasbus both need a GLib main loop
to serve, which is why each runs one on a private thread; jeepney is a plain
blocking loop with no GLib at all.)

Standard library only on this side of the fence: the bindings are imported
lazily, inside the function that needs them.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

#: The bus name the plasmoid (and ``busctl``/``gdbus``) addresses.
BUS_NAME = "org.kde.sarvam.Meter"
#: The single object this service exports.
OBJECT_PATH = "/org/kde/sarvam/Meter"
#: The interface every method below lives on (same spelling as the bus name).
INTERFACE = "org.kde.sarvam.Meter"

#: Import order for the bindings.  The first one that imports is used.
BINDINGS: Tuple[str, ...] = ("dbus", "jeepney", "dasbus")

#: Human-readable name of each binding, for the one log line and for tests.
BINDING_LABELS: Dict[str, str] = {
    "dbus": "dbus-python",
    "jeepney": "jeepney",
    "dasbus": "dasbus",
}

#: The D-Bus error name a rejected call is answered with.
ERROR_NAME = "org.kde.sarvam.Meter.Error"

#: Upper bound on a ``SetConfig`` argument (a settings document is tiny).  The
#: op handler enforces the same bound again; this one exists so an absurd
#: argument is refused before it is even parsed.
MAX_SET_CONFIG_CHARS = 64 * 1024
#: Upper bound on the two ``SetSession`` arguments, in characters.  The op
#: handler enforces the authoritative byte limits (16 KiB / 8 KiB); this is only
#: a guard against a caller streaming megabytes across the bus.
MAX_SESSION_ARG_CHARS = 64 * 1024


# --------------------------------------------------------------------------
# JSON marshalling (binding independent -- this is what the tests exercise)
# --------------------------------------------------------------------------


def json_ok(result: Any) -> str:
    """The ``{"ok": true, "result": ...}`` envelope, as a JSON string."""
    return json.dumps({"ok": True, "result": result}, allow_nan=False)


def json_error(code: str, message: str) -> str:
    """The ``{"ok": false, "error": {...}}`` envelope, as a JSON string."""
    return json.dumps(
        {"ok": False, "error": {"code": str(code), "message": str(message)}},
        allow_nan=False,
    )


def _handlers() -> Dict[str, Callable[[Any, dict], Any]]:
    """The daemon's op table (imported lazily so this module stays standalone)."""
    from .daemon import OP_HANDLERS

    return OP_HANDLERS


def _errors() -> Tuple[type, type]:
    """``(ConfigUpdateError, SessionUpdateError)`` from :mod:`sarvam_meter.daemon`."""
    from .daemon import ConfigUpdateError, SessionUpdateError

    return ConfigUpdateError, SessionUpdateError


class MeterAPI:
    """The six methods, as plain Python returning JSON strings.

    Deliberately free of any D-Bus machinery: a test can construct one around a
    stand-in daemon and check the marshalling without a bus in sight.  The
    binding adapters below only forward to these.
    """

    def __init__(
        self, daemon: Any, logger: Optional[Callable[[str], Any]] = None
    ) -> None:
        #: Anything with the :class:`sarvam_meter.daemon.Daemon` surface; the op
        #: handlers only ever call its public methods.
        self.daemon = daemon
        self._logger = logger

    # -- the envelope ----------------------------------------------------
    def _call(self, op: str, request: dict, method: str = "") -> str:
        """Run one op handler and encode its outcome; never raises."""
        started_request = dict(request)
        started_request.setdefault("op", op)
        try:
            handler = _handlers().get(op)
        except Exception as exc:  # noqa: BLE001 - a broken import is not fatal
            self._emit(
                "dbus %s -> internal_error (%s)"
                % (method or op, exc.__class__.__name__,)
            )
            return json_error(
                "internal_error", "internal error: %s" % (exc.__class__.__name__,)
            )
        if handler is None:  # pragma: no cover - the table is fixed
            return json_error("unknown_op", "unknown op: %s" % (op[:64],))
        config_error, session_error = _errors()
        try:
            result = handler(self.daemon, started_request)
        except (config_error, session_error) as exc:
            # The message names a field or a key; it never carries a value.
            self._emit("dbus %s -> %s" % (method or op, getattr(exc, "code", "error"),))
            return json_error(str(getattr(exc, "code", "error")), str(exc))
        except Exception as exc:  # noqa: BLE001 - never leak a traceback over the bus
            self._emit(
                "dbus %s -> internal_error (%s)"
                % (method or op, exc.__class__.__name__,)
            )
            return json_error(
                "internal_error", "internal error: %s" % (exc.__class__.__name__,)
            )
        self._emit("dbus %s -> ok" % (method or op,))
        try:
            return json_ok(result)
        except (TypeError, ValueError):
            # A payload that cannot be encoded (a NaN in a figure, say) must not
            # turn into an exception on the bus: answer with an error envelope.
            return json_error("internal_error", "the reply could not be encoded as JSON")

    def _emit(self, line: str) -> None:
        logger = self._logger
        if logger is None:
            return
        try:
            info = getattr(logger, "info", None)
            if callable(info):
                info(line)
            elif callable(logger):
                logger(line)
        except Exception:  # pragma: no cover - a logger must never break a call
            pass

    # -- the methods -----------------------------------------------------
    def status(self) -> str:
        """``Status()``: the same payload ``{"op": "status"}`` produces."""
        return self._call("status", {}, "Status")

    def refresh(self) -> str:
        """``Refresh()``: force a re-poll of both sources, then return status."""
        return self._call("refresh", {}, "Refresh")

    def get_config(self) -> str:
        """``GetConfig()``: the sanitised config document."""
        return self._call("config.get", {}, "GetConfig")

    def set_config(self, document: Any) -> str:
        """``SetConfig(json)``: apply a writable-subset update.

        *document* is the JSON the caller sent: a ``str`` carrying a JSON object
        (``{"dashboard": {"poll_seconds": 300}}``) or an already-decoded dict.
        A malformed or oversized argument is refused here with the same error
        codes the socket uses, so the two transports cannot drift.
        """
        if isinstance(document, (bytes, bytearray)):
            try:
                document = bytes(document).decode("utf-8")
            except UnicodeDecodeError:
                return json_error(
                    "invalid_config", "request body must be UTF-8 encoded JSON"
                )
        if isinstance(document, str):
            if len(document) > MAX_SET_CONFIG_CHARS:
                return json_error("invalid_config", "request body is too large")
            try:
                document = json.loads(document)
            except ValueError:
                return json_error("invalid_config", "request body is not valid JSON")
        if not isinstance(document, dict):
            return json_error("invalid_config", "request body must be a JSON object")
        return self._call(
            "config.set", {"config": document}, "SetConfig"
        )

    def get_session(self) -> str:
        """``GetSession()``: configured?, path, file mode, header *names*."""
        return self._call("session.get", {}, "GetSession")

    def set_session(self, cookie: Any = "", authorization: Any = "") -> str:
        """``SetSession(cookie, authorization)``: store a captured session.

        An empty argument counts as "not supplied", exactly like the socket
        protocol, and at least one must be present.  The reply reports what was
        stored (header *names*, the path and the file mode) and **never** a
        value: the summary comes from :meth:`Daemon.apply_session`, which is the
        same code the socket runs.
        """
        request: Dict[str, Any] = {}
        for name, value in (("cookie", cookie), ("authorization", authorization)):
            if value is None:
                continue
            if not isinstance(value, str):
                return json_error("invalid_session", "%s: must be a string" % (name,))
            if len(value) > MAX_SESSION_ARG_CHARS:
                return json_error(
                    "invalid_session",
                    "%s: must be at most %d characters"
                    % (name, MAX_SESSION_ARG_CHARS),
                )
            stripped = value.strip()
            if stripped:
                request[name] = stripped
        if not request:
            return json_error(
                "invalid_session", "at least one of cookie or authorization is required"
            )
        return self._call("session.set", request, "SetSession")


# --------------------------------------------------------------------------
# binding discovery
# --------------------------------------------------------------------------


def available_binding() -> Optional[str]:
    """The first importable D-Bus binding, or ``None`` when there is none.

    Nothing is imported permanently: a binding that is absent simply does not
    appear, which is the whole reason this package has no hard dependency.
    """
    for name in BINDINGS:
        try:
            __import__(name)
        except Exception:  # noqa: BLE001 - ImportError, or a broken install
            continue
        return name
    return None


def unavailable_message() -> str:
    """The one line logged when no binding is installed."""
    return (
        "sarvam-meter: D-Bus is unavailable (none of %s is installed); "
        "the Unix socket remains the transport"
        % (", ".join(BINDING_LABELS.get(name, name) for name in BINDINGS),)
    )


def _log(logger: Optional[Callable[[str], Any]], line: str) -> None:
    if logger is None:
        return
    try:
        info = getattr(logger, "info", None)
        if callable(info):
            info(line)
        elif callable(logger):
            logger(line)
    except Exception:  # pragma: no cover - logging must never break serving
        pass


# --------------------------------------------------------------------------
# the running service
# --------------------------------------------------------------------------


class ServiceHandle:
    """What :func:`start` returns when a service is actually on the bus.

    ``stop()`` is idempotent and never raises: shutting a daemon down must not
    be able to fail because a bus went away first.
    """

    def __init__(
        self,
        binding: str,
        bus_name: str = BUS_NAME,
        object_path: str = OBJECT_PATH,
        interface: str = INTERFACE,
        cleanup: Optional[Callable[[], None]] = None,
    ) -> None:
        self.binding = binding
        self.bus_name = bus_name
        self.object_path = object_path
        self.interface = interface
        self._cleanup = cleanup
        self._stopped = False
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        """True until :meth:`stop` has been called."""
        return not self._stopped

    def stop(self) -> None:
        """Release the bus name, the object and any main loop; never raises."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            cleanup = self._cleanup
            self._cleanup = None
        if cleanup is None:
            return
        try:
            cleanup()
        except Exception:  # noqa: BLE001 - teardown is best effort
            pass

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<ServiceHandle %s %s>" % (self.binding, self.bus_name)


def start(
    daemon: Any,
    logger: Optional[Callable[[str], Any]] = None,
    bus_name: str = BUS_NAME,
) -> Optional[ServiceHandle]:
    """Publish :class:`MeterAPI` on the session bus; ``None`` when impossible.

    Never raises.  A machine with no binding, no session bus, or a bus name that
    is already taken all end the same way: one log line and ``None``, after
    which the daemon carries on serving its socket exactly as before.
    """
    binding = available_binding()
    if binding is None:
        _log(logger, unavailable_message())
        return None
    api = MeterAPI(daemon, logger=logger)
    starter = _STARTERS.get(binding)
    if starter is None:  # pragma: no cover - every binding has a starter
        _log(
            logger,
            "sarvam-meter: D-Bus is unavailable (%s has no starter); "
            "the Unix socket remains the transport" % (binding,),
        )
        return None
    try:
        handle = starter(api, logger, bus_name)
    except Exception as exc:  # noqa: BLE001 - a bus problem must not stop the daemon
        _log(
            logger,
            "sarvam-meter: D-Bus service not published (%s: %s); "
            "the Unix socket remains the transport"
            % (BINDING_LABELS.get(binding, binding), exc.__class__.__name__),
        )
        return None
    if handle is not None:
        _log(
            logger,
            "sarvam-meter: D-Bus service %s on %s (%s)"
            % (bus_name, OBJECT_PATH, BINDING_LABELS.get(binding, binding)),
        )
    return handle


def stop(handle: Optional[ServiceHandle]) -> None:
    """Stop *handle* if it is running (a no-op for ``None``)."""
    if handle is not None:
        handle.stop()


# --------------------------------------------------------------------------
# dbus-python
# --------------------------------------------------------------------------


def _start_dbus_python(
    api: MeterAPI, logger: Optional[Callable[[str], Any]], bus_name: str
) -> ServiceHandle:
    """Serve through dbus-python, with a private GLib main loop on a thread."""
    import dbus  # noqa: F401 - imported for the side effect of failing loudly
    import dbus.service
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib

    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    # do_not_queue: if the name is taken (a second daemon), fail instead of
    # silently replacing the first one's service.
    name = dbus.service.BusName(bus_name, bus=bus, do_not_queue=True)

    class _MeterObject(dbus.service.Object):
        def __init__(self) -> None:
            super().__init__(bus, OBJECT_PATH)

        @dbus.service.method(INTERFACE, in_signature="", out_signature="s")
        def Status(self) -> str:
            return api.status()

        @dbus.service.method(INTERFACE, in_signature="", out_signature="s")
        def Refresh(self) -> str:
            return api.refresh()

        @dbus.service.method(INTERFACE, in_signature="", out_signature="s")
        def GetConfig(self) -> str:
            return api.get_config()

        @dbus.service.method(INTERFACE, in_signature="s", out_signature="s")
        def SetConfig(self, document: str) -> str:
            return api.set_config(document)

        @dbus.service.method(INTERFACE, in_signature="", out_signature="s")
        def GetSession(self) -> str:
            return api.get_session()

        @dbus.service.method(INTERFACE, in_signature="ss", out_signature="s")
        def SetSession(self, cookie: str, authorization: str) -> str:
            return api.set_session(cookie, authorization)

    obj = _MeterObject()
    loop = GLib.MainLoop()
    thread = threading.Thread(
        target=loop.run, name="sarvam-meter-dbus", daemon=True
    )
    thread.start()

    def _cleanup() -> None:
        try:
            loop.quit()
        except Exception:  # pragma: no cover - already gone
            pass
        try:
            obj.remove_from_connection()
        except Exception:  # pragma: no cover - already gone
            pass

    handle = ServiceHandle("dbus", bus_name=bus_name, cleanup=_cleanup)
    # Keep the objects alive for as long as the handle is.
    handle._objects = (name, bus, obj, loop, thread)  # type: ignore[attr-defined]
    return handle


# --------------------------------------------------------------------------
# jeepney
# --------------------------------------------------------------------------


def _start_jeepney(
    api: MeterAPI, logger: Optional[Callable[[str], Any]], bus_name: str
) -> ServiceHandle:
    """Serve through jeepney's blocking connection, on a thread of our own."""
    from jeepney import (
        HeaderFields,
        MessageType,
        new_error,
        new_method_return,
    )
    from jeepney.bus_messages import message_bus
    from jeepney.io.blocking import open_dbus_connection

    connection = open_dbus_connection(bus="SESSION")
    # DBUS_NAME_FLAG_DO_NOT_QUEUE = 0x4: a taken name is an error, not a queue.
    reply = connection.send_and_get_reply(message_bus.RequestName(bus_name, 0x4))
    code = reply.body[0] if reply.body else 0
    if code not in (1, 4):  # 1 = primary owner, 4 = already the owner
        connection.close()
        raise RuntimeError("cannot own the bus name %s" % (bus_name,))

    methods: Dict[str, Callable[[List[Any]], str]] = {
        "Status": lambda _body: api.status(),
        "Refresh": lambda _body: api.refresh(),
        "GetConfig": lambda _body: api.get_config(),
        "SetConfig": lambda body: api.set_config(body[0] if body else ""),
        "GetSession": lambda _body: api.get_session(),
        "SetSession": lambda body: api.set_session(
            body[0] if body else "", body[1] if len(body) > 1 else ""
        ),
    }
    stop_flag = threading.Event()

    def _serve() -> None:
        while not stop_flag.is_set():
            try:
                message = connection.receive()
            except Exception:  # noqa: BLE001 - a closed connection ends the loop
                return
            try:
                if message.header.message_type != MessageType.method_call:
                    continue
                member = message.header.fields.get(HeaderFields.member)
                handler = methods.get(str(member))
                if handler is None:
                    connection.send(
                        new_error(message, ERROR_NAME, "unknown method: %s" % (member,))
                    )
                    continue
                connection.send(
                    new_method_return(message, "s", (handler(list(message.body)),))
                )
            except Exception as exc:  # noqa: BLE001 - one bad call must not kill the loop
                try:
                    connection.send(
                        new_error(
                            message,
                            ERROR_NAME,
                            "internal error: %s" % (exc.__class__.__name__,),
                        )
                    )
                except Exception:  # pragma: no cover - connection gone
                    return

    thread = threading.Thread(target=_serve, name="sarvam-meter-dbus", daemon=True)
    thread.start()

    def _cleanup() -> None:
        stop_flag.set()
        try:
            connection.close()
        except Exception:  # pragma: no cover - already gone
            pass

    handle = ServiceHandle("jeepney", bus_name=bus_name, cleanup=_cleanup)
    handle._objects = (connection, thread)  # type: ignore[attr-defined]
    return handle


# --------------------------------------------------------------------------
# dasbus
# --------------------------------------------------------------------------


def _start_dasbus(
    api: MeterAPI, logger: Optional[Callable[[str], Any]], bus_name: str
) -> ServiceHandle:
    """Serve through dasbus, with a private GLib event loop on a thread."""
    from dasbus.connection import SessionMessageBus
    from dasbus.loop import EventLoop
    from dasbus.server.interface import dbus_interface
    from dasbus.server.publishable import Publishable
    from dasbus.typing import Str

    @dbus_interface(INTERFACE)
    class _MeterInterface(Publishable):
        def Status(self) -> Str:
            return api.status()

        def Refresh(self) -> Str:
            return api.refresh()

        def GetConfig(self) -> Str:
            return api.get_config()

        def SetConfig(self, document: Str) -> Str:
            return api.set_config(document)

        def GetSession(self) -> Str:
            return api.get_session()

        def SetSession(self, cookie: Str, authorization: Str) -> Str:
            return api.set_session(cookie, authorization)

    bus = SessionMessageBus()
    published = _MeterInterface()
    bus.publish_object(OBJECT_PATH, published)
    bus.register_service(bus_name)
    loop = EventLoop()
    thread = threading.Thread(
        target=loop.run, name="sarvam-meter-dbus", daemon=True
    )
    thread.start()

    def _cleanup() -> None:
        try:
            loop.quit()
        except Exception:  # pragma: no cover - already gone
            pass
        try:
            bus.unpublish_object(OBJECT_PATH, published)
        except Exception:  # pragma: no cover - already gone
            pass
        try:
            bus.disconnect()
        except Exception:  # pragma: no cover - already gone
            pass

    handle = ServiceHandle("dasbus", bus_name=bus_name, cleanup=_cleanup)
    handle._objects = (bus, published, loop, thread)  # type: ignore[attr-defined]
    return handle


#: binding module -> its starter.  Filled in after the definitions so the
#: functions above can be written in any order.
_STARTERS: Dict[str, Callable[[MeterAPI, Optional[Callable[[str], Any]], str], ServiceHandle]] = {
    "dbus": _start_dbus_python,
    "jeepney": _start_jeepney,
    "dasbus": _start_dasbus,
}
