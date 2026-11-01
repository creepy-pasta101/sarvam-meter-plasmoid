# SPDX-License-Identifier: MIT
"""Tests for the ``config.set`` / ``config.get`` ops (unit D: let the plasmoid
configure the daemon).

Every request goes over the daemon's ``AF_UNIX`` socket, exactly like
``tests/test_daemon.py``: :func:`sarvam_meter.client.call` for the ordinary
round trips, and one raw request line for the tests that pin the reply envelope.
What is pinned down here:

* a valid *partial* update writes the file, answers ``ok`` and is visible on
  ``config.get``;
* a key outside :data:`sarvam_meter.daemon.WRITABLE_SECTIONS` (and an unknown
  section) is rejected with ``invalid_config`` naming it, and the config file is
  left byte-identical;
* values are bounded by the same rules ``load_config`` uses
  (``dashboard.poll_seconds`` ``120..21600``, ``daemon.refresh_seconds >= 5``);
* the file is mode ``0600`` after a write and no temp file survives;
* the in-memory config the daemon serves afterwards matches the file, the
  dashboard cache is dropped and the poll interval is re-read each cycle;
* neither the management key nor any request value is ever echoed back.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import tempfile
import threading
import unittest
from unittest import mock

from sarvam_meter import client as client_module
from sarvam_meter import config as config_mod
from sarvam_meter.client import ClientError
from sarvam_meter.daemon import (
    ConfigUpdateError,
    Daemon,
    WRITABLE_SECTIONS,
    sanitized_config,
)

#: A management key that must never appear in a response body or a log line.
SECRET = "mgmt-key-do-not-leak-4321"
#: A value the tests send and then assert is *not* echoed back.
MARKER = "MARKER-VALUE-DO-NOT-ECHO"


class ConfigEndpointTestCase(unittest.TestCase):
    """A daemon serving a throwaway config file, plus request helpers."""

    #: Extra config sections/values applied to the fixture document.
    sections = {}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sarvam-config-endpoint-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

        # The environment must not override the fixture (an exported
        # SARVAM_METER_CLIPROXY_KEY would replace the key under test).
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(config_mod.ENV_CLIPROXY_KEY, None)
        os.environ.pop(config_mod.ENV_CONFIG_PATH, None)

        self.path = os.path.join(self.tmp, "config.json")
        document = config_mod.default_config_dict()
        document["db_path"] = os.path.join(self.tmp, "usage.db")
        document["dashboard"]["session_file"] = os.path.join(self.tmp, "session.json")
        document["cliproxy"]["management_key"] = SECRET
        for section, values in self.sections.items():
            document[section].update(values)
        self.write_document(document)

        self.config = config_mod.load_config(self.path)
        self.logs = []
        self.socket_path = os.path.join(self.tmp, "run", "daemon.sock")
        self.daemon = Daemon(
            self.config,
            logger=self.logs.append,
            config_path=self.path,
            socket_path=self.socket_path,
        )
        self.bound = self.daemon.start()
        self.addCleanup(self.daemon.shutdown)

    # -- helpers ---------------------------------------------------------
    def write_document(self, document):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2)
            handle.write("\n")

    def file_bytes(self):
        with open(self.path, "rb") as handle:
            return handle.read()

    def file_document(self):
        with open(self.path, "r", encoding="utf-8") as handle:
            return json.load(handle)

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

    def get_config(self):
        return self.call("config.get")

    def post_config(self, body):
        """``config.set`` with *body*; raises :class:`ClientError` when refused."""
        return self.call("config.set", config=body)

    def assert_rejected(self, body, expected_message):
        """``invalid_config`` naming *expected_message*, file untouched."""
        before = self.file_bytes()
        reply = self.send_raw({"op": "config.set", "config": body})
        self.assertIs(reply.get("ok"), False, reply)
        self.assertEqual(set(reply), {"ok", "error"})
        error = reply["error"]
        self.assertEqual(set(error), {"message", "code"})
        self.assertEqual(error["code"], "invalid_config")
        self.assertIn(expected_message, error["message"])
        self.assertEqual(self.file_bytes(), before)
        return error


class PartialUpdateTest(ConfigEndpointTestCase):
    def test_valid_partial_update_writes_and_returns_new_values(self):
        payload = self.post_config(
            {
                "dashboard": {"poll_seconds": 600, "base_url": "https://indus.sarvam.ai"},
                "cliproxy": {"enabled": False, "api_version": "v0"},
                "daemon": {"refresh_seconds": 120},
            }
        )
        self.assertEqual(payload["dashboard"]["poll_seconds"], 600)
        self.assertEqual(payload["cliproxy"]["api_version"], "v0")
        self.assertEqual(payload["daemon"]["refresh_seconds"], 120)

        # The file carries the change, and nothing else moved.
        document = self.file_document()
        self.assertEqual(document["dashboard"]["poll_seconds"], 600)
        self.assertEqual(document["cliproxy"]["api_version"], "v0")
        self.assertEqual(document["daemon"]["refresh_seconds"], 120)
        self.assertEqual(document["version"], 1)
        self.assertEqual(document["sarvam"]["currency"], "INR")
        self.assertEqual(document["cliproxy"]["management_key"], SECRET)

        # ``config.get`` agrees with the ``config.set`` response.
        self.assertEqual(self.get_config(), payload)

    def test_set_response_is_the_sanitised_get_document(self):
        payload = self.post_config({"daemon": {"refresh_seconds": 90}})
        self.assertEqual(self.get_config(), payload)
        self.assertEqual(payload, sanitized_config(self.daemon.config))
        self.assertEqual(
            set(payload),
            {"version", "db_path", "daemon", "sarvam", "cliproxy", "dashboard"},
        )

    def test_one_key_update_leaves_the_others_alone(self):
        before = self.file_document()
        payload = self.post_config({"daemon": {"refresh_seconds": 240}})
        after = self.file_document()
        self.assertEqual(after["daemon"]["refresh_seconds"], 240)
        for section in ("sarvam", "cliproxy", "dashboard"):
            self.assertEqual(after[section], before[section], section)
        self.assertEqual(payload["dashboard"]["poll_seconds"], before["dashboard"]["poll_seconds"])

    def test_empty_object_is_a_valid_no_op(self):
        payload = self.post_config({})
        self.assertEqual(payload["dashboard"]["poll_seconds"], 300)

    def test_apply_config_accepts_a_dict_directly(self):
        payload = self.daemon.apply_config({"dashboard": {"poll_seconds": 900}})
        self.assertEqual(payload["dashboard"]["poll_seconds"], 900)
        self.assertEqual(self.daemon.config.dashboard.poll_seconds, 900)
        with self.assertRaises(ConfigUpdateError) as raised:
            self.daemon.apply_config({"cliproxy": {"management_key": "nope"}})
        self.assertIn("cliproxy.management_key is not writable", str(raised.exception))
        self.assertEqual(self.daemon.config.dashboard.poll_seconds, 900)


class RejectedKeyTest(ConfigEndpointTestCase):
    def test_management_key_is_not_writable(self):
        self.assert_rejected(
            {"cliproxy": {"management_key": "attacker-supplied"}},
            "cliproxy.management_key is not writable",
        )
        # The secret on disk is untouched, and the update did not half-apply.
        self.assertEqual(self.file_document()["cliproxy"]["management_key"], SECRET)

    def test_sarvam_section_is_not_writable(self):
        self.assert_rejected(
            {"sarvam": {"starting_balance": 999.0}},
            "sarvam.starting_balance is not writable",
        )

    def test_typo_in_a_writable_section_is_rejected(self):
        self.assert_rejected(
            {"dashboard": {"poll_secondz": 300}},
            "dashboard.poll_secondz is not writable",
        )

    def test_unknown_section_is_rejected(self):
        self.assert_rejected(
            {"daemons": {"refresh_seconds": 60}}, "daemons is not writable"
        )
        self.assert_rejected({"version": 1}, "version is not writable")

    def test_partial_application_never_happens(self):
        # A valid key and an invalid one in the same request: nothing applies.
        before = self.file_bytes()
        payload = self.assert_rejected(
            {"daemon": {"refresh_seconds": 90}, "cliproxy": {"quota_fetch": False}},
            "cliproxy.quota_fetch is not writable",
        )
        self.assertNotIn("daemon.refresh_seconds", payload["message"])
        self.assertEqual(self.file_bytes(), before)
        self.assertEqual(self.daemon.config.daemon.refresh_seconds, 60)
        self.assertEqual(self.get_config()["daemon"]["refresh_seconds"], 60)

    def test_section_that_is_not_an_object_is_rejected(self):
        self.assert_rejected(
            {"dashboard": "https://example.com"}, "dashboard: must be a JSON object"
        )


class OutOfRangeTest(ConfigEndpointTestCase):
    def test_poll_seconds_below_the_floor(self):
        self.assert_rejected(
            {"dashboard": {"poll_seconds": 60}},
            "dashboard.poll_seconds: must be >= 120",
        )

    def test_poll_seconds_above_the_ceiling(self):
        self.assert_rejected(
            {"dashboard": {"poll_seconds": 21601}},
            "dashboard.poll_seconds: must be <= 21600",
        )

    def test_poll_seconds_bounds_are_inclusive(self):
        for value in (120, 21600):
            payload = self.post_config({"dashboard": {"poll_seconds": value}})
            self.assertEqual(payload["dashboard"]["poll_seconds"], value)

    def test_refresh_seconds_below_the_floor(self):
        self.assert_rejected(
            {"daemon": {"refresh_seconds": 4}},
            "daemon.refresh_seconds: must be >= 5",
        )

    def test_wrong_types_are_rejected_by_the_config_rules(self):
        self.assert_rejected(
            {"dashboard": {"enabled": "yes"}},
            "dashboard.enabled: must be true or false",
        )
        self.assert_rejected(
            {"dashboard": {"poll_seconds": "300"}},
            "dashboard.poll_seconds: must be an integer",
        )
        self.assert_rejected(
            {"cliproxy": {"api_version": "v9"}},
            "cliproxy.api_version: must be one of",
        )
        self.assert_rejected(
            {"dashboard": {"base_url": "http://remote.example.com"}},
            "dashboard.base_url: must be an https:// URL",
        )
        self.assert_rejected(
            {"dashboard": {"session_file": "   "}},
            "dashboard.session_file: must not be empty",
        )


class MalformedRequestTest(ConfigEndpointTestCase):
    def test_config_get_still_works(self):
        self.assertEqual(self.get_config()["version"], 1)

    def test_malformed_requests_are_rejected(self):
        before = self.file_bytes()
        # A request with no ``config`` member at all.
        with self.assertRaises(ClientError) as raised:
            self.call("config.set")
        self.assertEqual(raised.exception.code, "invalid_config")
        self.assertEqual(
            raised.exception.message, 'request must carry a "config" object'
        )
        # ... and a ``config`` that is not a JSON object.  A string is decoded
        # as JSON first (it is what a caller who already encoded the document
        # sends), so it is refused as *invalid JSON*, not as a non-object.
        cases = (
            ([1, 2, 3], "request body must be a JSON object"),
            (None, "request body must be a JSON object"),
            (5, "request body must be a JSON object"),
            (True, "request body must be a JSON object"),
            ("a string", "request body is not valid JSON"),
        )
        for value, expected in cases:
            with self.assertRaises(ClientError) as raised:
                self.post_config(value)
            self.assertEqual(raised.exception.code, "invalid_config", value)
            self.assertEqual(raised.exception.message, expected, value)
        # A line that is not JSON at all never reaches the op.
        reply = self.send_raw(b"not json")
        self.assertIs(reply["ok"], False)
        self.assertEqual(reply["error"]["code"], "bad_request")
        self.assertEqual(self.file_bytes(), before)


class FileModeTest(ConfigEndpointTestCase):
    def test_file_mode_is_0600_after_a_write(self):
        # Start permissive: the write must tighten it, not inherit it.
        os.chmod(self.path, 0o644)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o644)
        self.post_config({"daemon": {"refresh_seconds": 90}})
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_no_temp_file_survives_a_write_or_a_rejection(self):
        self.post_config({"daemon": {"refresh_seconds": 90}})
        with self.assertRaises(ClientError):
            self.post_config({"cliproxy": {"management_key": "nope"}})
        leftovers = [
            name
            for name in os.listdir(self.tmp)
            if name.startswith(".config-") or name.endswith(".tmp")
        ]
        self.assertEqual(leftovers, [])

    def test_rejected_request_does_not_touch_the_mode(self):
        os.chmod(self.path, 0o640)
        self.assert_rejected({"dashboard": {"poll_seconds": 1}}, "dashboard.poll_seconds")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)


class ReloadTest(ConfigEndpointTestCase):
    def test_in_memory_config_matches_the_file(self):
        self.post_config(
            {
                "dashboard": {"poll_seconds": 480, "enabled": True},
                "daemon": {"refresh_seconds": 45},
                "cliproxy": {"base_url": "http://127.0.0.1:9317", "api_version": "v0"},
            }
        )
        self.assertEqual(self.daemon.config, config_mod.load_config(self.path))
        self.assertEqual(self.daemon.config.dashboard.poll_seconds, 480)
        self.assertEqual(self.daemon.config.daemon.refresh_seconds, 45)
        self.assertEqual(self.daemon.config.cliproxy.base_url, "http://127.0.0.1:9317")
        self.assertEqual(self.daemon.config.cliproxy.api_version, "v0")

    def test_poll_interval_is_reread_from_the_current_config(self):
        # The refresh loop reads ``dashboard_ttl`` on every cycle, so this is the
        # value a new interval takes effect through -- no restart involved.
        self.assertEqual(self.daemon.dashboard_ttl, 300)
        self.post_config({"dashboard": {"poll_seconds": 900}})
        self.assertEqual(self.daemon.dashboard_ttl, 900)

    def test_rejected_update_leaves_the_in_memory_config_alone(self):
        before = self.daemon.config
        self.assert_rejected({"dashboard": {"poll_seconds": 10}}, "dashboard.poll_seconds")
        self.assertIs(self.daemon.config, before)

    def test_dashboard_cache_is_dropped(self):
        # Populate the cache through the public path, then change the settings.
        self.daemon.refresh_dashboard()
        self.assertIsNotNone(self.daemon.dashboard_snapshot)
        self.post_config({"daemon": {"refresh_seconds": 90}})
        self.assertIsNone(self.daemon.dashboard_snapshot)
        self.assertFalse(self.daemon.dashboard_snapshot_is_fresh())

    def test_dashboard_cache_is_dropped_when_poll_seconds_change(self):
        self.daemon.refresh_dashboard()
        self.assertIsNotNone(self.daemon.dashboard_snapshot)
        self.post_config({"dashboard": {"poll_seconds": 600}})
        self.assertIsNone(self.daemon.dashboard_snapshot)


class SecretSafetyTest(ConfigEndpointTestCase):
    sections = {"cliproxy": {"enabled": True}}

    def test_response_never_carries_the_management_key(self):
        payload = self.post_config({"cliproxy": {"api_version": "v0", "enabled": True}})
        self.assertNotIn(SECRET, json.dumps(payload))
        self.assertEqual(payload["cliproxy"]["management_key"], "***")

    def test_request_values_are_not_echoed(self):
        reply = self.send_raw(
            {"op": "config.set", "config": {"dashboard": {"bogus": MARKER}}}
        )
        self.assertIs(reply["ok"], False)
        self.assertNotIn(MARKER, json.dumps(reply))
        self.assertEqual(
            reply["error"],
            {"message": "dashboard.bogus is not writable", "code": "invalid_config"},
        )

    def test_rejected_key_value_is_not_echoed(self):
        # The *value* sent for a key that is not writable is dropped: only the
        # key name comes back.  (A value rejected by config.py's own rules may
        # be quoted by config.py's message, which the contract returns verbatim.)
        reply = self.send_raw(
            {"op": "config.set", "config": {"cliproxy": {"management_key": MARKER}}}
        )
        self.assertIs(reply["ok"], False)
        self.assertNotIn(MARKER, json.dumps(reply))
        self.assertEqual(
            reply["error"]["message"], "cliproxy.management_key is not writable"
        )

    def test_log_lines_name_keys_only(self):
        self.post_config({"dashboard": {"poll_seconds": 600}})
        with self.assertRaises(ClientError):
            self.post_config({"cliproxy": {"management_key": MARKER}})
        config_lines = [line for line in self.logs if "config updated" in line]
        self.assertTrue(config_lines)
        for line in config_lines:
            self.assertIn("dashboard.poll_seconds", line)
        for line in self.logs:
            self.assertNotIn(SECRET, line)
            self.assertNotIn(MARKER, line)


class MissingFileTest(unittest.TestCase):
    """A daemon whose config file is absent: the first update creates it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sarvam-config-missing-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        base = os.path.join(self.tmp, "base.json")
        document = config_mod.default_config_dict()
        document["db_path"] = os.path.join(self.tmp, "usage.db")
        document["dashboard"]["session_file"] = os.path.join(self.tmp, "session.json")
        document["dashboard"]["enabled"] = False
        with open(base, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        self.path = os.path.join(self.tmp, "nested", "config.json")
        config = config_mod.load_config(base)
        self.socket_path = os.path.join(self.tmp, "run", "daemon.sock")
        self.daemon = Daemon(
            config,
            logger=lambda _line: None,
            config_path=self.path,
            socket_path=self.socket_path,
        )
        self.bound = self.daemon.start()
        self.addCleanup(self.daemon.shutdown)

    def test_first_update_creates_the_file_at_0600(self):
        payload = client_module.call(
            "config.set",
            socket_path=self.bound,
            timeout=10.0,
            config={"daemon": {"refresh_seconds": 30}},
        )
        self.assertEqual(payload["daemon"]["refresh_seconds"], 30)
        self.assertTrue(os.path.isfile(self.path))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(config_mod.load_config(self.path).daemon.refresh_seconds, 30)


class ConcurrencyTest(ConfigEndpointTestCase):
    sections = {"dashboard": {"enabled": False}}

    def test_concurrent_updates_all_succeed_and_leave_a_valid_file(self):
        statuses = []
        lock = threading.Lock()

        def worker(index):
            outcome = "ok"
            try:
                client_module.call(
                    "config.set",
                    socket_path=self.bound,
                    timeout=20.0,
                    config={"daemon": {"refresh_seconds": 5 + index}},
                )
            except ClientError as exc:  # noqa: BLE001 - reported below
                outcome = str(exc)
            with lock:
                statuses.append(outcome)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(statuses, ["ok"] * 6, statuses)
        # The read-modify-write never interleaved: the file still loads, and the
        # in-memory config matches it.
        self.assertIn(
            config_mod.load_config(self.path).daemon.refresh_seconds, range(5, 11)
        )
        self.assertEqual(self.daemon.config, config_mod.load_config(self.path))


class SanitiserTest(ConfigEndpointTestCase):
    def test_writable_sections_are_the_documented_set(self):
        self.assertEqual(
            {section: tuple(keys) for section, keys in WRITABLE_SECTIONS.items()},
            {
                "dashboard": ("enabled", "base_url", "poll_seconds", "session_file"),
                "cliproxy": ("enabled", "base_url", "api_version"),
                "daemon": ("refresh_seconds",),
            },
        )

    def test_session_value_never_appears(self):
        # The session *file* is a path (already in ``config.get``); the session
        # *value* lives in the file and is never read by this op.
        payload = self.post_config(
            {"dashboard": {"session_file": os.path.join(self.tmp, "session.json")}}
        )
        text = json.dumps(payload)
        self.assertIn("session_file", text)
        self.assertNotIn("session_value", text)
        self.assertEqual(
            payload["dashboard"]["session_file"], os.path.join(self.tmp, "session.json")
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
