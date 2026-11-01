# SPDX-License-Identifier: MIT
"""Tests for sarvam_meter.daemon (SPEC section 11).

Every op is exercised over the daemon's real ``AF_UNIX`` socket, plus the
guarantees that matter: no secret in ``config.get`` or in a log line, and a slow
CLIProxyAPI poll that never delays ``status``.

Nothing here opens a TCP socket: the daemon has no TCP listener left, so every
request goes through :func:`sarvam_meter.client.call` (or, for the few tests
that assert on the reply envelope, one raw line down the same socket file).
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
from unittest import mock

from sarvam_meter import client as client_module
from sarvam_meter import config as config_mod
from sarvam_meter.daemon import (
    Daemon,
    _dashboard_dates,
    fetch_dashboard_snapshot,
    make_server,
    sanitized_config,
)
from sarvam_meter.ledger import Ledger

SECRET = "mgmt-key-do-not-leak-1234"


def make_config(directory, **sections):
    document = config_mod.default_config_dict()
    document["db_path"] = os.path.join(directory, "usage.db")
    # The dashboard is on by default; keep its session file (and therefore any
    # poll) inside the throwaway directory instead of ~/.config.
    document["dashboard"]["session_file"] = os.path.join(directory, "session.json")
    for section, values in sections.items():
        document[section].update(values)
    path = os.path.join(directory, "config.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle)
    return config_mod.load_config(path)


class DaemonTestCase(unittest.TestCase):
    """A daemon bound to a throwaway socket inside the test's temp directory."""

    sections = {}
    #: True when the daemon must rewrite the fixture config file (rather than
    #: resolve the user's real one) for ``config.set``.
    writes_config = False

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sarvam-daemon-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = make_config(self.tmp, **self.sections)
        self.logs = []
        #: The daemon creates ``run/`` itself, at mode 0700.
        self.socket_path = os.path.join(self.tmp, "run", "daemon.sock")
        self.daemon = Daemon(
            self.config,
            logger=self.logs.append,
            config_path=os.path.join(self.tmp, "config.json")
            if self.writes_config
            else None,
            socket_path=self.socket_path,
        )
        self.bound = self.daemon.start()
        self.addCleanup(self.daemon.shutdown)

    def wait_for_logs(self, count, timeout=5.0):
        """Block until the daemon has logged `count` lines.

        The daemon writes its log line *after* the response, so a client can
        return before the line exists.  Asserting on the count straight away is a
        race -- it failed once in CI on a loaded runner.
        """
        deadline = time.time() + timeout
        while len(self.logs) < count and time.time() < deadline:
            time.sleep(0.01)
        return len(self.logs)

    # -- request helpers -------------------------------------------------
    def call(self, op, **args):
        """One request over the daemon's socket; returns the result object."""
        return client_module.call(op, socket_path=self.bound, timeout=10.0, **args)

    def send_raw(self, request, timeout=10.0):
        """Send *request* verbatim as one line; return the parsed reply."""
        payload = (
            request if isinstance(request, bytes) else json.dumps(request).encode("utf-8")
        )
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(timeout)
        try:
            connection.connect(self.bound)
            connection.sendall(payload + b"\n")
            buffer = b""
            while b"\n" not in buffer:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                buffer += chunk
        finally:
            connection.close()
        line = buffer.split(b"\n", 1)[0]
        return json.loads(line.decode("utf-8"))


class HealthTest(DaemonTestCase):
    def test_health_payload(self):
        payload = self.call("health")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["version"], "0.1.0")
        self.assertIsInstance(payload["uptime_seconds"], int)
        self.assertGreaterEqual(payload["uptime_seconds"], 0)
        self.assertEqual(set(payload.keys()), {"ok", "version", "uptime_seconds"})


class StatusRouteTest(DaemonTestCase):
    def test_status_payload_shape(self):
        payload = self.call("status")
        self.assertEqual(payload["schema"], 2)
        self.assertEqual(
            set(payload.keys()),
            {"schema", "generated_at", "stale", "errors", "sarvam", "cliproxy"},
        )
        self.assertIn("dashboard", payload["sarvam"])
        # The dashboard source is on by default, so the block is configured even
        # before the first poll; nothing has been fetched yet.
        self.assertTrue(payload["sarvam"]["dashboard"]["configured"])
        self.assertFalse(payload["sarvam"]["dashboard"]["reachable"])
        self.assertFalse(payload["stale"])
        self.assertEqual(payload["errors"], [])

    def test_status_reflects_ledger_spend_without_claiming_a_balance(self):
        ledger = Ledger(self.config.db_path)
        try:
            ledger.set_balance(500.0, "INR")
            ledger.record_event(source="manual", api="chat", cost=12.3, ts="2099-01-01T00:00:00Z")
        finally:
            ledger.close()
        payload = self.call("status")
        balance = payload["sarvam"]["balance"]
        # No dashboard balance has been reported, so there is no "left" figure --
        # but the ledger's spend is still there, and 500 - 12.3 is not invented.
        self.assertFalse(balance["known"])
        self.assertIsNone(balance["remaining"])
        self.assertIsNone(balance["starting"])
        self.assertEqual(balance["spent_total"], 12.3)
        self.assertEqual(balance["source"], "ledger")


class UnknownOpTest(DaemonTestCase):
    def test_an_unknown_op_is_refused_with_a_named_code(self):
        # The socket's analogue of the removed 404: an op the daemon does not
        # serve is refused, and the reply carries the documented envelope.
        reply = self.send_raw({"op": "nope"})
        self.assertEqual(sorted(reply), ["error", "ok"])
        self.assertIs(reply["ok"], False)
        self.assertEqual(set(reply["error"]), {"code", "message"})
        self.assertEqual(reply["error"]["code"], "unknown_op")


class RefreshRouteTest(DaemonTestCase):
    def test_refresh_returns_status(self):
        payload = self.call("refresh")
        self.assertEqual(payload["schema"], 2)
        self.assertFalse(payload["cliproxy"]["configured"])
        self.assertFalse(payload["stale"])


class ConfigRouteTest(DaemonTestCase):
    sections = {"cliproxy": {"enabled": True, "management_key": SECRET}}
    writes_config = True

    def test_config_is_redacted(self):
        payload = self.call("config.get")
        text = json.dumps(payload)
        self.assertNotIn(SECRET, text)
        self.assertEqual(payload["cliproxy"]["management_key"], "***")
        self.assertEqual(payload["daemon"]["socket_path"], self.config.daemon.socket_path)
        self.assertEqual(payload["sarvam"]["currency"], "INR")

    def test_config_set_accepts_an_empty_document(self):
        # `config.set` is the one writable op (the plasmoid's settings page uses
        # it); an empty document is a valid no-op update.
        payload = self.call("config.set", config={})
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["cliproxy"]["management_key"], "***")
        self.assertEqual(
            set(payload),
            {"version", "db_path", "daemon", "sarvam", "cliproxy", "dashboard"},
        )

    def test_sanitized_config_helper_redacts_empty_key(self):
        config = make_config(self.tmp)
        document = sanitized_config(config)
        self.assertEqual(document["cliproxy"]["management_key"], "")
        self.assertEqual(document["version"], 1)


class LoggingTest(DaemonTestCase):
    def test_one_line_per_request_without_secrets(self):
        self.call("status")
        self.call("health")
        self.send_raw({"op": "nope"})
        self.assertGreaterEqual(self.wait_for_logs(3), 3)
        for line in self.logs:
            self.assertNotIn(SECRET, line)
            self.assertNotIn("api-subscription-key", line.lower())
        # One line per request: the op name and its outcome, nothing else.
        self.assertTrue(any(" status -> ok " in line for line in self.logs))
        self.assertTrue(
            any(" nope -> unknown_op " in line for line in self.logs)
        )


class SlowCliproxyTest(unittest.TestCase):
    """A slow CLIProxyAPI poll must never delay ``status``."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sarvam-daemon-slow-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = make_config(
            self.tmp,
            cliproxy={
                "enabled": True,
                "management_key": SECRET,
                "quota_fetch": False,
                "quota_fetch_interval_seconds": 3600,
            },
        )
        self.logs = []
        self.socket_path = os.path.join(self.tmp, "run", "daemon.sock")
        self.daemon = Daemon(
            self.config, logger=self.logs.append, socket_path=self.socket_path
        )
        self.bound = self.daemon.start()
        self.addCleanup(self.daemon.shutdown)

    def test_status_is_not_blocked_by_a_slow_snapshot(self):
        release = threading.Event()

        class SlowClient:
            def snapshot(self):
                release.wait(5.0)
                return {
                    "configured": True,
                    "reachable": True,
                    "api_version": "v8",
                    "accounts": [],
                    "errors": [],
                }

        with mock.patch(
            "sarvam_meter.daemon._make_cliproxy_client", return_value=SlowClient()
        ):
            worker = threading.Thread(target=self.daemon.refresh_cliproxy, daemon=True)
            worker.start()
            time.sleep(0.15)

            started = time.time()
            payload = client_module.call(
                "status", socket_path=self.bound, timeout=10.0
            )
            elapsed = time.time() - started

            release.set()
            worker.join(timeout=5.0)

        self.assertLess(elapsed, 1.0)
        self.assertTrue(payload["stale"])
        # after the refresh completed the snapshot is served
        status = self.daemon.status()
        self.assertFalse(status["stale"])
        self.assertTrue(status["cliproxy"]["reachable"])

    def test_unreachable_cliproxy_becomes_an_error_not_an_exception(self):
        class Boom:
            def snapshot(self):
                raise OSError("connection refused")

        with mock.patch("sarvam_meter.daemon._make_cliproxy_client", return_value=Boom()):
            snapshot = self.daemon.refresh_cliproxy()
        self.assertFalse(snapshot["reachable"])
        self.assertEqual(snapshot["errors"][0]["code"], "unreachable")
        self.assertIn("connection refused", snapshot["errors"][0]["message"])


class BrokenLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sarvam-daemon-broken-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = make_config(self.tmp)

    def test_status_still_serves_with_a_ledger_error(self):
        class Broken:
            def __getattr__(self, name):
                if name.startswith("__"):
                    raise AttributeError(name)

                def _fail(*_args, **_kwargs):
                    raise RuntimeError("unable to open database file")

                return _fail

        daemon = Daemon(
            self.config,
            ledger=Broken(),
            logger=lambda _line: None,
            socket_path=os.path.join(self.tmp, "run", "daemon.sock"),
        )
        path = daemon.start()
        self.addCleanup(daemon.shutdown)
        payload = client_module.call("status", socket_path=path, timeout=10.0)
        self.assertEqual(payload["errors"][0]["source"], "ledger")
        self.assertEqual(payload["errors"][0]["code"], "read_failed")
        self.assertEqual(
            payload["sarvam"]["spend"],
            {"today": 0.0, "month": 0.0, "source": "ledger"},
        )


class ServerFlagsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sarvam-daemon-flags-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = make_config(self.tmp)

    def test_make_server_returns_a_bound_socket_server(self):
        path = os.path.join(self.tmp, "run", "daemon.sock")
        server, daemon = make_server(self.config, socket_path=path)
        self.addCleanup(daemon.shutdown)
        try:
            # Bound, but not serving yet: the socket file exists, it is a socket
            # and it is private to the user.
            self.assertEqual(server.server_address, path)
            self.assertEqual(daemon.socket_path, path)
            info = os.lstat(path)
            self.assertTrue(stat.S_ISSOCK(info.st_mode))
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
            self.assertEqual(
                stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700
            )
        finally:
            server.server_close()

    def test_two_instances_share_nothing(self):
        first = Daemon(
            self.config,
            logger=lambda _line: None,
            socket_path=os.path.join(self.tmp, "a", "daemon.sock"),
        )
        second = Daemon(
            self.config,
            logger=lambda _line: None,
            socket_path=os.path.join(self.tmp, "b", "daemon.sock"),
        )
        path_a = first.start()
        path_b = second.start()
        self.addCleanup(first.shutdown)
        self.addCleanup(second.shutdown)
        self.assertNotEqual(path_a, path_b)
        for path in (path_a, path_b):
            result = client_module.call("health", socket_path=path, timeout=10.0)
            self.assertTrue(result["ok"])


# ---------------------------------------------------------------------------
# the dashboard snapshot: TTL cache, refresh, and a non-blocking status
# ---------------------------------------------------------------------------


def dashboard_snapshot(**overrides):
    snapshot = {
        "configured": True,
        "reachable": True,
        "as_of": "2026-11-15T04:30:00Z",
        "org_id": "019f37be-0000-7000-8000-000000000000",
        "currency": "INR",
        "window": {"start": "2026-10-20", "end": "2026-11-15"},
        "spend": {"window": 134.23, "today": 12.3},
        "by_model": [{"model": "saaras:v4", "cost": 74.05}],
        "usage": [],
        "error": None,
    }
    snapshot.update(overrides)
    return snapshot


class DashboardCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sarvam-daemon-dashboard-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = make_config(
            self.tmp,
            dashboard={
                "enabled": True,
                "poll_seconds": 300,
                "session_file": os.path.join(self.tmp, "session.json"),
            },
        )
        self.socket_path = os.path.join(self.tmp, "run", "daemon.sock")
        self.daemon = Daemon(
            self.config, logger=lambda _line: None, socket_path=self.socket_path
        )
        self.addCleanup(self.daemon.shutdown)

    def test_ttl_follows_poll_seconds(self):
        self.assertEqual(self.daemon.dashboard_ttl, 300)
        other = make_config(
            self.tmp, dashboard={"enabled": True, "poll_seconds": 900}
        )
        self.assertEqual(Daemon(other, logger=lambda _line: None).dashboard_ttl, 900)
        # The floor of the accepted range is honoured too (120 s minimum).
        floor = make_config(
            self.tmp, dashboard={"enabled": True, "poll_seconds": 120}
        )
        self.assertEqual(Daemon(floor, logger=lambda _line: None).dashboard_ttl, 120)

    def test_nothing_is_cached_before_the_first_refresh(self):
        self.assertIsNone(self.daemon.dashboard_snapshot)
        self.assertFalse(self.daemon.dashboard_snapshot_is_fresh())

    def test_refresh_dashboard_caches_and_stamps_the_snapshot(self):
        snapshot = dashboard_snapshot()
        with mock.patch(
            "sarvam_meter.daemon.fetch_dashboard_snapshot", return_value=snapshot
        ) as fetch:
            returned = self.daemon.refresh_dashboard()
        self.assertEqual(fetch.call_count, 1)
        self.assertIs(returned, snapshot)
        self.assertIs(self.daemon.dashboard_snapshot, snapshot)
        self.assertTrue(self.daemon.dashboard_snapshot_is_fresh())

    def test_status_never_fetches_and_uses_the_cached_snapshot(self):
        with mock.patch(
            "sarvam_meter.daemon.fetch_dashboard_snapshot",
            return_value=dashboard_snapshot(),
        ) as fetch:
            # No refresh yet: status must not block on a fetch.
            payload = self.daemon.status()
            self.assertEqual(fetch.call_count, 0)
            self.assertTrue(payload["sarvam"]["dashboard"]["configured"])
            self.assertFalse(payload["sarvam"]["dashboard"]["reachable"])
            self.assertEqual(payload["sarvam"]["spend"]["source"], "ledger")

            self.daemon.refresh_dashboard()
            payload = self.daemon.status()
        self.assertEqual(fetch.call_count, 1)
        self.assertTrue(payload["sarvam"]["dashboard"]["reachable"])
        self.assertEqual(payload["sarvam"]["spend"]["source"], "dashboard")
        self.assertEqual(payload["sarvam"]["dashboard"]["by_model"][0]["cost"], 74.05)

    def test_disabled_dashboard_starts_no_thread(self):
        config = make_config(self.tmp, dashboard={"enabled": False})
        daemon = Daemon(config, logger=lambda _line: None)
        self.addCleanup(daemon.shutdown)
        daemon.start()
        self.assertIsNone(daemon._dashboard_thread)
        self.assertFalse(daemon.status()["sarvam"]["dashboard"]["configured"])

    def test_refresh_op_refreshes_both_sources(self):
        path = self.daemon.start()
        with mock.patch(
            "sarvam_meter.daemon.fetch_dashboard_snapshot",
            return_value=dashboard_snapshot(),
        ) as dash_fetch, mock.patch(
            "sarvam_meter.daemon.fetch_cliproxy_snapshot",
            return_value={"configured": False, "reachable": False, "errors": []},
        ) as proxy_fetch:
            payload = client_module.call("refresh", socket_path=path, timeout=10.0)
        self.assertGreaterEqual(proxy_fetch.call_count, 1)
        self.assertGreaterEqual(dash_fetch.call_count, 1)
        self.assertTrue(payload["sarvam"]["dashboard"]["reachable"])

    def test_config_route_exposes_the_dashboard_section_without_secrets(self):
        document = sanitized_config(self.config)
        self.assertEqual(
            set(document["dashboard"]),
            {"enabled", "base_url", "session_file", "poll_seconds", "product"},
        )
        self.assertTrue(document["dashboard"]["enabled"])
        self.assertEqual(document["dashboard"]["poll_seconds"], 300)


class DashboardFetchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sarvam-daemon-dashfetch-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_disabled_source_returns_a_configured_false_snapshot(self):
        config = make_config(self.tmp, dashboard={"enabled": False})
        snapshot = fetch_dashboard_snapshot(config, None)
        self.assertFalse(snapshot["configured"])
        self.assertFalse(snapshot["reachable"])
        self.assertIsNone(snapshot["error"])

    def test_a_missing_session_becomes_an_error_not_an_exception(self):
        config = make_config(
            self.tmp,
            dashboard={
                "enabled": True,
                "session_file": os.path.join(self.tmp, "absent.json"),
            },
        )
        snapshot = fetch_dashboard_snapshot(config, None)
        self.assertIsInstance(snapshot, dict)
        self.assertTrue(snapshot["configured"])
        self.assertFalse(snapshot["reachable"])
        self.assertIsInstance(snapshot["error"], dict)
        self.assertEqual(snapshot["error"]["source"], "dashboard")
        self.assertTrue(snapshot["error"]["code"])

    def test_the_window_starts_at_the_balance_set_date(self):
        class FixedLedger:
            def get_balance(self):
                return {"starting": 500.0, "set_at": "2026-10-20T09:00:00Z"}

        since, today = _dashboard_dates(FixedLedger())
        self.assertEqual(since, "2026-10-20")
        self.assertRegex(today, r"^\d{4}-\d{2}-\d{2}$")
        self.assertLessEqual(since, today)

    def test_a_broken_ledger_still_yields_dates(self):
        class Boom:
            def get_balance(self):
                raise RuntimeError("database is locked")

        since, today = _dashboard_dates(Boom())
        self.assertRegex(since, r"^\d{4}-\d{2}-\d{2}$")
        self.assertTrue(since.endswith("-01"), since)
        self.assertRegex(today, r"^\d{4}-\d{2}-\d{2}$")

    def test_no_ledger_still_yields_dates(self):
        since, today = _dashboard_dates(None)
        self.assertRegex(since, r"^\d{4}-\d{2}-\d{2}$")
        self.assertRegex(today, r"^\d{4}-\d{2}-\d{2}$")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
