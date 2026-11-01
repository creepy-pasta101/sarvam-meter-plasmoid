# SPDX-License-Identifier: MIT
"""End-to-end integration tests for the whole metering chain (SPEC sections 8-11).

One process, real sockets, no mocks of the modules under test:

    client --HTTP--> recording proxy --HTTP--> fake api.sarvam.ai
                          |
                          v
                     SQLite ledger <---- daemon --HTTP--> GET /v1/status

A second loopback server plays CLIProxyAPI's management API for the cliproxy
tests.  Every server binds ``127.0.0.1:0``; nothing reaches the internet, and
the config, the ledger and the cliproxy state all live in a temporary directory.

Asynchronous ledger writes are awaited with a bounded poll (10 ms steps, 10 s
budget) rather than a fixed sleep.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import http.client
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
from urllib.parse import parse_qs, urlsplit

from sarvam_meter import config as config_mod
from sarvam_meter import rates
from sarvam_meter.daemon import Daemon
from sarvam_meter.ledger import Ledger
from sarvam_meter.proxy import ProxyServer

API_KEY = "sk-test-DO-NOT-LEAK-0123456789"
MANAGEMENT_KEY = "mgmt-test-DO-NOT-LEAK-9876543210"

#: Long enough for a busy CI box, short enough to fail fast when broken.
POLL_TIMEOUT = 10.0
POLL_INTERVAL = 0.01

#: A chat response carries this usage unless the request overrides it.
DEFAULT_USAGE = {"prompt_tokens": 1000, "completion_tokens": 500, "cached_tokens": 0}

#: Sentinel meaning "use DEFAULT_USAGE"; passing ``None`` omits usage entirely.
_USE_DEFAULT_USAGE = object()

# -- the widget contract (SPEC section 10): exact key sets ------------------

TOP_KEYS = {"schema", "generated_at", "stale", "errors", "sarvam", "cliproxy"}
SARVAM_KEYS = {
    "configured",
    "currency",
    "balance",
    "spend",
    "calls",
    "by_api",
    "rate_limits",
    "quota_exhausted",
    "quota_exhausted_at",
    "untracked_calls",
}
BALANCE_KEYS = {"known", "remaining", "starting", "spent_total", "set_at"}
SPEND_KEYS = {"today", "month"}
CALLS_KEYS = {"today"}
RATE_LIMIT_KEYS = {"count_24h", "last_at"}
UNTRACKED_KEYS = {"today", "total"}
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


def wav_bytes(duration_seconds=1.0, sample_rate=16000, byte_rate=32000):
    """A RIFF/WAVE file whose declared data size encodes *duration_seconds*."""
    data_size = int(round(byte_rate * duration_seconds))
    fmt_payload = (
        (1).to_bytes(2, "little")  # PCM
        + (1).to_bytes(2, "little")  # mono
        + sample_rate.to_bytes(4, "little")
        + byte_rate.to_bytes(4, "little")
        + (2).to_bytes(2, "little")  # block align
        + (16).to_bytes(2, "little")  # bits per sample
    )
    chunks = (
        b"fmt " + len(fmt_payload).to_bytes(4, "little") + fmt_payload
        + b"data" + data_size.to_bytes(4, "little") + b"\x00" * 64
    )
    return b"RIFF" + (len(chunks) + 4).to_bytes(4, "little") + b"WAVE" + chunks


def multipart_body(field, filename, content, content_type, boundary="sarvam-test-boundary"):
    """Build a one-part ``multipart/form-data`` upload body."""
    body = (
        ("--%s\r\n" % boundary).encode("utf-8")
        + (
            'Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
            % (field, filename)
        ).encode("utf-8")
        + ("Content-Type: %s\r\n\r\n" % content_type).encode("utf-8")
        + content
        + b"\r\n"
        + ("--%s--\r\n" % boundary).encode("utf-8")
    )
    return body, "multipart/form-data; boundary=%s" % boundary


class LoopbackServer(object):
    """A ``ThreadingHTTPServer`` on 127.0.0.1:0 with a clean start/stop."""

    def __init__(self, handler_class):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
        self.httpd.daemon_threads = True
        self.port = int(self.httpd.server_address[1])
        self._thread = threading.Thread(
            target=self.httpd.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="test-loopback-%d" % self.port,
            daemon=True,
        )

    def start(self):
        self._thread.start()
        return self

    @property
    def base_url(self):
        return "http://127.0.0.1:%d" % (self.port,)

    def stop(self):
        try:
            self.httpd.shutdown()
        finally:
            self.httpd.server_close()
        self._thread.join(timeout=5.0)
        if self._thread.is_alive():  # pragma: no cover - only on a leak
            raise AssertionError("loopback server thread did not stop")


class _CapturingHandler(logging.Handler):
    """Collect log records so a swallowed metering failure cannot hide."""

    def __init__(self):
        logging.Handler.__init__(self)
        self.records = []

    def emit(self, record):  # noqa: D102
        self.records.append(record)


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


# ---------------------------------------------------------------------------
# fake api.sarvam.ai
# ---------------------------------------------------------------------------


class FakeUpstreamHandler(BaseHTTPRequestHandler):
    """Mimics the three Sarvam endpoints the meter cares about.

    ``?fail=402`` / ``?fail=429`` force that status with a JSON error body and
    no ``usage`` object.  ``fake_usage`` in a chat request body overrides the
    reported usage; ``"fake_usage": null`` omits it entirely.
    """

    protocol_version = "HTTP/1.1"
    state = None  # type: dict

    def log_message(self, fmt, *args):  # keep the test output readable
        return

    def do_POST(self):  # noqa: N802 - http.server API
        parsed = urlsplit(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        body = self.rfile.read(length) if length else b""

        state = self.state if self.state is not None else {}
        state.setdefault("paths", []).append(path)
        state.setdefault("headers", []).append(dict(self.headers.items()))

        request_json = None
        try:
            request_json = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            request_json = None

        forced = query.get("fail", [None])[0]
        if forced:
            status = int(forced)
            payload = {
                "error": {"message": "forced %d" % status, "code": "forced_error"}
            }
            return self._send_json(status, payload)

        if path.endswith("/chat/completions"):
            model = (request_json or {}).get("model") or "sarvam-105b"
            payload = {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            }
            usage_hint = (request_json or {}).get("fake_usage", DEFAULT_USAGE)
            if usage_hint is not None:
                prompt = int(usage_hint.get("prompt_tokens", 0))
                completion = int(usage_hint.get("completion_tokens", 0))
                payload["usage"] = {
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "total_tokens": prompt + completion,
                    "prompt_tokens_details": {
                        "cached_tokens": int(usage_hint.get("cached_tokens", 0))
                    },
                }
            return self._send_json(200, payload)

        if path.endswith("/speech-to-text"):
            return self._send_json(
                200, {"transcript": "namaste", "language_code": "en-IN"}
            )

        if path.endswith("/text-to-speech"):
            audio = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 64
            return self._send(200, {"Content-Type": "audio/mpeg"}, audio)

        return self._send_json(
            404, {"error": {"message": "not found", "code": "not_found"}}
        )

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        return self._send(status, {"Content-Type": "application/json"}, body)

    def _send(self, status, headers, body):
        try:
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            self.close_connection = True


def make_upstream_handler(state):
    return type("UpstreamHandler", (FakeUpstreamHandler,), {"state": state})


# ---------------------------------------------------------------------------
# shared chain fixture: fake upstream + proxy + daemon + ledger
# ---------------------------------------------------------------------------


class ChainTestCase(unittest.TestCase):
    """Starts the full chain once per test; tears every socket and thread down."""

    extra_sections = {}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sarvam-int-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db_path = os.path.join(self.tmp, "usage.db")

        saved = {}
        for name in ("SARVAM_METER_CONFIG", "SARVAM_METER_CLIPROXY_KEY"):
            saved[name] = os.environ.get(name)
        self.addCleanup(self._restore_env, saved)
        os.environ["SARVAM_METER_CONFIG"] = os.path.join(self.tmp, "config.json")
        os.environ.pop("SARVAM_METER_CLIPROXY_KEY", None)

        self.upstream_state = {}
        self.upstream = LoopbackServer(
            make_upstream_handler(self.upstream_state)
        ).start()
        self.addCleanup(self.upstream.stop)

        self.config = self.make_config()
        self.ledger = Ledger(self.db_path)
        self.addCleanup(self.ledger.close)

        self.log_records = _CapturingHandler()
        proxy_logger = logging.getLogger("sarvam_meter.proxy")
        proxy_logger.addHandler(self.log_records)
        self.addCleanup(proxy_logger.removeHandler, self.log_records)

        self.proxy = ProxyServer(self.config, self.ledger)
        self.proxy_port = self.proxy.port  # binds on 127.0.0.1:0
        self.proxy_thread = threading.Thread(
            target=self.proxy.serve_forever, name="test-proxy", daemon=True
        )
        self.proxy_thread.start()
        self.addCleanup(self._stop_proxy)

        self.daemon = Daemon(self.config, ledger=self.ledger, logger=lambda line: None)
        self.daemon_port = self.daemon.start()
        self.addCleanup(self.daemon.shutdown)

    @staticmethod
    def _restore_env(saved):
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _stop_proxy(self):
        self.proxy.shutdown()
        self.proxy_thread.join(timeout=5.0)
        if self.proxy_thread.is_alive():  # pragma: no cover - only on a leak
            raise AssertionError("proxy thread did not stop")

    def make_config(self, **sections):
        """Write a config document, load it, then apply ephemeral ports."""
        document = config_mod.default_config_dict()
        document["db_path"] = self.db_path
        document["proxy"]["enabled"] = True
        document["proxy"]["upstream_base"] = self.upstream.base_url
        document["proxy"]["port"] = free_port()
        document["daemon"]["port"] = free_port()
        merged = {}
        merged.update(self.extra_sections)
        merged.update(sections)
        for section, values in merged.items():
            document[section].update(values)
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2)
        config = config_mod.load_config(path)
        # The schema only allows 1..65535, so port 0 is applied post-load.
        return dataclasses.replace(
            config,
            daemon=dataclasses.replace(config.daemon, port=0),
            proxy=dataclasses.replace(config.proxy, port=0),
        )

    # -- HTTP helpers -------------------------------------------------------

    def proxy_post(self, path, payload=None, body=None, content_type="application/json"):
        """POST through the recording proxy; returns ``(status, headers, body)``."""
        if body is None:
            body = json.dumps(payload if payload is not None else {}).encode("utf-8")
        headers = {
            "Content-Type": content_type,
            "api-subscription-key": API_KEY,
            "Connection": "close",
        }
        connection = self._connect(self.proxy_port)
        try:
            connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
            return (
                response.status,
                {key.lower(): value for key, value in response.getheaders()},
                response.read(),
            )
        finally:
            connection.close()

    def chat(self, model="sarvam-105b", usage=_USE_DEFAULT_USAGE, fail=None):
        """One proxied chat completion; returns ``(status, headers, body)``.

        ``usage=None`` asks the fake upstream to omit the ``usage`` object.
        """
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "hello"}],
        }
        payload["fake_usage"] = (
            DEFAULT_USAGE if usage is _USE_DEFAULT_USAGE else usage
        )
        path = "/v1/chat/completions"
        if fail is not None:
            path += "?fail=%d" % int(fail)
        return self.proxy_post(path, payload)

    def daemon_request(self, method, path):
        connection = self._connect(self.daemon_port)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            body = response.read()
            headers = {key.lower(): value for key, value in response.getheaders()}
            return response.status, headers, body
        finally:
            connection.close()

    def status_payload(self):
        status, headers, body = self.daemon_request("GET", "/v1/status")
        self.assertEqual(status, 200, body)
        self.assertEqual(headers.get("content-type"), "application/json; charset=utf-8")
        self.assertEqual(headers.get("cache-control"), "no-store")
        return json.loads(body.decode("utf-8"))

    @staticmethod
    def _connect(port):
        return http.client.HTTPConnection("127.0.0.1", port, timeout=20)

    # -- waiting ------------------------------------------------------------

    def wait_for(self, predicate, message, timeout=POLL_TIMEOUT):
        """Poll *predicate* until it is truthy; fail the test when it never is."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(POLL_INTERVAL)
        self.fail("timed out after %.1fs waiting for %s" % (timeout, message))

    def wait_for_events(self, count):
        def enough():
            rows = db_events(self.db_path)
            return rows if len(rows) >= count else None

        return self.wait_for(enough, "%d ledger row(s)" % count)


# ---------------------------------------------------------------------------
# the full chain
# ---------------------------------------------------------------------------


class ChainTest(ChainTestCase):
    def test_spend_calls_by_api_and_balance_reflect_proxied_calls(self):
        self.ledger.set_balance(500.0, "INR")

        chat_status, _headers, chat_body = self.chat()
        self.assertEqual(chat_status, 200, chat_body)

        audio = wav_bytes(1.0)
        upload, upload_type = multipart_body("file", "sample.wav", audio, "audio/wav")
        stt_status, _headers, stt_body = self.proxy_post(
            "/speech-to-text", body=upload, content_type=upload_type
        )
        self.assertEqual(stt_status, 200, stt_body)
        self.assertEqual(json.loads(stt_body.decode("utf-8"))["transcript"], "namaste")

        tts_status, _headers, tts_body = self.proxy_post(
            "/text-to-speech",
            {
                "text": "a" * 500,
                "target_language_code": "hi-IN",
                "speaker": "meera",
            },
        )
        self.assertEqual(tts_status, 200, tts_body)
        self.assertTrue(tts_body.startswith(b"ID3"))

        events = self.wait_for_events(3)
        self.assertEqual(len(events), 3, events)
        self.assertEqual([event["api"] for event in events], ["chat", "stt", "tts"])
        for event in events:
            self.assertEqual(event["source"], "proxy")
            self.assertEqual(event["estimated"], 0, event)
            self.assertEqual(event["status"], 200, event)

        # 1000 in + 500 out on sarvam-105b = (1000*15 + 500*60) / 1e6
        self.assertAlmostEqual(events[0]["cost"], 0.045, places=9)
        self.assertEqual(events[0]["quantity"], 1500.0)
        self.assertEqual(events[0]["unit"], "tokens")
        self.assertEqual(events[0]["model"], "sarvam-105b")
        # 1.0 s of audio at INR 0.49 / minute
        self.assertAlmostEqual(events[1]["cost"], round(0.49 / 60.0, 6), places=9)
        self.assertAlmostEqual(events[1]["quantity"], 1.0, places=6)
        self.assertEqual(events[1]["unit"], "seconds")
        # 500 characters at INR 3.00 / 1000 characters
        self.assertAlmostEqual(events[2]["cost"], 1.5, places=9)
        self.assertEqual(events[2]["quantity"], 500.0)
        self.assertEqual(events[2]["unit"], "chars")

        payload = self.status_payload()
        sarvam = payload["sarvam"]
        self.assertGreater(sarvam["spend"]["today"], 0.0)
        self.assertEqual(sarvam["calls"]["today"], 3)
        self.assertEqual(sarvam["spend"]["month"], 1.55)
        self.assertEqual(sarvam["spend"]["today"], 1.55)
        self.assertEqual(
            {entry["api"] for entry in sarvam["by_api"]}, {"chat", "stt", "tts"}
        )
        self.assertIs(sarvam["balance"]["known"], True)
        self.assertEqual(sarvam["balance"]["starting"], 500.0)
        self.assertEqual(sarvam["balance"]["spent_total"], 1.55)
        self.assertEqual(
            sarvam["balance"]["remaining"],
            round(500.0 - sarvam["balance"]["spent_total"], 2),
        )
        self.assertEqual(sarvam["balance"]["remaining"], 498.45)
        self.assertEqual(sarvam["untracked_calls"], {"today": 0, "total": 0})
        self.assertIs(sarvam["quota_exhausted"], False)
        self.assertEqual(payload["errors"], [])
        self.assertIs(payload["stale"], False)

        # The ledger keeps the exact 6 dp total.
        total = self.ledger.spend_since("0001-01-01T00:00:00Z")
        self.assertAlmostEqual(total, 0.045 + round(0.49 / 60.0, 6) + 1.5, places=6)

        # No secret ever reaches the payload the widget reads.
        self.assertNotIn(API_KEY, json.dumps(payload))

    def test_chat_cost_prices_input_cached_and_output_separately(self):
        card = rates.DEFAULT_RATE_CARD["chat"]["sarvam-105b"]
        usage = {"prompt_tokens": 1000, "completion_tokens": 500, "cached_tokens": 400}
        # prompt_tokens already include the cached ones: 600 fresh + 400 cached.
        expected = (
            600 * card["in_per_million"]
            + 400 * card["cached_in_per_million"]
            + 500 * card["out_per_million"]
        ) / 1000000.0
        expected = round(expected, 6)
        self.assertAlmostEqual(expected, 0.041, places=9)

        status, _headers, body = self.chat(usage=usage)
        self.assertEqual(status, 200, body)

        events = self.wait_for_events(1)
        self.assertEqual(len(events), 1, events)
        self.assertEqual(events[0]["quantity"], 1500.0)  # in + out, for display
        self.assertAlmostEqual(events[0]["cost"], expected, places=9)

        # A blended single-rate price would be visibly different: output tokens
        # cost 4x input, so pricing everything at the input rate understates.
        blended = round(1500 * card["in_per_million"] / 1000000.0, 6)
        self.assertNotAlmostEqual(events[0]["cost"], blended, places=6)

    def test_402_from_upstream_marks_quota_exhausted(self):
        status, _headers, body = self.chat(fail=402)
        self.assertEqual(status, 402, body)

        events = self.wait_for_events(1)
        self.assertEqual(len(events), 1, events)
        self.assertEqual(events[0]["status"], 402)
        self.assertEqual(events[0]["cost"], 0.0, "an errored call is not billable")
        self.assertIsNone(events[0]["quantity"])
        self.assertEqual(events[0]["estimated"], 1)

        payload = self.status_payload()
        sarvam = payload["sarvam"]
        self.assertIs(sarvam["quota_exhausted"], True)
        self.assertIsInstance(sarvam["quota_exhausted_at"], str)
        self.assertTrue(sarvam["quota_exhausted_at"].endswith("Z"))
        self.assertEqual(sarvam["spend"]["today"], 0.0)
        self.assertEqual(sarvam["calls"]["today"], 1)
        self.assertEqual(sarvam["untracked_calls"]["today"], 1)

    def test_429_is_counted_in_rate_limits_and_does_not_set_the_flag(self):
        for _ in range(2):
            status, _headers, body = self.chat(fail=429)
            self.assertEqual(status, 429, body)

        events = self.wait_for_events(2)
        self.assertEqual(len(events), 2, events)
        self.assertEqual([event["status"] for event in events], [429, 429])

        payload = self.status_payload()
        sarvam = payload["sarvam"]
        self.assertEqual(sarvam["rate_limits"]["count_24h"], 2)
        self.assertIsInstance(sarvam["rate_limits"]["last_at"], str)
        self.assertTrue(sarvam["rate_limits"]["last_at"].endswith("Z"))
        self.assertIs(sarvam["quota_exhausted"], False)
        self.assertIsNone(sarvam["quota_exhausted_at"])
        self.assertEqual(sarvam["spend"]["today"], 0.0)

    def test_ten_parallel_chat_requests_record_ten_rows(self):
        # 200 in + 100 out on sarvam-105b = 0.009 INR per call.
        usage = {"prompt_tokens": 200, "completion_tokens": 100, "cached_tokens": 0}

        def one(_index):
            return self.chat(usage=usage)[0]

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            statuses = list(pool.map(one, range(10)))

        self.assertEqual(statuses, [200] * 10)

        events = self.wait_for_events(10)
        self.assertEqual(len(events), 10, events)
        self.assertEqual(len({event["id"] for event in events}), 10)
        for event in events:
            self.assertAlmostEqual(event["cost"], 0.009, places=9)

        payload = self.status_payload()
        sarvam = payload["sarvam"]
        self.assertEqual(sarvam["calls"]["today"], 10)
        self.assertEqual(sarvam["spend"]["today"], 0.09)
        self.assertEqual(payload["errors"], [])

        # A "database is locked" would have been swallowed by
        # record_from_exchange's warning log; no warning may be emitted.
        problems = [
            record.getMessage()
            for record in self.log_records.records
            if record.levelno >= logging.WARNING
        ]
        self.assertEqual(problems, [], "the proxy logged metering failures")

    def test_chat_response_without_usage_is_untracked(self):
        status, _headers, body = self.chat(usage=None)
        self.assertEqual(status, 200, body)
        self.assertNotIn("usage", json.loads(body.decode("utf-8")))

        events = self.wait_for_events(1)
        self.assertEqual(len(events), 1, events)
        self.assertIsNone(events[0]["quantity"])
        self.assertEqual(events[0]["cost"], 0.0)
        self.assertEqual(events[0]["estimated"], 1)

        sarvam = self.status_payload()["sarvam"]
        self.assertEqual(sarvam["untracked_calls"], {"today": 1, "total": 1})
        self.assertEqual(sarvam["calls"]["today"], 1)


# ---------------------------------------------------------------------------
# the widget contract
# ---------------------------------------------------------------------------


class StatusContractTest(ChainTestCase):
    def test_status_payload_key_sets_are_exact(self):
        self.ledger.set_balance(500.0, "INR")
        self.assertEqual(self.chat()[0], 200)
        self.wait_for_events(1)

        payload = self.status_payload()

        self.assertEqual(set(payload), TOP_KEYS)
        self.assertEqual(payload["schema"], 1)
        self.assertIsInstance(payload["generated_at"], str)
        self.assertIsInstance(payload["stale"], bool)
        self.assertIsInstance(payload["errors"], list)

        sarvam = payload["sarvam"]
        self.assertEqual(set(sarvam), SARVAM_KEYS)
        self.assertEqual(set(sarvam["balance"]), BALANCE_KEYS)
        self.assertEqual(set(sarvam["spend"]), SPEND_KEYS)
        self.assertEqual(set(sarvam["calls"]), CALLS_KEYS)
        self.assertEqual(set(sarvam["rate_limits"]), RATE_LIMIT_KEYS)
        self.assertEqual(set(sarvam["untracked_calls"]), UNTRACKED_KEYS)
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
        status, _headers, body = self.daemon_request("GET", "/v1/config")
        self.assertEqual(status, 200, body)
        document = json.loads(body.decode("utf-8"))
        self.assertNotIn(API_KEY, body.decode("utf-8"))
        self.assertEqual(document["cliproxy"]["management_key"], "")

        # Mutating verbs are refused and change nothing.
        for method in ("PUT", "PATCH", "DELETE"):
            status, _headers, _body = self.daemon_request(method, "/v1/status")
            self.assertEqual(status, 405, method)

        status, _headers, body = self.daemon_request("GET", "/v1/health")
        self.assertEqual(status, 200)
        health = json.loads(body.decode("utf-8"))
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


class CliproxyEndToEndTest(unittest.TestCase):
    """A real daemon polling a real (fake) management API over loopback."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sarvam-cliproxy-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db_path = os.path.join(self.tmp, "usage.db")

        saved = {}
        for name in ("SARVAM_METER_CONFIG", "SARVAM_METER_CLIPROXY_KEY"):
            saved[name] = os.environ.get(name)
        self.addCleanup(self._restore_env, saved)
        os.environ["SARVAM_METER_CONFIG"] = os.path.join(self.tmp, "config.json")
        os.environ.pop("SARVAM_METER_CLIPROXY_KEY", None)

        self.management_state = {}
        self.management = LoopbackServer(
            make_management_handler(self.management_state)
        ).start()
        self.addCleanup(self.management.stop)

        document = config_mod.default_config_dict()
        document["db_path"] = self.db_path
        document["daemon"]["port"] = free_port()
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
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2)
        loaded = config_mod.load_config(path)
        self.config = dataclasses.replace(
            loaded, daemon=dataclasses.replace(loaded.daemon, port=0)
        )

        self.ledger = Ledger(self.db_path)
        self.addCleanup(self.ledger.close)
        self.daemon = Daemon(self.config, ledger=self.ledger, logger=lambda line: None)
        self.daemon_port = self.daemon.start()
        self.addCleanup(self.daemon.shutdown)

    @staticmethod
    def _restore_env(saved):
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def request(self, method, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.daemon_port, timeout=20)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def refreshed_status(self):
        status, body = self.request("POST", "/v1/refresh")
        self.assertEqual(status, 200, body)
        payload = json.loads(body.decode("utf-8"))
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

        status, body = self.request("GET", "/v1/config")
        self.assertEqual(status, 200)
        self.assertNotIn(MANAGEMENT_KEY, body.decode("utf-8"))
        document = json.loads(body.decode("utf-8"))
        self.assertEqual(document["cliproxy"]["management_key"], "***")


# ---------------------------------------------------------------------------
# robustness
# ---------------------------------------------------------------------------


class RobustnessTest(unittest.TestCase):
    """The chain degrades instead of failing when a dependency is down."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sarvam-robust-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db_path = os.path.join(self.tmp, "usage.db")

        saved = {}
        for name in ("SARVAM_METER_CONFIG", "SARVAM_METER_CLIPROXY_KEY"):
            saved[name] = os.environ.get(name)
        self.addCleanup(self._restore_env, saved)
        os.environ["SARVAM_METER_CONFIG"] = os.path.join(self.tmp, "config.json")
        os.environ.pop("SARVAM_METER_CLIPROXY_KEY", None)

        self.ledger = Ledger(self.db_path)
        self.addCleanup(self.ledger.close)
        self._servers = []

    @staticmethod
    def _restore_env(saved):
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def start_daemon(self, document_updates=None):
        document = config_mod.default_config_dict()
        document["db_path"] = self.db_path
        document["daemon"]["port"] = free_port()
        for section, values in (document_updates or {}).items():
            document[section].update(values)
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2)
        loaded = config_mod.load_config(path)
        config = dataclasses.replace(
            loaded, daemon=dataclasses.replace(loaded.daemon, port=0)
        )
        daemon = Daemon(config, ledger=self.ledger, logger=lambda line: None)
        port = daemon.start()
        self.addCleanup(daemon.shutdown)
        return config, daemon, port

    @staticmethod
    def get(port, path):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    @staticmethod
    def post(port, path):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        try:
            connection.request("POST", path, body=b"")
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_status_still_answers_when_the_upstream_is_closed(self):
        dead_port = free_port()  # nothing is listening here
        document_updates = {
            "proxy": {
                "enabled": True,
                "upstream_base": "http://127.0.0.1:%d" % (dead_port,),
            }
        }
        config, _daemon, daemon_port = self.start_daemon(document_updates)

        proxy = ProxyServer(config, self.ledger)
        proxy_port = proxy.port
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5.0)
        self.addCleanup(proxy.shutdown)

        connection = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=20)
        try:
            body = json.dumps({"model": "sarvam-105b", "messages": []}).encode("utf-8")
            connection.request(
                "POST",
                "/v1/chat/completions",
                body=body,
                headers={"Content-Type": "application/json", "Connection": "close"},
            )
            response = connection.getresponse()
            status = response.status
            payload = json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()

        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["code"], "meter_upstream_error")
        self.assertEqual(db_events(self.db_path), [], "a failed dial records nothing")

        status, body = self.get(daemon_port, "/v1/status")
        self.assertEqual(status, 200, body)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(set(payload), TOP_KEYS)
        self.assertEqual(payload["sarvam"]["calls"]["today"], 0)

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
        _config, _daemon, daemon_port = self.start_daemon(document_updates)

        # Force one poll so the assertion does not race the refresh thread.
        status, body = self.post(daemon_port, "/v1/refresh")
        self.assertEqual(status, 200, body)

        status, body = self.get(daemon_port, "/v1/status")
        self.assertEqual(status, 200, body)
        payload = json.loads(body.decode("utf-8"))
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
        document["daemon"]["port"] = free_port()
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2)
        loaded = config_mod.load_config(path)
        config = dataclasses.replace(
            loaded, daemon=dataclasses.replace(loaded.daemon, port=0)
        )

        # A ledger that cannot open must not stop the daemon from serving.
        daemon = Daemon(config, logger=lambda line: None)
        self.addCleanup(daemon.shutdown)
        self.assertIsNotNone(daemon.ledger)
        port = daemon.start()

        status, body = self.get(port, "/v1/status")
        self.assertEqual(status, 200, body)
        payload = json.loads(body.decode("utf-8"))
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
