# SPDX-License-Identifier: MIT
"""Regression tests for the defects found in the adversarial review.

Each test names the review id it pins down, so a future regression is obvious.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from sarvam_meter import rates  # noqa: E402
from sarvam_meter.cliproxy import CliproxyClient, CliproxyError  # noqa: E402
from sarvam_meter.config import (  # noqa: E402
    ConfigError,
    default_config_dict,
    is_loopback_host,
    load_config,
)
from sarvam_meter.ledger import Ledger  # noqa: E402
from sarvam_meter.status import build_status  # noqa: E402


def _write_config(directory, **overrides):
    document = default_config_dict()
    document["db_path"] = os.path.join(directory, "usage.db")
    # The dashboard is on by default; keep its session file inside the throwaway
    # directory so a daemon started from this config never polls the real one.
    document["dashboard"]["session_file"] = os.path.join(directory, "session.json")
    for section, values in overrides.items():
        document.setdefault(section, {}).update(values)
    path = os.path.join(directory, "config.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle)
    return path


class ProxyEnvironmentTest(unittest.TestCase):
    """CR-1: $http_proxy must never receive the management key."""

    def test_default_transport_ignores_environment_proxy(self):
        seen = []

        class _Sink(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                seen.append((self.path, dict(self.headers)))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                return

        sink = ThreadingHTTPServer(("127.0.0.1", 0), _Sink)
        thread = threading.Thread(target=sink.serve_forever, daemon=True)
        thread.start()
        proxy_url = "http://127.0.0.1:%d" % sink.server_address[1]
        saved = {name: os.environ.get(name) for name in ("http_proxy", "HTTP_PROXY")}
        try:
            os.environ["http_proxy"] = proxy_url
            os.environ["HTTP_PROXY"] = proxy_url
            client = CliproxyClient("http://127.0.0.1:9", "MgmtKeyXYZ", timeout=2.0)
            snapshot = client.snapshot()
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            sink.shutdown()
            sink.server_close()

        self.assertEqual(seen, [], "the environment proxy was contacted")
        self.assertFalse(snapshot["reachable"])
        self.assertNotIn("MgmtKeyXYZ", json.dumps(snapshot))


class InMemoryLedgerTest(unittest.TestCase):
    """MJ-2: an in-memory ledger must not zero the whole payload."""

    def test_status_with_in_memory_ledger(self):
        ledger = Ledger(":memory:")
        try:
            ledger.set_balance(500.0)
            ledger.record_event(source="manual", api="chat", quantity=10.0,
                                unit="tokens", cost=12.5)
            status = build_status(load_config(_write_config(tempfile.mkdtemp())), ledger)
        finally:
            ledger.close()
        self.assertEqual(status["errors"], [])
        self.assertEqual(status["sarvam"]["spend"]["today"], 12.5)
        # The spend survives; a balance is not derived from it any more.
        self.assertEqual(status["sarvam"]["balance"]["spent_total"], 12.5)
        self.assertFalse(status["sarvam"]["balance"]["known"])
        self.assertIsNone(status["sarvam"]["balance"]["remaining"])
        self.assertIsNone(status["sarvam"]["balance"]["starting"])


class DashboardSnapshotRegressionTest(unittest.TestCase):
    """Schema 2: a bad dashboard snapshot must never break the widget contract."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sarvam-regress-dash-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = load_config(_write_config(self.tmp))
        self.ledger = Ledger(self.config.db_path)
        self.addCleanup(self.ledger.close)

    def test_key_sets_survive_any_snapshot_shape(self):
        expected_sarvam = {
            "configured",
            "currency",
            "balance",
            "spend",
            "calls",
            "by_api",
            "dashboard",
        }
        for dash in (None, "nope", [], 0, {"reachable": True}, {"error": "boom"}):
            payload = build_status(
                self.config,
                self.ledger,
                None,
                None,
                dashboard_snapshot=dash,
            )
            self.assertEqual(payload["schema"], 2)
            self.assertEqual(set(payload["sarvam"]), expected_sarvam)
            self.assertIn("dashboard", payload["sarvam"])
            self.assertEqual(
                set(payload["sarvam"]["balance"]),
                {"known", "remaining", "starting", "spent_total", "set_at", "source"},
            )

    def test_a_broken_ledger_and_a_dashboard_snapshot_still_zero_the_money(self):
        class Broken:
            def __getattr__(self, name):
                if name.startswith("__"):
                    raise AttributeError(name)

                def _fail(*_args, **_kwargs):
                    raise RuntimeError("database is locked")

                return _fail

        payload = build_status(
            self.config,
            Broken(),
            None,
            None,
            dashboard_snapshot={
                "configured": True,
                "reachable": True,
                "spend": {"window": 10.0, "today": 1.0},
                "by_model": [],
                "error": None,
            },
        )
        self.assertEqual(payload["errors"][0]["source"], "ledger")
        # With no readable ledger there is no starting balance, so the money is
        # the zeroed fallback and the source stays "ledger" (SPEC section 10's
        # read-failed contract is unchanged by schema 2).
        self.assertEqual(payload["sarvam"]["balance"]["source"], "ledger")
        self.assertEqual(payload["sarvam"]["balance"]["spent_total"], 0.0)
        self.assertFalse(payload["sarvam"]["balance"]["known"])


class CostOverflowTest(unittest.TestCase):
    """MJ-3: an infinite price must be refused, not stored."""

    def test_cost_for_refuses_overflow(self):
        with self.assertRaises(ValueError):
            rates.cost_for("chat", "sarvam-105b", 1e308, "tokens")

    def test_ledger_refuses_non_finite_cost(self):
        ledger = Ledger(":memory:")
        try:
            with self.assertRaises(ValueError):
                ledger.record_event(source="manual", api="chat", cost=float("inf"))
        finally:
            ledger.close()

    def test_cli_record_exits_two_on_overflow(self):
        directory = tempfile.mkdtemp()
        config = _write_config(directory)
        result = subprocess.run(
            [sys.executable, "-m", "sarvam_meter.cli", "record", "--config", config,
             "--api", "chat", "--quantity", "1e308", "--unit", "tokens"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertNotIn("inf", result.stdout.lower())


class LoopbackGuardTest(unittest.TestCase):
    """MN-3 / MN-4: host names resolve, credentials are refused."""

    def test_single_label_local_name_resolves(self):
        self.assertTrue(is_loopback_host("localhost"))
        self.assertTrue(is_loopback_host("127.0.0.1"))
        self.assertTrue(is_loopback_host("::1"))
        self.assertFalse(is_loopback_host("example.com"))
        self.assertFalse(is_loopback_host("10.3.40.219"))

    def test_credentials_in_base_url_are_rejected(self):
        with self.assertRaises(CliproxyError):
            CliproxyClient("http://user:secret@127.0.0.1:8317", "key")

    def test_credentials_in_config_are_rejected(self):
        directory = tempfile.mkdtemp()
        with self.assertRaises(ConfigError):
            load_config(_write_config(directory,
                                      cliproxy={"base_url": "http://a:b@127.0.0.1:8317"}))


class ShutdownCleanlinessTest(unittest.TestCase):
    """MJ-6 / NT-6: a clean stop must checkpoint the WAL."""

    def test_close_all_removes_wal_files(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "usage.db")
        ledger = Ledger(path)
        for _ in range(50):
            ledger.record_event(source="manual", api="chat", quantity=1.0,
                                unit="tokens", cost=0.0001)
        ledger.close_all()
        leftover = [name for name in os.listdir(directory) if name.endswith(("-wal", "-shm"))]
        self.assertEqual(leftover, [], leftover)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
