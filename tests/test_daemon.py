# SPDX-License-Identifier: MIT
"""Tests for sarvam_meter.daemon (SPEC section 11).

Every route is exercised over a real loopback socket bound to port 0, plus the
guarantees that matter: no mutating method, no secret in ``/v1/config`` or in a
log line, and a slow CLIProxyAPI poll that never delays ``/v1/status``.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

import dataclasses

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
    for section, values in sections.items():
        document[section].update(values)
    path = os.path.join(directory, "config.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle)
    return config_mod.load_config(path)


def ephemeral_port(config):
    """Copy of *config* whose daemon binds an ephemeral port (tests only).

    The config schema only allows ports 1..65535, so port 0 is applied to the
    loaded dataclass instead of being written into the JSON document.
    """
    return dataclasses.replace(
        config, daemon=dataclasses.replace(config.daemon, port=0)
    )


class DaemonTestCase(unittest.TestCase):
    sections = {}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sarvam-daemon-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = ephemeral_port(make_config(self.tmp, **self.sections))
        self.logs = []
        self.daemon = Daemon(self.config, logger=self.logs.append)
        self.port = self.daemon.start()
        self.addCleanup(self.daemon.shutdown)

    def request(self, method, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            body = response.read()
            headers = {key.lower(): value for key, value in response.getheaders()}
            return response.status, headers, body
        finally:
            connection.close()

    def json_body(self, method, path):
        status, headers, body = self.request(method, path)
        return status, headers, json.loads(body.decode("utf-8"))


class HealthTest(DaemonTestCase):
    def test_health_payload(self):
        status, headers, payload = self.json_body("GET", "/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["version"], "0.1.0")
        self.assertIsInstance(payload["uptime_seconds"], int)
        self.assertGreaterEqual(payload["uptime_seconds"], 0)
        self.assertEqual(set(payload.keys()), {"ok", "version", "uptime_seconds"})


class StatusRouteTest(DaemonTestCase):
    def test_status_headers_and_shape(self):
        status, headers, payload = self.json_body("GET", "/v1/status")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(payload["schema"], 2)
        self.assertEqual(
            set(payload.keys()),
            {"schema", "generated_at", "stale", "errors", "sarvam", "cliproxy"},
        )
        self.assertIn("dashboard", payload["sarvam"])
        self.assertFalse(payload["sarvam"]["dashboard"]["configured"])
        self.assertFalse(payload["stale"])
        self.assertEqual(payload["errors"], [])

    def test_status_reflects_ledger(self):
        ledger = Ledger(self.config.db_path)
        try:
            ledger.set_balance(500.0, "INR")
            ledger.record_event(source="proxy", api="chat", cost=12.3, ts="2099-01-01T00:00:00Z")
        finally:
            ledger.close()
        status, _headers, payload = self.json_body("GET", "/v1/status")
        self.assertEqual(status, 200)
        self.assertTrue(payload["sarvam"]["balance"]["known"])

    def test_status_accepts_trailing_slash(self):
        status, _headers, payload = self.json_body("GET", "/v1/status/")
        self.assertEqual(status, 200)
        self.assertEqual(payload["schema"], 2)


class NotFoundTest(DaemonTestCase):
    def test_unknown_path_is_404_with_error_body(self):
        for path in ("/", "/v1/nope", "/v1/status/extra"):
            status, headers, payload = self.json_body("GET", path)
            self.assertEqual(status, 404, path)
            self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
            self.assertEqual(payload, {"error": {"message": "not found", "code": "not_found"}})

    def test_unknown_path_with_put_is_still_404(self):
        status, _headers, payload = self.json_body("PUT", "/nope")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")


class MethodNotAllowedTest(DaemonTestCase):
    def assert_405(self, method, path):
        status, headers, payload = self.json_body(method, path)
        self.assertEqual(status, 405, "%s %s" % (method, path))
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        self.assertEqual(
            payload,
            {"error": {"message": "method not allowed", "code": "method_not_allowed"}},
        )

    def test_put_and_delete_are_rejected(self):
        for method in ("PUT", "DELETE", "PATCH"):
            self.assert_405(method, "/v1/status")

    def test_wrong_verb_on_other_routes(self):
        self.assert_405("POST", "/v1/health")
        self.assert_405("GET", "/v1/refresh")
        self.assert_405("DELETE", "/v1/config")

    def test_nothing_can_mutate_state(self):
        ledger = Ledger(self.config.db_path)
        try:
            before = ledger.calls_since("0001-01-01T00:00:00Z")
        finally:
            ledger.close()
        for method, path in (
            ("PUT", "/v1/status"),
            ("DELETE", "/v1/status"),
            ("POST", "/v1/status"),
            ("PUT", "/v1/config"),
            ("DELETE", "/v1/refresh"),
        ):
            status, _headers, _body = self.request(method, path)
            self.assertEqual(status, 405)
        ledger = Ledger(self.config.db_path)
        try:
            after = ledger.calls_since("0001-01-01T00:00:00Z")
        finally:
            ledger.close()
        self.assertEqual(before, after)


class RefreshRouteTest(DaemonTestCase):
    def test_refresh_returns_status(self):
        status, headers, payload = self.json_body("POST", "/v1/refresh")
        self.assertEqual(status, 200)
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(payload["schema"], 2)
        self.assertFalse(payload["cliproxy"]["configured"])
        self.assertFalse(payload["stale"])


class ConfigRouteTest(DaemonTestCase):
    sections = {"cliproxy": {"enabled": True, "management_key": SECRET}}

    def test_config_is_redacted(self):
        status, headers, body = self.request("GET", "/v1/config")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertNotIn(SECRET, text)
        payload = json.loads(text)
        self.assertEqual(payload["cliproxy"]["management_key"], "***")
        self.assertEqual(payload["daemon"]["port"], self.config.daemon.port)
        self.assertEqual(payload["sarvam"]["currency"], "INR")
        self.assertEqual(headers["cache-control"], "no-store")

    def test_config_is_read_only(self):
        status, _headers, _body = self.request("POST", "/v1/config")
        self.assertEqual(status, 405)

    def test_sanitized_config_helper_redacts_empty_key(self):
        config = make_config(self.tmp)
        document = sanitized_config(config)
        self.assertEqual(document["cliproxy"]["management_key"], "")
        self.assertEqual(document["version"], 1)


class LoggingTest(DaemonTestCase):
    def test_one_line_per_request_without_secrets(self):
        self.request("GET", "/v1/status")
        self.request("GET", "/v1/health")
        self.request("PUT", "/v1/status")
        self.assertGreaterEqual(len(self.logs), 3)
        for line in self.logs:
            self.assertIn("/v1/", line)
            self.assertNotIn(SECRET, line)
            self.assertNotIn("api-subscription-key", line.lower())
        self.assertTrue(any("GET /v1/status -> 200" in line for line in self.logs))
        self.assertTrue(any("PUT /v1/status -> 405" in line for line in self.logs))

    def test_query_string_is_not_logged(self):
        self.request("GET", "/v1/status?key=" + SECRET)
        self.assertFalse(any(SECRET in line for line in self.logs))


class SlowCliproxyTest(unittest.TestCase):
    """A slow CLIProxyAPI poll must never delay ``/v1/status``."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sarvam-daemon-slow-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = ephemeral_port(make_config(
            self.tmp,
            cliproxy={
                "enabled": True,
                "management_key": SECRET,
                "quota_fetch": False,
                "quota_fetch_interval_seconds": 3600,
            },
        ))
        self.logs = []
        self.daemon = Daemon(self.config, logger=self.logs.append)
        self.port = self.daemon.start()
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
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            try:
                connection.request("GET", "/v1/status")
                response = connection.getresponse()
                body = response.read()
            finally:
                connection.close()
            elapsed = time.time() - started

            release.set()
            worker.join(timeout=5.0)

        self.assertEqual(response.status, 200)
        self.assertLess(elapsed, 1.0)
        payload = json.loads(body.decode("utf-8"))
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
        self.config = ephemeral_port(make_config(self.tmp))

    def test_status_still_serves_with_a_ledger_error(self):
        class Broken:
            def __getattr__(self, name):
                if name.startswith("__"):
                    raise AttributeError(name)

                def _fail(*_args, **_kwargs):
                    raise RuntimeError("unable to open database file")

                return _fail

        daemon = Daemon(self.config, ledger=Broken(), logger=lambda _line: None)
        port = daemon.start()
        self.addCleanup(daemon.shutdown)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            connection.request("GET", "/v1/status")
            response = connection.getresponse()
            body = response.read()
        finally:
            connection.close()
        self.assertEqual(response.status, 200)
        payload = json.loads(body.decode("utf-8"))
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
        self.config = ephemeral_port(make_config(self.tmp))

    def test_make_server_returns_bound_threaded_server(self):
        server, daemon = make_server(self.config)
        self.addCleanup(daemon.shutdown)
        try:
            self.assertTrue(server.daemon_threads)
            self.assertTrue(server.allow_reuse_address)
            self.assertGreater(daemon.port, 0)
            self.assertEqual(daemon.port, server.server_address[1])
        finally:
            server.server_close()

    def test_two_instances_share_nothing(self):
        first = Daemon(self.config, logger=lambda _line: None)
        second = Daemon(self.config, logger=lambda _line: None)
        port_a = first.start()
        port_b = second.start()
        self.addCleanup(first.shutdown)
        self.addCleanup(second.shutdown)
        self.assertNotEqual(port_a, port_b)
        for port in (port_a, port_b):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                connection.request("GET", "/v1/health")
                response = connection.getresponse()
                response.read()
            finally:
                connection.close()
            self.assertEqual(response.status, 200)


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
        self.config = ephemeral_port(
            make_config(
                self.tmp,
                dashboard={
                    "enabled": True,
                    "poll_seconds": 60,
                    "session_file": os.path.join(self.tmp, "session.json"),
                },
            )
        )
        self.daemon = Daemon(self.config, logger=lambda _line: None)
        self.addCleanup(self.daemon.shutdown)

    def test_ttl_follows_poll_seconds(self):
        self.assertEqual(self.daemon.dashboard_ttl, 60)
        other = make_config(
            self.tmp, dashboard={"enabled": True, "poll_seconds": 900}
        )
        self.assertEqual(Daemon(other, logger=lambda _line: None).dashboard_ttl, 900)

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
        config = ephemeral_port(make_config(self.tmp))
        daemon = Daemon(config, logger=lambda _line: None)
        self.addCleanup(daemon.shutdown)
        daemon.start()
        self.assertIsNone(daemon._dashboard_thread)
        self.assertFalse(daemon.status()["sarvam"]["dashboard"]["configured"])

    def test_refresh_route_refreshes_both_sources(self):
        port = self.daemon.start()
        with mock.patch(
            "sarvam_meter.daemon.fetch_dashboard_snapshot",
            return_value=dashboard_snapshot(),
        ) as dash_fetch, mock.patch(
            "sarvam_meter.daemon.fetch_cliproxy_snapshot",
            return_value={"configured": False, "reachable": False, "errors": []},
        ) as proxy_fetch:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                connection.request("POST", "/v1/refresh")
                response = connection.getresponse()
                payload = json.loads(response.read().decode("utf-8"))
            finally:
                connection.close()
        self.assertEqual(response.status, 200)
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
        self.assertEqual(document["dashboard"]["poll_seconds"], 60)


class DashboardFetchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sarvam-daemon-dashfetch-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_disabled_source_returns_a_configured_false_snapshot(self):
        config = make_config(self.tmp)
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
