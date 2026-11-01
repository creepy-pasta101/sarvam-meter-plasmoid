# SPDX-License-Identifier: MIT
"""Tests for ``POST /v1/config`` (unit D: let the plasmoid configure the daemon).

Every request goes over a real loopback socket bound to port ``0``, exactly like
``tests/test_daemon.py``.  What is pinned down here:

* a valid *partial* update writes the file, answers ``200`` and is visible on
  ``GET /v1/config``;
* a key outside :data:`sarvam_meter.daemon.WRITABLE_SECTIONS` (and an unknown
  section) is rejected with ``400`` naming it, and the config file is left
  byte-identical;
* values are bounded by the same rules ``load_config`` uses
  (``dashboard.poll_seconds`` ``120..21600``, ``daemon.refresh_seconds >= 5``);
* the file is mode ``0600`` after a write and no temp file survives;
* the in-memory config the daemon serves afterwards matches the file, the
  dashboard cache is dropped and the poll interval is re-read each cycle;
* neither the management key nor any request value is ever echoed back.
"""

from __future__ import annotations

import dataclasses
import http.client
import json
import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from sarvam_meter import config as config_mod
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


def ephemeral_port(config):
    """Copy of *config* whose daemon binds an ephemeral port (tests only)."""
    return dataclasses.replace(
        config, daemon=dataclasses.replace(config.daemon, port=0)
    )


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

        self.config = ephemeral_port(config_mod.load_config(self.path))
        self.logs = []
        self.daemon = Daemon(
            self.config, logger=self.logs.append, config_path=self.path
        )
        self.port = self.daemon.start()
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

    def request(self, method, path, body=None, send_body=True):
        headers = {}
        payload = None
        if send_body and body is not None:
            payload = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(payload))
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            headers_out = {key.lower(): value for key, value in response.getheaders()}
            return response.status, headers_out, raw
        finally:
            connection.close()

    def get_config(self):
        status, headers, raw = self.request("GET", "/v1/config")
        self.assertEqual(status, 200)
        return headers, raw, json.loads(raw.decode("utf-8"))

    def post_config(self, body):
        status, headers, raw = self.request("POST", "/v1/config", body)
        payload = json.loads(raw.decode("utf-8")) if raw else None
        return status, headers, raw, payload

    def assert_rejected(self, body, expected_message):
        """``400 invalid_config`` naming *expected_message*, file untouched."""
        before = self.file_bytes()
        status, headers, raw, payload = self.post_config(body)
        self.assertEqual(status, 400, raw)
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        self.assertEqual(payload["error"]["code"], "invalid_config")
        self.assertIn(expected_message, payload["error"]["message"])
        self.assertEqual(set(payload), {"error"})
        self.assertEqual(set(payload["error"]), {"message", "code"})
        self.assertEqual(self.file_bytes(), before)
        return payload


class PartialUpdateTest(ConfigEndpointTestCase):
    def test_valid_partial_update_writes_and_returns_new_values(self):
        status, headers, raw, payload = self.post_config(
            {
                "dashboard": {"poll_seconds": 600, "base_url": "https://indus.sarvam.ai"},
                "cliproxy": {"enabled": False, "api_version": "v0"},
                "proxy": {"enabled": True},
                "daemon": {"refresh_seconds": 120},
            }
        )
        self.assertEqual(status, 200, raw)
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(payload["dashboard"]["poll_seconds"], 600)
        self.assertEqual(payload["cliproxy"]["api_version"], "v0")
        self.assertTrue(payload["proxy"]["enabled"])
        self.assertEqual(payload["daemon"]["refresh_seconds"], 120)

        # The file carries the change, and nothing else moved.
        document = self.file_document()
        self.assertEqual(document["dashboard"]["poll_seconds"], 600)
        self.assertEqual(document["cliproxy"]["api_version"], "v0")
        self.assertIs(document["proxy"]["enabled"], True)
        self.assertEqual(document["daemon"]["refresh_seconds"], 120)
        self.assertEqual(document["version"], 1)
        self.assertEqual(document["sarvam"]["currency"], "INR")
        self.assertEqual(document["cliproxy"]["management_key"], SECRET)

        # GET /v1/config agrees with the POST response.
        _headers, _raw, fetched = self.get_config()
        self.assertEqual(fetched, payload)

    def test_post_response_is_the_sanitised_get_document(self):
        status, _headers, raw, payload = self.post_config({"daemon": {"refresh_seconds": 90}})
        self.assertEqual(status, 200)
        _h, _raw_get, fetched = self.get_config()
        self.assertEqual(fetched, payload)
        self.assertEqual(payload, sanitized_config(self.daemon.config))
        self.assertEqual(
            set(payload),
            {"version", "db_path", "daemon", "proxy", "sarvam", "cliproxy", "dashboard"},
        )

    def test_one_key_update_leaves_the_others_alone(self):
        before = self.file_document()
        status, _headers, raw, payload = self.post_config({"proxy": {"enabled": True}})
        self.assertEqual(status, 200, raw)
        after = self.file_document()
        self.assertTrue(after["proxy"]["enabled"])
        for section in ("daemon", "sarvam", "cliproxy", "dashboard"):
            self.assertEqual(after[section], before[section], section)
        self.assertEqual(payload["dashboard"]["poll_seconds"], before["dashboard"]["poll_seconds"])

    def test_empty_object_is_a_valid_no_op(self):
        status, _headers, raw, payload = self.post_config({})
        self.assertEqual(status, 200, raw)
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
        self.assertNotIn("daemon.refresh_seconds", payload["error"]["message"])
        self.assertEqual(self.file_bytes(), before)
        self.assertEqual(self.daemon.config.daemon.refresh_seconds, 60)
        _h, _raw, fetched = self.get_config()
        self.assertEqual(fetched["daemon"]["refresh_seconds"], 60)

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
            status, _headers, raw, payload = self.post_config(
                {"dashboard": {"poll_seconds": value}}
            )
            self.assertEqual(status, 200, raw)
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


class MethodTest(ConfigEndpointTestCase):
    def test_get_still_works(self):
        _headers, _raw, payload = self.get_config()
        self.assertEqual(payload["version"], 1)

    def test_other_verbs_stay_405(self):
        before = self.file_bytes()
        for method in ("PUT", "PATCH", "DELETE"):
            status, _headers, raw = self.request(
                method, "/v1/config", {"daemon": {"refresh_seconds": 90}}
            )
            self.assertEqual(status, 405, raw)
            payload = json.loads(raw.decode("utf-8"))
            self.assertEqual(payload["error"]["code"], "method_not_allowed")
        self.assertEqual(self.file_bytes(), before)

    def test_malformed_bodies_are_rejected(self):
        before = self.file_bytes()
        for body in (b"", b"not json", b"[1, 2, 3]", b'"a string"', b"null"):
            status, _headers, raw, payload = self.post_config(body)
            self.assertEqual(status, 400, (body, raw))
            self.assertEqual(payload["error"]["code"], "invalid_config")
        status, _headers, _raw = self.request("POST", "/v1/config", send_body=False)
        self.assertEqual(status, 400)
        self.assertEqual(self.file_bytes(), before)


class FileModeTest(ConfigEndpointTestCase):
    def test_file_mode_is_0600_after_a_write(self):
        # Start permissive: the write must tighten it, not inherit it.
        os.chmod(self.path, 0o644)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o644)
        status, _headers, raw, _payload = self.post_config({"daemon": {"refresh_seconds": 90}})
        self.assertEqual(status, 200, raw)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_no_temp_file_survives_a_write_or_a_rejection(self):
        self.post_config({"daemon": {"refresh_seconds": 90}})
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
        status, _headers, raw, _payload = self.post_config(
            {
                "dashboard": {"poll_seconds": 480, "enabled": True},
                "daemon": {"refresh_seconds": 45},
                "cliproxy": {"base_url": "http://127.0.0.1:9317", "api_version": "v0"},
            }
        )
        self.assertEqual(status, 200, raw)
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
        status, _headers, raw, _payload = self.post_config({"daemon": {"refresh_seconds": 90}})
        self.assertEqual(status, 200, raw)
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
        status, _headers, raw, payload = self.post_config(
            {"cliproxy": {"api_version": "v0", "enabled": True}}
        )
        self.assertEqual(status, 200)
        self.assertNotIn(SECRET, raw.decode("utf-8"))
        self.assertEqual(payload["cliproxy"]["management_key"], "***")

    def test_request_values_are_not_echoed(self):
        status, _headers, raw, payload = self.post_config({"dashboard": {"bogus": MARKER}})
        self.assertEqual(status, 400)
        self.assertNotIn(MARKER, raw.decode("utf-8"))
        self.assertEqual(
            payload,
            {"error": {"message": "dashboard.bogus is not writable", "code": "invalid_config"}},
        )

    def test_rejected_key_value_is_not_echoed(self):
        # The *value* sent for a key that is not writable is dropped: only the
        # key name comes back.  (A value rejected by config.py's own rules may
        # be quoted by config.py's message, which the contract returns verbatim.)
        status, _headers, raw, payload = self.post_config(
            {"cliproxy": {"management_key": MARKER}}
        )
        self.assertEqual(status, 400)
        self.assertNotIn(MARKER, raw.decode("utf-8"))
        self.assertEqual(
            payload["error"]["message"], "cliproxy.management_key is not writable"
        )

    def test_log_lines_name_keys_only(self):
        self.post_config({"dashboard": {"poll_seconds": 600}})
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
        config = ephemeral_port(config_mod.load_config(base))
        self.daemon = Daemon(config, logger=lambda _line: None, config_path=self.path)
        self.port = self.daemon.start()
        self.addCleanup(self.daemon.shutdown)

    def test_first_update_creates_the_file_at_0600(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            body = json.dumps({"daemon": {"refresh_seconds": 30}}).encode("utf-8")
            connection.request(
                "POST",
                "/v1/config",
                body=body,
                headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
            )
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["daemon"]["refresh_seconds"], 30)
        self.assertTrue(os.path.isfile(self.path))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(config_mod.load_config(self.path).daemon.refresh_seconds, 30)


class ConcurrencyTest(ConfigEndpointTestCase):
    sections = {"dashboard": {"enabled": False}}

    def test_concurrent_updates_all_succeed_and_leave_a_valid_file(self):
        import threading

        statuses = []
        lock = threading.Lock()

        def worker(index):
            status, _headers, raw = self.request(
                "POST", "/v1/config", {"daemon": {"refresh_seconds": 5 + index}}
            )
            with lock:
                statuses.append((status, raw))

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual([status for status, _raw in statuses], [200] * 6, statuses)
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
                "proxy": ("enabled",),
                "daemon": ("refresh_seconds",),
            },
        )

    def test_session_value_never_appears(self):
        # The session *file* is a path (already in GET /v1/config); the session
        # *value* lives in the file and is never read by this endpoint.
        status, _headers, raw, payload = self.post_config(
            {"dashboard": {"session_file": os.path.join(self.tmp, "session.json")}}
        )
        self.assertEqual(status, 200)
        text = raw.decode("utf-8")
        self.assertIn("session_file", text)
        self.assertNotIn("session_value", text)
        self.assertEqual(
            payload["dashboard"]["session_file"], os.path.join(self.tmp, "session.json")
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
