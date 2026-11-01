# SPDX-License-Identifier: MIT
"""Tests for sarvam_meter.cliproxy (SPEC section 9).

Every test injects a fake transport: the suite never opens a network
connection.
"""

import json
import socket
import unittest
import urllib.error
import urllib.parse

from sarvam_meter import cliproxy
from sarvam_meter.cliproxy import (
    CliproxyClient,
    CliproxyError,
    normalize_account,
    normalize_quota,
    worst_fraction,
)

V8 = "/v8/management"
V0 = "/v0/management"
ACCOUNTS_PATH = V8 + "/credentials"
PROVIDERS_PATH = V8 + "/credentials/quota/providers"
FETCH_PATH = V8 + "/credentials/quota/fetch"
APIKEY_PATH = V8 + "/observability/usage/api-keys"

KEY = "sk-test-management-key-do-not-log"


# ---------------------------------------------------------------------------
# fake transport
# ---------------------------------------------------------------------------


class FakeTransport:
    """Records every request and answers from a table.  No sockets involved."""

    def __init__(self):
        self.requests = []
        self.routes = {}
        self.fallback = (404, {}, b'{"error":"not found"}')

    # -- configuration ------------------------------------------------------

    def route(self, method, path, status=200, body=b"", headers=None, exc=None):
        """Register a canned response for ``method`` + exact URL path."""
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.routes[(method.upper(), path)] = (status, dict(headers or {}), body, exc)
        return self

    def route_any(self, path, status=200, body=b"", headers=None, exc=None):
        """Register a response for any HTTP method on ``path``."""
        for method in ("GET", "POST"):
            self.route(method, path, status=status, body=body, headers=headers, exc=exc)
        return self

    # -- transport protocol -------------------------------------------------

    def __call__(self, method, url, headers, body_bytes, timeout):
        parsed = urllib.parse.urlsplit(url)
        self.requests.append(
            {
                "method": method.upper(),
                "url": url,
                "path": parsed.path,
                "headers": dict(headers or {}),
                "body": body_bytes,
                "timeout": timeout,
            }
        )
        entry = self.routes.get((method.upper(), parsed.path))
        if entry is None:
            entry = self.routes.get((method.upper(), "*"))
        if entry is None:
            entry = self.fallback
        status, resp_headers, payload, exc = (entry + (None,))[:4] if len(entry) == 3 else entry
        if exc is not None:
            raise exc
        return status, resp_headers, payload

    # -- helpers ------------------------------------------------------------

    def urls(self):
        return [request["url"] for request in self.requests]

    def paths(self):
        return [request["path"] for request in self.requests]


def make_client(transport, api_version="v8", **kwargs):
    kwargs.setdefault("timeout", 5.0)
    return CliproxyClient(
        "http://127.0.0.1:8317",
        KEY,
        api_version=api_version,
        transport=transport,
        **kwargs
    )


def quota_payload(fraction=0.62, window="5h"):
    return {
        "subscription": {"plan": "Max", "tierName": "Max", "tierId": "max-1"},
        "summary": [
            {
                "key": "credits",
                "label": "Credits left",
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
                        "window": window,
                        "remainingFraction": fraction,
                        "resetTime": "2026-11-01T10:00:00Z",
                        "description": "rolling",
                    }
                ],
            }
        ],
    }


# ---------------------------------------------------------------------------
# transport contract: headers and paths
# ---------------------------------------------------------------------------


class TestAuthHeaders(unittest.TestCase):
    def test_sends_both_auth_headers(self):
        transport = FakeTransport()
        transport.route("GET", ACCOUNTS_PATH, body={"files": []})
        make_client(transport).list_accounts()

        self.assertEqual(len(transport.requests), 1)
        headers = transport.requests[0]["headers"]
        self.assertEqual(headers.get("Authorization"), "Bearer " + KEY)
        self.assertEqual(headers.get("X-Management-Key"), KEY)

    def test_both_headers_on_post_quota_fetch(self):
        transport = FakeTransport()
        transport.route("POST", FETCH_PATH, body=quota_payload())
        make_client(transport).fetch_quota("idx-1", "claude")

        request = transport.requests[0]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["headers"].get("Authorization"), "Bearer " + KEY)
        self.assertEqual(request["headers"].get("X-Management-Key"), KEY)
        self.assertEqual(request["headers"].get("Content-Type"), "application/json")
        self.assertEqual(
            json.loads(request["body"].decode("utf-8")),
            {"auth_index": "idx-1", "provider": "claude"},
        )

    def test_transport_receives_timeout(self):
        transport = FakeTransport()
        transport.route("GET", ACCOUNTS_PATH, body={"files": []})
        make_client(transport, timeout=3.5).list_accounts()
        self.assertEqual(transport.requests[0]["timeout"], 3.5)


class TestNoQueueRequests(unittest.TestCase):
    """The destructive usage-queue endpoints must never be requested."""

    def test_no_requested_url_contains_queue(self):
        transport = FakeTransport()
        transport.route("GET", ACCOUNTS_PATH, body={"files": [{"id": "a", "auth_index": "a",
                                                              "provider": "claude"}]})
        transport.route("GET", PROVIDERS_PATH, body={"providers": ["claude"]})
        transport.route("POST", FETCH_PATH, body=quota_payload())
        transport.route("GET", APIKEY_PATH, body={"claude": {"https://x|k": {"success": 1}}})

        client = make_client(transport)
        client.health()
        client.list_accounts()
        client.quota_providers()
        client.fetch_quota("a", "claude")
        client.api_key_usage()
        client.snapshot()

        self.assertTrue(transport.requests)
        for url in transport.urls():
            self.assertNotIn("queue", url.lower())
            self.assertNotIn("usage-queue", url)
            self.assertNotIn("usage/queue", url)

    def test_v0_snapshot_also_never_requests_queue(self):
        transport = FakeTransport()
        transport.route("GET", V0 + "/auth-files", body={"files": [{"id": "a"}]})
        transport.route("POST", V0 + "/quota/fetch", body=quota_payload())
        transport.route("GET", V0 + "/api-key-usage", body={})

        client = make_client(transport, api_version="v0")
        client.snapshot()
        client.api_key_usage()
        for path in transport.paths():
            self.assertNotIn("queue", path.lower())

    def test_queue_path_is_refused_before_transport(self):
        transport = FakeTransport()
        client = make_client(transport)
        client._paths = dict(client._paths)
        client._paths["accounts"] = "/usage-queue"
        with self.assertRaises(CliproxyError) as ctx:
            client.list_accounts()
        self.assertEqual(ctx.exception.code, "forbidden_endpoint")
        self.assertEqual(transport.requests, [])


# ---------------------------------------------------------------------------
# reachability / error mapping
# ---------------------------------------------------------------------------


class TestReachability(unittest.TestCase):
    def _snapshot(self, exc):
        transport = FakeTransport()
        transport.route_any(ACCOUNTS_PATH, exc=exc)
        return make_client(transport).snapshot()

    def test_connection_refused(self):
        snap = self._snapshot(urllib.error.URLError(ConnectionRefusedError(111, "refused")))
        self.assertFalse(snap["reachable"])
        self.assertEqual(snap["accounts"], [])
        self.assertEqual(len(snap["errors"]), 1)
        self.assertEqual(snap["errors"][0]["source"], "cliproxy")
        self.assertEqual(snap["errors"][0]["code"], "unreachable")
        self.assertTrue(snap["errors"][0]["message"])

    def test_timeout(self):
        snap = self._snapshot(socket.timeout("timed out"))
        self.assertFalse(snap["reachable"])
        self.assertEqual(snap["errors"][0]["code"], "unreachable")

    def test_unexpected_transport_exception_is_unreachable(self):
        snap = self._snapshot(RuntimeError("boom"))
        self.assertFalse(snap["reachable"])
        self.assertEqual(snap["errors"][0]["code"], "unreachable")

    def test_401_is_unauthorized(self):
        transport = FakeTransport()
        transport.route_any(ACCOUNTS_PATH, status=401, body=b'{"error":"unauthorized"}')
        snap = make_client(transport).snapshot()
        self.assertEqual(snap["errors"][0]["code"], "unauthorized")
        self.assertEqual(snap["errors"][0]["source"], "cliproxy")
        self.assertEqual(snap["accounts"], [])

    def test_403_is_unauthorized(self):
        transport = FakeTransport()
        transport.route_any(ACCOUNTS_PATH, status=403, body=b"forbidden")
        snap = make_client(transport).snapshot()
        self.assertEqual(snap["errors"][0]["code"], "unauthorized")

    def test_404_is_endpoint_missing(self):
        transport = FakeTransport()
        transport.route_any(ACCOUNTS_PATH, status=404, body=b"nope")
        snap = make_client(transport).snapshot()
        self.assertEqual(snap["errors"][0]["code"], "endpoint_missing")

    def test_html_body_is_bad_response(self):
        transport = FakeTransport()
        transport.route_any(
            ACCOUNTS_PATH,
            status=200,
            body=b"<!DOCTYPE html><html><body>502 Bad Gateway</body></html>",
            headers={"Content-Type": "text/html"},
        )
        snap = make_client(transport).snapshot()
        self.assertFalse(snap["reachable"])
        self.assertEqual(snap["errors"][0]["code"], "bad_response")

    def test_500_is_http_error(self):
        transport = FakeTransport()
        transport.route_any(ACCOUNTS_PATH, status=500, body=b"oops")
        snap = make_client(transport).snapshot()
        self.assertEqual(snap["errors"][0]["code"], "http_error")

    def test_unconfigured_never_calls_transport(self):
        transport = FakeTransport()
        client = CliproxyClient("http://127.0.0.1:8317", "", transport=transport)
        snap = client.snapshot()
        self.assertFalse(snap["configured"])
        self.assertFalse(snap["reachable"])
        self.assertEqual(snap["accounts"], [])
        self.assertEqual(snap["errors"], [])
        self.assertEqual(transport.requests, [])

    def test_snapshot_shape(self):
        transport = FakeTransport()
        transport.route_any(ACCOUNTS_PATH, body={"files": []})
        snap = make_client(transport).snapshot()
        self.assertEqual(
            sorted(snap.keys()),
            ["accounts", "api_version", "configured", "errors", "reachable"],
        )
        self.assertTrue(snap["configured"])
        self.assertTrue(snap["reachable"])
        self.assertEqual(snap["api_version"], "v8")
        self.assertEqual(snap["accounts"], [])
        self.assertEqual(snap["errors"], [])


class TestHealth(unittest.TestCase):
    def test_health_ok(self):
        transport = FakeTransport()
        transport.route_any(ACCOUNTS_PATH, body={"files": []})
        info = make_client(transport).health()
        self.assertTrue(info["reachable"])
        self.assertTrue(info["ok"])
        self.assertEqual(info["status"], 200)
        self.assertIsNone(info["error"])

    def test_health_401_reachable_but_not_ok(self):
        transport = FakeTransport()
        transport.route_any(ACCOUNTS_PATH, status=401, body=b"")
        info = make_client(transport).health()
        self.assertTrue(info["reachable"])
        self.assertFalse(info["ok"])
        self.assertEqual(info["status"], 401)

    def test_health_unreachable_never_raises(self):
        transport = FakeTransport()
        transport.route_any(ACCOUNTS_PATH, exc=urllib.error.URLError("refused"))
        info = make_client(transport).health()
        self.assertFalse(info["reachable"])
        self.assertTrue(info["error"])


# ---------------------------------------------------------------------------
# account list shapes
# ---------------------------------------------------------------------------


class TestListAccounts(unittest.TestCase):
    RAW = {
        "id": "file-1.json",
        "auth_index": "idx-1",
        "name": "claude-max.json",
        "type": "claude",
        "provider": "claude",
        "label": "work",
        "status": "active",
        "status_message": "ok",
        "disabled": False,
        "unavailable": False,
        "source": "file",
        "size": 1234,
    }

    def _accounts(self, body):
        transport = FakeTransport()
        transport.route_any(ACCOUNTS_PATH, body=body)
        return make_client(transport).list_accounts()

    def test_under_files(self):
        accounts = self._accounts({"files": [self.RAW]})
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["auth_index"], "idx-1")

    def test_under_accounts(self):
        accounts = self._accounts({"accounts": [self.RAW]})
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["id"], "file-1.json")

    def test_bare_array(self):
        accounts = self._accounts([self.RAW])
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["provider"], "claude")

    def test_empty_files_is_empty_list(self):
        self.assertEqual(self._accounts({"files": []}), [])

    def test_empty_object_is_empty_list(self):
        self.assertEqual(self._accounts({}), [])

    def test_v0_uses_auth_files_path(self):
        transport = FakeTransport()
        transport.route_any(V0 + "/auth-files", body={"files": [self.RAW]})
        accounts = make_client(transport, api_version="v0").list_accounts()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(transport.paths(), [V0 + "/auth-files"])

    def test_normalized_account_shape(self):
        account = normalize_account(self.RAW)
        self.assertEqual(
            sorted(account.keys()),
            [
                "auth_index",
                "buckets",
                "error",
                "id",
                "label",
                "name",
                "plan",
                "provider",
                "status",
                "summary",
            ],
        )
        self.assertEqual(account["status"], "active")
        self.assertIsNone(account["error"])
        self.assertEqual(account["buckets"], [])

    def test_status_from_flags_when_status_absent(self):
        self.assertEqual(normalize_account({"disabled": True})["status"], "disabled")
        self.assertEqual(normalize_account({"unavailable": True})["status"], "unavailable")
        self.assertEqual(normalize_account({})["status"], "active")
        self.assertEqual(normalize_account({"status": "cooldown"})["status"], "cooldown")
        self.assertEqual(normalize_account({"status": "error"})["status"], "error")

    def test_auth_index_falls_back_to_id(self):
        account = normalize_account({"id": "only-id"})
        self.assertEqual(account["auth_index"], "only-id")


# ---------------------------------------------------------------------------
# quota normalization
# ---------------------------------------------------------------------------


class TestNormalizeQuota(unittest.TestCase):
    def test_camel_case(self):
        quota = normalize_quota(quota_payload(0.62))
        self.assertEqual(quota["plan"], "Max")
        self.assertEqual(len(quota["buckets"]), 1)
        bucket = quota["buckets"][0]
        self.assertEqual(bucket["window"], "5h")
        self.assertAlmostEqual(bucket["remaining_fraction"], 0.62)
        self.assertFalse(bucket["unknown"])
        self.assertEqual(bucket["reset_at"], "2026-11-01T10:00:00Z")
        self.assertEqual(bucket["description"], "rolling")
        self.assertEqual(quota["server_time_offset_ms"], 0)

    def test_snake_case(self):
        payload = {
            "subscription": {"tier_name": "Pro", "tier_id": "pro-1"},
            "summary": [
                {
                    "key": "requests",
                    "label": "Requests",
                    "value": 42,
                    "unit": "req",
                    "format": "number",
                }
            ],
            "server_time_offset_ms": 17,
            "groups": [
                {
                    "display_name": "Codex",
                    "buckets": [
                        {
                            "window": "1d",
                            "remaining_fraction": 0.4,
                            "reset_time": "2026-11-02T00:00:00+05:30",
                            "description": "daily",
                        }
                    ],
                }
            ],
        }
        quota = normalize_quota(payload)
        self.assertEqual(quota["plan"], "Pro")
        self.assertEqual(quota["server_time_offset_ms"], 17)
        bucket = quota["buckets"][0]
        self.assertAlmostEqual(bucket["remaining_fraction"], 0.4)
        self.assertFalse(bucket["unknown"])
        self.assertEqual(bucket["reset_at"], "2026-11-01T18:30:00Z")
        self.assertEqual(quota["summary"][0]["value"], 42)

    def test_clamping(self):
        payload = {
            "groups": [
                {
                    "buckets": [
                        {"window": "5h", "remainingFraction": 1.75},
                        {"window": "1d", "remainingFraction": -0.25},
                        {"window": "7d", "remainingFraction": 0.5},
                    ]
                }
            ]
        }
        buckets = normalize_quota(payload)["buckets"]
        self.assertEqual([b["remaining_fraction"] for b in buckets], [1.0, 0.0, 0.5])
        self.assertEqual([b["unknown"] for b in buckets], [False, False, False])

    def test_missing_null_and_non_numeric_fractions_are_unknown(self):
        payload = {
            "groups": [
                {
                    "buckets": [
                        {"window": "5h"},
                        {"window": "1d", "remainingFraction": None},
                        {"window": "7d", "remainingFraction": "n/a"},
                        {"window": "30d", "remainingFraction": True},
                    ]
                }
            ]
        }
        buckets = normalize_quota(payload)["buckets"]
        self.assertEqual(len(buckets), 4)
        for bucket in buckets:
            self.assertTrue(bucket["unknown"])
            self.assertIsNone(bucket["remaining_fraction"])
            self.assertNotIn(bucket["remaining_fraction"], (0.0, 1.0))

    def test_reset_time_normalisation(self):
        self.assertEqual(
            normalize_quota(
                {"groups": [{"buckets": [{"resetTime": "2026-11-01T15:30:00+05:30"}]}]}
            )["buckets"][0]["reset_at"],
            "2026-11-01T10:00:00Z",
        )
        self.assertEqual(
            normalize_quota(
                {"groups": [{"buckets": [{"resetTime": "2026-11-01T10:00:00"}]}]}
            )["buckets"][0]["reset_at"],
            "2026-11-01T10:00:00Z",
        )
        # unparseable -> verbatim
        self.assertEqual(
            normalize_quota({"groups": [{"buckets": [{"resetTime": "in a while"}]}]})[
                "buckets"
            ][0]["reset_at"],
            "in a while",
        )
        # absent -> None
        self.assertIsNone(
            normalize_quota({"groups": [{"buckets": [{"window": "5h"}]}]})["buckets"][0][
                "reset_at"
            ]
        )

    def test_summary_preserves_currency_format(self):
        quota = normalize_quota(quota_payload())
        row = quota["summary"][0]
        self.assertEqual(row["format"], "currency")
        self.assertEqual(row["currency"], "USD")
        self.assertEqual(row["value"], 12.5)
        self.assertEqual(row["unit"], "USD")

    def test_summary_currency_is_uppercased(self):
        quota = normalize_quota(
            {"summary": [{"key": "c", "label": "C", "value": 1, "format": "currency",
                          "currency": "usd"}]}
        )
        self.assertEqual(quota["summary"][0]["currency"], "USD")

    def test_garbage_payload_does_not_raise(self):
        self.assertEqual(normalize_quota({})["buckets"], [])
        self.assertEqual(normalize_quota(None)["buckets"], [])
        self.assertEqual(normalize_quota({"groups": "nope"})["buckets"], [])


# ---------------------------------------------------------------------------
# worst_fraction
# ---------------------------------------------------------------------------


class TestWorstFraction(unittest.TestCase):
    def test_min_over_known_buckets(self):
        accounts = [
            {"buckets": [{"remaining_fraction": 0.8, "unknown": False},
                         {"remaining_fraction": 0.3, "unknown": False}]},
            {"buckets": [{"remaining_fraction": 0.55, "unknown": False}]},
        ]
        self.assertAlmostEqual(worst_fraction(accounts), 0.3)

    def test_none_when_nothing_known(self):
        accounts = [
            {"buckets": []},
            {"buckets": [{"remaining_fraction": None, "unknown": True}]},
            {"error": "no quota provider", "buckets": []},
        ]
        self.assertIsNone(worst_fraction(accounts))

    def test_none_for_empty_and_bad_input(self):
        self.assertIsNone(worst_fraction([]))
        self.assertIsNone(worst_fraction(None))

    def test_zero_is_kept(self):
        self.assertEqual(
            worst_fraction([{"buckets": [{"remaining_fraction": 0.0, "unknown": False}]}]),
            0.0,
        )


# ---------------------------------------------------------------------------
# snapshot composition / per-account isolation
# ---------------------------------------------------------------------------


class TestSnapshot(unittest.TestCase):
    def _two_accounts(self):
        return {
            "files": [
                {"id": "a.json", "auth_index": "idx-a", "provider": "claude", "status": "active"},
                {"id": "b.json", "auth_index": "idx-b", "provider": "gemini", "status": "active"},
            ]
        }

    def test_quota_merged_into_accounts(self):
        transport = FakeTransport()
        transport.route_any(ACCOUNTS_PATH, body=self._two_accounts())
        transport.route("POST", FETCH_PATH, body=quota_payload(0.62))
        snap = make_client(transport).snapshot()

        self.assertTrue(snap["reachable"])
        self.assertEqual(len(snap["accounts"]), 2)
        for account in snap["accounts"]:
            self.assertIsNone(account["error"])
            self.assertEqual(len(account["buckets"]), 1)
            self.assertAlmostEqual(account["buckets"][0]["remaining_fraction"], 0.62)
            self.assertEqual(account["plan"], "Max")
            self.assertEqual(account["summary"][0]["format"], "currency")
        self.assertAlmostEqual(worst_fraction(snap["accounts"]), 0.62)

        # provider passed through on the quota fetch body
        posts = [r for r in transport.requests if r["method"] == "POST"]
        self.assertEqual(len(posts), 2)
        bodies = sorted(json.loads(r["body"].decode("utf-8"))["auth_index"] for r in posts)
        self.assertEqual(bodies, ["idx-a", "idx-b"])

    def test_501_only_affects_that_account(self):
        def transport_fn(method, url, headers, body_bytes, timeout):
            transport_fn.calls.append((method, url, body_bytes))
            if url.endswith("/credentials"):
                return 200, {}, json.dumps(self._two_accounts()).encode("utf-8")
            payload = json.loads(body_bytes.decode("utf-8"))
            if payload["auth_index"] == "idx-b":
                return 501, {}, b'{"error":"no quota provider for provider"}'
            return 200, {}, json.dumps(quota_payload(0.45)).encode("utf-8")

        transport_fn.calls = []
        snap = make_client(transport_fn).snapshot()

        self.assertTrue(snap["reachable"])
        self.assertEqual(len(snap["accounts"]), 2)
        self.assertEqual(snap["errors"], [])
        by_index = {a["auth_index"]: a for a in snap["accounts"]}
        self.assertEqual(by_index["idx-b"]["error"], "no quota provider")
        self.assertEqual(by_index["idx-b"]["buckets"], [])
        self.assertIsNone(by_index["idx-a"]["error"])
        self.assertAlmostEqual(by_index["idx-a"]["buckets"][0]["remaining_fraction"], 0.45)
        self.assertAlmostEqual(worst_fraction(snap["accounts"]), 0.45)

    def test_one_account_transport_explosion_is_isolated(self):
        def transport_fn(method, url, headers, body_bytes, timeout):
            if url.endswith("/credentials"):
                return 200, {}, json.dumps(self._two_accounts()).encode("utf-8")
            payload = json.loads(body_bytes.decode("utf-8"))
            if payload["auth_index"] == "idx-a":
                raise RuntimeError("kaboom")
            return 200, {}, json.dumps(quota_payload(0.9)).encode("utf-8")

        snap = make_client(transport_fn).snapshot()
        self.assertTrue(snap["reachable"])
        self.assertEqual(len(snap["accounts"]), 2)
        by_index = {a["auth_index"]: a for a in snap["accounts"]}
        self.assertIn("kaboom", by_index["idx-a"]["error"])
        self.assertEqual(by_index["idx-a"]["buckets"], [])
        self.assertIsNone(by_index["idx-b"]["error"])

    def test_quota_500_is_per_account_error(self):
        transport = FakeTransport()
        transport.route_any(ACCOUNTS_PATH, body={"files": [{"id": "a", "auth_index": "a"}]})
        transport.route("POST", FETCH_PATH, status=500, body=b"boom")
        snap = make_client(transport).snapshot()
        self.assertTrue(snap["reachable"])
        self.assertTrue(snap["accounts"][0]["error"])
        self.assertEqual(snap["accounts"][0]["buckets"], [])
        self.assertIsNone(worst_fraction(snap["accounts"]))

    def test_account_without_auth_index_keeps_error(self):
        transport = FakeTransport()
        transport.route_any(ACCOUNTS_PATH, body={"files": [{"id": ""}]})
        snap = make_client(transport).snapshot()
        self.assertEqual(len(snap["accounts"]), 1)
        self.assertTrue(snap["accounts"][0]["error"])

    def test_unknown_fraction_survives_snapshot(self):
        transport = FakeTransport()
        transport.route_any(ACCOUNTS_PATH, body={"files": [{"auth_index": "a"}]})
        transport.route(
            "POST",
            FETCH_PATH,
            body={"groups": [{"buckets": [{"window": "5h", "remainingFraction": None}]}]},
        )
        snap = make_client(transport).snapshot()
        bucket = snap["accounts"][0]["buckets"][0]
        self.assertTrue(bucket["unknown"])
        self.assertIsNone(bucket["remaining_fraction"])
        self.assertIsNone(worst_fraction(snap["accounts"]))

    def test_snapshot_never_raises_on_garbage_everywhere(self):
        def transport_fn(method, url, headers, body_bytes, timeout):
            return 200, {}, b"\x00\x01 not json at all"

        snap = make_client(transport_fn).snapshot()
        self.assertIsInstance(snap, dict)
        self.assertEqual(snap["errors"][0]["code"], "bad_response")

    def test_management_key_never_appears_in_snapshot(self):
        transport = FakeTransport()
        transport.route_any(ACCOUNTS_PATH, status=401, body=b'{"error":"bad key"}')
        snap = make_client(transport).snapshot()
        self.assertNotIn(KEY, json.dumps(snap))
        self.assertNotIn("***", json.dumps(snap))

    def test_management_key_never_appears_in_errors(self):
        transport = FakeTransport()
        transport.route_any(ACCOUNTS_PATH, exc=urllib.error.URLError("refused"))
        client = make_client(transport)
        snap = client.snapshot()
        self.assertNotIn(KEY, json.dumps(snap))
        self.assertNotIn(KEY, repr(client))
        self.assertIn("***", repr(client))


# ---------------------------------------------------------------------------
# quota providers, api-key usage, versions
# ---------------------------------------------------------------------------


class TestProvidersAndUsage(unittest.TestCase):
    def test_quota_providers(self):
        transport = FakeTransport()
        transport.route_any(PROVIDERS_PATH, body={"providers": ["claude", "gemini"]})
        self.assertEqual(make_client(transport).quota_providers(), ["claude", "gemini"])
        self.assertEqual(transport.paths(), [PROVIDERS_PATH])

    def test_quota_providers_empty_is_not_an_error(self):
        transport = FakeTransport()
        transport.route_any(PROVIDERS_PATH, body={"providers": []})
        self.assertEqual(make_client(transport).quota_providers(), [])

    def test_quota_providers_bare_array(self):
        transport = FakeTransport()
        transport.route_any(PROVIDERS_PATH, body=["claude"])
        self.assertEqual(make_client(transport).quota_providers(), ["claude"])

    def test_v0_providers_path(self):
        transport = FakeTransport()
        transport.route_any(V0 + "/quota/providers", body={"providers": ["claude"]})
        make_client(transport, api_version="v0").quota_providers()
        self.assertEqual(transport.paths(), [V0 + "/quota/providers"])

    def test_api_key_usage(self):
        transport = FakeTransport()
        body = {"claude": {"https://api|key": {"success": 3, "failed": 1,
                                              "recent_requests": []}}}
        transport.route_any(APIKEY_PATH, body=body)
        self.assertEqual(make_client(transport).api_key_usage(), body)

    def test_api_key_usage_missing_endpoint_returns_none(self):
        transport = FakeTransport()
        transport.route_any(APIKEY_PATH, status=404, body=b"")
        self.assertIsNone(make_client(transport).api_key_usage())

    def test_v0_paths(self):
        transport = FakeTransport()
        transport.route_any(V0 + "/auth-files", body={"files": []})
        transport.route_any(V0 + "/quota/providers", body={"providers": []})
        transport.route("POST", V0 + "/quota/fetch", body=quota_payload())
        transport.route_any(V0 + "/api-key-usage", body={})

        client = make_client(transport, api_version="v0")
        client.list_accounts()
        client.quota_providers()
        client.fetch_quota("a")
        client.api_key_usage()
        self.assertEqual(
            transport.paths(),
            [
                V0 + "/auth-files",
                V0 + "/quota/providers",
                V0 + "/quota/fetch",
                V0 + "/api-key-usage",
            ],
        )
        self.assertEqual(client.api_version, "v0")


# ---------------------------------------------------------------------------
# construction guards
# ---------------------------------------------------------------------------


class TestConstruction(unittest.TestCase):
    def test_remote_base_url_refused_by_default(self):
        with self.assertRaises(CliproxyError):
            CliproxyClient("http://10.0.0.5:8317", KEY, transport=FakeTransport())

    def test_remote_base_url_allowed_with_flag(self):
        client = CliproxyClient(
            "http://10.0.0.5:8317", KEY, allow_remote=True, transport=FakeTransport()
        )
        self.assertEqual(client.base_url, "http://10.0.0.5:8317")

    def test_localhost_is_loopback(self):
        for url in ("http://localhost:8317", "http://127.0.0.1:8317",
                    "http://[::1]:8317"):
            client = CliproxyClient(url, KEY, transport=FakeTransport())
            self.assertEqual(client.base_url, url)

    def test_bad_scheme_refused(self):
        with self.assertRaises(CliproxyError):
            CliproxyClient("ftp://127.0.0.1:8317", KEY, transport=FakeTransport())
        with self.assertRaises(CliproxyError):
            CliproxyClient("", KEY, transport=FakeTransport())

    def test_bad_api_version_refused(self):
        with self.assertRaises(CliproxyError):
            CliproxyClient("http://127.0.0.1:8317", KEY, api_version="v9",
                           transport=FakeTransport())

    def test_trailing_slash_normalised(self):
        transport = FakeTransport()
        transport.route_any(ACCOUNTS_PATH, body={"files": []})
        client = CliproxyClient("http://127.0.0.1:8317/", KEY, transport=transport)
        client.list_accounts()
        self.assertEqual(transport.paths(), [ACCOUNTS_PATH])


# ---------------------------------------------------------------------------
# redaction helper
# ---------------------------------------------------------------------------


class TestRedaction(unittest.TestCase):
    def test_secret_headers_are_redacted(self):
        redacted = cliproxy._redact_headers(
            {
                "Authorization": "Bearer " + KEY,
                "X-Management-Key": KEY,
                "api-subscription-key": "sub-key",
                "Accept": "application/json",
            }
        )
        self.assertEqual(redacted["Authorization"], "***")
        self.assertEqual(redacted["X-Management-Key"], "***")
        self.assertEqual(redacted["api-subscription-key"], "***")
        self.assertEqual(redacted["Accept"], "application/json")
        self.assertNotIn(KEY, json.dumps(redacted))


# ---------------------------------------------------------------------------
# default transport (never called in this suite; checked by construction only)
# ---------------------------------------------------------------------------


class TestDefaultTransport(unittest.TestCase):
    def test_default_transport_is_used_when_none_given(self):
        client = CliproxyClient("http://127.0.0.1:8317", KEY)
        self.assertIs(client._transport, cliproxy._default_transport)

    def test_default_transport_does_not_follow_redirects(self):
        handler = cliproxy._NoRedirectHandler()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "http://x/"))

    def test_no_network_touched_in_this_suite(self):
        # Guards the whole module: nothing above may have opened a socket.
        self.assertFalse(hasattr(cliproxy, "requests"))


if __name__ == "__main__":
    unittest.main()
