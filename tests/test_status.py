# SPDX-License-Identifier: MIT
"""Tests for sarvam_meter.status (SPEC section 10).

The payload shape is the widget's contract, so these tests assert the exact key
sets, the rounding rules, ``stale`` semantics, ``balance.known``, the error
entry shape and that a broken ledger can never raise.
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
from sarvam_meter.status import build_status, fraction, money, worst_fraction

TOP_LEVEL_KEYS = {"schema", "generated_at", "stale", "errors", "sarvam", "cliproxy"}
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
CLIPROXY_KEYS = {"configured", "reachable", "api_version", "worst_fraction", "accounts"}

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 11, 15, 10, 0, 0, tzinfo=IST)


class BrokenLedger:
    """A ledger whose every method raises (simulates an unwritable database)."""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def _fail(*_args, **_kwargs):
            raise RuntimeError("database is locked")

        return _fail


def make_config(directory, db_path=None, **sections):
    """Write a real config.json and load it through the config module."""
    document = config_mod.default_config_dict()
    document["db_path"] = db_path or os.path.join(directory, "usage.db")
    for section, values in sections.items():
        document[section].update(values)
    path = os.path.join(directory, "config.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle)
    return config_mod.load_config(path)


class StatusTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sarvam-status-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = make_config(self.tmp)
        self.ledger = Ledger(self.config.db_path)
        self.addCleanup(self.ledger.close)

    def status(self, snapshot=None, now=NOW, config=None):
        return build_status(config or self.config, self.ledger, snapshot, now)


class ShapeTest(StatusTestBase):
    def test_exact_key_sets(self):
        payload = self.status()
        self.assertEqual(set(payload.keys()), TOP_LEVEL_KEYS)
        self.assertEqual(set(payload["sarvam"].keys()), SARVAM_KEYS)
        self.assertEqual(set(payload["sarvam"]["balance"].keys()), BALANCE_KEYS)
        self.assertEqual(set(payload["sarvam"]["spend"].keys()), SPEND_KEYS)
        self.assertEqual(set(payload["sarvam"]["dashboard"].keys()), DASHBOARD_KEYS)
        self.assertEqual(set(payload["cliproxy"].keys()), CLIPROXY_KEYS)
        self.assertEqual(payload["schema"], 2)

    def test_empty_ledger_is_zeroed_not_missing(self):
        payload = self.status()
        sarvam = payload["sarvam"]
        self.assertEqual(
            sarvam["spend"], {"today": 0.0, "month": 0.0, "source": "ledger"}
        )
        self.assertEqual(sarvam["calls"], {"today": 0})
        self.assertEqual(sarvam["by_api"], [])
        self.assertEqual(payload["errors"], [])

    def test_json_serialisable(self):
        text = json.dumps(self.status({"accounts": [], "errors": []}))
        self.assertIn('"schema": 2', text)

    def test_generated_at_is_local_iso_with_offset(self):
        payload = self.status()
        self.assertEqual(payload["generated_at"], "2026-11-15T10:00:00+05:30")

    def test_generated_at_utc_now_has_offset(self):
        payload = build_status(self.config, self.ledger, None, None)
        self.assertRegex(payload["generated_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")

    def test_sarvam_configured_and_currency(self):
        payload = self.status()
        self.assertTrue(payload["sarvam"]["configured"])
        self.assertEqual(payload["sarvam"]["currency"], "INR")

    def test_disabled_sarvam_is_reported(self):
        config = make_config(self.tmp, sarvam={"enabled": False})
        payload = self.status(config=config)
        self.assertFalse(payload["sarvam"]["configured"])


def dashboard_snapshot(balance=None, reachable=True, window=134.2312311905, today=12.3):
    """The dict ``DashboardClient.snapshot()`` would hand to ``build_status``."""
    return {
        "configured": True,
        "reachable": reachable,
        "as_of": "2026-11-15T04:30:00Z",
        "org_id": "019f37be-0000-7000-8000-000000000000",
        "currency": "INR",
        "window": {"start": "2026-10-20", "end": "2026-11-15"},
        "spend": {"window": window, "today": today},
        "by_model": [],
        "usage": [],
        "balance": balance
        if balance is not None
        else {
            "known": False,
            "amount": None,
            "currency": "INR",
            "as_of": None,
            "state": "",
            "billing_model": "",
            "unit": "",
        },
        "error": None,
    }


class BalanceTest(StatusTestBase):
    def test_reported_balance_rounds_to_two_dp(self):
        # The only balance there is: the figure Sarvam itself reports.
        self.ledger.set_balance(500.0, "INR")
        self.ledger.record_event(
            source="proxy", api="chat", quantity=1000.0, unit="tokens",
            cost=12.345, ts="2026-11-15T05:00:00Z",
        )
        payload = build_status(
            self.config,
            self.ledger,
            None,
            NOW,
            dashboard_snapshot=dashboard_snapshot(
                balance={"known": True, "amount": 487.654, "currency": "INR"}
            ),
        )
        balance = payload["sarvam"]["balance"]
        self.assertTrue(balance["known"])
        self.assertEqual(balance["remaining"], 487.65)
        self.assertEqual(balance["starting"], 500.0)
        self.assertEqual(balance["spent_total"], 134.23)
        self.assertEqual(balance["source"], "dashboard")
        self.assertIsNotNone(balance["set_at"])
        self.assertTrue(balance["set_at"].endswith("Z"))

    def test_a_starting_balance_alone_leaves_the_balance_unknown(self):
        # `balance set` records where the ledger started; it is never turned into
        # a "left" figure, because only Sarvam knows the wallet.
        self.ledger.set_balance(500.0, "INR")
        self.ledger.record_event(
            source="proxy", api="chat", cost=12.345, ts="2026-11-15T05:00:00Z"
        )
        balance = self.status()["sarvam"]["balance"]
        self.assertFalse(balance["known"])
        self.assertIsNone(balance["remaining"])
        self.assertIsNone(balance["starting"])
        self.assertIsNone(balance["set_at"])
        self.assertEqual(balance["spent_total"], 12.35)
        self.assertEqual(balance["source"], "ledger")

    def test_unknown_balance_when_no_starting_balance(self):
        self.ledger.record_event(
            source="manual", api="tts", quantity=1000.0, unit="chars",
            cost=3.0, ts="2026-09-01T00:00:00Z",
        )
        balance = self.status()["sarvam"]["balance"]
        self.assertFalse(balance["known"])
        self.assertIsNone(balance["remaining"])
        self.assertIsNone(balance["starting"])
        self.assertIsNone(balance["set_at"])
        self.assertEqual(balance["spent_total"], 3.0)

    def test_spent_total_covers_all_time_without_deriving_a_remaining(self):
        for ts, cost in (
            ("2026-11-15T05:00:00Z", 1.0),
            ("2026-11-05T05:00:00Z", 2.0),
            ("2026-01-05T05:00:00Z", 4.0),
        ):
            self.ledger.record_event(source="proxy", api="chat", cost=cost, ts=ts)
        self.ledger.set_balance(100.0)
        balance = self.status()["sarvam"]["balance"]
        self.assertEqual(balance["spent_total"], 7.0)
        # 100 - 7 is deliberately *not* reported: the balance stays unknown.
        self.assertFalse(balance["known"])
        self.assertIsNone(balance["remaining"])


class SpendWindowTest(StatusTestBase):
    def setUp(self):
        super().setUp()
        # 2026-11-15T10:00+05:30 == 2026-11-15T04:30Z
        self.ledger.record_event(
            source="proxy", api="chat", model="sarvam-105b", quantity=10.0,
            unit="tokens", cost=12.3, ts="2026-11-15T04:00:00Z",
        )
        self.ledger.record_event(
            source="proxy", api="stt", quantity=60.0, unit="seconds",
            cost=75.1, ts="2026-11-05T04:00:00Z",
        )
        self.ledger.record_event(
            source="proxy", api="chat", quantity=10.0, unit="tokens",
            cost=12.0, ts="2026-10-01T04:00:00Z",
        )

    def test_today_and_month_use_local_calendar_boundaries(self):
        sarvam = self.status()["sarvam"]
        self.assertEqual(sarvam["spend"]["today"], 12.3)
        self.assertEqual(sarvam["spend"]["month"], 87.4)
        self.assertEqual(sarvam["calls"]["today"], 1)

    def test_by_api_is_month_to_date_and_sorted(self):
        by_api = self.status()["sarvam"]["by_api"]
        self.assertEqual([entry["api"] for entry in by_api], ["stt", "chat"])
        self.assertEqual(by_api[0]["spend"], 75.1)
        self.assertEqual(by_api[1]["calls"], 1)
        self.assertEqual(set(by_api[0].keys()), {"api", "spend", "calls"})
        self.assertAlmostEqual(sum(e["spend"] for e in by_api), 87.4, places=2)


class CliproxyTest(StatusTestBase):
    def snapshot(self, accounts, reachable=True, errors=None):
        return {
            "configured": True,
            "reachable": reachable,
            "api_version": "v8",
            "accounts": accounts,
            "errors": errors or [],
        }

    def test_stale_when_enabled_without_snapshot(self):
        enabled = make_config(self.tmp, cliproxy={"enabled": True})
        self.assertTrue(self.status(config=enabled)["stale"])

    def test_not_stale_when_disabled_without_snapshot(self):
        self.assertFalse(self.status()["stale"])

    def test_not_stale_with_snapshot(self):
        enabled = make_config(self.tmp, cliproxy={"enabled": True})
        payload = self.status(self.snapshot([]), config=enabled)
        self.assertFalse(payload["stale"])
        self.assertTrue(payload["cliproxy"]["configured"])
        self.assertTrue(payload["cliproxy"]["reachable"])

    def test_worst_fraction_ignores_unknown_buckets(self):
        accounts = [
            {
                "name": "a",
                "buckets": [
                    {"window": "5h", "remaining_fraction": 0.62, "unknown": False},
                    {"window": "weekly", "remaining_fraction": None, "unknown": True},
                ],
            },
            {
                "name": "b",
                "buckets": [{"window": "5h", "remaining_fraction": 0.19999, "unknown": False}],
            },
        ]
        payload = self.status(self.snapshot(accounts))
        self.assertEqual(payload["cliproxy"]["worst_fraction"], 0.2)
        self.assertEqual(len(payload["cliproxy"]["accounts"]), 2)

    def test_worst_fraction_null_when_every_fraction_unknown(self):
        accounts = [{"name": "a", "buckets": [{"window": "5h", "remaining_fraction": None}]}]
        payload = self.status(self.snapshot(accounts))
        self.assertIsNone(payload["cliproxy"]["worst_fraction"])

    def test_worst_fraction_null_without_accounts(self):
        self.assertIsNone(self.status(self.snapshot([]))["cliproxy"]["worst_fraction"])

    def test_errors_carry_snapshot_errors(self):
        payload = self.status(
            self.snapshot(
                [],
                reachable=False,
                errors=[
                    {"source": "cliproxy", "code": "unreachable", "message": "refused"}
                ],
            )
        )
        self.assertEqual(
            payload["errors"],
            [{"source": "cliproxy", "code": "unreachable", "message": "refused"}],
        )

    def test_api_version_falls_back_to_config(self):
        enabled = make_config(self.tmp, cliproxy={"enabled": True, "api_version": "v0"})
        payload = self.status(config=enabled)
        self.assertEqual(payload["cliproxy"]["api_version"], "v0")

    def test_secrets_never_appear_in_accounts(self):
        accounts = [
            {
                "name": "a",
                "management_key": "super-secret-key",
                "buckets": [{"window": "5h", "remaining_fraction": 0.5}],
                "summary": [{"key": "credits", "value": 1.0, "currency": "USD"}],
            }
        ]
        payload = self.status(self.snapshot(accounts))
        text = json.dumps(payload)
        self.assertNotIn("super-secret-key", text)
        # the legitimate "key" field of a summary entry survives
        self.assertIn('"key": "credits"', text)


class NeverRaisesTest(StatusTestBase):
    def test_ledger_failure_is_reported_and_zeroed(self):
        payload = build_status(self.config, BrokenLedger(), None, NOW)
        self.assertEqual(payload["errors"][0]["source"], "ledger")
        self.assertEqual(payload["errors"][0]["code"], "read_failed")
        self.assertIn("database is locked", payload["errors"][0]["message"])
        sarvam = payload["sarvam"]
        self.assertEqual(
            sarvam["spend"], {"today": 0.0, "month": 0.0, "source": "ledger"}
        )
        self.assertEqual(sarvam["calls"], {"today": 0})
        self.assertEqual(sarvam["by_api"], [])
        self.assertFalse(sarvam["balance"]["known"])
        self.assertEqual(sarvam["balance"]["spent_total"], 0.0)
        self.assertEqual(set(payload.keys()), TOP_LEVEL_KEYS)

    def test_ledger_failure_keeps_cliproxy_snapshot_errors(self):
        snapshot = {
            "reachable": False,
            "accounts": [],
            "errors": [{"source": "cliproxy", "code": "unreachable", "message": "x"}],
        }
        payload = build_status(self.config, BrokenLedger(), snapshot, NOW)
        sources = [entry["source"] for entry in payload["errors"]]
        self.assertEqual(sources, ["ledger", "cliproxy"])

    def test_unusable_snapshot_is_ignored(self):
        payload = build_status(self.config, self.ledger, "not-a-dict", NOW)
        self.assertFalse(payload["cliproxy"]["reachable"])
        self.assertEqual(payload["cliproxy"]["accounts"], [])


class HelperTest(unittest.TestCase):
    def test_money_rounding(self):
        self.assertEqual(money(12.345), 12.35)
        self.assertEqual(money(0.0), 0.0)
        self.assertEqual(money(None), 0.0)
        self.assertEqual(money(float("nan")), 0.0)
        self.assertEqual(money(float("inf")), 0.0)

    def test_fraction_clamping_and_rounding(self):
        self.assertEqual(fraction(1.4), 1.0)
        self.assertEqual(fraction(-0.2), 0.0)
        self.assertEqual(fraction(0.123456), 0.1235)
        self.assertIsNone(fraction(None))
        self.assertIsNone(fraction("0.5"))

    def test_worst_fraction_helper(self):
        self.assertEqual(
            worst_fraction([{"buckets": [{"remaining_fraction": 0.9}, {"remaining_fraction": 0.3}]}]),
            0.3,
        )
        self.assertIsNone(worst_fraction([]))
        self.assertIsNone(worst_fraction([{"buckets": [{"remaining_fraction": None}]}]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
