# SPDX-License-Identifier: MIT
"""Status-level tests for the dashboard source (SPEC 17.5, schema 2).

These tests never construct a real ``DashboardClient``: the snapshot dict the
daemon would have cached is injected straight into :func:`build_status`, which is
exactly the boundary the widget sees.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from sarvam_meter import config as config_mod
from sarvam_meter.ledger import Ledger
from sarvam_meter.status import build_status

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 11, 15, 10, 0, 0, tzinfo=IST)

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


def make_config(directory, **sections):
    document = config_mod.default_config_dict()
    document["db_path"] = os.path.join(directory, "usage.db")
    document["dashboard"]["session_file"] = os.path.join(directory, "session.json")
    for section, values in sections.items():
        document[section].update(values)
    path = os.path.join(directory, "config.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle)
    return config_mod.load_config(path)


def snapshot(
    window=134.2312311905,
    today=12.3,
    reachable=True,
    configured=True,
    error=None,
    by_model=None,
    balance=None,
):
    """A dashboard snapshot shaped exactly like ``DashboardClient.snapshot()``."""
    return {
        "configured": configured,
        "reachable": reachable,
        "as_of": "2026-11-15T04:30:00Z",
        "org_id": "019f37be-0000-7000-8000-000000000000",
        "currency": "INR",
        "window": {"start": "2026-10-20", "end": "2026-11-15"},
        "spend": {"window": window, "today": today},
        "by_model": by_model
        if by_model is not None
        else [
            {"model": "saaras:v4", "cost": 74.0498055238},
            {"model": "glm5.3-flash", "cost": 53.388129},
        ],
        "usage": [],
        "balance": balance if balance is not None else {
            "known": False,
            "amount": None,
            "currency": "INR",
            "as_of": None,
            "state": "",
            "billing_model": "",
            "unit": "",
        },
        "error": error,
    }


class DashboardStatusTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sarvam-dash-status-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = make_config(self.tmp)
        self.enabled = make_config(self.tmp, dashboard={"enabled": True})
        self.ledger = Ledger(self.config.db_path)
        self.addCleanup(self.ledger.close)

    def status(self, dash=None, config=None):
        return build_status(
            config or self.config,
            self.ledger,
            None,
            NOW,
            dashboard_snapshot=dash,
        )


class DashboardBlockTest(DashboardStatusTestBase):
    def test_block_is_always_present_even_when_disabled(self):
        sarvam = self.status()["sarvam"]
        self.assertIn("dashboard", sarvam)
        self.assertEqual(set(sarvam["dashboard"]), DASHBOARD_KEYS)
        self.assertFalse(sarvam["dashboard"]["configured"])
        self.assertFalse(sarvam["dashboard"]["reachable"])
        self.assertIsNone(sarvam["dashboard"]["error"])
        self.assertEqual(sarvam["dashboard"]["by_model"], [])
        self.assertEqual(sarvam["dashboard"]["spend"],
                         {"window": 0.0, "today": 0.0, "month": 0.0})

    def test_configured_follows_the_config_section(self):
        sarvam = self.status(config=self.enabled)["sarvam"]
        self.assertTrue(sarvam["dashboard"]["configured"])
        self.assertFalse(sarvam["dashboard"]["reachable"])

    def test_reachable_snapshot_is_projected(self):
        sarvam = self.status(snapshot(), config=self.enabled)["sarvam"]
        block = sarvam["dashboard"]
        self.assertTrue(block["configured"])
        self.assertTrue(block["reachable"])
        self.assertEqual(block["as_of"], "2026-11-15T04:30:00Z")
        self.assertTrue(block["org_id"].startswith("019f37be-"))
        self.assertEqual(block["currency"], "INR")
        self.assertEqual(block["window"], {"start": "2026-10-20", "end": "2026-11-15"})
        self.assertEqual(block["spend"], {"window": 134.23, "today": 12.3})
        self.assertIsNone(block["error"])

    def test_by_model_is_rounded_to_two_dp(self):
        block = self.status(snapshot(), config=self.enabled)["sarvam"]["dashboard"]
        self.assertEqual(
            block["by_model"],
            [
                {"model": "saaras:v4", "cost": 74.05},
                {"model": "glm5.3-flash", "cost": 53.39},
            ],
        )
        for entry in block["by_model"]:
            self.assertEqual(set(entry), {"model", "cost"})

    def test_error_is_carried_from_the_snapshot(self):
        error = {
            "source": "dashboard",
            "code": "no_session",
            "message": "no session file",
        }
        block = self.status(
            snapshot(reachable=False, window=0.0, today=0.0, error=error),
            config=self.enabled,
        )["sarvam"]["dashboard"]
        self.assertFalse(block["reachable"])
        self.assertEqual(block["error"], error)

    def test_unusable_snapshot_degrades_to_the_empty_block(self):
        for bad in ("not-a-dict", 42, None, []):
            block = self.status(bad, config=self.enabled)["sarvam"]["dashboard"]
            self.assertEqual(set(block), DASHBOARD_KEYS)
            self.assertFalse(block["reachable"])

    def test_malformed_snapshot_fields_never_raise(self):
        block = self.status(
            {
                "configured": True,
                "reachable": True,
                "as_of": 5,
                "org_id": None,
                "currency": None,
                "window": "nope",
                "spend": [],
                "by_model": ["nope", {"cost": "free"}],
                "error": "boom",
            },
            config=self.enabled,
        )["sarvam"]["dashboard"]
        self.assertIsNone(block["as_of"])
        self.assertIsNone(block["org_id"])
        self.assertEqual(block["currency"], "INR")
        self.assertEqual(block["window"], {"start": None, "end": None})
        self.assertEqual(block["spend"], {"window": 0.0, "today": 0.0})
        self.assertEqual(block["by_model"], [{"model": "", "cost": 0.0}])
        self.assertIsNone(block["error"])


class BalanceArithmeticTest(DashboardStatusTestBase):
    def test_dashboard_window_drives_spent_total_and_remaining(self):
        self.ledger.set_balance(500.0, "INR")
        # Ledger spend is irrelevant once the dashboard is reachable.
        self.ledger.record_event(
            source="proxy", api="chat", cost=1.0, ts="2026-11-15T04:00:00Z"
        )
        balance = self.status(snapshot(), config=self.enabled)["sarvam"]["balance"]
        self.assertTrue(balance["known"])
        self.assertEqual(balance["starting"], 500.0)
        self.assertEqual(balance["spent_total"], 134.23)
        self.assertEqual(balance["remaining"], 365.77)
        self.assertEqual(balance["source"], "dashboard-derived")
        self.assertIsNotNone(balance["set_at"])

    def test_remaining_never_goes_negative(self):
        self.ledger.set_balance(100.0, "INR")
        balance = self.status(snapshot(window=134.2312311905), config=self.enabled)[
            "sarvam"
        ]["balance"]
        self.assertEqual(balance["spent_total"], 134.23)
        self.assertEqual(balance["remaining"], 0.0)

    def test_ledger_behaviour_is_unchanged_without_a_snapshot(self):
        self.ledger.set_balance(500.0, "INR")
        self.ledger.record_event(
            source="proxy", api="chat", cost=12.345, ts="2026-11-15T04:00:00Z"
        )
        balance = self.status(config=self.enabled)["sarvam"]["balance"]
        self.assertEqual(balance["spent_total"], 12.35)
        self.assertEqual(balance["remaining"], 487.65)
        self.assertEqual(balance["source"], "ledger")

    def test_reported_balance_needs_no_starting_balance(self):
        # No `balance set` at all: the dashboard reports the number itself.
        reported = {
            "known": True,
            "amount": 80.03533,
            "currency": "INR",
            "as_of": "2026-11-01T05:55:39.713767Z",
            "state": "provisioned",
            "billing_model": "prepaid",
            "unit": "rupees",
        }
        sarvam = self.status(snapshot(balance=reported), config=self.enabled)["sarvam"]
        self.assertTrue(sarvam["balance"]["known"])
        self.assertEqual(sarvam["balance"]["remaining"], 80.04)
        self.assertIsNone(sarvam["balance"]["starting"])
        self.assertEqual(sarvam["balance"]["source"], "dashboard")
        self.assertEqual(sarvam["dashboard"]["balance"]["amount"], 80.03533)
        self.assertEqual(
            sarvam["dashboard"]["balance"]["as_of"], "2026-11-01T05:55:39.713767Z"
        )

    def test_reported_balance_wins_over_the_derived_one(self):
        self.ledger.set_balance(500.0, "INR")
        reported = {"known": True, "amount": 80.03533, "currency": "INR"}
        balance = self.status(snapshot(balance=reported), config=self.enabled)[
            "sarvam"
        ]["balance"]
        # 500 - 134.23 would be 365.77; the reported number wins
        self.assertEqual(balance["remaining"], 80.04)
        self.assertEqual(balance["starting"], 500.0)
        self.assertEqual(balance["spent_total"], 134.23)
        self.assertEqual(balance["source"], "dashboard")

    def test_reported_balance_survives_when_the_windows_fail(self):
        reported = {"known": True, "amount": 80.03533, "currency": "INR"}
        payload = self.status(
            snapshot(
                reachable=False,
                window=0.0,
                today=0.0,
                balance=reported,
                error={"source": "dashboard", "code": "unauthorized", "message": "x"},
            ),
            config=self.enabled,
        )
        sarvam = payload["sarvam"]
        self.assertFalse(sarvam["dashboard"]["reachable"])
        self.assertEqual(sarvam["balance"]["source"], "dashboard")
        self.assertEqual(sarvam["balance"]["remaining"], 80.04)
        # spend is still ledger-sourced, because the windows did not answer
        self.assertEqual(sarvam["spend"]["source"], "ledger")

    def test_an_exhausted_reported_balance_is_known_not_unknown(self):
        reported = {"known": True, "amount": 0.0, "currency": "INR"}
        balance = self.status(snapshot(balance=reported), config=self.enabled)[
            "sarvam"
        ]["balance"]
        self.assertTrue(balance["known"])
        self.assertEqual(balance["remaining"], 0.0)

    def test_unreachable_snapshot_falls_back_to_the_ledger(self):
        self.ledger.set_balance(500.0, "INR")
        self.ledger.record_event(
            source="proxy", api="chat", cost=12.345, ts="2026-11-15T04:00:00Z"
        )
        payload = self.status(
            snapshot(
                reachable=False,
                window=999.0,
                today=999.0,
                error={"source": "dashboard", "code": "unreachable", "message": "x"},
            ),
            config=self.enabled,
        )
        sarvam = payload["sarvam"]
        self.assertEqual(sarvam["balance"]["spent_total"], 12.35)
        self.assertEqual(sarvam["balance"]["remaining"], 487.65)
        self.assertEqual(sarvam["balance"]["source"], "ledger")
        self.assertEqual(sarvam["spend"]["source"], "ledger")

    def test_no_starting_balance_keeps_the_ledger_all_time_total(self):
        self.ledger.record_event(
            source="manual", api="tts", cost=3.0, ts="2026-09-01T00:00:00Z"
        )
        balance = self.status(snapshot(), config=self.enabled)["sarvam"]["balance"]
        self.assertFalse(balance["known"])
        self.assertIsNone(balance["starting"])
        self.assertIsNone(balance["remaining"])
        self.assertIsNone(balance["set_at"])
        self.assertEqual(balance["spent_total"], 3.0)
        # SPEC 17.5: the *source* follows reachability, not `known`.
        self.assertEqual(balance["source"], "dashboard")

    def test_set_at_is_preserved_from_the_ledger(self):
        self.ledger.set_balance(500.0, "INR")
        ledger_set_at = self.ledger.get_balance()["set_at"]
        balance = self.status(snapshot(), config=self.enabled)["sarvam"]["balance"]
        self.assertEqual(balance["set_at"], ledger_set_at)


class SpendSourceTest(DashboardStatusTestBase):
    def setUp(self):
        super().setUp()
        self.ledger.record_event(
            source="proxy", api="chat", cost=12.3, ts="2026-11-15T04:00:00Z"
        )
        self.ledger.record_event(
            source="proxy", api="stt", cost=75.1, ts="2026-11-05T04:00:00Z"
        )

    def test_today_prefers_the_dashboard(self):
        spend = self.status(snapshot(today=7.5), config=self.enabled)["sarvam"]["spend"]
        self.assertEqual(spend["today"], 7.5)
        self.assertEqual(spend["source"], "dashboard")
        # "this month" is not a dashboard figure: it stays on the ledger.
        self.assertEqual(spend["month"], 87.4)

    def test_today_falls_back_to_the_ledger_when_unreachable(self):
        spend = self.status(snapshot(reachable=False), config=self.enabled)["sarvam"][
            "spend"
        ]
        self.assertEqual(spend["today"], 12.3)
        self.assertEqual(spend["source"], "ledger")

    def test_today_rounds_to_two_dp(self):
        spend = self.status(snapshot(today=0.005), config=self.enabled)["sarvam"]["spend"]
        self.assertEqual(spend["today"], round(0.005, 2))

    def test_ledger_sources_when_the_dashboard_is_disabled(self):
        spend = self.status()["sarvam"]["spend"]
        self.assertEqual(spend, {"today": 12.3, "month": 87.4, "source": "ledger"})


class SchemaTest(DashboardStatusTestBase):
    def test_schema_is_two(self):
        payload = self.status()
        self.assertEqual(payload["schema"], 2)
        self.assertEqual(
            set(payload), {"schema", "generated_at", "stale", "errors", "sarvam", "cliproxy"}
        )

    def test_payload_is_json_serialisable(self):
        text = json.dumps(self.status(snapshot(), config=self.enabled))
        self.assertIn('"schema": 2', text)
        self.assertIn('"source": "dashboard"', text)

    def test_dashboard_error_is_not_duplicated_in_top_level_errors(self):
        payload = self.status(
            snapshot(
                reachable=False,
                error={"source": "dashboard", "code": "no_session", "message": "x"},
            ),
            config=self.enabled,
        )
        self.assertEqual(payload["errors"], [])
        self.assertEqual(payload["sarvam"]["dashboard"]["error"]["code"], "no_session")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
