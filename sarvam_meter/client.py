# SPDX-License-Identifier: MIT
"""Client for the ``sarvam-meter`` daemon's Unix-domain socket.

The daemon (``sarvam_meter.daemon``) no longer listens on a TCP port: it binds
an ``AF_UNIX`` stream socket under the user's runtime directory, at mode
``0600`` inside a ``0700`` directory, and speaks one JSON object per line in
each direction::

    -> {"op": "status"}\n
    <- {"ok": true, "result": {...}}\n
    <- {"ok": false, "error": {"code": "unknown_op", "message": "..."}}\n

This module is the only place the socket *path* is worked out -- the daemon
imports :func:`socket_path` from here, so the two sides can never disagree --
and the only place a client needs to know about the wire format.

Resolution order for the path (first non-empty wins):

1. ``$SARVAM_METER_SOCKET`` -- an explicit override, useful in tests and for a
   daemon started with a hand-written ``--socket``;
2. ``daemon.socket_path`` in the config document, when it is set;
3. ``$XDG_RUNTIME_DIR/sarvam-meter/daemon.sock``;
4. ``~/.cache/sarvam-meter/daemon.sock`` when ``XDG_RUNTIME_DIR`` is unset.

Standard library only, Python 3.9 compatible.
"""

from __future__ import annotations

import json
import os
import socket
import time
from typing import Any, Dict, Optional

#: Directory (below the runtime directory) the daemon's socket lives in.
SOCKET_DIRNAME = "sarvam-meter"
#: File name of the daemon's socket inside :data:`SOCKET_DIRNAME`.
SOCKET_FILENAME = "daemon.sock"
#: Environment variable that overrides the resolved socket path outright.
ENV_SOCKET_PATH = "SARVAM_METER_SOCKET"
#: Default timeout (seconds) for a request/reply round trip.
DEFAULT_TIMEOUT = 10.0
#: Upper bound on a single reply line; a larger one is a broken daemon.
MAX_REPLY_BYTES = 8 * 1024 * 1024


class ClientError(Exception):
    """A request to the daemon failed, or the daemon answered ``ok: false``.

    ``code`` is the daemon's error code (``unknown_op``, ``bad_request``,
    ``too_large``, ``invalid_config``, ``invalid_session``, ``internal_error``)
    when the daemon produced one, and ``None`` when the failure happened before
    the daemon could answer (no daemon, timeout, malformed reply).
    """

    def __init__(self, message: str, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _default_socket_path() -> str:
    """The socket path implied by the environment alone (no config involved)."""
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if runtime.strip():
        base = os.path.expanduser(runtime.strip())
    else:
        # ``XDG_RUNTIME_DIR`` is unset (e.g. a cron job or a bare SSH session):
        # fall back to the cache directory, which is still per-user.
        base = os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, SOCKET_DIRNAME, SOCKET_FILENAME)


def socket_path(config: Any = None) -> str:
    """The daemon's socket path for *config* (or for the environment alone).

    *config* may be a :class:`sarvam_meter.config.Config`, the ``daemon``
    section of one, or ``None``.  The value is expanded (``~``) but not
    otherwise normalised, so a path the caller set is used verbatim.
    """
    explicit = os.environ.get(ENV_SOCKET_PATH, "")
    if explicit.strip():
        return os.path.expanduser(explicit.strip())
    section = getattr(config, "daemon", config)
    configured = getattr(section, "socket_path", "") if section is not None else ""
    if isinstance(configured, str) and configured.strip():
        return os.path.expanduser(configured.strip())
    return _default_socket_path()


def _connect(path: str, timeout: float) -> socket.socket:
    """Open an ``AF_UNIX`` connection to *path*, or raise :class:`ClientError`."""
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    try:
        connection.connect(path)
    except FileNotFoundError:
        connection.close()
        raise ClientError(
            "no daemon listening on %s (socket file is missing)" % (path,)
        ) from None
    except ConnectionRefusedError:
        connection.close()
        raise ClientError(
            "no daemon listening on %s (stale socket file)" % (path,)
        ) from None
    except socket.timeout:
        connection.close()
        raise ClientError(
            "timed out connecting to the daemon on %s" % (path,)
        ) from None
    except OSError as exc:
        connection.close()
        raise ClientError(
            "cannot connect to the daemon on %s (%s)"
            % (path, exc.strerror or exc.__class__.__name__)
        ) from None
    return connection


def _read_line(connection: socket.socket, deadline: float) -> bytes:
    """Read one ``\\n``-terminated reply line, bounded by :data:`MAX_REPLY_BYTES`."""
    buffer = bytearray()
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise ClientError("timed out waiting for the daemon to answer")
        connection.settimeout(remaining)
        try:
            chunk = connection.recv(65536)
        except socket.timeout:
            raise ClientError("timed out waiting for the daemon to answer") from None
        except OSError as exc:
            raise ClientError(
                "lost the connection to the daemon (%s)"
                % (exc.strerror or exc.__class__.__name__,)
            ) from None
        if not chunk:
            raise ClientError("the daemon closed the connection without answering")
        buffer.extend(chunk)
        if len(buffer) > MAX_REPLY_BYTES:
            raise ClientError("the daemon's reply is too large")
        newline = buffer.find(b"\n")
        if newline >= 0:
            return bytes(buffer[:newline])


def call(
    op: str,
    socket_path: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
    **args: Any
) -> Dict[str, Any]:
    """Send *op* with *args* to the daemon and return its ``result`` object.

    *socket_path* defaults to the path :func:`socket_path` resolves from the
    environment.  Raises :class:`ClientError` when the daemon cannot be
    reached, when the reply is not a well-formed envelope, or when the daemon
    answered ``ok: false`` -- in which case ``error.code`` is preserved on the
    exception.

    One request is one line; the connection is opened, used and closed, so a
    caller never has to manage state.  (The daemon also accepts several ops on
    one connection, for a client that would rather keep it open.)
    """
    if not isinstance(op, str) or not op.strip():
        raise ClientError("an op name is required")
    path = socket_path
    if path is None or not str(path).strip():
        path = _default_socket_path()
    else:
        path = os.path.expanduser(str(path))

    request: Dict[str, Any] = {"op": op.strip()}
    request.update(args)
    try:
        payload = (json.dumps(request, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ClientError("cannot encode the request as JSON (%s)" % (exc,)) from None

    deadline = time.time() + float(timeout)
    connection = _connect(path, float(timeout))
    try:
        try:
            connection.sendall(payload)
        except OSError as exc:
            raise ClientError(
                "cannot send the request to the daemon (%s)"
                % (exc.strerror or exc.__class__.__name__,)
            ) from None
        line = _read_line(connection, deadline)
    finally:
        try:
            connection.close()
        except OSError:  # pragma: no cover - defensive
            pass

    try:
        reply = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ClientError("the daemon sent a reply that is not JSON") from None
    if not isinstance(reply, dict):
        raise ClientError("the daemon sent a reply that is not a JSON object")
    if reply.get("ok") is True:
        result = reply.get("result")
        if not isinstance(result, dict):
            raise ClientError("the daemon's reply carries no result object")
        return result
    error = reply.get("error")
    if not isinstance(error, dict):
        raise ClientError("the daemon rejected the request without an error object")
    message = error.get("message")
    code = error.get("code")
    raise ClientError(
        str(message) if isinstance(message, str) and message else "the request failed",
        str(code) if isinstance(code, str) and code else None,
    )


def is_running(
    socket_path: Optional[str] = None, timeout: float = 2.0
) -> bool:
    """True when a daemon answers a ``health`` request on *socket_path*.

    A missing socket file, a stale one (nothing listening) and a socket that
    accepts the connection but never answers all count as "not running".
    """
    try:
        call("health", socket_path=socket_path, timeout=timeout)
    except ClientError:
        return False
    return True
