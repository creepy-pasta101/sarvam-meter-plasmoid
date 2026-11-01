# SPDX-License-Identifier: MIT
"""End-to-end tests for the dashboard source, over a real socket.

The unit tests inject snapshot dicts; these drive the actual wire path — session
file, HTTP request, response parsing, daemon cache, status payload — against a
local server that replays the captured fixtures. No internet access.

The *upstream* dashboard is still HTTP (that is what the source speaks); the
daemon itself is reached over its Unix socket, through
:func:`sarvam_meter.client.call`.
"""

from __future__ import annotations

import datetime
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from sarvam_meter import client as client_module  # noqa: E402
from sarvam_meter import config as config_mod  # noqa: E402
from sarvam_meter.daemon import Daemon  # noqa: E402
from sarvam_meter.ledger import Ledger  # noqa: E402

FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures")
SUMMARY = os.path.join(FIXTURES, "dhan_usage_summary_by_model.json")
BALANCE = os.path.join(FIXTURES, "dhan_org_balance.json")

COOKIE = "session=SuperSecretCookieValue; __cf_bm=abc"


def _summary_payload():
    with open(SUMMARY, encoding="utf-8") as handle:
        return json.load(handle)


def _balance_payload():
    with open(BALANCE, encoding="utf-8") as handle:
        return json.load(handle)


def _today_payload():
    """A small payload for the start==end (today) call, distinct from the window."""
    doc = _summary_payload()
    doc["subtotal"] = "12.5"
    doc["total_cost"] = "12.5"
    doc["total_org_cost"] = "12.5"
    doc["groups"] = [{"model": "sarvam-105b", "subtotal": "12.5",
                      "total_discount": "0", "total_cost": "12.5",
                      "currency": "inr", "usage": []}]
    return doc


class _FakeDashboard(BaseHTTPRequestHandler):
    """Replays the captured fixtures; records what it was asked for."""

    requests = []
    unauthorised = False
    payload_for_today = None

    def do_GET(self):  # noqa: N802
        _FakeDashboard.requests.append(
            {"path": self.path, "cookie": self.headers.get("Cookie"),
             "authorization": self.headers.get("Authorization")}
        )
        from urllib.parse import parse_qs, urlsplit
        path = urlsplit(self.path).path
        if _FakeDashboard.unauthorised:
            body = json.dumps({"error": "unauthorized"}).encode()
            self.send_response(401)
        elif path.endswith("/balance"):
            body = json.dumps(_balance_payload()).encode()
            self.send_response(200)
        else:
            query = parse_qs(urlsplit(self.path).query)
            start = (query.get("start") or [""])[0]
            end = (query.get("end") or [""])[0]
            doc = _today_payload() if start == end else _summary_payload()
            body = json.dumps(doc).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


class DashboardEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _FakeDashboard.requests = []
        _FakeDashboard.unauthorised = False
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeDashboard)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

        self.session_path = os.path.join(self.tmp, "session.json")
        with open(self.session_path, "w", encoding="utf-8") as handle:
            json.dump({"Cookie": COOKIE}, handle)
        os.chmod(self.session_path, 0o600)

        self.db_path = os.path.join(self.tmp, "usage.db")
        document = config_mod.default_config_dict()
        document["db_path"] = self.db_path
        document["dashboard"] = {
            "enabled": True,
            "base_url": self.base,
            "session_file": self.session_path,
            "poll_seconds": 300,
            "product": "sarvam-api-platform",
        }
        self.config_path = os.path.join(self.tmp, "config.json")
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        self.config = config_mod.load_config(self.config_path)

        self.ledger = Ledger(self.db_path)
        self.ledger.set_balance(500.0)
        # Backdate the balance so the dashboard window spans a month instead of
        # collapsing to today (the window starts at the date the balance was set).
        backdated = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.ledger._execute("UPDATE balance SET set_at = ? WHERE id = 1", (backdated,))
        self.ledger.record_event(source="manual", api="chat", quantity=1000.0,
                                 unit="tokens", cost=1.0)
        self.socket_path = os.path.join(self.tmp, "run", "daemon.sock")
        self.daemon = Daemon(
            self.config, ledger=self.ledger, socket_path=self.socket_path
        )
        self.bound = self.daemon.start()

    def tearDown(self):
        try:
            self.daemon.shutdown()
        finally:
            self.ledger.close_all()
            self.server.shutdown()
            self.server.server_close()

    def call(self, op, **args):
        return client_module.call(op, socket_path=self.bound, timeout=20.0, **args)

    def _refresh_and_status(self):
        self.call("refresh")
        return self.call("status")

    def test_status_carries_the_dashboard_numbers(self):
        status = self._refresh_and_status()
        sarvam = status["sarvam"]
        self.assertEqual(status["schema"], 2)
        self.assertTrue(sarvam["dashboard"]["configured"])
        self.assertTrue(sarvam["dashboard"]["reachable"],
                        sarvam["dashboard"]["error"])
        self.assertEqual(sarvam["dashboard"]["currency"], "INR")
        # window spend is the captured month total, rounded for display
        self.assertAlmostEqual(sarvam["dashboard"]["spend"]["window"], 134.23, places=2)
        self.assertAlmostEqual(sarvam["dashboard"]["spend"]["today"], 12.5, places=2)
        # today comes from the dashboard, not from the ledger's 1.0
        self.assertAlmostEqual(sarvam["spend"]["today"], 12.5, places=2)
        self.assertEqual(sarvam["spend"]["source"], "dashboard")
        # the reported balance wins: no arithmetic, no starting balance needed
        self.assertTrue(sarvam["dashboard"]["balance"]["known"])
        self.assertAlmostEqual(
            sarvam["dashboard"]["balance"]["amount"], 80.03533, places=5
        )
        self.assertEqual(
            sarvam["dashboard"]["balance"]["as_of"], "2026-11-01T05:55:39.713767Z"
        )
        self.assertEqual(sarvam["balance"]["source"], "dashboard")
        self.assertTrue(sarvam["balance"]["known"])
        self.assertAlmostEqual(sarvam["balance"]["remaining"], 80.04, places=2)
        self.assertAlmostEqual(sarvam["balance"]["spent_total"], 134.23, places=2)
        # per-model rows, largest first is not required, but all four are there
        models = {row["model"]: row["cost"] for row in sarvam["dashboard"]["by_model"]}
        self.assertAlmostEqual(models["saaras:v4"], 74.05, places=2)
        self.assertAlmostEqual(models["glm5.3-flash"], 53.39, places=2)

    def test_the_session_cookie_is_sent_and_never_echoed(self):
        status = self._refresh_and_status()
        self.assertTrue(_FakeDashboard.requests)
        self.assertEqual(_FakeDashboard.requests[0]["cookie"], COOKIE)
        blob = json.dumps(status)
        self.assertNotIn("SuperSecretCookieValue", blob)
        config_blob = json.dumps(self.call("config.get"))
        self.assertNotIn("SuperSecretCookieValue", config_blob)

    def test_a_401_falls_back_to_the_ledger_spend_without_a_balance(self):
        _FakeDashboard.unauthorised = True
        status = self._refresh_and_status()
        sarvam = status["sarvam"]
        self.assertFalse(sarvam["dashboard"]["reachable"])
        self.assertEqual(sarvam["dashboard"]["error"]["code"], "unauthorized")
        self.assertEqual(sarvam["spend"]["source"], "ledger")
        self.assertEqual(sarvam["balance"]["source"], "ledger")
        # the ledger's own spend is still shown
        self.assertAlmostEqual(sarvam["spend"]["today"], 1.0, places=2)
        self.assertAlmostEqual(sarvam["balance"]["spent_total"], 1.0, places=2)
        # ... but no balance was reported, so none is claimed: 500 - 1 is not
        # invented, and the starting balance is not presented as a "left" figure.
        self.assertFalse(sarvam["dashboard"]["balance"]["known"])
        self.assertFalse(sarvam["balance"]["known"])
        self.assertIsNone(sarvam["balance"]["remaining"])
        self.assertIsNone(sarvam["balance"]["starting"])


class SessionCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config_path = os.path.join(self.tmp, "config.json")
        subprocess.run([sys.executable, "-m", "sarvam_meter.cli", "init",
                        "--config", self.config_path],
                       cwd=REPO_ROOT, check=True, capture_output=True)
        with open(self.config_path, encoding="utf-8") as handle:
            document = json.load(handle)
        self.session_path = os.path.join(self.tmp, "session.json")
        document["dashboard"]["session_file"] = self.session_path
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "sarvam_meter.cli", *args,
             "--config", self.config_path],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
        )

    def test_session_set_writes_a_private_file_and_show_never_prints_it(self):
        result = self._run("session", "set", "--cookie", COOKIE)
        self.assertEqual(result.returncode, 0, result.stderr)
        mode = stat.S_IMODE(os.stat(self.session_path).st_mode)
        self.assertEqual(mode, 0o600, oct(mode))
        with open(self.session_path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["Cookie"], COOKIE)

        shown = self._run("session", "show")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertNotIn("SuperSecretCookieValue", shown.stdout + shown.stderr)
        self.assertIn("Cookie", shown.stdout)

    def test_session_set_refuses_a_multiline_value(self):
        result = self._run("session", "set", "--cookie", "a=b\nInjected: 1")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse(os.path.exists(self.session_path))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
