# SPDX-License-Identifier: MIT
"""Tests for :mod:`sarvam_meter.dbus_service` (unit B2).

Three things are checked here, in order of how much machinery they need:

* the JSON marshalling of all six methods, against a stand-in daemon, with no
  bus and no binding involved at all;
* the import-safe degradation: the module must import and ``start()`` must
  return ``None`` (with one explanatory log line) when none of ``dbus``,
  ``jeepney`` or ``dasbus`` is installed;
* one end-to-end test over a real session bus, which runs **only** when a
  binding *and* ``dbus-run-session`` are both available, and skips with a clear
  reason otherwise.

The daemon's own guarantees -- validation, redaction, "a stored value is never
echoed" -- are tested by the daemon's tests; what is tested here is that the
D-Bus layer *uses* those handlers and cannot leak around them.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from sarvam_meter import dbus_service
from sarvam_meter.daemon import ConfigUpdateError, SessionUpdateError

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: A value that must never appear in a D-Bus reply, a log line or an error.
SECRET_COOKIE = "session=COOKIE-VALUE-DO-NOT-ECHO"
SECRET_AUTHORIZATION = "Bearer TOKEN-VALUE-DO-NOT-ECHO"


class FakeDaemon:
    """The slice of :class:`sarvam_meter.daemon.Daemon` the op handlers use."""

    def __init__(self) -> None:
        self.status_calls = 0
        self.cliproxy_refreshes = 0
        self.dashboard_refreshes = 0
        self.config_written = None
        self.session_written = None
        self.raise_on_session = None
        self.raise_on_config = None

    # -- reads -----------------------------------------------------------
    def status(self) -> dict:
        self.status_calls += 1
        return {
            "schema": 2,
            "generated_at": "2026-11-01T06:00:00+05:30",
            "stale": False,
            "errors": [],
            "sarvam": {"currency": "INR", "configured": True},
            "cliproxy": {"configured": False, "accounts": []},
        }

    def health(self) -> dict:
        return {"ok": True, "version": "0.1.0", "uptime_seconds": 3}

    def config_payload(self) -> dict:
        return {"version": 1, "dashboard": {"enabled": True, "poll_seconds": 300}}

    def session_payload(self) -> dict:
        return {
            "configured": True,
            "path": "/home/user/.config/sarvam-meter/session.json",
            "mode": "0600",
            "headers": ["cookie"],
        }

    # -- refreshes -------------------------------------------------------
    def refresh_cliproxy(self) -> dict:
        self.cliproxy_refreshes += 1
        return {}

    def refresh_dashboard(self) -> dict:
        self.dashboard_refreshes += 1
        return {}

    # -- writes ----------------------------------------------------------
    def apply_config(self, request):
        if self.raise_on_config is not None:
            raise self.raise_on_config
        self.config_written = request
        return self.config_payload()

    def apply_session(self, request):
        if self.raise_on_session is not None:
            raise self.raise_on_session
        # Exactly what the real daemon returns: names, path, mode - no values.
        self.session_written = dict(request)
        stored = [name for name in ("cookie", "authorization") if name in request]
        return {
            "stored": stored,
            "path": "/home/user/.config/sarvam-meter/session.json",
            "mode": "0600",
        }


def parse(reply: str) -> dict:
    """The envelope a D-Bus method returned, decoded."""
    document = json.loads(reply)
    assert isinstance(document, dict), document
    return document


class MarshallingTest(unittest.TestCase):
    """Every method returns the envelope the socket would have produced."""

    def setUp(self):
        self.daemon = FakeDaemon()
        self.logs = []
        self.api = dbus_service.MeterAPI(self.daemon, logger=self.logs.append)

    def test_status_wraps_the_daemons_own_status(self):
        envelope = parse(self.api.status())
        self.assertIs(envelope["ok"], True)
        self.assertEqual(envelope["result"], self.daemon.status())

    def test_refresh_repolls_both_sources_then_returns_status(self):
        envelope = parse(self.api.refresh())
        self.assertIs(envelope["ok"], True)
        self.assertEqual(self.daemon.cliproxy_refreshes, 1)
        self.assertEqual(self.daemon.dashboard_refreshes, 1)
        self.assertEqual(envelope["result"]["schema"], 2)

    def test_get_config_is_the_sanitised_document(self):
        envelope = parse(self.api.get_config())
        self.assertIs(envelope["ok"], True)
        self.assertEqual(envelope["result"], self.daemon.config_payload())

    def test_set_config_decodes_the_json_argument(self):
        subset = {"dashboard": {"poll_seconds": 600}}
        envelope = parse(self.api.set_config(json.dumps(subset)))
        self.assertIs(envelope["ok"], True)
        self.assertEqual(self.daemon.config_written, subset)

    def test_set_config_refuses_a_malformed_argument_without_writing(self):
        envelope = parse(self.api.set_config("{not json"))
        self.assertIs(envelope["ok"], False)
        self.assertEqual(envelope["error"]["code"], "invalid_config")
        self.assertIsNone(self.daemon.config_written)

    def test_set_config_refuses_a_non_object(self):
        envelope = parse(self.api.set_config("[1, 2, 3]"))
        self.assertIs(envelope["ok"], False)
        self.assertEqual(envelope["error"]["code"], "invalid_config")
        self.assertIsNone(self.daemon.config_written)

    def test_set_config_refuses_an_oversized_argument(self):
        payload = json.dumps({"dashboard": {"base_url": "x" * (70 * 1024)}})
        envelope = parse(self.api.set_config(payload))
        self.assertIs(envelope["ok"], False)
        self.assertEqual(envelope["error"]["code"], "invalid_config")
        self.assertIsNone(self.daemon.config_written)

    def test_set_config_reports_a_rejection_with_the_daemons_code(self):
        self.daemon.raise_on_config = ConfigUpdateError("proxy is not writable")
        envelope = parse(self.api.set_config(json.dumps({"proxy": {"enabled": True}})))
        self.assertIs(envelope["ok"], False)
        self.assertEqual(envelope["error"]["code"], "invalid_config")
        self.assertIn("not writable", envelope["error"]["message"])

    def test_get_session_reports_header_names_only(self):
        envelope = parse(self.api.get_session())
        self.assertIs(envelope["ok"], True)
        self.assertEqual(envelope["result"]["headers"], ["cookie"])
        self.assertEqual(envelope["result"]["mode"], "0600")

    def test_set_session_never_echoes_a_value(self):
        reply = self.api.set_session(SECRET_COOKIE, SECRET_AUTHORIZATION)
        self.assertNotIn("COOKIE-VALUE-DO-NOT-ECHO", reply)
        self.assertNotIn("TOKEN-VALUE-DO-NOT-ECHO", reply)
        envelope = parse(reply)
        self.assertIs(envelope["ok"], True)
        self.assertEqual(sorted(envelope["result"]["stored"]), ["authorization", "cookie"])
        self.assertEqual(
            sorted(self.daemon.session_written), ["authorization", "cookie"]
        )

    def test_set_session_never_echoes_a_value_in_a_log_line(self):
        self.api.set_session(SECRET_COOKIE, SECRET_AUTHORIZATION)
        joined = "\n".join(self.logs)
        self.assertNotIn("COOKIE-VALUE-DO-NOT-ECHO", joined)
        self.assertNotIn("TOKEN-VALUE-DO-NOT-ECHO", joined)

    def test_set_session_omits_an_empty_authorization(self):
        envelope = parse(self.api.set_session(SECRET_COOKIE, "   "))
        self.assertIs(envelope["ok"], True)
        self.assertEqual(sorted(self.daemon.session_written), ["cookie"])

    def test_set_session_requires_at_least_one_value(self):
        envelope = parse(self.api.set_session("", ""))
        self.assertIs(envelope["ok"], False)
        self.assertEqual(envelope["error"]["code"], "invalid_session")
        self.assertIsNone(self.daemon.session_written)

    def test_set_session_refuses_a_non_string_argument(self):
        envelope = parse(self.api.set_session(42, ""))
        self.assertIs(envelope["ok"], False)
        self.assertEqual(envelope["error"]["code"], "invalid_session")

    def test_set_session_reports_a_rejection_without_the_value(self):
        self.daemon.raise_on_session = SessionUpdateError("cookie: must not contain CR, LF or NUL")
        reply = self.api.set_session(SECRET_COOKIE, "")
        self.assertNotIn("COOKIE-VALUE-DO-NOT-ECHO", reply)
        envelope = parse(reply)
        self.assertIs(envelope["ok"], False)
        self.assertEqual(envelope["error"]["code"], "invalid_session")

    def test_an_unencodable_payload_becomes_an_error_envelope(self):
        self.daemon.status = lambda: {"spend": float("nan")}  # type: ignore[assignment]
        envelope = parse(self.api.status())
        self.assertIs(envelope["ok"], False)
        self.assertEqual(envelope["error"]["code"], "internal_error")

    def test_a_crashing_handler_becomes_an_internal_error(self):
        def boom(*_args, **_kwargs):
            raise RuntimeError("kaboom: /home/user/secret-path")

        self.daemon.status = boom  # type: ignore[assignment]
        reply = self.api.status()
        envelope = parse(reply)
        self.assertIs(envelope["ok"], False)
        self.assertEqual(envelope["error"]["code"], "internal_error")
        # The class name only: a traceback or a path must never cross the bus.
        self.assertIn("RuntimeError", envelope["error"]["message"])
        self.assertNotIn("secret-path", reply)


class DegradationTest(unittest.TestCase):
    """No binding installed means no service -- and no failure."""

    def test_no_binding_returns_none_and_logs_one_line(self):
        lines = []
        with mock.patch.object(dbus_service, "BINDINGS", ()):
            self.assertIsNone(dbus_service.available_binding())
            handle = dbus_service.start(FakeDaemon(), logger=lines.append)
        self.assertIsNone(handle)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("D-Bus is unavailable", lines[0])
        self.assertIn("socket", lines[0])

    def test_a_binding_that_cannot_publish_returns_none(self):
        lines = []
        with mock.patch.object(dbus_service, "available_binding", return_value="jeepney"), \
                mock.patch.dict(
                    dbus_service._STARTERS,
                    {"jeepney": mock.Mock(side_effect=RuntimeError("no bus"))},
                ):
            handle = dbus_service.start(FakeDaemon(), logger=lines.append)
        self.assertIsNone(handle)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("socket remains the transport", lines[0])

    def test_the_module_imports_with_every_binding_blocked(self):
        program = textwrap.dedent(
            """
            import sys

            BLOCKED = {"dbus", "jeepney", "dasbus", "gi", "gi.repository"}

            class Blocker:
                def find_spec(self, name, path=None, target=None):
                    if name.split(".")[0] in BLOCKED:
                        raise ImportError("blocked for the test: %s" % name)
                    return None

            sys.meta_path.insert(0, Blocker())

            from sarvam_meter import dbus_service

            binding = dbus_service.available_binding()
            assert binding is None, "a binding was importable: %r" % (binding,)

            lines = []
            handle = dbus_service.start(object(), logger=lines.append)
            assert handle is None, handle
            assert len(lines) == 1, lines
            assert "socket remains the transport" in lines[0], lines
            print("degraded cleanly")
            """
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = REPO_ROOT + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )
        completed = subprocess.run(
            [sys.executable, "-c", program],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("degraded cleanly", completed.stdout)


class ServiceHandleTest(unittest.TestCase):
    def test_stop_is_idempotent_and_never_raises(self):
        calls = []

        def cleanup():
            calls.append(1)
            raise RuntimeError("the bus went away first")

        handle = dbus_service.ServiceHandle("jeepney", cleanup=cleanup)
        self.assertTrue(handle.running)
        handle.stop()
        handle.stop()
        self.assertFalse(handle.running)
        self.assertEqual(calls, [1])

    def test_stop_accepts_none(self):
        dbus_service.stop(None)  # must not raise


class DaemonIntegrationTest(unittest.TestCase):
    """The daemon starts the service, and a broken one cannot stop the socket."""

    def setUp(self):
        from sarvam_meter import config as config_mod

        self.tmp = tempfile.mkdtemp(prefix="sarvam-dbus-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        document = config_mod.default_config_dict()
        document["db_path"] = os.path.join(self.tmp, "usage.db")
        document["dashboard"]["session_file"] = os.path.join(self.tmp, "session.json")
        document["dashboard"]["enabled"] = False
        document["cliproxy"]["enabled"] = False
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        self.config = config_mod.load_config(path)
        self.socket_path = os.path.join(self.tmp, "daemon.sock")

    def _daemon(self, logs):
        from sarvam_meter.daemon import Daemon

        return Daemon(
            self.config,
            logger=logs.append,
            config_path=os.path.join(self.tmp, "config.json"),
            socket_path=self.socket_path,
        )

    def test_the_socket_still_serves_when_the_service_cannot_start(self):
        from sarvam_meter import client

        logs = []
        daemon = self._daemon(logs)
        self.addCleanup(daemon.shutdown)
        with mock.patch(
            "sarvam_meter.dbus_service.start", side_effect=RuntimeError("no bus")
        ):
            daemon.start()
        # The socket is up and answering, which is the only thing that matters.
        result = client.call("health", socket_path=self.socket_path, timeout=5)
        self.assertTrue(result.get("ok"))
        self.assertIsNone(daemon.dbus_service)

    def test_the_daemon_holds_and_releases_the_handle(self):
        daemon = self._daemon([])
        self.addCleanup(daemon.shutdown)
        handle = mock.Mock()
        with mock.patch("sarvam_meter.dbus_service.start", return_value=handle):
            daemon.start()
        self.assertIs(daemon.dbus_service, handle)
        daemon.shutdown()
        self.assertIsNone(daemon.dbus_service)
        self.assertEqual(handle.stop.call_count, 1)

    def test_start_never_raises_when_the_module_is_missing(self):
        daemon = self._daemon([])
        self.addCleanup(daemon.shutdown)
        with mock.patch.dict(sys.modules, {"sarvam_meter.dbus_service": None}):
            daemon.start()
        self.assertIsNone(daemon.dbus_service)


def _binding_available() -> bool:
    return dbus_service.available_binding() is not None


def _runner_available() -> bool:
    return shutil.which("dbus-run-session") is not None


@unittest.skipUnless(
    _binding_available() and _runner_available(),
    "needs a D-Bus binding (%s) and dbus-run-session; neither is required to "
    "use sarvam-meter, so this test is skipped rather than failed"
    % (", ".join(dbus_service.BINDINGS),),
)
class EndToEndTest(unittest.TestCase):
    """A real session bus, a real service, real busctl calls.

    The whole thing runs inside ``dbus-run-session``, because a test process
    cannot join a session bus that does not exist.  The child program starts a
    daemon (socket + D-Bus service), asks it for Status, stores a session with
    SetSession and checks that the reply -- and the session file -- never carry
    the value it sent.
    """

    def test_status_and_set_session_over_a_real_bus(self):
        program = textwrap.dedent(
            """
            import json, os, subprocess, sys, tempfile, time

            from sarvam_meter import config as config_mod
            from sarvam_meter.daemon import Daemon

            tmp = tempfile.mkdtemp(prefix="sarvam-dbus-e2e-")
            document = config_mod.default_config_dict()
            document["db_path"] = os.path.join(tmp, "usage.db")
            document["dashboard"]["session_file"] = os.path.join(tmp, "session.json")
            document["dashboard"]["enabled"] = False
            document["cliproxy"]["enabled"] = False
            path = os.path.join(tmp, "config.json")
            with open(path, "w") as handle:
                json.dump(document, handle)

            daemon = Daemon(
                config_mod.load_config(path),
                logger=lambda line: None,
                config_path=path,
                socket_path=os.path.join(tmp, "daemon.sock"),
            )
            daemon.start()
            if daemon.dbus_service is None:
                print("NO SERVICE")
                sys.exit(3)

            def call(*args):
                return subprocess.run(
                    ["busctl", "--user", "call", "org.kde.sarvam.Meter",
                     "/org/kde/sarvam/Meter", "org.kde.sarvam.Meter"] + list(args),
                    capture_output=True, text=True, timeout=30,
                )

            status = call("Status")
            assert status.returncode == 0, status.stderr
            assert '"ok": true' in status.stdout.replace("'", '"'), status.stdout

            secret = "session=E2E-VALUE-DO-NOT-ECHO"
            stored = call("SetSession", "ss", secret, "")
            assert stored.returncode == 0, stored.stderr
            assert "E2E-VALUE-DO-NOT-ECHO" not in stored.stdout, stored.stdout

            with open(os.path.join(tmp, "session.json")) as handle:
                written = json.load(handle)
            assert written["Cookie"] == secret, written

            mode = oct(os.stat(os.path.join(tmp, "session.json")).st_mode & 0o777)
            assert mode == "0o600", mode

            daemon.shutdown()
            print("OK")
            """
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = REPO_ROOT + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )
        completed = subprocess.run(
            ["dbus-run-session", "--", sys.executable, "-c", program],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("OK", completed.stdout)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
