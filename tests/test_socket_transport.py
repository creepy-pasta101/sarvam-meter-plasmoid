# SPDX-License-Identifier: MIT
"""Tests for the daemon's Unix-domain socket transport (unit U1).

The daemon used to serve ``GET /v1/status`` and friends over a loopback TCP
port; every local process could talk to it.  It now binds one ``AF_UNIX``
socket at ``0600`` inside a ``0700`` directory and speaks one JSON object per
line.  Nothing in this file opens a TCP socket: every request goes over the
socket file itself, exactly like the plasmoid does.

What is pinned down here:

* every op round-trips (``status``, ``health``, ``refresh``, ``config.get``,
  ``config.set``, ``session.get``, ``session.set``);
* the reply envelope is exactly ``{"ok": true, "result": ...}`` or
  ``{"ok": false, "error": {"code", "message"}}``;
* an unknown op is ``unknown_op``, a malformed line is ``bad_request`` and a
  line over 256 KiB is ``too_large`` followed by a closed connection;
* the socket file is ``0600``, its directory ``0700``, a *stale* socket file is
  replaced and a *live* one is never clobbered;
* two clients are served concurrently, and a silent client is dropped after the
  idle timeout instead of holding a thread for ever;
* ``config.set`` still rejects a non-whitelisted key and leaves the file
  byte-identical; ``session.set`` still writes ``0600`` and never echoes a
  value;
* :func:`sarvam_meter.client.call` raises ``ClientError`` carrying the daemon's
  own error code, and ``is_running`` answers honestly;
* the daemon imports no HTTP server at all -- there is no TCP listener left.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import tempfile
import threading
import time
import unittest

from sarvam_meter import client
from sarvam_meter import config as config_mod
from sarvam_meter import daemon as daemon_module
from sarvam_meter.daemon import (
    MAX_LINE_BYTES,
    Daemon,
    SESSION_FIELD_LIMITS,
)
from sarvam_meter.client import ClientError

#: A cookie value that must never appear in a reply or in a log line.
COOKIE = "sid=COOKIE-DO-NOT-LEAK-8f2a91; token=COOKIE-DO-NOT-LEAK-8f2a91"
#: A bearer value with the same rule.
AUTHORIZATION = "Bearer AUTH-DO-NOT-LEAK-77c1"

#: Every op the daemon answers, and whether it takes arguments.
ARGUMENT_OPS = ("config.set", "session.set")
SIMPLE_OPS = ("status", "health", "refresh", "config.get", "session.get")


class SocketTransportTestCase(unittest.TestCase):
    """A daemon bound to a throwaway socket, plus raw-socket helpers."""

    #: ``dashboard`` values applied on top of the defaults (no polling).
    dashboard = {"enabled": False}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sarvam-socket-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config_path = os.path.join(self.tmp, "config.json")
        self.session_path = os.path.join(self.tmp, "session.json")
        document = config_mod.default_config_dict()
        document["db_path"] = os.path.join(self.tmp, "usage.db")
        document["dashboard"]["session_file"] = self.session_path
        document["dashboard"].update(self.dashboard)
        self.write_document(document)
        self.config = config_mod.load_config(self.config_path)
        self.logs = []
        #: The daemon creates ``run/`` itself, at mode 0700.
        self.socket_path = os.path.join(self.tmp, "run", "daemon.sock")
        self.daemon = self.start_daemon(self.socket_path)
        self.bound = self.daemon.socket_path

    # -- fixtures --------------------------------------------------------
    def write_document(self, document):
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2)
            handle.write("\n")

    def start_daemon(self, socket_path, idle_timeout=None):
        """Start a daemon on *socket_path*; shut down at the end of the test."""
        daemon = Daemon(
            self.config,
            logger=self.logs.append,
            config_path=self.config_path,
            socket_path=socket_path,
            idle_timeout=idle_timeout,
        )
        daemon.start()
        self.addCleanup(daemon.shutdown)
        return daemon

    def config_bytes(self):
        with open(self.config_path, "rb") as handle:
            return handle.read()

    def session_bytes(self):
        with open(self.session_path, "rb") as handle:
            return handle.read()

    def session_mode(self):
        return stat.S_IMODE(os.stat(self.session_path).st_mode)

    def file_identity(self, path):
        info = os.lstat(path)
        return (info.st_dev, info.st_ino)

    # -- client helper ---------------------------------------------------
    def call(self, op, **args):
        return client.call(op, socket_path=self.bound, timeout=10.0, **args)

    # -- raw socket helpers ----------------------------------------------
    def connect(self, path=None, timeout=10.0):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(timeout)
        connection.connect(self.bound if path is None else path)
        self.addCleanup(connection.close)
        return connection

    def read_lines(self, connection, count=1):
        """Read *count* newline-terminated reply lines (``[]`` at EOF)."""
        buffer = b""
        lines = []
        while len(lines) < count:
            newline = buffer.find(b"\n")
            if newline >= 0:
                lines.append(buffer[:newline])
                buffer = buffer[newline + 1:]
                continue
            chunk = connection.recv(65536)
            if not chunk:
                break
            buffer += chunk
        return lines

    def send_raw(self, payload, count=1):
        """Send *payload* verbatim; return the parsed reply objects."""
        connection = self.connect()
        try:
            connection.sendall(payload)
        except OSError:  # the daemon may close the connection mid-send
            pass
        lines = self.read_lines(connection, count)
        return [json.loads(line.decode("utf-8")) for line in lines]

    def assert_error(self, reply, code):
        self.assertEqual(reply.get("ok"), False, reply)
        error = reply.get("error")
        self.assertIsInstance(error, dict, reply)
        self.assertEqual(sorted(error), ["code", "message"], reply)
        self.assertEqual(error["code"], code, reply)
        self.assertTrue(error["message"], reply)
        return error


class ConfigSocketPathTest(SocketTransportTestCase):
    """`daemon.socket_path` must actually decide where the daemon binds.

    A config key that is validated but ignored is worse than no key: it reads as
    a setting and behaves as a no-op.  This pins the whole chain -- config key ->
    client.socket_path() -> the socket the daemon binds -> a call that answers.
    """

    def test_the_config_key_decides_where_the_daemon_binds(self):
        from sarvam_meter.config import load_config, write_default_config

        target = os.path.join(self.tmp, "custom", "meter.sock")
        config_path = write_default_config(os.path.join(self.tmp, "config.json"))
        with open(config_path, encoding="utf-8") as handle:
            document = json.load(handle)
        document["daemon"]["socket_path"] = target
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        config = load_config(config_path)

        self.assertEqual(client.socket_path(config), target)

        daemon = Daemon(config, logger=self.logs.append, config_path=config_path)
        daemon.start()
        self.addCleanup(daemon.shutdown)
        self.assertEqual(daemon.socket_path, target)
        self.assertTrue(stat.S_ISSOCK(os.lstat(target).st_mode))
        self.assertTrue(client.call("health", socket_path=target)["ok"])

    def test_the_environment_beats_the_config_key(self):
        from sarvam_meter.config import load_config, write_default_config

        config_path = write_default_config(os.path.join(self.tmp, "config.json"))
        with open(config_path, encoding="utf-8") as handle:
            document = json.load(handle)
        document["daemon"]["socket_path"] = os.path.join(self.tmp, "from-config.sock")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        config = load_config(config_path)

        override = os.path.join(self.tmp, "from-env.sock")
        os.environ["SARVAM_METER_SOCKET"] = override
        self.addCleanup(os.environ.pop, "SARVAM_METER_SOCKET", None)

        self.assertEqual(client.socket_path(config), override)


class RoundTripTest(SocketTransportTestCase):
    """Every op answers with the payload its HTTP route used to serve."""

    def test_status_round_trips(self):
        result = self.call("status")
        self.assertIn("sarvam", result)
        self.assertIn("cliproxy", result)
        self.assertIn("errors", result)

    def test_health_round_trips(self):
        result = self.call("health")
        self.assertTrue(result["ok"])
        self.assertIn("version", result)
        self.assertIsInstance(result["uptime_seconds"], int)

    def test_refresh_round_trips_and_returns_status(self):
        result = self.call("refresh")
        self.assertIn("sarvam", result)
        self.assertIn("generated_at", result)

    def test_config_get_round_trips(self):
        result = self.call("config.get")
        self.assertEqual(result["daemon"]["refresh_seconds"], 60)
        self.assertIn("dashboard", result)

    def test_config_set_round_trips(self):
        result = self.call("config.set", config={"dashboard": {"poll_seconds": 120}})
        self.assertEqual(result["dashboard"]["poll_seconds"], 120)
        with open(self.config_path, "r", encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["dashboard"]["poll_seconds"], 120)

    def test_session_get_round_trips(self):
        result = self.call("session.get")
        self.assertEqual(
            sorted(result), ["configured", "headers", "mode", "path"]
        )
        self.assertFalse(result["configured"])

    def test_session_set_round_trips(self):
        result = self.call("session.set", cookie=COOKIE)
        self.assertEqual(result["stored"], ["cookie"])
        self.assertEqual(result["mode"], "0600")
        self.assertEqual(result["path"], self.session_path)

    def test_every_documented_op_is_served(self):
        self.assertEqual(
            sorted(daemon_module.OPS),
            sorted(SIMPLE_OPS + ARGUMENT_OPS),
        )
        self.assertEqual(sorted(daemon_module.OP_HANDLERS), sorted(daemon_module.OPS))

    def test_several_ops_on_one_connection(self):
        connection = self.connect()
        connection.sendall(b'{"op": "health"}\n{"op": "session.get"}\n')
        replies = self.read_lines(connection, 2)
        self.assertEqual(len(replies), 2)
        first, second = (json.loads(line.decode("utf-8")) for line in replies)
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertIn("headers", second["result"])

    def test_a_partial_line_is_not_answered_until_it_is_complete(self):
        connection = self.connect()
        connection.sendall(b'{"op": "hea')
        time.sleep(0.2)
        connection.settimeout(0.2)
        with self.assertRaises(socket.timeout):
            connection.recv(1024)
        connection.settimeout(10.0)
        connection.sendall(b'lth"}\n')
        reply = json.loads(self.read_lines(connection, 1)[0].decode("utf-8"))
        self.assertTrue(reply["ok"])


class EnvelopeTest(SocketTransportTestCase):
    """The reply envelope is exactly the documented shape."""

    def test_success_envelope(self):
        reply = self.send_raw(b'{"op": "health"}\n')[0]
        self.assertEqual(sorted(reply), ["ok", "result"])
        self.assertIs(reply["ok"], True)
        self.assertIsInstance(reply["result"], dict)

    def test_error_envelope(self):
        reply = self.send_raw(b'{"op": "nope"}\n')[0]
        self.assertEqual(sorted(reply), ["error", "ok"])
        self.assertIs(reply["ok"], False)
        self.assert_error(reply, "unknown_op")

    def test_unknown_op_names_the_op(self):
        error = self.assert_error(self.send_raw(b'{"op": "nope"}\n')[0], "unknown_op")
        self.assertIn("nope", error["message"])

    def test_malformed_lines_are_bad_request(self):
        for payload in (
            b"this is not json\n",
            b'{"op": "status"\n',
            b"[]\n",
            b"[1, 2, 3]\n",
            b'"status"\n',
            b"\n",
            b'{"no_op": 1}\n',
            b'{"op": 7}\n',
            b'{"op": ""}\n',
        ):
            with self.subTest(payload=payload):
                reply = self.send_raw(payload)[0]
                self.assert_error(reply, "bad_request")

    def test_a_connection_survives_a_bad_line(self):
        connection = self.connect()
        connection.sendall(b"garbage\n")
        first = json.loads(self.read_lines(connection, 1)[0].decode("utf-8"))
        self.assertEqual(first["error"]["code"], "bad_request")
        connection.sendall(b'{"op": "health"}\n')
        second = json.loads(self.read_lines(connection, 1)[0].decode("utf-8"))
        self.assertTrue(second["ok"])

    def test_oversized_line_is_too_large_and_closes_the_connection(self):
        connection = self.connect()
        payload = b"x" * (MAX_LINE_BYTES + 4096)
        try:
            connection.sendall(payload)
        except OSError:
            pass  # the daemon may close the connection while we are still writing
        reply = json.loads(self.read_lines(connection, 1)[0].decode("utf-8"))
        self.assert_error(reply, "too_large")
        # The connection is closed after the reply: no second answer is coming.
        connection.settimeout(5.0)
        self.assertEqual(connection.recv(1024), b"")

    def test_a_line_at_the_limit_is_answered(self):
        # A well-formed request padded with spaces to just under the limit.
        body = b'{"op": "health"}'
        payload = body + b" " * (MAX_LINE_BYTES - len(body) - 1) + b"\n"
        reply = self.send_raw(payload)[0]
        self.assertTrue(reply["ok"], reply)


class SocketFileTest(SocketTransportTestCase):
    """The socket file is private, replaceable when stale, and never stolen."""

    def test_socket_is_0600_in_a_0700_directory(self):
        info = os.lstat(self.bound)
        self.assertTrue(stat.S_ISSOCK(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        directory = os.path.dirname(self.bound)
        self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o700)

    def test_the_bound_path_is_the_configured_one(self):
        self.assertEqual(self.bound, self.socket_path)
        self.assertEqual(self.daemon.socket_path, self.socket_path)

    def test_a_stale_socket_file_is_replaced(self):
        path = os.path.join(self.tmp, "stale", "daemon.sock")
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(path)
        stale.close()  # the file stays behind; nothing is listening on it
        self.assertTrue(stat.S_ISSOCK(os.lstat(path).st_mode))

        daemon = self.start_daemon(path)
        # Not an inode comparison: after the stale file is unlinked the kernel is
        # free to hand the same inode number straight back, which made this fail
        # on a loaded CI runner.  What matters is that a daemon now answers where
        # nothing was listening before.
        self.assertTrue(stat.S_ISSOCK(os.lstat(path).st_mode))
        self.assertTrue(client.is_running(daemon.socket_path))
        self.assertTrue(client.call("health", socket_path=daemon.socket_path)["ok"])

    def test_a_live_socket_is_not_clobbered(self):
        before = self.file_identity(self.bound)
        second = Daemon(
            self.config,
            logger=self.logs.append,
            config_path=self.config_path,
            socket_path=self.bound,
        )
        with self.assertRaises(OSError):
            second.start()
        self.addCleanup(second.shutdown)
        # The first daemon still owns the path and still answers.
        self.assertEqual(self.file_identity(self.bound), before)
        self.assertTrue(self.call("health")["ok"])

    def test_a_plain_file_is_never_replaced(self):
        path = os.path.join(self.tmp, "plain", "daemon.sock")
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("not a socket\n")
        daemon = Daemon(
            self.config,
            logger=self.logs.append,
            config_path=self.config_path,
            socket_path=path,
        )
        self.addCleanup(daemon.shutdown)
        with self.assertRaises(OSError):
            daemon.start()
        with open(path, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "not a socket\n")

    def test_a_clean_shutdown_unlinks_the_socket(self):
        path = os.path.join(self.tmp, "clean", "daemon.sock")
        daemon = Daemon(
            self.config,
            logger=self.logs.append,
            config_path=self.config_path,
            socket_path=path,
        )
        daemon.start()
        self.assertTrue(os.path.exists(path))
        daemon.shutdown()
        self.assertFalse(os.path.exists(path), "the socket file was left behind")
        self.assertFalse(client.is_running(path))


class ConcurrencyTest(SocketTransportTestCase):
    """Several clients at once, and a silent one that must not linger."""

    def test_two_concurrent_clients(self):
        results = []
        errors = []
        barrier = threading.Barrier(4)

        def worker():
            try:
                barrier.wait(timeout=10.0)
                for _ in range(5):
                    results.append(self.call("health")["ok"])
                    results.append(bool(self.call("status")["sarvam"]))
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30.0)
            self.assertFalse(thread.is_alive(), "a client thread hung")
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 40)
        self.assertTrue(all(results))

    def test_an_idle_client_is_dropped_after_the_timeout(self):
        path = os.path.join(self.tmp, "idle", "daemon.sock")
        daemon = self.start_daemon(path, idle_timeout=0.4)
        connection = self.connect(daemon.socket_path, timeout=10.0)
        started = time.time()
        self.assertEqual(
            connection.recv(1024), b"", "the daemon did not close an idle connection"
        )
        elapsed = time.time() - started
        self.assertGreaterEqual(elapsed, 0.2)
        self.assertLess(elapsed, 9.0)
        # The daemon is still healthy after dropping that client.
        self.assertTrue(client.call("health", socket_path=daemon.socket_path)["ok"])

    def test_a_client_that_says_nothing_does_not_block_another(self):
        path = os.path.join(self.tmp, "idle2", "daemon.sock")
        daemon = self.start_daemon(path, idle_timeout=2.0)
        silent = self.connect(daemon.socket_path)
        self.assertTrue(client.call("health", socket_path=daemon.socket_path)["ok"])
        silent.close()


class ConfigSetTest(SocketTransportTestCase):
    """``config.set`` keeps the HTTP route's whitelist and atomic write."""

    def test_a_non_whitelisted_key_is_rejected_and_the_file_is_untouched(self):
        before = self.config_bytes()
        with self.assertRaises(ClientError) as raised:
            self.call("config.set", config={"sarvam": {"starting_balance": 10.0}})
        self.assertEqual(raised.exception.code, "invalid_config")
        self.assertIn("sarvam.starting_balance is not writable", str(raised.exception))
        self.assertEqual(self.config_bytes(), before)

    def test_an_unknown_section_is_rejected(self):
        before = self.config_bytes()
        with self.assertRaises(ClientError) as raised:
            self.call("config.set", config={"nonsense": {"a": 1}})
        self.assertEqual(raised.exception.code, "invalid_config")
        self.assertIn("nonsense is not writable", str(raised.exception))
        self.assertEqual(self.config_bytes(), before)

    def test_an_invalid_value_is_rejected(self):
        before = self.config_bytes()
        with self.assertRaises(ClientError) as raised:
            self.call("config.set", config={"dashboard": {"poll_seconds": 1}})
        self.assertEqual(raised.exception.code, "invalid_config")
        self.assertEqual(self.config_bytes(), before)

    def test_a_missing_config_argument_is_rejected(self):
        reply = self.send_raw(b'{"op": "config.set"}\n')[0]
        self.assert_error(reply, "invalid_config")

    def test_a_config_body_that_is_not_an_object_is_rejected(self):
        before = self.config_bytes()
        reply = self.send_raw(b'{"op": "config.set", "config": [1, 2]}\n')[0]
        self.assert_error(reply, "invalid_config")
        self.assertEqual(self.config_bytes(), before)

    def test_the_config_file_stays_private_after_a_successful_write(self):
        self.call("config.set", config={"dashboard": {"poll_seconds": 300}})
        mode = stat.S_IMODE(os.stat(self.config_path).st_mode)
        self.assertEqual(mode, 0o600)


class SessionSetTest(SocketTransportTestCase):
    """``session.set`` writes ``0600`` and never echoes a value."""

    def test_a_cookie_is_stored_at_0600_and_never_echoed(self):
        result = self.call("session.set", cookie=COOKIE)
        self.assertEqual(result["stored"], ["cookie"])
        self.assertEqual(result["mode"], "0600")
        self.assertNotIn("COOKIE-DO-NOT-LEAK", json.dumps(result))
        self.assertEqual(self.session_mode(), 0o600)
        with open(self.session_path, "r", encoding="utf-8") as handle:
            stored = json.load(handle)
        self.assertEqual(stored, {"Cookie": COOKIE})

    def test_both_headers_are_stored_and_never_echoed(self):
        result = self.call("session.set", cookie=COOKIE, authorization=AUTHORIZATION)
        self.assertEqual(result["stored"], ["cookie", "authorization"])
        text = json.dumps(result)
        self.assertNotIn("COOKIE-DO-NOT-LEAK", text)
        self.assertNotIn("AUTH-DO-NOT-LEAK", text)
        self.assertEqual(self.session_mode(), 0o600)

    def test_session_get_reports_names_only(self):
        self.call("session.set", cookie=COOKIE, authorization=AUTHORIZATION)
        result = self.call("session.get")
        self.assertTrue(result["configured"])
        self.assertEqual(result["mode"], "0600")
        self.assertEqual(result["headers"], ["cookie", "authorization"])
        self.assertNotIn("COOKIE-DO-NOT-LEAK", json.dumps(result))
        self.assertNotIn("AUTH-DO-NOT-LEAK", json.dumps(result))

    def test_an_unknown_field_is_rejected_and_the_file_is_untouched(self):
        self.call("session.set", cookie=COOKIE)
        before = self.session_bytes()
        with self.assertRaises(ClientError) as raised:
            self.call("session.set", cookie=COOKIE, extra="x")
        self.assertEqual(raised.exception.code, "invalid_session")
        self.assertIn("extra is not a session field", str(raised.exception))
        self.assertEqual(self.session_bytes(), before)

    def test_a_newline_value_is_rejected(self):
        with self.assertRaises(ClientError) as raised:
            self.call("session.set", cookie="sid=1\nX-Injected: 1")
        self.assertEqual(raised.exception.code, "invalid_session")
        self.assertFalse(os.path.exists(self.session_path))

    def test_an_oversized_value_is_rejected(self):
        limit = SESSION_FIELD_LIMITS["cookie"]
        with self.assertRaises(ClientError) as raised:
            self.call("session.set", cookie="c" * (limit + 1))
        self.assertEqual(raised.exception.code, "invalid_session")
        self.assertFalse(os.path.exists(self.session_path))

    def test_an_empty_capture_is_rejected(self):
        reply = self.send_raw(b'{"op": "session.set"}\n')[0]
        self.assert_error(reply, "invalid_session")

    def test_no_value_is_ever_logged(self):
        self.call("session.set", cookie=COOKIE, authorization=AUTHORIZATION)
        text = "\n".join(str(line) for line in self.logs)
        self.assertNotIn("COOKIE-DO-NOT-LEAK", text)
        self.assertNotIn("AUTH-DO-NOT-LEAK", text)
        self.assertIn("session.set", text)


class ClientTest(SocketTransportTestCase):
    """``sarvam_meter.client``: the error code survives, ``is_running`` is honest."""

    def test_a_daemon_error_carries_its_code(self):
        with self.assertRaises(ClientError) as raised:
            self.call("no.such.op")
        self.assertEqual(raised.exception.code, "unknown_op")
        self.assertIn("unknown op", str(raised.exception))

    def test_a_rejected_config_update_carries_its_code(self):
        with self.assertRaises(ClientError) as raised:
            self.call("config.set", config={"daemon": {"port": 1}})
        self.assertEqual(raised.exception.code, "invalid_config")

    def test_no_daemon_is_a_client_error_without_a_code(self):
        with self.assertRaises(ClientError) as raised:
            client.call("health", socket_path=os.path.join(self.tmp, "absent.sock"))
        self.assertIsNone(raised.exception.code)
        self.assertIn("no daemon listening", str(raised.exception))

    def test_is_running_is_false_without_a_daemon(self):
        self.assertFalse(client.is_running(os.path.join(self.tmp, "absent.sock")))

    def test_is_running_is_true_with_a_daemon(self):
        self.assertTrue(client.is_running(self.bound))

    def test_is_running_is_false_for_a_stale_socket_file(self):
        path = os.path.join(self.tmp, "stale2", "daemon.sock")
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(path)
        stale.close()
        self.assertFalse(client.is_running(path))

    def test_the_socket_path_resolution_is_shared(self):
        original = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = self.tmp
        try:
            self.assertEqual(
                client.socket_path(self.config),
                os.path.join(self.tmp, "sarvam-meter", "daemon.sock"),
            )
            self.assertEqual(
                client.socket_path(),
                os.path.join(self.tmp, "sarvam-meter", "daemon.sock"),
            )
        finally:
            if original is None:
                os.environ.pop("XDG_RUNTIME_DIR", None)
            else:
                os.environ["XDG_RUNTIME_DIR"] = original

    def test_the_socket_path_falls_back_to_the_cache_directory(self):
        original = os.environ.pop("XDG_RUNTIME_DIR", None)
        try:
            self.assertEqual(
                client.socket_path(),
                os.path.join(
                    os.path.expanduser("~"), ".cache", "sarvam-meter", "daemon.sock"
                ),
            )
        finally:
            if original is not None:
                os.environ["XDG_RUNTIME_DIR"] = original

    def test_the_daemon_and_the_client_agree_on_the_path(self):
        path = os.path.join(self.tmp, "shared", "daemon.sock")
        daemon = self.start_daemon(path)
        self.assertEqual(daemon.socket_path, path)
        self.assertTrue(client.is_running(path))


class NoTcpTest(SocketTransportTestCase):
    """There is no HTTP server and no TCP listener left in the daemon."""

    def test_the_daemon_imports_no_http_server(self):
        for name in ("ThreadingHTTPServer", "BaseHTTPRequestHandler", "HTTPServer"):
            self.assertFalse(
                hasattr(daemon_module, name),
                "sarvam_meter.daemon still exposes %s" % (name,),
            )
        self.assertFalse(hasattr(daemon_module, "_Handler"))

    def test_no_source_file_mentions_an_http_server(self):
        for module in (daemon_module, client):
            with open(module.__file__, "r", encoding="utf-8") as handle:
                source = handle.read()
            for forbidden in (
                "http.server",
                "ThreadingHTTPServer",
                "BaseHTTPRequestHandler",
                "socketserver",
                "AF_INET",
                "127.0.0.1:8787",
            ):
                with self.subTest(module=module.__name__, forbidden=forbidden):
                    self.assertNotIn(forbidden, source)

    def test_the_listening_socket_is_a_unix_socket(self):
        server = self.daemon.server
        self.assertIsNotNone(server)
        listening = server._socket
        self.assertIsNotNone(listening)
        self.assertEqual(listening.family, socket.AF_UNIX)
        self.assertEqual(self.daemon.family, socket.AF_UNIX)

    def test_the_daemon_module_carries_no_http_machinery(self):
        namespace = daemon_module.__dict__
        for name in ("ThreadingHTTPServer", "BaseHTTPRequestHandler", "urlsplit"):
            self.assertNotIn(
                name, namespace, "sarvam_meter.daemon still imports %s" % (name,)
            )
        # ``http.server`` must not be pulled in through the daemon either.
        self.assertNotIn("http.server", dir(daemon_module))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
