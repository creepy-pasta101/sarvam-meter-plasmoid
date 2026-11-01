# SPDX-License-Identifier: MIT
"""End-to-end integration tests for the metering chain (SPEC sections 8-11).

One process, real sockets, no mocks of the modules under test:

    CLI (or the plasmoid) --unix socket--> daemon --> SQLite ledger
                                                 \\-> CLIProxyAPI management API

The daemon is reached over its ``AF_UNIX`` socket with
:func:`sarvam_meter.client.call`; a loopback HTTP server plays CLIProxyAPI's
management API (and the Sarvam dashboard, in ``test_dashboard_e2e``), because
those are *remote* HTTP sources.  Nothing reaches the internet, and the config,
the ledger and the cliproxy state all live in a temporary directory.

The recording proxy was removed, so the tests that drove a request through it
and read the rows it wrote are gone with it; what remains pins the widget
contract (exact key sets), the CLIProxyAPI source end to end, and the daemon's
degraded-but-serving behaviour when a dependency is down.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from sarvam_meter import client as client_module
from sarvam_meter import config as config_mod
from sarvam_meter.daemon import Daemon
from sarvam_meter.ledger import Ledger

MANAGEMENT_KEY = "mgmt-test-DO-NOT-LEAK-9876543210"

#: Long enough for a busy CI box, short enough to fail fast when broken.
POLL_TIMEOUT = 10.0
POLL_INTERVAL = 0.01

# -- the widget contract (SPEC section 10): exact key sets ------------------

TOP_KEYS = {"schema", "generated_at", "stale", "errors", "sarvam", "cliproxy"}
SARVAM_KEYS = {
    "configured",
    "currency",
    "balance",
    "spend",
    "calls",
    "by_api",
    "dashboard",
}
BALANCE_KEYS = {"known", "remaining", "starting", "spent_total", "set_at", "source"}
SPEND_KEYS = {"today", "month", "source"}
DASHBOARD_KEYS = {
    "configured",
    "reachable",
    "as_of",
    "org_id",
    "currency",
    "window",
    "spend",
    "by_model",
    "error",
    "balance",
}
CALLS_KEYS = {"today"}
BY_API_KEYS = {"api", "spend", "calls"}
CLIPROXY_KEYS = {"configured", "reachable", "api_version", "worst_fraction", "accounts"}
ACCOUNT_KEYS = {
    "auth_index",
    "id",
    "name",
    "provider",
    "label",
    "status",
    "plan",
    "buckets",
    "summary",
    "error",
}
BUCKET_KEYS = {"window", "remaining_fraction", "reset_at", "description", "unknown"}
SUMMARY_KEYS = {"key", "label", "value", "unit", "format", "currency"}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def free_port():
    """A loopback port that was free a moment ago (bound then released)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


class LoopbackServer(object):
    """A ``ThreadingHTTPServer`` on 127.0.0.1:0 with a clean start/stop."""

    def __init__(self, handler_class):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
        self.thread = None

    def start(self):
        self.thread = threading.Thread(
            target=self.httpd.serve_forever,
            name="test-loopback-%d" % (self.httpd.server_address[1],),
            daemon=True,
        )
        self.thread.start()
        return self

    @property
    def base_url(self):
        return "http://127.0.0.1:%d" % (self.httpd.server_address[1],)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5.0)


def db_events(db_path):
    """Every ledger row, oldest first (``[]`` when the db was never created)."""
    if not os.path.isfile(db_path):
        return []
    connection = sqlite3.connect(db_path, timeout=10.0)
    try:
        connection.row_factory = sqlite3.Row
        try:
            return [
                dict(row)
                for row in connection.execute("SELECT * FROM events ORDER BY id")
            ]
        except sqlite3.OperationalError:
            return []
    finally:
        connection.close()


class DaemonTestCase(unittest.TestCase):
    """A daemon bound to a throwaway socket, plus one shared env-restoring hook."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sarvam-int-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db_path = os.path.join(self.tmp, "usage.db")

        saved = {}
        for name in ("SARVAM_METER_CONFIG", "SARVAM_METER_CLIPROXY_KEY"):
            saved[name] = os.environ.get(name)
        self.addCleanup(self._restore_env, saved)
        os.environ["SARVAM_METER_CONFIG"] = os.path.join(self.tmp, "config.json")
        os.environ.pop(config_mod.ENV_CLIPROXY_KEY, None)

    @staticmethod
    def _restore_env(saved):
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def write_config(self, document):
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2)
        return config_mod.load_config(path)

    def start_daemon(self, config, ledger=None):
        """Start a daemon on this test's socket; shut it down at the end."""
        daemon = Daemon(
            config,
            ledger=ledger,
            logger=lambda _line: None,
            socket_path=os.path.join(self.tmp, "run", "daemon.sock"),
        )
        path = daemon.start()
        self.addCleanup(daemon.shutdown)
        return daemon, path

    @staticmethod
    def call(path, op, **args):
        return client_module.call(op, socket_path=path, timeout=20.0, **args)


# ---------------------------------------------------------------------------
# the widget contract
# ---------------------------------------------------------------------------


class StatusContractTest(DaemonTestCase):
    def setUp(self):
        super().setUp()
        document = config_mod.default_config_dict()
        document["db_path"] = self.db_path
        document["dashboard"]["session_file"] = os.path.join(self.tmp, "session.json")
        document["dashboard"]["enabled"] = False
        self.config = self.write_config(document)
        self.ledger = Ledger(self.db_path)
        self.addCleanup(self.ledger.close)
        self.daemon, self.bound = self.start_daemon(self.config, self.ledger)

    def test_status_payload_key_sets_are_exact(self):
        self.ledger.set_balance(500.0, "INR")
        self.ledger.record_event(
            source="manual", api="chat", quantity=1500.0, unit="tokens", cost=0.045
        )

        payload = self.call(self.bound, "status")

        self.assertEqual(set(payload), TOP_KEYS)
        self.assertEqual(payload["schema"], 2)
        self.assertIsInstance(payload["generated_at"], str)
        self.assertIsInstance(payload["stale"], bool)
        self.assertIsInstance(payload["errors"], list)

        sarvam = payload["sarvam"]
        self.assertEqual(set(sarvam), SARVAM_KEYS)
        self.assertEqual(set(sarvam["balance"]), BALANCE_KEYS)
        self.assertEqual(set(sarvam["spend"]), SPEND_KEYS)
        self.assertEqual(set(sarvam["calls"]), CALLS_KEYS)
        self.assertEqual(set(sarvam["dashboard"]), DASHBOARD_KEYS)
        self.assertEqual(sarvam["currency"], "INR")
        self.assertIs(sarvam["configured"], True)
        self.assertTrue(sarvam["by_api"], "by_api should list the chat call")
        for entry in sarvam["by_api"]:
            self.assertEqual(set(entry), BY_API_KEYS)

        cliproxy = payload["cliproxy"]
        self.assertEqual(set(cliproxy), CLIPROXY_KEYS)
        self.assertIs(cliproxy["configured"], False)
        self.assertIs(cliproxy["reachable"], False)
        self.assertEqual(cliproxy["api_version"], "v8")
        self.assertIsNone(cliproxy["worst_fraction"])
        self.assertEqual(cliproxy["accounts"], [])

        # No extra top-level keys may appear either.
        self.assertEqual(sorted(payload), sorted(TOP_KEYS))

    def test_status_does_not_leak_secrets_and_stays_read_only(self):
        document = self.call(self.bound, "config.get")
        blob = json.dumps(document)
        self.assertNotIn(MANAGEMENT_KEY, blob)
        self.assertEqual(document["cliproxy"]["management_key"], "")

        # A key outside the writable subset is refused and changes nothing.
        with self.assertRaises(client_module.ClientError) as raised:
            self.call(
                self.bound,
                "config.set",
                config={"cliproxy": {"management_key": MANAGEMENT_KEY}},
            )
        self.assertEqual(raised.exception.code, "invalid_config")
        self.assertNotIn(MANAGEMENT_KEY, raised.exception.message)
        self.assertEqual(
            self.call(self.bound, "config.get")["cliproxy"]["management_key"], ""
        )

        health = self.call(self.bound, "health")
        self.assertIs(health["ok"], True)


# ---------------------------------------------------------------------------
# CLIProxyAPI end to end against a fake management API
# ---------------------------------------------------------------------------

QUOTA_BY_AUTH_INDEX = {
    "idx-claude": {
        "subscription": {"plan": "Max", "tierName": "Max 20x", "tierId": "max-20x"},
        "summary": [
            {
                "key": "credits",
                "label": "Credits",
                "value": 12.5,
                "unit": "USD",
                "format": "currency",
                "currency": "USD",
            }
        ],
        "serverTimeOffsetMs": 0,
        "groups": [
            {
                "displayName": "Claude",
                "buckets": [
                    {
                        "window": "5h",
                        "remainingFraction": 0.62,
                        "resetTime": "2026-11-01T10:00:00Z",
                        "description": "5 hour window",
                    },
                    {
                        "window": "7d",
                        "remainingFraction": 0.9,
                        "resetTime": "2026-11-05T10:00:00Z",
                        "description": "weekly window",
                    },
                ],
            }
        ],
    },
    "idx-codex": {
        "subscription": {"plan": "Pro"},
        "summary": [],
        "serverTimeOffsetMs": 0,
        "groups": [
            {
                "displayName": "Codex",
                "buckets": [
                    {
                        "window": "5h",
                        "remainingFraction": 0.35,
                        "resetTime": "2026-11-01T09:00:00Z",
                        "description": "5 hour window",
                    }
                ],
            }
        ],
    },
}

ACCOUNTS_PAYLOAD = {
    "files": [
        {
            "auth_index": "idx-claude",
            "id": "acct-1",
            "name": "claude-max",
            "provider": "claude",
            "status": "active",
            "disabled": False,
            "unavailable": False,
        },
        {
            "auth_index": "idx-codex",
            "id": "acct-2",
            "name": "codex-pro",
            "provider": "codex",
            "status": "active",
            "disabled": False,
            "unavailable": False,
        },
    ]
}


class FakeManagementHandler(BaseHTTPRequestHandler):
    """Answers the two v8 management endpoints the client actually uses."""

    protocol_version = "HTTP/1.1"
    state = None  # type: dict

    def log_message(self, fmt, *args):
        return

    def do_GET(self):  # noqa: N802
        parsed = urlsplit(self.path)
        state = self.state if self.state is not None else {}
        state.setdefault("paths", []).append(self.path)
        state.setdefault("headers", []).append(dict(self.headers.items()))
        if parsed.path == "/v8/management/credentials":
            return self._send_json(200, ACCOUNTS_PAYLOAD)
        return self._send_json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        parsed = urlsplit(self.path)
        state = self.state if self.state is not None else {}
        state.setdefault("paths", []).append(self.path)
        state.setdefault("headers", []).append(dict(self.headers.items()))
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        body = self.rfile.read(length) if length else b""
        if parsed.path != "/v8/management/credentials/quota/fetch":
            return self._send_json(404, {"error": "not found"})
        try:
            request = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            request = {}
        auth_index = request.get("auth_index")
        quota = QUOTA_BY_AUTH_INDEX.get(auth_index)
        if quota is None:
            return self._send_json(501, {"error": "no quota provider"})
        return self._send_json(200, quota)

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            self.close_connection = True


def make_management_handler(state):
    return type("ManagementHandler", (FakeManagementHandler,), {"state": state})


class CliproxyEndToEndTest(DaemonTestCase):
    """A real daemon polling a real (fake) management API over loopback."""

    def setUp(self):
        super().setUp()
        self.management_state = {}
        self.management = LoopbackServer(
            make_management_handler(self.management_state)
        ).start()
        self.addCleanup(self.management.stop)

        document = config_mod.default_config_dict()
        document["db_path"] = self.db_path
        document["cliproxy"].update(
            {
                "enabled": True,
                "base_url": self.management.base_url,
                "management_key": MANAGEMENT_KEY,
                "api_version": "v8",
                "quota_fetch": True,
                # Poll once on start; the test drives any further poll itself.
                "quota_fetch_interval_seconds": 3600,
                "allow_remote": False,
            }
        )
        self.config = self.write_config(document)
        self.ledger = Ledger(self.db_path)
        self.addCleanup(self.ledger.close)
        self.daemon, self.bound = self.start_daemon(self.config, self.ledger)

    def refreshed_status(self):
        payload = self.call(self.bound, "refresh")
        self.assertIsInstance(payload, dict)
        return payload

    def test_status_reports_accounts_buckets_and_worst_fraction(self):
        payload = self.refreshed_status()

        cliproxy = payload["cliproxy"]
        self.assertEqual(set(cliproxy), CLIPROXY_KEYS)
        self.assertIs(cliproxy["configured"], True)
        self.assertIs(cliproxy["reachable"], True)
        self.assertEqual(cliproxy["api_version"], "v8")
        self.assertEqual(payload["errors"], [])
        self.assertIs(payload["stale"], False)

        accounts = cliproxy["accounts"]
        self.assertEqual(len(accounts), 2, accounts)
        by_index = {}
        for account in accounts:
            self.assertEqual(set(account), ACCOUNT_KEYS)
            by_index[account["auth_index"]] = account

        claude = by_index["idx-claude"]
        self.assertEqual(claude["provider"], "claude")
        self.assertEqual(claude["name"], "claude-max")
        self.assertEqual(claude["status"], "active")
        self.assertEqual(claude["plan"], "Max")
        self.assertIsNone(claude["error"])
        self.assertEqual(len(claude["buckets"]), 2)
        for bucket in claude["buckets"]:
            self.assertEqual(set(bucket), BUCKET_KEYS)
            self.assertIs(bucket["unknown"], False)
            self.assertTrue(bucket["reset_at"].endswith("Z"))
        self.assertEqual(
            {bucket["window"] for bucket in claude["buckets"]}, {"5h", "7d"}
        )
        self.assertEqual(
            {bucket["remaining_fraction"] for bucket in claude["buckets"]}, {0.62, 0.9}
        )
        self.assertEqual(len(claude["summary"]), 1)
        summary_row = claude["summary"][0]
        self.assertEqual(set(summary_row), SUMMARY_KEYS)
        self.assertEqual(summary_row["currency"], "USD")
        self.assertEqual(summary_row["format"], "currency")

        codex = by_index["idx-codex"]
        self.assertEqual(codex["provider"], "codex")
        self.assertEqual(len(codex["buckets"]), 1)
        self.assertEqual(codex["buckets"][0]["window"], "5h")
        self.assertEqual(codex["buckets"][0]["remaining_fraction"], 0.35)
        self.assertEqual(codex["buckets"][0]["reset_at"], "2026-11-01T09:00:00Z")

        # worst_fraction is the lowest remaining fraction across all accounts.
        fractions = [
            bucket["remaining_fraction"]
            for account in accounts
            for bucket in account["buckets"]
            if bucket["remaining_fraction"] is not None
        ]
        self.assertEqual(min(fractions), 0.35)
        self.assertEqual(cliproxy["worst_fraction"], min(fractions))

        # Both auth headers must be sent on every management request.
        headers = self.management_state["headers"]
        self.assertTrue(headers)
        for sent in headers:
            lowered = {key.lower(): value for key, value in sent.items()}
            self.assertEqual(lowered.get("authorization"), "Bearer " + MANAGEMENT_KEY)
            self.assertEqual(lowered.get("x-management-key"), MANAGEMENT_KEY)

    def test_the_destructive_usage_queue_endpoint_is_never_requested(self):
        self.refreshed_status()

        paths = self.management_state.get("paths") or []
        self.assertTrue(paths, "the daemon never polled the management API")
        offenders = [path for path in paths if "queue" in path.lower()]
        self.assertEqual(offenders, [], "polled a destructive usage-queue endpoint")
        # And it really did use the documented v8 endpoints.
        self.assertIn("/v8/management/credentials", paths)
        self.assertIn("/v8/management/credentials/quota/fetch", paths)

    def test_management_key_never_reaches_the_status_or_config(self):
        payload = self.refreshed_status()
        self.assertNotIn(MANAGEMENT_KEY, json.dumps(payload))

        document = self.call(self.bound, "config.get")
        self.assertNotIn(MANAGEMENT_KEY, json.dumps(document))
        self.assertEqual(document["cliproxy"]["management_key"], "***")


# ---------------------------------------------------------------------------
# robustness
# ---------------------------------------------------------------------------


class RobustnessTest(DaemonTestCase):
    """The chain degrades instead of failing when a dependency is down."""

    def setUp(self):
        super().setUp()
        self.ledger = Ledger(self.db_path)
        self.addCleanup(self.ledger.close)

    def start(self, document_updates=None):
        document = config_mod.default_config_dict()
        document["db_path"] = self.db_path
        for section, values in (document_updates or {}).items():
            document[section].update(values)
        config = self.write_config(document)
        daemon, path = self.start_daemon(config, self.ledger)
        return config, daemon, path

    def test_status_reports_an_errors_entry_when_cliproxy_refuses_connections(self):
        dead_port = free_port()  # connection refused
        document_updates = {
            "cliproxy": {
                "enabled": True,
                "base_url": "http://127.0.0.1:%d" % (dead_port,),
                "management_key": MANAGEMENT_KEY,
                "api_version": "v8",
                "quota_fetch_interval_seconds": 3600,
            }
        }
        _config, _daemon, path = self.start(document_updates)

        # Force one poll so the assertion does not race the refresh thread.
        self.call(path, "refresh")
        payload = self.call(path, "status")

        self.assertEqual(set(payload), TOP_KEYS)
        self.assertIs(payload["cliproxy"]["configured"], True)
        self.assertIs(payload["cliproxy"]["reachable"], False)
        self.assertEqual(payload["cliproxy"]["accounts"], [])

        errors = payload["errors"]
        self.assertEqual(len(errors), 1, errors)
        self.assertEqual(set(errors[0]), {"source", "code", "message"})
        self.assertEqual(errors[0]["source"], "cliproxy")
        self.assertEqual(errors[0]["code"], "unreachable")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                     "root ignores permission bits, so the failure cannot be reproduced")
    def test_status_answers_when_the_ledger_database_cannot_be_opened(self):
        broken = os.path.join(self.tmp, "broken")
        os.makedirs(broken)
        os.chmod(broken, 0o500)
        self.addCleanup(os.chmod, broken, 0o700)
        self.db_path = os.path.join(broken, "usage.db")

        document = config_mod.default_config_dict()
        document["db_path"] = self.db_path
        config = self.write_config(document)

        # A ledger that cannot open must not stop the daemon from serving.
        daemon = Daemon(
            config,
            logger=lambda _line: None,
            socket_path=os.path.join(self.tmp, "run", "broken.sock"),
        )
        self.addCleanup(daemon.shutdown)
        self.assertIsNotNone(daemon.ledger)
        path = daemon.start()

        payload = self.call(path, "status")
        self.assertEqual(set(payload), TOP_KEYS)
        self.assertEqual(payload["sarvam"]["spend"]["today"], 0.0)
        self.assertEqual(payload["sarvam"]["calls"]["today"], 0)
        sources = [entry["source"] for entry in payload["errors"]]
        self.assertIn("ledger", sources, payload["errors"])
        ledger_error = [
            entry for entry in payload["errors"] if entry["source"] == "ledger"
        ][0]
        self.assertEqual(ledger_error["code"], "read_failed")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
