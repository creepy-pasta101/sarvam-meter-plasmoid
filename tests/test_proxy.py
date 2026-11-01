# SPDX-License-Identifier: MIT
"""Tests for sarvam_meter.proxy (SPEC section 8).

No test reaches the public internet: the end-to-end tests start a fake upstream
``http.server`` on 127.0.0.1 with port 0 and point the proxy at it.
"""

import dataclasses
import hashlib
import http.client
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sarvam_meter import config as config_mod
from sarvam_meter import ledger as ledger_mod
from sarvam_meter import proxy, rates

SECRET_KEY = "sk-live-DO-NOT-LEAK-0123456789"
SECRET_AUTH = "Bearer DO-NOT-LEAK-AUTHORIZATION-9876543210"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _write_config(tmpdir, upstream_base, **proxy_overrides):
    data = config_mod.default_config_dict()
    data["db_path"] = os.path.join(tmpdir, "usage.db")
    data["proxy"]["upstream_base"] = upstream_base
    data["proxy"]["enabled"] = True
    data["proxy"].update(proxy_overrides)
    path = os.path.join(tmpdir, "config.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    return config_mod.load_config(path)


def _rows(db_path):
    connection = sqlite3.connect(db_path)
    try:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(
            "SELECT * FROM events ORDER BY id")]
    finally:
        connection.close()


class _FakeUpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep test output clean
        return

    def do_GET(self):
        self._serve()

    def do_POST(self):
        self._serve()

    def do_PUT(self):
        self._serve()

    def _read_body(self):
        encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in encoding:
            parts = []
            while True:
                line = self.rfile.readline(65536).strip()
                if not line:
                    break
                size = int(line.split(b";")[0], 16)
                if size == 0:
                    self.rfile.readline()
                    break
                parts.append(self.rfile.read(size))
                self.rfile.read(2)
            return b"".join(parts)
        raw = self.headers.get("Content-Length")
        if not raw:
            return b""
        remaining = int(raw)
        parts = []
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            parts.append(chunk)
        return b"".join(parts)

    def _serve(self):
        body = self._read_body()
        record = {
            "method": self.command,
            "path": self.path,
            "headers": dict(self.headers.items()),
            "body": body,
        }
        self.server.requests.append(record)
        responder = self.server.responder
        if responder is not None:
            responder(self, body)
            return
        payload = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _FakeUpstream:
    """A loopback stand-in for api.sarvam.ai."""

    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstreamHandler)
        self.httpd.daemon_threads = True
        self.httpd.requests = []
        self.httpd.responder = None
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self.thread.start()

    def url(self):
        return "http://127.0.0.1:%d" % self.port

    @property
    def requests(self):
        return self.httpd.requests

    def close(self):
        try:
            self.httpd.shutdown()
        finally:
            self.httpd.server_close()
            self.thread.join(5)


def _json_response(status, payload, extra_headers=None):
    raw = json.dumps(payload).encode("utf-8")

    def responder(handler, body):
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(raw)))
        for name, value in (extra_headers or {}).items():
            handler.send_header(name, value)
        handler.end_headers()
        handler.wfile.write(raw)

    return responder


def _request(port, method, path, body=None, headers=None, timeout=15):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        return response.status, dict(response.getheaders()), data
    finally:
        connection.close()


class _CaptureLogger:
    def __init__(self):
        self.messages = []

    def info(self, fmt, *args):
        self.messages.append(fmt % args if args else fmt)

    def warning(self, fmt, *args):
        self.messages.append(fmt % args if args else fmt)

    def debug(self, fmt, *args):
        self.messages.append(fmt % args if args else fmt)

    def error(self, fmt, *args):
        self.messages.append(fmt % args if args else fmt)


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------

class ClassifyTests(unittest.TestCase):
    def test_chat_paths(self):
        for path in ("/v1/chat/completions", "/v2/chat/completions",
                     "/chat/completions", "/v1/chat/completions?foo=1"):
            info = proxy.classify("POST", path)
            self.assertEqual(info["api"], "chat", path)
            self.assertTrue(info["metered"])
            self.assertFalse(info["stream"])
            self.assertEqual(set(info), {"api", "metered", "stream"})

    def test_stt_paths(self):
        for path in ("/speech-to-text", "/speech-to-text-translate"):
            info = proxy.classify("POST", path)
            self.assertEqual(info["api"], "stt", path)
            self.assertTrue(info["metered"])

    def test_tts_path(self):
        info = proxy.classify("POST", "/text-to-speech")
        self.assertEqual(info["api"], "tts")
        self.assertTrue(info["metered"])

    def test_translate_path(self):
        self.assertEqual(proxy.classify("POST", "/translate")["api"], "translate")

    def test_transliterate_path(self):
        self.assertEqual(proxy.classify("POST", "/transliterate")["api"],
                         "transliterate")

    def test_lid_path(self):
        self.assertEqual(proxy.classify("POST", "/text-lid")["api"], "lid")

    def test_other_paths(self):
        for path in ("/", "/v1/models", "/v1/files", "/health", ""):
            info = proxy.classify("POST", path)
            self.assertEqual(info["api"], "other", path)
            self.assertFalse(info["metered"])

    def test_query_is_ignored(self):
        self.assertEqual(proxy.classify("POST", "/text-to-speech?x=1")["api"], "tts")
        self.assertEqual(proxy.classify("POST", "/v1/chat/completions?stream=true")["api"],
                         "chat")

    def test_method_does_not_change_routing(self):
        self.assertEqual(proxy.classify("GET", "/v1/chat/completions")["api"], "chat")


# ---------------------------------------------------------------------------
# chat_usage
# ---------------------------------------------------------------------------

class ChatUsageTests(unittest.TestCase):
    def test_normal_body(self):
        body = json.dumps({
            "model": "sarvam-105b",
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                      "total_tokens": 150},
        }).encode("utf-8")
        self.assertEqual(proxy.chat_usage(body),
                         {"in_tokens": 100, "out_tokens": 50, "cached_tokens": 0})

    def test_cached_tokens_nested(self):
        body = json.dumps({
            "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                      "prompt_tokens_details": {"cached_tokens": 40}},
        }).encode("utf-8")
        self.assertEqual(proxy.chat_usage(body),
                         {"in_tokens": 100, "out_tokens": 20, "cached_tokens": 40})

    def test_body_without_usage(self):
        body = json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode("utf-8")
        self.assertIsNone(proxy.chat_usage(body))

    def test_garbage_and_empty(self):
        self.assertIsNone(proxy.chat_usage(b""))
        self.assertIsNone(proxy.chat_usage(b"not json"))
        self.assertIsNone(proxy.chat_usage(b"[]"))
        self.assertIsNone(proxy.chat_usage(b'{"usage": "nope"}'))
        self.assertIsNone(proxy.chat_usage(b'{"usage": {}}'))

    def test_truncated_body_keeps_usage(self):
        body = (b'{"choices": [' + b"0" * 500 + b'], "usage": {"prompt_tokens": 7, '
                b'"completion_tokens": 3}}')
        self.assertEqual(proxy.chat_usage(body),
                         {"in_tokens": 7, "out_tokens": 3, "cached_tokens": 0})

    def test_tolerates_unicode(self):
        body = json.dumps({"usage": {"prompt_tokens": 1, "completion_tokens": 1},
                           "note": "\u20b9 emoji \U0001F600"}).encode("utf-8")
        self.assertEqual(proxy.chat_usage(body)["in_tokens"], 1)


# ---------------------------------------------------------------------------
# chat_stream_usage
# ---------------------------------------------------------------------------

class ChatStreamUsageTests(unittest.TestCase):
    def test_usage_in_final_chunk(self):
        lines = [
            'data: {"choices":[{"delta":{"content":"he"}}]}',
            "",
            'data: {"choices":[{"delta":{"content":"llo"}}]}',
            'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":12,'
            '"completion_tokens":8,"total_tokens":20}}',
            "data: [DONE]",
        ]
        self.assertEqual(proxy.chat_stream_usage(lines),
                         {"in_tokens": 12, "out_tokens": 8, "cached_tokens": 0})

    def test_no_usage_chunk(self):
        lines = [
            'data: {"choices":[{"delta":{"content":"hi"}}]}',
            "data: [DONE]",
        ]
        self.assertIsNone(proxy.chat_stream_usage(lines))

    def test_ignores_non_data_lines_and_bad_json(self):
        lines = [
            "event: message",
            ": keep-alive comment",
            "data: {broken json",
            'data: {"usage":{"prompt_tokens":3,"completion_tokens":1}}',
        ]
        self.assertEqual(proxy.chat_stream_usage(lines),
                         {"in_tokens": 3, "out_tokens": 1, "cached_tokens": 0})

    def test_accepts_bytes_lines(self):
        lines = [b'data: {"usage":{"prompt_tokens":5,"completion_tokens":5}}']
        self.assertEqual(proxy.chat_stream_usage(lines),
                         {"in_tokens": 5, "out_tokens": 5, "cached_tokens": 0})

    def test_empty(self):
        self.assertIsNone(proxy.chat_stream_usage([]))
        self.assertIsNone(proxy.chat_stream_usage(None))


# ---------------------------------------------------------------------------
# tts_char_count
# ---------------------------------------------------------------------------

class TtsCharCountTests(unittest.TestCase):
    def test_ascii(self):
        self.assertEqual(proxy.tts_char_count(b'{"text": "hello"}'), 5)

    def test_emoji_counts_code_points_not_bytes(self):
        body = json.dumps({"text": "a\U0001F600b"}).encode("utf-8")
        self.assertEqual(proxy.tts_char_count(body), 3)
        self.assertEqual(len(json.loads(body)["text"].encode("utf-8")), 6)

    def test_non_ascii_devanagari(self):
        text = "\u0928\u092e\u0938\u094d\u0924\u0947"  # "namaste"
        body = json.dumps({"text": text}).encode("utf-8")
        self.assertEqual(proxy.tts_char_count(body), len(text))
        self.assertEqual(proxy.tts_char_count(body), 6)
        self.assertGreater(len(text.encode("utf-8")), len(text))

    def test_rupee_sign_is_one_code_point(self):
        body = json.dumps({"text": "\u20b9\u20b9"}).encode("utf-8")
        self.assertEqual(proxy.tts_char_count(body), 2)

    def test_inputs_list(self):
        body = json.dumps({"inputs": ["ab", "cde"]}).encode("utf-8")
        self.assertEqual(proxy.tts_char_count(body), 5)

    def test_missing_or_bad(self):
        self.assertIsNone(proxy.tts_char_count(b"{}"))
        self.assertIsNone(proxy.tts_char_count(b"not json"))
        self.assertIsNone(proxy.tts_char_count(b""))
        self.assertIsNone(proxy.tts_char_count(b'{"text": 5}'))


# ---------------------------------------------------------------------------
# record_from_exchange (direct, real Ledger)
# ---------------------------------------------------------------------------

class RecordFromExchangeTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sarvam-meter-rec-")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.config = _write_config(self.tmpdir, "http://127.0.0.1:9")
        self.ledger = ledger_mod.Ledger(self.config.db_path)
        self.addCleanup(self.ledger.close)

    def _record(self, method, path, body=b"", status=200, resp=b"", headers=None,
                duration_hint=None):
        proxy.record_from_exchange(
            self.config, self.ledger, method, path, headers or {}, body, status, resp,
            duration_hint=duration_hint,
        )
        return _rows(self.config.db_path)

    def test_chat_with_usage(self):
        resp = json.dumps({
            "model": "sarvam-105b",
            "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                      "prompt_tokens_details": {"cached_tokens": 10}},
        }).encode("utf-8")
        rows = self._record("POST", "/v1/chat/completions", resp=resp)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["source"], "proxy")
        self.assertEqual(row["api"], "chat")
        self.assertEqual(row["model"], "sarvam-105b")
        self.assertEqual(row["unit"], "tokens")
        # in + out; prompt_tokens already include the cached ones
        self.assertEqual(row["quantity"], 150.0)
        self.assertEqual(row["status"], 200)
        self.assertEqual(row["estimated"], 0)
        # (100-10) in * 15 + 10 cached * 5 + 50 out * 60, per million tokens
        self.assertAlmostEqual(row["cost"], 0.0044, places=9)
        expected = rates.cost_for("chat", "sarvam-105b", 90.0, "tokens",
                                  cached_quantity=10.0, out_quantity=50.0)
        self.assertAlmostEqual(row["cost"], round(expected, 6), places=9)
        meta = json.loads(row["meta"])
        self.assertEqual(meta["api"], "chat")
        self.assertEqual(meta["path"], "/v1/chat/completions")
        self.assertEqual(meta["status"], 200)

    def test_chat_without_usage_is_estimated(self):
        rows = self._record("POST", "/v1/chat/completions",
                            resp=b'{"choices": []}')
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["estimated"], 1)
        self.assertIsNone(row["quantity"])
        self.assertEqual(row["cost"], 0.0)
        self.assertEqual(row["unit"], "tokens")

    def test_chat_stream_usage_from_tail(self):
        tail = (
            b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
            b'data: {"usage":{"prompt_tokens":9,"completion_tokens":6}}\n\n'
            b"data: [DONE]\n\n"
        )
        rows = self._record("POST", "/v1/chat/completions", resp=tail)
        self.assertEqual(rows[0]["quantity"], 15.0)
        self.assertEqual(rows[0]["estimated"], 0)

    def test_chat_error_status_records_zero_cost(self):
        rows = self._record("POST", "/v1/chat/completions", status=500,
                            resp=b'{"error": "boom"}')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], 500)
        self.assertEqual(rows[0]["cost"], 0.0)
        self.assertIsNone(rows[0]["quantity"])
        self.assertEqual(rows[0]["estimated"], 1)

    def test_stt_with_duration_hint(self):
        rows = self._record("POST", "/speech-to-text", status=200,
                            resp=b'{"transcript": "hi"}', duration_hint=90.0)
        row = rows[0]
        self.assertEqual(row["api"], "stt")
        self.assertEqual(row["unit"], "seconds")
        self.assertEqual(row["quantity"], 90.0)
        expected = rates.cost_for("stt", None, 90.0, "seconds")
        self.assertAlmostEqual(row["cost"], round(expected, 6), places=9)

    def test_stt_without_duration_is_estimated(self):
        rows = self._record("POST", "/speech-to-text", resp=b"{}")
        self.assertIsNone(rows[0]["quantity"])
        self.assertEqual(rows[0]["estimated"], 1)
        self.assertEqual(rows[0]["cost"], 0.0)

    def test_tts_code_points(self):
        body = json.dumps({"text": "a\U0001F600b"}).encode("utf-8")
        rows = self._record("POST", "/text-to-speech", body=body,
                            resp=b'{"audios": []}')
        row = rows[0]
        self.assertEqual(row["api"], "tts")
        self.assertEqual(row["unit"], "chars")
        self.assertEqual(row["quantity"], 3.0)
        expected = rates.cost_for("tts", None, 3.0, "chars")
        self.assertAlmostEqual(row["cost"], round(expected, 6), places=9)

    def test_translate_costs_zero(self):
        body = json.dumps({"input": "\u0928\u092e\u0938\u094d\u0924\u0947"}).encode("utf-8")
        rows = self._record("POST", "/translate", body=body, resp=b'{"translated_text":"x"}')
        self.assertEqual(rows[0]["unit"], "chars")
        self.assertEqual(rows[0]["quantity"], float(len("\u0928\u092e\u0938\u094d\u0924\u0947")))
        self.assertEqual(rows[0]["cost"], 0.0)

    def test_transliterate_char_count(self):
        body = json.dumps({"input": ["abc", "de"]}).encode("utf-8")
        rows = self._record("POST", "/transliterate", body=body, resp=b"{}")
        self.assertEqual(rows[0]["quantity"], 5.0)

    def test_lid_counts_one_request(self):
        rows = self._record("POST", "/text-lid", body=b'{"input":"hi"}',
                            resp=b'{"language_code":"en-IN"}')
        self.assertEqual(rows[0]["unit"], "requests")
        self.assertEqual(rows[0]["quantity"], 1.0)

    def test_402_sets_quota_exhausted(self):
        self._record("POST", "/v1/chat/completions", status=402,
                     resp=b'{"error":"insufficient credits"}')
        stamp = self.ledger.get_kv("quota_exhausted_at")
        self.assertIsNotNone(stamp)
        self.assertTrue(str(stamp).endswith("Z"))

    def test_429_is_recorded_without_kv(self):
        rows = self._record("POST", "/v1/chat/completions", status=429, resp=b"{}")
        self.assertEqual(rows[0]["status"], 429)
        self.assertIsNone(self.ledger.get_kv("quota_exhausted_at"))

    def test_unmetered_path_is_not_recorded(self):
        rows = self._record("POST", "/v1/models", resp=b"{}")
        self.assertEqual(rows, [])

    def test_secret_headers_never_reach_meta(self):
        headers = {"api-subscription-key": SECRET_KEY, "authorization": SECRET_AUTH}
        rows = self._record("POST", "/v1/chat/completions", headers=headers,
                            resp=b'{"usage":{"prompt_tokens":1,"completion_tokens":1}}')
        self.assertEqual(len(rows), 1)
        meta = rows[0]["meta"] or ""
        self.assertNotIn(SECRET_KEY, meta)
        self.assertNotIn(SECRET_AUTH, meta)
        self.assertNotIn("DO-NOT-LEAK", meta)
        # the query string must not be smuggled in either
        rows = self._record("POST", "/v1/chat/completions?key=" + SECRET_KEY,
                            resp=b"{}")
        self.assertNotIn(SECRET_KEY, rows[0]["meta"] or "")

    def test_model_falls_back_to_request(self):
        body = json.dumps({"model": "glm5.3"}).encode("utf-8")
        rows = self._record("POST", "/v1/chat/completions", body=body,
                            resp=b'{"usage":{"prompt_tokens":1,"completion_tokens":1}}')
        self.assertEqual(rows[0]["model"], "glm5.3")

    def test_never_raises_on_broken_ledger(self):
        class Broken:
            def record_event(self, **kwargs):
                raise RuntimeError("db gone")

            def set_kv(self, key, value):
                raise RuntimeError("db gone")

        with self.assertLogs("sarvam_meter.proxy", level="WARNING"):
            proxy.record_from_exchange(
                self.config, Broken(), "POST", "/v1/chat/completions", {}, b"", 200,
                b"{}",
            )


# ---------------------------------------------------------------------------
# end-to-end proxy server tests
# ---------------------------------------------------------------------------

class ProxyServerTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sarvam-meter-proxy-")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.upstream = _FakeUpstream()
        self.addCleanup(self.upstream.close)
        base_config = _write_config(self.tmpdir, self.upstream.url())
        self.config = dataclasses.replace(
            base_config,
            proxy=dataclasses.replace(base_config.proxy, port=0),
        )
        self.ledger = ledger_mod.Ledger(self.config.db_path)
        self.addCleanup(self.ledger.close)
        self.logger = _CaptureLogger()
        self.server = proxy.ProxyServer(self.config, self.ledger, logger=self.logger)
        self.port = self.server.port          # binds the ephemeral port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.server.shutdown()
        self.thread.join(5)

    def _rows(self):
        return _rows(self.config.db_path)

    def _wait_rows(self, count=1, timeout=10.0):
        """Wait for the proxy's (post-response) ledger write to land."""
        deadline = time.monotonic() + timeout
        rows = self._rows()
        while len(rows) < count and time.monotonic() < deadline:
            time.sleep(0.01)
            rows = self._rows()
        return rows

    def _assert_no_rows(self):
        time.sleep(0.2)     # give a stray write the chance to appear
        self.assertEqual(self._rows(), [])

    # -- happy paths -------------------------------------------------------
    def test_chat_completion_end_to_end(self):
        self.upstream.httpd.responder = _json_response(200, {
            "model": "sarvam-105b",
            "choices": [{"message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                      "total_tokens": 150},
        })
        body = json.dumps({"model": "sarvam-105b",
                           "messages": [{"role": "user", "content": "hi"}]}).encode()
        status, headers, data = _request(
            self.port, "POST", "/v1/chat/completions", body,
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["model"], "sarvam-105b")

        rows = self._wait_rows(1)
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual(row["source"], "proxy")
        self.assertEqual(row["api"], "chat")
        self.assertEqual(row["model"], "sarvam-105b")
        self.assertEqual(row["unit"], "tokens")
        self.assertEqual(row["quantity"], 150.0)
        self.assertEqual(row["status"], 200)
        # 100 in * 15 + 50 out * 60 per million; output must NOT be priced at the
        # input rate (that would give 0.00225, a 2x undercount).
        self.assertAlmostEqual(row["cost"], 0.0045, places=9)
        expected_cost = rates.cost_for("chat", "sarvam-105b", 100.0, "tokens",
                                       cached_quantity=0.0, out_quantity=50.0)
        self.assertAlmostEqual(row["cost"], round(expected_cost, 6), places=9)
        self.assertGreater(row["cost"], 0.0)

        # the request reached upstream with the right path and body
        self.assertEqual(len(self.upstream.requests), 1)
        forwarded = self.upstream.requests[0]
        self.assertEqual(forwarded["path"], "/v1/chat/completions")
        self.assertEqual(json.loads(forwarded["body"])["model"], "sarvam-105b")
        # Accept-Encoding is rewritten to identity, Host is not the client's
        self.assertEqual(forwarded["headers"].get("Accept-Encoding"), "identity")

    def test_get_is_forwarded_and_not_metered_for_unknown_paths(self):
        self.upstream.httpd.responder = _json_response(200, {"models": []})
        status, _, data = _request(self.port, "GET", "/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), {"models": []})
        self._assert_no_rows()

    def test_tts_counts_code_points(self):
        self.upstream.httpd.responder = _json_response(200, {"audios": []})
        body = json.dumps({"text": "\U0001F600\u0928\u092e"}).encode("utf-8")
        status, _, _ = _request(self.port, "POST", "/text-to-speech", body,
                                {"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        rows = self._wait_rows(1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["api"], "tts")
        self.assertEqual(rows[0]["unit"], "chars")
        self.assertEqual(rows[0]["quantity"], 3.0)

    def test_stt_duration_from_uploaded_wav_header(self):
        self.upstream.httpd.responder = _json_response(200, {"transcript": "hi"})
        wav = _wav_upload(declared_audio_bytes=1000000, byte_rate=32000)
        boundary = b"----sarvammeter"
        body = (
            b"--" + boundary + b"\r\n"
            b'Content-Disposition: form-data; name="file"; filename="a.wav"\r\n'
            b"Content-Type: audio/wav\r\n\r\n"
            + wav + b"\r\n--" + boundary + b"--\r\n"
        )
        status, _, _ = _request(
            self.port, "POST", "/speech-to-text", body,
            {"Content-Type": "multipart/form-data; boundary=" + boundary.decode()},
        )
        self.assertEqual(status, 200)
        rows = self._wait_rows(1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["api"], "stt")
        self.assertEqual(rows[0]["unit"], "seconds")
        self.assertAlmostEqual(rows[0]["quantity"], 31.25, places=6)
        self.assertEqual(rows[0]["estimated"], 0)
        expected = rates.cost_for("stt", None, 31.25, "seconds")
        self.assertAlmostEqual(rows[0]["cost"], round(expected, 6), places=9)

    def test_sse_stream_is_relayed_and_usage_recorded(self):
        lines = [
            b'data: {"choices":[{"delta":{"content":"he"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n',
            b'data: {"usage":{"prompt_tokens":20,"completion_tokens":5}}\n\n',
            b"data: [DONE]\n\n",
        ]

        def responder(handler, body):
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Transfer-Encoding", "chunked")
            handler.end_headers()
            for line in lines:
                handler.wfile.write(b"%x\r\n" % len(line) + line + b"\r\n")
                handler.wfile.flush()
            handler.wfile.write(b"0\r\n\r\n")
            handler.wfile.flush()

        self.upstream.httpd.responder = responder
        body = json.dumps({"model": "sarvam-105b", "stream": True,
                           "messages": [{"role": "user", "content": "hi"}]}).encode()
        status, headers, data = _request(self.port, "POST", "/v1/chat/completions",
                                         body)
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers.get("Content-Type", ""))
        # every line reached the client intact
        for line in lines:
            self.assertIn(line.strip(), data)

        rows = self._wait_rows(1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["api"], "chat")
        self.assertEqual(rows[0]["quantity"], 25.0)
        self.assertEqual(rows[0]["estimated"], 0)

    def test_stream_flag_comes_from_request_body(self):
        self.assertTrue(proxy._wants_stream(b'{"stream": true}'))
        self.assertFalse(proxy._wants_stream(b'{"stream": false}'))
        self.assertFalse(proxy._wants_stream(b"not json"))

    # -- error handling ----------------------------------------------------
    def test_402_sets_quota_exhausted_kv(self):
        self.upstream.httpd.responder = _json_response(402, {"error": "no credits"})
        body = json.dumps({"messages": []}).encode()
        status, _, _ = _request(self.port, "POST", "/v1/chat/completions", body)
        self.assertEqual(status, 402)
        rows = self._wait_rows(1)
        deadline = time.monotonic() + 10.0
        while self.ledger.get_kv("quota_exhausted_at") is None \
                and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNotNone(self.ledger.get_kv("quota_exhausted_at"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], 402)
        self.assertEqual(rows[0]["cost"], 0.0)

    def test_dotdot_path_rejected(self):
        status, headers, data = _request(self.port, "POST", "/v1/../etc/passwd", b"{}")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(data)["error"]["code"], "meter_bad_path")
        self.assertEqual(self.upstream.requests, [])
        self._assert_no_rows()

    def test_absolute_url_path_rejected(self):
        status, _, data = _request(self.port, "POST", "http://evil.example/x", b"{}")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(data)["error"]["code"], "meter_bad_path")

    def test_unsupported_method_returns_405(self):
        for method in ("PUT", "DELETE", "PATCH"):
            status, _, data = _request(self.port, method, "/v1/chat/completions", b"{}")
            self.assertEqual(status, 405, method)
            self.assertEqual(json.loads(data)["error"]["code"],
                             "meter_method_not_allowed")
        self.assertEqual(self.upstream.requests, [])

    def test_upstream_unreachable_returns_502_and_records_nothing(self):
        import socket
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()

        base = _write_config(self.tmpdir, "http://127.0.0.1:%d" % dead_port)
        dead_config = dataclasses.replace(
            base, proxy=dataclasses.replace(base.proxy, port=0))
        dead_server = proxy.ProxyServer(dead_config, self.ledger)
        dead_port_actual = dead_server.port
        thread = threading.Thread(target=dead_server.serve_forever, daemon=True)
        thread.start()
        try:
            status, _, data = _request(dead_port_actual, "POST", "/v1/chat/completions",
                                       b"{}")
        finally:
            dead_server.shutdown()
            thread.join(5)
        self.assertEqual(status, 502)
        payload = json.loads(data)
        self.assertEqual(payload["error"]["message"], "upstream unreachable")
        self.assertEqual(payload["error"]["code"], "meter_upstream_error")
        self._assert_no_rows()

    def test_max_body_bytes_enforced(self):
        base = _write_config(self.tmpdir, self.upstream.url(), max_body_bytes=1024)
        limited = dataclasses.replace(
            base, proxy=dataclasses.replace(base.proxy, port=0))
        server = proxy.ProxyServer(limited, self.ledger)
        port = server.port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, _, data = _request(port, "POST", "/v1/chat/completions",
                                       b"x" * 4096)
        finally:
            server.shutdown()
            thread.join(5)
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(data)["error"]["code"], "meter_body_too_large")
        self.assertEqual(self.upstream.requests, [])
        self._assert_no_rows()

    # -- streaming / secrets ----------------------------------------------
    def test_large_body_is_relayed_intact(self):
        self.upstream.httpd.responder = _json_response(200, {"ok": True})
        payload = os.urandom(1024 * 1024)          # 1 MiB, > HEAD_LIMIT
        digest = hashlib.sha256(payload).hexdigest()
        status, _, _ = _request(self.port, "POST", "/v1/files", payload,
                                {"Content-Type": "application/octet-stream"})
        self.assertEqual(status, 200)
        self.assertEqual(len(self.upstream.requests), 1)
        received = self.upstream.requests[0]["body"]
        self.assertEqual(len(received), len(payload))
        self.assertEqual(hashlib.sha256(received).hexdigest(), digest)

    def test_chunked_request_body_is_streamed(self):
        self.upstream.httpd.responder = _json_response(200, {"ok": True})
        chunks = [b"a" * 1000, b"b" * 2000, b"c" * 3000]
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            connection.request(
                "POST", "/v1/files", body=iter(chunks),
                headers={"Content-Type": "application/octet-stream"},
                encode_chunked=True,
            )
            response = connection.getresponse()
            data = response.read()
        finally:
            connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(data), {"ok": True})
        self.assertEqual(len(self.upstream.requests), 1)
        self.assertEqual(self.upstream.requests[0]["body"], b"".join(chunks))
        # the chunked body was re-chunked upstream, not buffered into one blob
        self.assertIn("chunked",
                      (self.upstream.requests[0]["headers"].get(
                          "Transfer-Encoding") or "").lower())

    def test_metering_buffer_is_bounded(self):
        # the whole point of streaming: only a head of the body is ever kept
        self.assertLessEqual(proxy.HEAD_LIMIT, 64 * 1024)
        self.assertLessEqual(proxy.SSE_TAIL_LIMIT, 64 * 1024)
        self.assertEqual(proxy.CHUNK_SIZE, 64 * 1024)

    def test_secrets_never_reach_ledger_or_logs(self):
        self.upstream.httpd.responder = _json_response(200, {
            "model": "sarvam-105b",
            "usage": {"prompt_tokens": 2, "completion_tokens": 2},
        })
        headers = {
            "Content-Type": "application/json",
            "api-subscription-key": SECRET_KEY,
            "Authorization": SECRET_AUTH,
        }
        body = json.dumps({"model": "sarvam-105b", "messages": []}).encode()
        status, _, _ = _request(self.port, "POST", "/v1/chat/completions", body, headers)
        self.assertEqual(status, 200)

        # the secret must be forwarded upstream unchanged ...
        forwarded = self.upstream.requests[0]["headers"]
        self.assertEqual(forwarded.get("api-subscription-key"), SECRET_KEY)
        self.assertEqual(forwarded.get("Authorization"), SECRET_AUTH)

        # ... but never stored or logged
        rows = self._wait_rows(1)
        self.assertEqual(len(rows), 1)
        blob = json.dumps([{k: r[k] for k in r} for r in rows], default=str)
        self.assertNotIn(SECRET_KEY, blob)
        self.assertNotIn(SECRET_AUTH, blob)
        self.assertNotIn("DO-NOT-LEAK", "".join(self.logger.messages))
        for message in self.logger.messages:
            self.assertNotIn(SECRET_KEY, message)
            self.assertNotIn("Bearer", message)

    def test_ledger_rows_survive_parallel_requests(self):
        self.upstream.httpd.responder = _json_response(200, {
            "model": "sarvam-105b",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })
        body = json.dumps({"model": "sarvam-105b", "messages": []}).encode()
        results = []
        lock = threading.Lock()

        def worker():
            try:
                status, _, _ = _request(self.port, "POST", "/v1/chat/completions", body)
            except Exception as error:  # pragma: no cover - surfaces as failure
                status = repr(error)
            with lock:
                results.append(status)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(results, [200] * 8)
        self.assertEqual(len(self._wait_rows(8)), 8)


def _wav_upload(declared_audio_bytes=1000000, byte_rate=32000, sample_rate=16000):
    """A WAV file whose `data` chunk declares far more bytes than it carries."""
    fmt = (
        (1).to_bytes(2, "little")
        + (1).to_bytes(2, "little")
        + sample_rate.to_bytes(4, "little")
        + byte_rate.to_bytes(4, "little")
        + (2).to_bytes(2, "little")
        + (16).to_bytes(2, "little")
    )
    chunks = b"fmt " + len(fmt).to_bytes(4, "little") + fmt
    chunks += b"data" + declared_audio_bytes.to_bytes(4, "little") + b"\x00" * 128
    return b"RIFF" + (len(chunks) + 4).to_bytes(4, "little") + b"WAVE" + chunks


if __name__ == "__main__":
    unittest.main()
