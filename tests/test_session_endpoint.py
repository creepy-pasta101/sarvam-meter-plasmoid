# SPDX-License-Identifier: MIT
"""Tests for the ``session.set`` and ``session.get`` ops (unit C).

The plasmoid signs the user in to the Sarvam dashboard in a webview and hands
the resulting cookies to the daemon; this op is how they arrive.  Every request
goes over the daemon's ``AF_UNIX`` socket, exactly like
``tests/test_config_endpoint.py``: :func:`sarvam_meter.client.call` for the
ordinary round trips, one raw request line where the reply envelope itself is
the subject.  What is pinned down here:

* a cookie-only capture writes ``{"Cookie": "..."}`` at mode ``0600`` and the
  dashboard client's own :func:`dashboard.load_session` reads it back;
* both headers together, and an authorization-only capture, work the same way;
* the mode is ``0600`` even when the file pre-existed as ``0644``;
* an unknown key, an oversized value, a CR/LF/NUL value and a malformed request
  are rejected with ``invalid_session`` (``bad_request`` for a line that is not
  a JSON object at all) and leave the file byte-identical;
* ``session.get`` reports ``configured``/``path``/``mode``/header *names* and
  never a value -- not even a prefix of one -- and never a value in a log record;
* the cached dashboard snapshot is dropped, and a running-but-failing poller
  picks the new file up on its next tick without a restart.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import stat
import tempfile
import unittest

from sarvam_meter import client as client_module
from sarvam_meter import config as config_mod
from sarvam_meter.client import ClientError
from sarvam_meter.daemon import (
    Daemon,
    SESSION_FIELD_LIMITS,
    SessionUpdateError,
    check_session_request,
    read_session_header_names,
)
from sarvam_meter.dashboard import load_session

#: A cookie value that must never appear in a response body or a log record.
COOKIE = "sid=COOKIE-DO-NOT-LEAK-8f2a91; token=COOKIE-DO-NOT-LEAK-8f2a91"
#: A bearer value with the same rule.
AUTHORIZATION = "Bearer AUTH-DO-NOT-LEAK-77c1"

#: Loopback port nothing listens on: a poll fails fast and never leaves the box.
DEAD_BASE_URL = "http://127.0.0.1:9"


class _ListHandler(logging.Handler):
    """Collect :class:`logging.LogRecord` objects instead of formatting them."""

    def __init__(self, sink):
        super().__init__()
        self.sink = sink

    def emit(self, record):  # noqa: D102 - logging.Handler API
        self.sink.append(record)


class SessionEndpointTestCase(unittest.TestCase):
    """A daemon serving a throwaway config, plus session request helpers."""

    #: Extra ``dashboard`` values applied to the fixture document.
    dashboard = {"enabled": False}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sarvam-session-endpoint-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self.path = os.path.join(self.tmp, "config.json")
        self.session_path = os.path.join(self.tmp, "session.json")
        document = config_mod.default_config_dict()
        document["db_path"] = os.path.join(self.tmp, "usage.db")
        document["dashboard"]["session_file"] = self.session_path
        document["dashboard"].update(self.dashboard)
        self.write_document(document)

        self.config = config_mod.load_config(self.path)
        self.logs = []
        self.records = []
        self.socket_path = os.path.join(self.tmp, "run", "daemon.sock")
        self.daemon = Daemon(
            self.config,
            logger=self.make_logger(),
            config_path=self.path,
            socket_path=self.socket_path,
        )
        self.bound = self.daemon.start()
        self.addCleanup(self.daemon.shutdown)

    # -- helpers ---------------------------------------------------------
    def make_logger(self):
        """What ``Daemon`` logs through; the record test overrides this."""
        return self.logs.append

    def write_document(self, document):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2)
            handle.write("\n")

    def file_bytes(self):
        with open(self.session_path, "rb") as handle:
            return handle.read()

    def file_document(self):
        with open(self.session_path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def file_mode(self):
        return stat.S_IMODE(os.stat(self.session_path).st_mode)

    def write_raw_session(self, text):
        with open(self.session_path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def call(self, op, **args):
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
        return json.loads(buffer.split(b"\n", 1)[0].decode("utf-8"))

    def post_session(self, body):
        """``session.set`` with *body*; raises :class:`ClientError` when refused."""
        return self.call("session.set", **body)

    def get_session(self):
        return self.call("session.get")

    def assert_rejected(self, body, expected_message):
        """``invalid_session`` naming *expected_message*, file untouched."""
        before = self.file_bytes() if os.path.exists(self.session_path) else None
        reply = self.send_raw(dict({"op": "session.set"}, **body))
        self.assertIs(reply.get("ok"), False, reply)
        self.assertEqual(set(reply), {"ok", "error"})
        error = reply["error"]
        self.assertEqual(set(error), {"message", "code"})
        self.assertEqual(error["code"], "invalid_session")
        self.assertIn(expected_message, error["message"])
        after = self.file_bytes() if os.path.exists(self.session_path) else None
        self.assertEqual(after, before)
        return error


class CookieOnlyTest(SessionEndpointTestCase):
    def test_cookie_only_writes_the_canonical_key_and_is_readable(self):
        payload = self.post_session({"cookie": COOKIE})
        self.assertEqual(
            payload,
            {"stored": ["cookie"], "path": self.session_path, "mode": "0600"},
        )
        self.assertEqual(set(payload), {"stored", "path", "mode"})

        # The file holds exactly the key the dashboard client reads.
        self.assertTrue(os.path.isfile(self.session_path))
        self.assertEqual(self.file_document(), {"Cookie": COOKIE})
        self.assertEqual(list(self.file_document()), ["Cookie"])
        self.assertEqual(self.file_mode(), 0o600)

        # And the poller's own reader agrees.
        self.assertEqual(load_session(self.session_path), {"cookie": COOKIE})

    def test_authorization_only(self):
        payload = self.post_session({"authorization": AUTHORIZATION})
        self.assertEqual(payload["stored"], ["authorization"])
        self.assertEqual(self.file_document(), {"Authorization": AUTHORIZATION})
        self.assertEqual(load_session(self.session_path), {"authorization": AUTHORIZATION})

    def test_both_headers_together(self):
        payload = self.post_session({"cookie": COOKIE, "authorization": AUTHORIZATION})
        self.assertEqual(payload["stored"], ["cookie", "authorization"])
        self.assertEqual(
            self.file_document(), {"Cookie": COOKIE, "Authorization": AUTHORIZATION}
        )
        self.assertEqual(
            load_session(self.session_path),
            {"cookie": COOKIE, "authorization": AUTHORIZATION},
        )

    def test_second_post_replaces_the_first(self):
        self.post_session({"cookie": COOKIE, "authorization": AUTHORIZATION})
        self.post_session({"cookie": "sid=new"})
        # A header that was not supplied is omitted, not carried over.
        self.assertEqual(self.file_document(), {"Cookie": "sid=new"})
        self.assertEqual(load_session(self.session_path), {"cookie": "sid=new"})

    def test_value_is_stripped_like_the_dashboard_client_strips_it(self):
        self.post_session({"cookie": "  sid=abc  "})
        self.assertEqual(self.file_document(), {"Cookie": "sid=abc"})

    def test_missing_parent_directory_is_created_at_0700(self):
        nested = os.path.join(self.tmp, "nested", "deeper")
        document = config_mod.default_config_dict()
        document["db_path"] = os.path.join(self.tmp, "usage.db")
        document["dashboard"]["session_file"] = os.path.join(nested, "session.json")
        document["dashboard"]["enabled"] = False
        self.write_document(document)
        daemon = Daemon(
            config_mod.load_config(self.path),
            logger=self.logs.append,
            config_path=self.path,
            socket_path=os.path.join(self.tmp, "run", "nested.sock"),
        )
        path = daemon.start()
        self.addCleanup(daemon.shutdown)

        payload = client_module.call(
            "session.set", socket_path=path, timeout=10.0, cookie=COOKIE
        )
        self.assertEqual(payload["path"], os.path.join(nested, "session.json"))
        target = os.path.join(nested, "session.json")
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(nested).st_mode), 0o700)

    def test_no_temp_file_survives_a_write_or_a_rejection(self):
        self.post_session({"cookie": COOKIE})
        with self.assertRaises(ClientError):
            self.post_session({"cookie": COOKIE, "token": "nope"})
        leftovers = [
            name
            for name in os.listdir(self.tmp)
            if name.startswith(".session-") or name.endswith(".tmp")
        ]
        self.assertEqual(leftovers, [])


class FileModeTest(SessionEndpointTestCase):
    def test_mode_is_0600_even_when_the_file_pre_existed_as_0644(self):
        self.write_raw_session('{"Cookie": "old"}\n')
        os.chmod(self.session_path, 0o644)
        self.assertEqual(self.file_mode(), 0o644)
        payload = self.post_session({"cookie": COOKIE})
        self.assertEqual(payload["mode"], "0600")
        self.assertEqual(self.file_mode(), 0o600)
        self.assertEqual(self.file_document(), {"Cookie": COOKIE})

    def test_rejected_request_does_not_touch_the_mode(self):
        self.post_session({"cookie": COOKIE})
        os.chmod(self.session_path, 0o640)
        before = self.file_bytes()
        self.assert_rejected({"cookie": COOKIE, "bogus": "x"}, "bogus is not a session field")
        self.assertEqual(self.file_mode(), 0o640)
        self.assertEqual(self.file_bytes(), before)

    def test_oversized_value_does_not_change_the_mode(self):
        self.post_session({"cookie": COOKIE})
        os.chmod(self.session_path, 0o600)
        self.assert_rejected({"cookie": "a" * (SESSION_FIELD_LIMITS["cookie"] + 1)}, "cookie")
        self.assertEqual(self.file_mode(), 0o600)


class RejectedKeyTest(SessionEndpointTestCase):
    def setUp(self):
        super().setUp()
        self.post_session({"cookie": COOKIE})

    def test_unknown_key_is_rejected_and_names_only_the_key(self):
        payload = self.assert_rejected(
            {"cookie": "sid=attacker", "token": "attacker-supplied"},
            "token is not a session field",
        )
        self.assertEqual(payload["message"], "token is not a session field")
        # The valid key in the same request was not applied.
        self.assertEqual(self.file_document(), {"Cookie": COOKIE})

    def test_capitalised_key_is_not_a_session_field(self):
        self.assert_rejected({"Cookie": COOKIE}, "Cookie is not a session field")

    def test_nested_and_section_keys_are_rejected(self):
        self.assert_rejected(
            {"dashboard": {"cookie": COOKIE}}, "dashboard is not a session field"
        )
        self.assert_rejected({"headers": {"cookie": COOKIE}}, "headers is not a session field")

    def test_empty_object_is_rejected(self):
        self.assert_rejected(
            {}, "at least one of cookie or authorization is required"
        )

    def test_empty_values_count_as_not_supplied(self):
        self.assert_rejected(
            {"cookie": "", "authorization": "   "},
            "at least one of cookie or authorization is required",
        )

    def test_empty_cookie_with_a_real_authorization_stores_only_the_latter(self):
        payload = self.post_session({"cookie": "", "authorization": AUTHORIZATION})
        self.assertEqual(payload["stored"], ["authorization"])
        self.assertEqual(self.file_document(), {"Authorization": AUTHORIZATION})


class ValidationTest(SessionEndpointTestCase):
    def setUp(self):
        super().setUp()
        self.post_session({"cookie": COOKIE})

    def test_oversized_cookie_is_rejected(self):
        limit = SESSION_FIELD_LIMITS["cookie"]
        self.assertEqual(limit, 16 * 1024)
        self.assert_rejected(
            {"cookie": "a" * (limit + 1)},
            "cookie: must be at most %d bytes" % (limit,),
        )

    def test_cookie_at_the_cap_is_accepted(self):
        limit = SESSION_FIELD_LIMITS["cookie"]
        payload = self.post_session({"cookie": "a" * limit})
        self.assertEqual(payload["stored"], ["cookie"])
        self.assertEqual(len(self.file_document()["Cookie"]), limit)

    def test_oversized_authorization_is_rejected(self):
        limit = SESSION_FIELD_LIMITS["authorization"]
        self.assertEqual(limit, 8 * 1024)
        self.assert_rejected(
            {"authorization": "b" * (limit + 1)},
            "authorization: must be at most %d bytes" % (limit,),
        )

    def test_authorization_at_the_cap_is_accepted(self):
        limit = SESSION_FIELD_LIMITS["authorization"]
        self.post_session({"authorization": "b" * limit})
        self.assertEqual(len(self.file_document()["Authorization"]), limit)

    def test_crlf_and_nul_are_rejected(self):
        for value in (
            "sid=abc\r\nX-Injected: 1",
            "sid=abc\nX-Injected: 1",
            "sid=abc\rX-Injected: 1",
            "sid=abc\x00def",
        ):
            self.assert_rejected({"cookie": value}, "cookie: must not contain CR, LF or NUL")
            self.assert_rejected(
                {"authorization": value}, "authorization: must not contain CR, LF or NUL"
            )

    def test_non_string_values_are_rejected(self):
        for value in (5, True, ["a"], {"a": 1}, None):
            self.assert_rejected({"cookie": value}, "cookie: must be a string")

    def test_malformed_requests_are_rejected(self):
        # A line that is not a JSON object never reaches the op, so the daemon
        # answers ``bad_request`` (the transport-level code) instead of
        # ``invalid_session``.  Either way nothing is written.
        before = self.file_bytes() if os.path.exists(self.session_path) else None
        for line in (b"", b"not json", b"[1, 2, 3]", b'"a string"', b"null", b"5"):
            reply = self.send_raw(line)
            self.assertIs(reply["ok"], False, line)
            self.assertEqual(reply["error"]["code"], "bad_request", line)
        # A request with no header at all is a JSON object, so it is the op that
        # refuses it -- with the same message the route used to produce.
        with self.assertRaises(ClientError) as raised:
            self.call("session.set")
        self.assertEqual(raised.exception.code, "invalid_session")
        self.assertEqual(
            raised.exception.message,
            "at least one of cookie or authorization is required",
        )
        after = self.file_bytes() if os.path.exists(self.session_path) else None
        self.assertEqual(after, before)


class GetTest(SessionEndpointTestCase):
    def test_get_works_without_a_session(self):
        payload = self.get_session()
        self.assertFalse(payload["configured"])

    def test_no_session_file_reports_not_configured(self):
        self.assertFalse(os.path.exists(self.session_path))
        payload = self.get_session()
        self.assertEqual(
            payload,
            {
                "configured": False,
                "path": self.session_path,
                "mode": "0000",
                "headers": [],
            },
        )
        self.assertEqual(set(payload), {"configured", "path", "mode", "headers"})

    def test_configured_session_reports_names_but_never_a_value(self):
        self.post_session({"cookie": COOKIE})
        payload = self.get_session()
        self.assertTrue(payload["configured"])
        self.assertEqual(payload["path"], self.session_path)
        self.assertEqual(payload["mode"], "0600")
        self.assertEqual(payload["headers"], ["cookie"])

        text = json.dumps(payload)
        self.assertNotIn(COOKIE, text)
        self.assertNotIn(COOKIE[:8], text)  # not even a prefix of the value
        self.assertNotIn("sid=", text)
        self.assertNotIn("COOKIE-DO-NOT-LEAK", text)

    def test_both_headers_are_reported_in_canonical_order(self):
        self.post_session({"authorization": AUTHORIZATION, "cookie": COOKIE})
        payload = self.get_session()
        self.assertEqual(payload["headers"], ["cookie", "authorization"])
        text = json.dumps(payload)
        self.assertNotIn(AUTHORIZATION, text)
        self.assertNotIn(COOKIE, text)
        self.assertNotIn("Bearer", text)

    def test_reports_the_actual_mode(self):
        self.post_session({"cookie": COOKIE})
        os.chmod(self.session_path, 0o644)
        payload = self.get_session()
        self.assertEqual(payload["mode"], "0644")
        self.assertTrue(payload["configured"])

    def test_headers_agree_with_the_dashboard_reader(self):
        self.post_session({"cookie": COOKIE})
        payload = self.get_session()
        self.assertEqual(payload["headers"], sorted(load_session(self.session_path)))
        self.assertEqual(payload["headers"], read_session_header_names(self.session_path))

    def test_unreadable_file_is_configured_but_holds_no_headers(self):
        self.write_raw_session("this is not json")
        os.chmod(self.session_path, 0o600)
        payload = self.get_session()
        self.assertTrue(payload["configured"])
        self.assertEqual(payload["headers"], [])
        self.assertEqual(payload["mode"], "0600")

    def test_unknown_key_in_the_file_is_not_reported(self):
        self.write_raw_session(
            json.dumps({"Cookie": COOKIE, "Set-Cookie": "other", "Token": "x"})
        )
        os.chmod(self.session_path, 0o600)
        payload = self.get_session()
        self.assertEqual(payload["headers"], ["cookie"])


class CacheTest(SessionEndpointTestCase):
    def test_dashboard_cache_is_dropped_after_a_successful_post(self):
        self.daemon.refresh_dashboard()
        self.assertIsNotNone(self.daemon.dashboard_snapshot)
        self.post_session({"cookie": COOKIE})
        self.assertIsNone(self.daemon.dashboard_snapshot)
        self.assertFalse(self.daemon.dashboard_snapshot_is_fresh())

    def test_a_rejected_post_leaves_the_cache_alone(self):
        self.daemon.refresh_dashboard()
        cached = self.daemon.dashboard_snapshot
        self.assertIsNotNone(cached)
        self.assert_rejected({"cookie": COOKIE, "token": "x"}, "token is not a session field")
        self.assertIs(self.daemon.dashboard_snapshot, cached)
        self.assertTrue(self.daemon.dashboard_snapshot_is_fresh())


class PollPickupTest(SessionEndpointTestCase):
    """A running-but-failing poller uses the new file on its next tick."""

    dashboard = {"enabled": True, "base_url": DEAD_BASE_URL}

    def test_next_poll_reads_the_new_session_file(self):
        # No session file yet: the poll reports ``no_session``.
        before = self.daemon.refresh_dashboard()
        self.assertEqual(before["error"]["code"], "no_session")

        self.post_session({"cookie": COOKIE})

        # Same daemon, same thread, no restart: the very next poll loads the file
        # and gets as far as the (unreachable) network, so the error moved on.
        after = self.daemon.refresh_dashboard()
        self.assertIsNotNone(after["error"])
        self.assertNotEqual(after["error"]["code"], "no_session")
        self.assertNotIn(COOKIE, json.dumps(after))


class SecretSafetyTest(SessionEndpointTestCase):
    def test_no_value_appears_in_any_response(self):
        reply = self.send_raw(
            {"op": "session.set", "cookie": COOKIE, "authorization": AUTHORIZATION}
        )
        self.assertIs(reply["ok"], True)
        text = json.dumps(reply)
        self.assertNotIn(COOKIE, text)
        self.assertNotIn(AUTHORIZATION, text)
        self.assertNotIn("DO-NOT-LEAK", text)
        self.assertEqual(reply["result"]["stored"], ["cookie", "authorization"])

        get_text = json.dumps(self.get_session())
        self.assertNotIn(COOKIE, get_text)
        self.assertNotIn(AUTHORIZATION, get_text)
        self.assertNotIn("DO-NOT-LEAK", get_text)

    def test_rejected_value_is_not_echoed(self):
        reply = self.send_raw({"op": "session.set", "token": COOKIE})
        self.assertIs(reply["ok"], False)
        self.assertNotIn(COOKIE, json.dumps(reply))
        self.assertEqual(
            reply["error"],
            {"message": "token is not a session field", "code": "invalid_session"},
        )

        # An oversized value is named by field only, never quoted.
        reply = self.send_raw(
            {"op": "session.set", "cookie": "Z" * (SESSION_FIELD_LIMITS["cookie"] + 1)}
        )
        self.assertIs(reply["ok"], False)
        self.assertNotIn("ZZZZ", json.dumps(reply))

    def test_log_lines_name_headers_and_path_only(self):
        self.post_session({"cookie": COOKIE, "authorization": AUTHORIZATION})
        with self.assertRaises(ClientError):
            self.post_session({"cookie": COOKIE, "token": "x"})
        lines = [line for line in self.logs if "session stored" in line]
        self.assertTrue(lines)
        for line in lines:
            self.assertIn("cookie", line)
            self.assertIn(self.session_path, line)
        for line in self.logs:
            self.assertNotIn(COOKIE, line)
            self.assertNotIn(AUTHORIZATION, line)
            self.assertNotIn("DO-NOT-LEAK", line)


class LogRecordTest(SessionEndpointTestCase):
    """The same guarantee, asserted on :class:`logging.LogRecord` objects."""

    def make_logger(self):
        logger = logging.getLogger("sarvam-meter-test-session-%d" % (id(self),))
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        handler = _ListHandler(self.records)
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        return logger

    def test_no_secret_in_any_log_record(self):
        self.post_session({"cookie": COOKIE, "authorization": AUTHORIZATION})
        with self.assertRaises(ClientError):
            self.post_session({"cookie": COOKIE, "token": "x"})
        self.get_session()

        self.assertTrue(self.records)
        texts = []
        for record in self.records:
            texts.append(record.getMessage())
            texts.append(str(record.args))
            texts.append(record.name)
        blob = "\n".join(texts)
        self.assertNotIn(COOKIE, blob)
        self.assertNotIn(AUTHORIZATION, blob)
        self.assertNotIn("DO-NOT-LEAK", blob)

        stored = [r.getMessage() for r in self.records if "session stored" in r.getMessage()]
        self.assertEqual(len(stored), 1)
        self.assertIn("cookie, authorization", stored[0])
        self.assertIn(self.session_path, stored[0])


class CheckSessionRequestTest(unittest.TestCase):
    """The validator on its own: a dict in, ``{name: value}`` out."""

    def test_accepts_a_dict_and_a_json_string(self):
        self.assertEqual(check_session_request({"cookie": " sid=a "}), {"cookie": "sid=a"})
        self.assertEqual(
            check_session_request('{"authorization": "Bearer t"}'),
            {"authorization": "Bearer t"},
        )
        self.assertEqual(
            check_session_request(b'{"cookie": "sid=a"}'), {"cookie": "sid=a"}
        )

    def test_rejects_an_unknown_key(self):
        with self.assertRaises(SessionUpdateError) as raised:
            check_session_request({"cookie": "sid=a", "api_key": "nope"})
        self.assertEqual(str(raised.exception), "api_key is not a session field")
        self.assertEqual(raised.exception.code, "invalid_session")

    def test_rejects_an_empty_document(self):
        with self.assertRaises(SessionUpdateError):
            check_session_request({})

    def test_stored_order_is_canonical(self):
        self.assertEqual(
            list(check_session_request({"authorization": "Bearer t", "cookie": "sid=a"})),
            ["cookie", "authorization"],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
