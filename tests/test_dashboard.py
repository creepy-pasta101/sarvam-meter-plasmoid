# SPDX-License-Identifier: MIT
"""Tests for sarvam_meter.dashboard (SPEC section 17).

Every test injects a fake transport: the suite never opens a network connection.
The two committed fixtures under ``tests/fixtures/`` are the source of truth for
payload shapes and for the figures asserted here.
"""

import json
import os
import tempfile
import unittest
import urllib.parse

from sarvam_meter.dashboard import (
    SUMMARY_PATH,
    TIMESERIES_PATH,
    DashboardClient,
    DashboardError,
    load_session,
    parse_summary,
    parse_timeseries,
)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
SUMMARY_FIXTURE = os.path.join(FIXTURES, "dhan_usage_summary_by_model.json")
TIMESERIES_FIXTURE = os.path.join(FIXTURES, "dhan_usage_timeseries_by_product.json")

BASE = "https://indus.sarvam.ai"
COOKIE = "session-cookie-value-0123456789abcdef"
BEARER = "Bearer session-bearer-value-0123456789abcdef"
SESSION = {"cookie": COOKIE, "authorization": BEARER}
SECRETS = (COOKIE, BEARER, "session-bearer-value-0123456789abcdef")


def read_fixture(path):
    """Return the raw bytes of a committed fixture."""
    with open(path, "rb") as handle:
        return handle.read()


def header_value(headers, name):
    """Case-insensitive header lookup on a plain dict; ``None`` when absent."""
    for key, value in (headers or {}).items():
        if str(key).lower() == name.lower():
            return value
    return None


def summary_body(**overrides):
    """A minimal valid summary payload; ``overrides`` replace top-level keys."""
    document = {
        "org_id": "019f37be-0185-7fcb-9063-5392bd2f4e63",
        "range": {"start": "2026-10-02T18:30:00Z", "end": "2026-11-01T18:29:59Z"},
        "group_by": ["model"],
        "subtotal": "1.5",
        "total_discount": "0",
        "total_cost": "1.5",
        "currency": "inr",
        "usage": [{"unit": "token", "quantity": "10"}],
        "groups": [
            {"model": "saaras:v4", "total_cost": "1.0", "currency": "inr"},
            {"model": "sarvam-105b", "total_cost": "0.5", "currency": "inr"},
        ],
    }
    document.update(overrides)
    return document


# ---------------------------------------------------------------------------
# fake transport
# ---------------------------------------------------------------------------


class FakeTransport:
    """Records every request and answers from a table.  No sockets involved."""

    def __init__(self, status=200, body=b"", headers=None, exc=None):
        self.requests = []
        self.routes = {}
        self.handler = None
        self.default = {"status": status, "body": body, "headers": headers, "exc": exc}

    # -- configuration ------------------------------------------------------

    def route(self, method, path, status=200, body=b"", headers=None, exc=None):
        """Register a canned response for ``method`` + exact URL path."""
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.routes[(method.upper(), path)] = {
            "status": status,
            "body": body,
            "headers": dict(headers or {}),
            "exc": exc,
        }
        return self

    def set_handler(self, handler):
        """Install ``handler(method, url, headers, timeout) -> (status, headers, body)``."""
        self.handler = handler
        return self

    # -- transport protocol -------------------------------------------------

    def __call__(self, method, url, headers, timeout):
        parsed = urllib.parse.urlsplit(url)
        self.requests.append(
            {
                "method": method.upper(),
                "url": url,
                "path": parsed.path,
                "query": dict(urllib.parse.parse_qsl(parsed.query)),
                "headers": dict(headers or {}),
                "timeout": timeout,
            }
        )
        if self.handler is not None:
            return self.handler(method, url, headers, timeout)
        entry = self.routes.get((method.upper(), parsed.path))
        if entry is None:
            entry = self.routes.get((method.upper(), "*"))
        if entry is None:
            entry = self.default
        if entry["exc"] is not None:
            raise entry["exc"]
        return entry["status"], dict(entry["headers"] or {}), entry["body"]

    # -- assertions helpers -------------------------------------------------

    def paths(self):
        return [request["path"] for request in self.requests]


# ---------------------------------------------------------------------------
# committed fixtures -- the exact figures (SPEC 17.8)
# ---------------------------------------------------------------------------


class TestSummaryFixture(unittest.TestCase):
    def setUp(self):
        self.parsed = parse_summary(read_fixture(SUMMARY_FIXTURE))

    def test_total_cost_exact(self):
        self.assertEqual(self.parsed["total_cost"], 134.2312311905)

    def test_subtotal_exact(self):
        self.assertEqual(self.parsed["subtotal"], 134.2312311905)

    def test_total_cost_is_float_not_string(self):
        self.assertIsInstance(self.parsed["total_cost"], float)

    def test_currency_normalised_to_upper(self):
        self.assertEqual(self.parsed["currency"], "INR")

    def test_org_id_and_range(self):
        self.assertEqual(self.parsed["org_id"], "019f37be-0185-7fcb-9063-5392bd2f4e63")
        self.assertEqual(
            self.parsed["range"],
            {"start": "2026-10-02T18:30:00Z", "end": "2026-11-01T18:29:59Z"},
        )

    def test_group_by_is_a_string(self):
        self.assertEqual(self.parsed["group_by"], "model")

    def test_four_models_in_payload_order(self):
        self.assertEqual(
            [entry["model"] for entry in self.parsed["by_model"]],
            ["glm5.3-flash", "saaras:v3-realtime", "saaras:v4", "sarvam-105b"],
        )

    def test_model_costs_exact(self):
        self.assertEqual(
            [entry["cost"] for entry in self.parsed["by_model"]],
            [53.388129, 0.1551666667, 74.0498055238, 6.63813],
        )

    def test_by_model_rows_carry_usage(self):
        for entry in self.parsed["by_model"]:
            self.assertIn("usage", entry)
            self.assertIsInstance(entry["usage"], list)

    def test_usage_token_quantity_and_breakdown(self):
        usage = self.parsed["usage"]
        self.assertEqual(len(usage), 2)
        tokens = usage[0]
        self.assertEqual(tokens["unit"], "token")
        self.assertEqual(tokens["quantity"], 13046343.0)
        self.assertIsInstance(tokens["quantity"], float)
        self.assertEqual(
            [(part["unit"], part["quantity"]) for part in tokens["breakdown"]],
            [
                ("cached_token", 11296128.0),
                ("input_token", 1513846.0),
                ("output_token", 236369.0),
            ],
        )

    def test_usage_seconds(self):
        seconds = self.parsed["usage"][1]
        self.assertEqual(seconds["unit"], "second")
        self.assertEqual(seconds["quantity"], 9086.323125)
        self.assertEqual(seconds["breakdown"], [])

    def test_no_timeseries_keys_in_summary(self):
        self.assertNotIn("points", self.parsed)
        self.assertNotIn("granularity", self.parsed)

    def test_accepts_bytes_and_dict_identically(self):
        document = json.loads(read_fixture(SUMMARY_FIXTURE).decode("utf-8"))
        self.assertEqual(parse_summary(document), self.parsed)


class TestTimeseriesFixture(unittest.TestCase):
    def setUp(self):
        self.parsed = parse_timeseries(read_fixture(TIMESERIES_FIXTURE))

    def test_total_cost_exact(self):
        self.assertEqual(self.parsed["total_cost"], 134.2312311907)

    def test_currency_normalised(self):
        self.assertEqual(self.parsed["currency"], "INR")

    def test_group_by_is_product(self):
        self.assertEqual(self.parsed["group_by"], "product")

    def test_granularity(self):
        self.assertEqual(self.parsed["granularity"], "hour")

    def test_twenty_eight_points(self):
        self.assertEqual(len(self.parsed["points"]), 28)

    def test_points_sum_to_total_cost(self):
        total = sum(point["cost"] for point in self.parsed["points"])
        self.assertAlmostEqual(total, 134.2312311907, places=9)

    def test_first_point_shape(self):
        first = self.parsed["points"][0]
        self.assertEqual(first["timestamp"], "2026-10-16T13:00:00Z")
        self.assertEqual(first["cost"], 0.2213968021)
        self.assertEqual(first["event_count"], 6)
        self.assertIsInstance(first["event_count"], int)
        self.assertIsInstance(first["cost"], float)

    def test_points_preserve_payload_order(self):
        stamps = [point["timestamp"] for point in self.parsed["points"]]
        self.assertEqual(stamps, sorted(stamps))

    def test_usage_matches_summary_fixture(self):
        usage = self.parsed["usage"]
        self.assertEqual(usage[0]["quantity"], 13046343.0)
        self.assertEqual(usage[1]["quantity"], 9086.323125)

    def test_no_by_model_key(self):
        self.assertNotIn("by_model", self.parsed)


# ---------------------------------------------------------------------------
# number and shape parsing
# ---------------------------------------------------------------------------


class TestNumberParsing(unittest.TestCase):
    def test_string_numbers_are_parsed_to_float(self):
        parsed = parse_summary(summary_body())
        self.assertEqual(parsed["total_cost"], 1.5)
        self.assertIsInstance(parsed["total_cost"], float)
        self.assertEqual(parsed["subtotal"], 1.5)
        self.assertEqual(parsed["usage"][0]["quantity"], 10.0)
        self.assertEqual(parsed["by_model"][0]["cost"], 1.0)

    def test_json_numbers_are_accepted_too(self):
        parsed = parse_summary(
            summary_body(total_cost=2, subtotal=2, usage=[{"unit": "token", "quantity": 3}])
        )
        self.assertEqual(parsed["total_cost"], 2.0)
        self.assertEqual(parsed["usage"][0]["quantity"], 3.0)

    def test_non_numeric_total_cost_is_bad_response(self):
        with self.assertRaises(DashboardError) as caught:
            parse_summary(summary_body(total_cost="not-a-number"))
        self.assertEqual(caught.exception.code, "bad_response")

    def test_non_numeric_group_cost_is_bad_response(self):
        body = summary_body(groups=[{"model": "saaras:v4", "total_cost": "NaN-ish"}])
        with self.assertRaises(DashboardError) as caught:
            parse_summary(body)
        self.assertEqual(caught.exception.code, "bad_response")

    def test_non_numeric_usage_quantity_is_bad_response(self):
        body = summary_body(usage=[{"unit": "token", "quantity": "many"}])
        with self.assertRaises(DashboardError) as caught:
            parse_summary(body)
        self.assertEqual(caught.exception.code, "bad_response")

    def test_non_numeric_point_cost_is_bad_response(self):
        body = {
            "total_cost": "1.0",
            "currency": "inr",
            "points": [{"timestamp": "2026-10-16T13:00:00Z", "total_cost": "x", "event_count": 1}],
        }
        with self.assertRaises(DashboardError) as caught:
            parse_timeseries(body)
        self.assertEqual(caught.exception.code, "bad_response")

    def test_missing_total_cost_is_bad_response(self):
        body = summary_body()
        del body["total_cost"]
        with self.assertRaises(DashboardError) as caught:
            parse_summary(body)
        self.assertEqual(caught.exception.code, "bad_response")

    def test_json_array_is_bad_response(self):
        with self.assertRaises(DashboardError) as caught:
            parse_summary(b'[{"total_cost": "1.0"}]')
        self.assertEqual(caught.exception.code, "bad_response")

    def test_html_body_is_bad_response(self):
        with self.assertRaises(DashboardError) as caught:
            parse_summary(b"<!doctype html><html><body>blocked</body></html>")
        self.assertEqual(caught.exception.code, "bad_response")

    def test_empty_body_is_bad_response(self):
        with self.assertRaises(DashboardError) as caught:
            parse_summary(b"")
        self.assertEqual(caught.exception.code, "bad_response")

    def test_absent_currency_is_empty_string(self):
        body = summary_body()
        del body["currency"]
        self.assertEqual(parse_summary(body)["currency"], "")

    def test_absent_usage_and_groups_are_empty_lists(self):
        body = summary_body()
        del body["usage"]
        del body["groups"]
        parsed = parse_summary(body)
        self.assertEqual(parsed["usage"], [])
        self.assertEqual(parsed["by_model"], [])


# ---------------------------------------------------------------------------
# session file
# ---------------------------------------------------------------------------


class TestLoadSession(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "session.json")

    def tearDown(self):
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        for name in os.listdir(self.tmpdir):
            os.chmod(os.path.join(self.tmpdir, name), 0o600)
            os.remove(os.path.join(self.tmpdir, name))
        os.rmdir(self.tmpdir)

    def write(self, document, mode=0o600):
        with open(self.path, "w", encoding="utf-8") as handle:
            if isinstance(document, str):
                handle.write(document)
            else:
                json.dump(document, handle)
        os.chmod(self.path, mode)
        return self.path

    def test_reads_both_headers_lowercased(self):
        self.write({"Cookie": COOKIE, "Authorization": BEARER})
        session = load_session(self.path)
        self.assertEqual(session, {"cookie": COOKIE, "authorization": BEARER})

    def test_key_match_is_case_insensitive(self):
        self.write({"COOKIE": COOKIE, "authorization": BEARER})
        session = load_session(self.path)
        self.assertEqual(session["cookie"], COOKIE)
        self.assertEqual(session["authorization"], BEARER)

    def test_other_keys_are_dropped(self):
        self.write({"Cookie": COOKIE, "X-Api-Key": "nope", "password": "nope"})
        session = load_session(self.path)
        self.assertEqual(list(session), ["cookie"])

    def test_only_cookie_is_allowed(self):
        self.write({"Cookie": COOKIE})
        self.assertEqual(load_session(self.path), {"cookie": COOKIE})

    def test_empty_object_has_no_headers(self):
        self.write({})
        self.assertEqual(load_session(self.path), {})

    def test_missing_file_is_no_session(self):
        with self.assertRaises(DashboardError) as caught:
            load_session(os.path.join(self.tmpdir, "absent.json"))
        self.assertEqual(caught.exception.code, "no_session")

    def test_unreadable_file_is_no_session(self):
        self.write({"Cookie": COOKIE}, mode=0o000)
        with self.assertRaises(DashboardError) as caught:
            load_session(self.path)
        self.assertEqual(caught.exception.code, "no_session")

    def test_invalid_json_is_no_session(self):
        self.write("not json at all")
        with self.assertRaises(DashboardError) as caught:
            load_session(self.path)
        self.assertEqual(caught.exception.code, "no_session")

    def test_non_object_json_is_no_session(self):
        self.write(["Cookie", COOKIE])
        with self.assertRaises(DashboardError) as caught:
            load_session(self.path)
        self.assertEqual(caught.exception.code, "no_session")

    def test_empty_path_is_no_session(self):
        with self.assertRaises(DashboardError) as caught:
            load_session("")
        self.assertEqual(caught.exception.code, "no_session")

    def test_error_message_never_echoes_a_value(self):
        self.write({"Cookie": COOKIE, "Authorization": BEARER})
        os.chmod(self.path, 0o000)
        with self.assertRaises(DashboardError) as caught:
            load_session(self.path)
        message = str(caught.exception)
        self.assertNotIn(COOKIE, message)
        self.assertNotIn(BEARER, message)


# ---------------------------------------------------------------------------
# the wire
# ---------------------------------------------------------------------------


class TestRequestShape(unittest.TestCase):
    def client(self, transport):
        return DashboardClient(BASE, SESSION, timeout=12.5, transport=transport)

    def test_summary_url(self):
        transport = FakeTransport(body=summary_body())
        self.client(transport).usage_summary("2026-10-02", "2026-11-01")
        request = transport.requests[0]
        self.assertEqual(request["method"], "GET")
        self.assertEqual(request["path"], SUMMARY_PATH)
        self.assertEqual(request["url"].split("?")[0], BASE + SUMMARY_PATH)
        self.assertEqual(
            request["query"],
            {
                "start": "2026-10-02",
                "end": "2026-11-01",
                "group_by": "model",
                "product": "sarvam-api-platform",
            },
        )

    def test_timeseries_url(self):
        transport = FakeTransport(body={"total_cost": "0", "currency": "inr", "points": []})
        self.client(transport).usage_timeseries("2026-10-02", "2026-11-01")
        request = transport.requests[0]
        self.assertEqual(request["path"], TIMESERIES_PATH)
        self.assertEqual(
            request["query"],
            {
                "start": "2026-10-02",
                "end": "2026-11-01",
                "group_by": "product",
                "product": "sarvam-api-platform",
                "granularity": "auto",
            },
        )

    def test_custom_product_is_sent(self):
        transport = FakeTransport(body=summary_body())
        DashboardClient(BASE, SESSION, product="other-product", transport=transport).usage_summary(
            "2026-10-02", "2026-11-01"
        )
        self.assertEqual(transport.requests[0]["query"]["product"], "other-product")

    def test_both_session_headers_on_the_wire(self):
        transport = FakeTransport(body=summary_body())
        self.client(transport).usage_summary("2026-10-02", "2026-11-01")
        headers = transport.requests[0]["headers"]
        self.assertEqual(header_value(headers, "Cookie"), COOKIE)
        self.assertEqual(header_value(headers, "Authorization"), BEARER)
        self.assertEqual(header_value(headers, "Accept"), "application/json")

    def test_both_session_headers_on_the_timeseries_call(self):
        transport = FakeTransport(body={"total_cost": "0", "points": []})
        self.client(transport).usage_timeseries("2026-10-02", "2026-11-01")
        headers = transport.requests[0]["headers"]
        self.assertEqual(header_value(headers, "Cookie"), COOKIE)
        self.assertEqual(header_value(headers, "Authorization"), BEARER)

    def test_transport_receives_timeout(self):
        transport = FakeTransport(body=summary_body())
        self.client(transport).usage_summary("2026-10-02", "2026-11-01")
        self.assertEqual(transport.requests[0]["timeout"], 12.5)

    def test_bad_date_is_bad_request_and_sends_nothing(self):
        transport = FakeTransport(body=summary_body())
        client = self.client(transport)
        for bad in ("yesterday", "2026-13-01", "2026-1-1", "", "2026/10/02"):
            with self.assertRaises(DashboardError) as caught:
                client.usage_summary(bad, "2026-11-01")
            self.assertEqual(caught.exception.code, "bad_request")
        self.assertEqual(transport.requests, [])

    def test_non_string_date_is_bad_request(self):
        transport = FakeTransport(body=summary_body())
        with self.assertRaises(DashboardError) as caught:
            self.client(transport).usage_summary(None, "2026-11-01")
        self.assertEqual(caught.exception.code, "bad_request")
        self.assertEqual(transport.requests, [])

    def test_base_url_with_credentials_is_refused(self):
        with self.assertRaises(DashboardError) as caught:
            DashboardClient("https://user:pass@indus.sarvam.ai", SESSION, transport=FakeTransport())
        self.assertEqual(caught.exception.code, "config")

    def test_empty_base_url_is_refused(self):
        with self.assertRaises(DashboardError) as caught:
            DashboardClient("", SESSION, transport=FakeTransport())
        self.assertEqual(caught.exception.code, "config")


# ---------------------------------------------------------------------------
# error mapping
# ---------------------------------------------------------------------------


class TestErrorMapping(unittest.TestCase):
    def client(self, transport):
        return DashboardClient(BASE, SESSION, transport=transport)

    def test_401_is_unauthorized(self):
        transport = FakeTransport().route("GET", SUMMARY_PATH, status=401, body=b'{"detail":"no"}')
        with self.assertRaises(DashboardError) as caught:
            self.client(transport).usage_summary("2026-10-02", "2026-11-01")
        self.assertEqual(caught.exception.code, "unauthorized")
        self.assertEqual(caught.exception.status, 401)

    def test_403_is_unauthorized(self):
        transport = FakeTransport().route("GET", SUMMARY_PATH, status=403, body=b"")
        with self.assertRaises(DashboardError) as caught:
            self.client(transport).usage_summary("2026-10-02", "2026-11-01")
        self.assertEqual(caught.exception.code, "unauthorized")
        self.assertEqual(caught.exception.status, 403)

    def test_500_is_http_error(self):
        transport = FakeTransport().route("GET", SUMMARY_PATH, status=500, body=b'{"e":1}')
        with self.assertRaises(DashboardError) as caught:
            self.client(transport).usage_summary("2026-10-02", "2026-11-01")
        self.assertEqual(caught.exception.code, "http_error")
        self.assertEqual(caught.exception.status, 500)

    def test_html_body_with_200_is_bad_response(self):
        transport = FakeTransport().route(
            "GET", SUMMARY_PATH, status=200, body=b"<!doctype html><html>spa</html>"
        )
        with self.assertRaises(DashboardError) as caught:
            self.client(transport).usage_summary("2026-10-02", "2026-11-01")
        self.assertEqual(caught.exception.code, "bad_response")

    def test_empty_body_with_200_is_bad_response(self):
        transport = FakeTransport().route("GET", SUMMARY_PATH, status=200, body=b"")
        with self.assertRaises(DashboardError) as caught:
            self.client(transport).usage_summary("2026-10-02", "2026-11-01")
        self.assertEqual(caught.exception.code, "bad_response")

    def test_transport_exception_is_unreachable(self):
        transport = FakeTransport().route(
            "GET", SUMMARY_PATH, exc=OSError("connection refused")
        )
        with self.assertRaises(DashboardError) as caught:
            self.client(transport).usage_summary("2026-10-02", "2026-11-01")
        self.assertEqual(caught.exception.code, "unreachable")
        self.assertIsNone(caught.exception.status)

    def test_transport_returning_garbage_is_unreachable(self):
        def transport(method, url, headers, timeout):
            raise ValueError("no route to host")

        with self.assertRaises(DashboardError) as caught:
            self.client(transport).usage_summary("2026-10-02", "2026-11-01")
        self.assertEqual(caught.exception.code, "unreachable")

    def test_non_integer_status_is_bad_response(self):
        transport = FakeTransport().set_handler(
            lambda method, url, headers, timeout: ("ok", {}, b"{}")
        )
        with self.assertRaises(DashboardError) as caught:
            self.client(transport).usage_summary("2026-10-02", "2026-11-01")
        self.assertEqual(caught.exception.code, "bad_response")

    def test_str_body_is_accepted(self):
        transport = FakeTransport().route(
            "GET", SUMMARY_PATH, status=200, body=json.dumps(summary_body())
        )
        parsed = self.client(transport).usage_summary("2026-10-02", "2026-11-01")
        self.assertEqual(parsed["total_cost"], 1.5)


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


class TestSnapshot(unittest.TestCase):
    def two_call_transport(self, window_body, today_body, today_status=200):
        """Answer the window call and today's call from separate bodies."""
        if not isinstance(window_body, bytes):
            window_body = json.dumps(window_body).encode("utf-8")
        if not isinstance(today_body, bytes):
            today_body = json.dumps(today_body).encode("utf-8")

        def handler(method, url, headers, timeout):
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
            if query.get("start") == query.get("end"):
                return today_status, {}, today_body
            return 200, {}, window_body

        return FakeTransport().set_handler(handler)

    def window_body(self):
        return summary_body(total_cost="12.5", subtotal="12.5")

    def test_snapshot_shape_and_figures(self):
        transport = self.two_call_transport(self.window_body(), summary_body(total_cost="0.75"))
        snapshot = DashboardClient(BASE, SESSION, transport=transport).snapshot(
            since_date="2026-10-20", today="2026-11-01"
        )
        self.assertTrue(snapshot["configured"])
        self.assertTrue(snapshot["reachable"])
        self.assertIsNone(snapshot["error"])
        self.assertEqual(snapshot["currency"], "INR")
        self.assertEqual(snapshot["org_id"], "019f37be-0185-7fcb-9063-5392bd2f4e63")
        self.assertEqual(snapshot["window"], {"start": "2026-10-20", "end": "2026-11-01"})
        # the fake transport answers every non-window call with the same body
        self.assertEqual(snapshot["spend"], {"window": 12.5, "today": 0.75, "month": 0.75})
        self.assertEqual(
            snapshot["by_model"],
            [{"model": "saaras:v4", "cost": 1.0}, {"model": "sarvam-105b", "cost": 0.5}],
        )
        self.assertEqual(snapshot["usage"][0]["unit"], "token")
        self.assertIsInstance(snapshot["as_of"], str)
        self.assertEqual(
            set(snapshot),
            {
                "configured",
                "reachable",
                "as_of",
                "org_id",
                "currency",
                "window",
                "spend",
                "by_model",
                "usage",
                "error",
            },
        )

    def test_snapshot_makes_three_calls(self):
        transport = self.two_call_transport(self.window_body(), summary_body())
        DashboardClient(BASE, SESSION, transport=transport).snapshot(
            since_date="2026-10-20", today="2026-11-01"
        )
        # window, today, and month-to-date (first of the month .. today)
        self.assertEqual(transport.paths(), [SUMMARY_PATH, SUMMARY_PATH, SUMMARY_PATH])
        self.assertEqual(
            [request["query"]["start"] for request in transport.requests],
            ["2026-10-20", "2026-11-01", "2026-11-01"],
        )

    def test_today_failure_keeps_window_result(self):
        transport = self.two_call_transport(
            self.window_body(), b"", today_status=500
        )
        snapshot = DashboardClient(BASE, SESSION, transport=transport).snapshot(
            since_date="2026-10-20", today="2026-11-01"
        )
        self.assertTrue(snapshot["reachable"])
        self.assertEqual(snapshot["spend"]["window"], 12.5)
        self.assertEqual(snapshot["spend"]["today"], 0.0)
        self.assertEqual(len(snapshot["by_model"]), 2)
        self.assertEqual(snapshot["error"]["source"], "dashboard")
        self.assertEqual(snapshot["error"]["code"], "http_error")

    def test_window_failure_is_reported_and_not_reachable(self):
        transport = FakeTransport().route("GET", SUMMARY_PATH, status=401, body=b"{}")
        snapshot = DashboardClient(BASE, SESSION, transport=transport).snapshot(
            since_date="2026-10-20", today="2026-11-01"
        )
        self.assertFalse(snapshot["reachable"])
        self.assertEqual(snapshot["spend"], {"window": 0.0, "today": 0.0, "month": 0.0})
        self.assertEqual(snapshot["by_model"], [])
        self.assertEqual(snapshot["error"]["code"], "unauthorized")

    def test_window_transport_failure_is_unreachable(self):
        transport = FakeTransport().route("GET", SUMMARY_PATH, exc=OSError("boom"))
        snapshot = DashboardClient(BASE, SESSION, transport=transport).snapshot(
            since_date="2026-10-20", today="2026-11-01"
        )
        self.assertFalse(snapshot["reachable"])
        self.assertEqual(snapshot["error"]["code"], "unreachable")

    def test_window_html_body_is_bad_response(self):
        transport = FakeTransport().route("GET", SUMMARY_PATH, body=b"<html>blocked</html>")
        snapshot = DashboardClient(BASE, SESSION, transport=transport).snapshot(
            since_date="2026-10-20", today="2026-11-01"
        )
        self.assertEqual(snapshot["error"]["code"], "bad_response")

    def test_bad_date_in_snapshot_is_bad_request_not_a_raise(self):
        transport = FakeTransport(body=summary_body())
        snapshot = DashboardClient(BASE, SESSION, transport=transport).snapshot(
            since_date="nonsense", today="2026-11-01"
        )
        self.assertFalse(snapshot["reachable"])
        self.assertEqual(snapshot["error"]["code"], "bad_request")
        self.assertEqual(transport.requests, [])

    def test_unconfigured_client_never_calls_transport(self):
        transport = FakeTransport(body=summary_body())
        snapshot = DashboardClient(BASE, {}, transport=transport).snapshot(
            since_date="2026-10-20", today="2026-11-01"
        )
        self.assertFalse(snapshot["configured"])
        self.assertFalse(snapshot["reachable"])
        self.assertEqual(transport.requests, [])
        self.assertEqual(snapshot["currency"], "INR")

    def test_unknown_session_keys_do_not_configure_the_client(self):
        transport = FakeTransport(body=summary_body())
        client = DashboardClient(BASE, {"x-api-key": "abc"}, transport=transport)
        self.assertFalse(client.configured)
        self.assertFalse(client.snapshot(since_date="2026-10-20", today="2026-11-01")["configured"])

    def test_snapshot_never_raises_on_a_broken_transport(self):
        def transport(method, url, headers, timeout):
            raise RuntimeError("transport exploded")

        snapshot = DashboardClient(BASE, SESSION, transport=transport).snapshot(
            since_date="2026-10-20", today="2026-11-01"
        )
        self.assertEqual(snapshot["error"]["code"], "unreachable")

    def test_snapshot_never_raises_on_odd_shapes(self):
        transport = FakeTransport().route("GET", SUMMARY_PATH, body=b'{"total_cost": []}')
        snapshot = DashboardClient(BASE, SESSION, transport=transport).snapshot(
            since_date="2026-10-20", today="2026-11-01"
        )
        self.assertEqual(snapshot["error"]["code"], "bad_response")


# ---------------------------------------------------------------------------
# secrets
# ---------------------------------------------------------------------------


class TestNoSecretsLeak(unittest.TestCase):
    def assert_no_secret(self, text):
        for secret in SECRETS:
            self.assertNotIn(secret, text)

    def test_session_values_absent_from_success_snapshot(self):
        def handler(method, url, headers, timeout):
            return 200, {}, json.dumps(summary_body()).encode("utf-8")

        snapshot = DashboardClient(BASE, SESSION, transport=FakeTransport().set_handler(handler)).snapshot(
            since_date="2026-10-20", today="2026-11-01"
        )
        self.assert_no_secret(json.dumps(snapshot, sort_keys=True))
        self.assertNotIn("cookie", json.dumps(snapshot, sort_keys=True).lower())
        self.assertNotIn("authorization", json.dumps(snapshot, sort_keys=True).lower())

    def test_session_values_absent_from_unauthorized_error(self):
        transport = FakeTransport().route("GET", SUMMARY_PATH, status=401, body=b"{}")
        snapshot = DashboardClient(BASE, SESSION, transport=transport).snapshot(
            since_date="2026-10-20", today="2026-11-01"
        )
        self.assert_no_secret(json.dumps(snapshot, sort_keys=True))
        self.assert_no_secret(snapshot["error"]["message"])

    def test_transport_exception_echoing_headers_is_scrubbed(self):
        def transport(method, url, headers, timeout):
            raise RuntimeError("failed with headers %r" % (headers,))

        snapshot = DashboardClient(BASE, SESSION, transport=transport).snapshot(
            since_date="2026-10-20", today="2026-11-01"
        )
        self.assert_no_secret(json.dumps(snapshot, sort_keys=True))
        self.assertIn("***", snapshot["error"]["message"])

    def test_repr_never_shows_a_value(self):
        text = repr(DashboardClient(BASE, SESSION, transport=FakeTransport()))
        self.assert_no_secret(text)

    def test_error_from_parse_never_carries_a_value(self):
        with self.assertRaises(DashboardError) as caught:
            parse_summary(summary_body(total_cost="x"))
        self.assert_no_secret(str(caught.exception))


if __name__ == "__main__":
    unittest.main()
