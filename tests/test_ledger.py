# SPDX-License-Identifier: MIT
"""Tests for sarvam_meter.ledger (SPEC section 6)."""

import gc
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone

from sarvam_meter.ledger import (
    Ledger,
    local_day_start_iso,
    local_month_start_iso,
    utc_now_iso,
)

EPOCH_ISO = "1970-01-01T00:00:00Z"

try:  # Asia/Kolkata is a fixed +05:30 zone; fall back if tzdata is absent.
    from zoneinfo import ZoneInfo

    KOLKATA = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover
    KOLKATA = timezone(timedelta(hours=5, minutes=30))


class LedgerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sarvam-ledger-")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.db_path = os.path.join(self.tmpdir, "nested", "usage.db")
        self.ledger = Ledger(self.db_path)
        self.addCleanup(self.ledger.close)


class TimestampTests(unittest.TestCase):
    def test_utc_now_iso_format(self):
        stamp = utc_now_iso()
        self.assertRegex(stamp, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertTrue(stamp.endswith("Z"))
        self.assertEqual(len(stamp), 20)

    def test_utc_now_iso_parses(self):
        parsed = datetime.strptime(utc_now_iso(), "%Y-%m-%dT%H:%M:%SZ")
        self.assertIsInstance(parsed, datetime)

    def test_day_start_in_local_timezone(self):
        now = datetime(2026, 11, 1, 10, 0, 0, tzinfo=KOLKATA)
        # 2026-11-01T00:00:00+05:30 == 2026-10-31T18:30:00Z
        self.assertEqual(local_day_start_iso(now), "2026-10-31T18:30:00Z")

    def test_day_start_mid_month(self):
        now = datetime(2026, 11, 15, 9, 30, 0, tzinfo=KOLKATA)
        self.assertEqual(local_day_start_iso(now), "2026-11-14T18:30:00Z")

    def test_month_start_in_local_timezone(self):
        now = datetime(2026, 11, 15, 9, 30, 0, tzinfo=KOLKATA)
        self.assertEqual(local_month_start_iso(now), "2026-10-31T18:30:00Z")

    def test_month_start_on_first_day(self):
        now = datetime(2026, 3, 1, 0, 30, 0, tzinfo=KOLKATA)
        self.assertEqual(local_month_start_iso(now), "2026-02-28T18:30:00Z")

    def test_aware_datetime_is_bucketed_in_its_own_zone(self):
        # An aware datetime is bucketed in its own timezone, which is what makes
        # these helpers TZ-independent for callers and tests.
        utc_now = datetime(2026, 11, 1, 20, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(local_day_start_iso(utc_now), "2026-11-01T00:00:00Z")
        # The same instant seen from Asia/Kolkata is already 2026-11-02 there.
        kolkata_now = utc_now.astimezone(KOLKATA)
        self.assertEqual(local_day_start_iso(kolkata_now), "2026-11-01T18:30:00Z")
        self.assertEqual(local_month_start_iso(kolkata_now), "2026-10-31T18:30:00Z")

    def test_default_now_is_utc_z_string(self):
        for func in (local_day_start_iso, local_month_start_iso):
            stamp = func()
            self.assertTrue(stamp.endswith("Z"))
            self.assertRegex(stamp, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_naive_datetime_is_treated_as_local(self):
        naive = datetime(2026, 11, 1, 10, 0, 0)
        self.assertEqual(local_day_start_iso(naive), local_day_start_iso(naive.astimezone()))

    def test_rejects_non_datetime(self):
        with self.assertRaises(TypeError):
            local_day_start_iso("2026-11-01")


class SchemaTests(LedgerTestBase):
    def test_parent_directory_created(self):
        self.assertTrue(os.path.isfile(self.db_path))

    def test_wal_mode_enabled(self):
        conn = sqlite3.connect(self.db_path)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(str(mode).lower(), "wal")

    def test_tables_and_index_exist(self):
        conn = sqlite3.connect(self.db_path)
        try:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
                )
            }
        finally:
            conn.close()
        for expected in ("events", "balance", "kv", "idx_events_ts"):
            self.assertIn(expected, names)

    def test_reopen_is_idempotent(self):
        second = Ledger(self.db_path)
        try:
            second.record_event(source="manual", api="chat")
        finally:
            second.close()
        self.assertEqual(self.ledger.calls_since(EPOCH_ISO), 1)

    def test_rejects_empty_path(self):
        with self.assertRaises(ValueError):
            Ledger("")


class LifecycleTests(unittest.TestCase):
    def test_in_memory_ledger_works_across_threads(self):
        ledger = Ledger(":memory:")
        self.addCleanup(ledger.close)
        errors = []

        def worker():
            try:
                ledger.record_event(source="manual", api="chat", cost=1.0)
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(ledger.calls_since(EPOCH_ISO), 5)

    def test_close_is_idempotent(self):
        tmpdir = tempfile.mkdtemp(prefix="sarvam-ledger-")
        self.addCleanup(shutil.rmtree, tmpdir, True)
        ledger = Ledger(os.path.join(tmpdir, "usage.db"))
        ledger.close()
        ledger.close()

    def test_context_manager_closes(self):
        tmpdir = tempfile.mkdtemp(prefix="sarvam-ledger-")
        self.addCleanup(shutil.rmtree, tmpdir, True)
        with Ledger(os.path.join(tmpdir, "usage.db")) as ledger:
            ledger.record_event(source="manual", api="chat")
        self.assertEqual(ledger.calls_since(EPOCH_ISO), 1)


class LedgerDisposalTests(LedgerTestBase):
    """A dropped ledger must close its connections, not leave them to the GC.

    Python 3.13+ emits ``ResourceWarning: unclosed database`` when a sqlite3
    connection is finalised unclosed.  One of those surfacing at GC time inside
    another test's stderr assertion is what failed the RPM's %check on Fedora 43.
    """

    def test_close_all_is_idempotent(self):
        self.ledger.record_event(source="manual", api="chat", cost=1.0)
        self.ledger.close_all()
        self.ledger.close_all()  # a second shutdown must not raise

    def test_a_dropped_ledger_closes_its_connections(self):
        ledger = Ledger(os.path.join(self.tmpdir, "dropped.db"))
        ledger.record_event(source="manual", api="chat", cost=1.0)
        connection = ledger._conn()

        del ledger
        gc.collect()

        # A closed connection refuses to be used, which proves the finaliser ran
        # rather than the warning merely being suppressed.
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_the_daemon_closes_its_ledger_on_shutdown(self):
        from sarvam_meter.config import load_config, write_default_config
        from sarvam_meter.daemon import Daemon

        config_path = write_default_config(os.path.join(self.tmpdir, "config.json"))
        config = load_config(config_path)
        daemon = Daemon(
            config,
            logger=lambda line: None,
            socket_path=os.path.join(self.tmpdir, "daemon.sock"),
            ledger=Ledger(os.path.join(self.tmpdir, "daemon.db")),
        )
        connection = daemon.ledger._conn()

        daemon.shutdown()

        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


class RecordEventTests(LedgerTestBase):
    def test_returns_increasing_ids(self):
        first = self.ledger.record_event(source="proxy", api="chat")
        second = self.ledger.record_event(source="proxy", api="chat")
        self.assertEqual(second, first + 1)

    def test_stores_all_fields(self):
        self.ledger.record_event(
            source="proxy",
            api="chat",
            model="sarvam-105b",
            quantity=1500.0,
            unit="tokens",
            cost=0.0225,
            status=200,
            estimated=False,
            meta={"path": "/v1/chat/completions", "model": "sarvam-105b"},
            ts="2026-11-01T04:30:00Z",
        )
        row = self._raw_row()
        self.assertEqual(row["ts"], "2026-11-01T04:30:00Z")
        self.assertEqual(row["source"], "proxy")
        self.assertEqual(row["api"], "chat")
        self.assertEqual(row["model"], "sarvam-105b")
        self.assertAlmostEqual(row["quantity"], 1500.0)
        self.assertEqual(row["unit"], "tokens")
        self.assertAlmostEqual(row["cost"], 0.0225)
        self.assertEqual(row["status"], 200)
        self.assertEqual(row["estimated"], 0)

    def test_defaults_for_optional_fields(self):
        self.ledger.record_event(source="manual", api="tts")
        row = self._raw_row()
        self.assertIsNone(row["model"])
        self.assertIsNone(row["quantity"])
        self.assertIsNone(row["unit"])
        self.assertEqual(row["cost"], 0.0)
        self.assertIsNone(row["status"])
        self.assertEqual(row["estimated"], 0)
        self.assertIsNone(row["meta"])

    def test_estimated_flag_stored_as_one(self):
        self.ledger.record_event(source="proxy", api="stt", estimated=True)
        self.assertEqual(self._raw_row()["estimated"], 1)

    def test_meta_json_is_sorted_and_unicode_preserved(self):
        meta = {"z": 1, "a": {"y": 2, "x": "\u00e9"}, "note": "hi"}
        self.ledger.record_event(source="proxy", api="chat", meta=meta)
        raw = self._raw_row()["meta"]
        self.assertEqual(raw, json.dumps(meta, sort_keys=True, ensure_ascii=False))
        self.assertIn("\u00e9", raw)
        self.assertEqual(json.loads(raw), meta)

    def test_cost_rounded_to_six_decimals(self):
        self.ledger.record_event(source="proxy", api="chat", cost=0.123456789)
        self.assertEqual(self._raw_row()["cost"], 0.123457)

    def test_default_ts_is_now(self):
        before = utc_now_iso()
        self.ledger.record_event(source="manual", api="chat")
        after = utc_now_iso()
        stamp = self._raw_row()["ts"]
        self.assertTrue(before <= stamp <= after)

    def test_requires_source_and_api(self):
        with self.assertRaises(ValueError):
            self.ledger.record_event(source="", api="chat")
        with self.assertRaises(ValueError):
            self.ledger.record_event(source="proxy", api="")

    def _raw_row(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            conn.close()


class AggregateTests(LedgerTestBase):
    def setUp(self):
        super().setUp()
        self.ledger.record_event(
            source="proxy",
            api="chat",
            quantity=1500.0,
            unit="tokens",
            cost=0.0225,
            status=200,
            ts="2026-10-31T18:30:00Z",
        )
        self.ledger.record_event(
            source="proxy", api="tts", quantity=1000.0, unit="chars", cost=3.0,
            status=200, ts="2026-11-01T04:30:00Z",
        )
        self.ledger.record_event(
            source="proxy", api="chat", cost=0.0, status=429,
            ts="2026-11-01T05:00:00Z",
        )
        self.ledger.record_event(
            source="proxy", api="stt", cost=0.0, estimated=True, status=402,
            ts="2026-11-01T06:00:00Z",
        )
        self.ledger.record_event(
            source="manual", api="translate", quantity=10.0, unit="chars",
            cost=0.0, ts="2026-09-15T00:00:00Z",
        )

    def test_spend_since_sums_cost(self):
        self.assertAlmostEqual(self.ledger.spend_since(EPOCH_ISO), 3.0225)
        self.assertAlmostEqual(self.ledger.spend_since("2026-11-01T00:00:00Z"), 3.0)
        self.assertAlmostEqual(
            self.ledger.spend_since("2026-10-31T18:30:00Z"), 3.0225
        )

    def test_spend_since_boundary_is_inclusive(self):
        self.assertAlmostEqual(
            self.ledger.spend_since("2026-10-31T18:30:00Z"), 3.0225
        )
        self.assertAlmostEqual(
            self.ledger.spend_since("2026-10-31T18:30:01Z"), 3.0
        )

    def test_spend_since_future_is_zero(self):
        self.assertEqual(self.ledger.spend_since("2030-01-01T00:00:00Z"), 0.0)

    def test_calls_since(self):
        self.assertEqual(self.ledger.calls_since(EPOCH_ISO), 5)
        self.assertEqual(self.ledger.calls_since("2026-11-01T00:00:00Z"), 3)

    def test_spend_by_api_since_ordered_by_spend_desc(self):
        rows = self.ledger.spend_by_api_since(EPOCH_ISO)
        self.assertEqual([row["api"] for row in rows], ["tts", "chat", "stt", "translate"])
        self.assertEqual(rows[0]["calls"], 1)
        self.assertAlmostEqual(rows[0]["spend"], 3.0)
        self.assertEqual(rows[1]["calls"], 2)
        self.assertAlmostEqual(rows[1]["spend"], 0.0225)

    def test_spend_by_api_since_empty_window(self):
        self.assertEqual(self.ledger.spend_by_api_since("2030-01-01T00:00:00Z"), [])

    def test_count_status_since(self):
        self.assertEqual(self.ledger.count_status_since(200, EPOCH_ISO), 2)
        self.assertEqual(self.ledger.count_status_since(429, EPOCH_ISO), 1)
        self.assertEqual(self.ledger.count_status_since(402, EPOCH_ISO), 1)
        self.assertEqual(self.ledger.count_status_since(500, EPOCH_ISO), 0)
        self.assertEqual(
            self.ledger.count_status_since(429, "2026-11-01T05:00:01Z"), 0
        )

    def test_last_status_ts(self):
        self.assertEqual(self.ledger.last_status_ts(429), "2026-11-01T05:00:00Z")
        self.assertEqual(self.ledger.last_status_ts(402), "2026-11-01T06:00:00Z")
        self.assertIsNone(self.ledger.last_status_ts(503))

    def test_purge_before(self):
        removed = self.ledger.purge_before("2026-11-01T00:00:00Z")
        self.assertEqual(removed, 2)  # the 2026-09-15 row and the 2026-10-31 row
        self.assertEqual(self.ledger.calls_since(EPOCH_ISO), 3)

    def test_purge_before_nothing_matches(self):
        self.assertEqual(self.ledger.purge_before("1970-01-02T00:00:00Z"), 0)


class BalanceTests(LedgerTestBase):
    def test_get_balance_none_when_unset(self):
        self.assertIsNone(self.ledger.get_balance())

    def test_set_and_get_balance(self):
        self.ledger.set_balance(500.0)
        balance = self.ledger.get_balance()
        self.assertEqual(balance["starting"], 500.0)
        self.assertEqual(balance["currency"], "INR")
        self.assertTrue(balance["set_at"].endswith("Z"))

    def test_set_balance_upserts_single_row(self):
        self.ledger.set_balance(500.0)
        self.ledger.set_balance(412.6, "USD")
        balance = self.ledger.get_balance()
        self.assertEqual(balance["starting"], 412.6)
        self.assertEqual(balance["currency"], "USD")
        conn = sqlite3.connect(self.db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM balance").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)


class KeyValueTests(LedgerTestBase):
    def test_missing_key_is_none(self):
        self.assertIsNone(self.ledger.get_kv("nope"))

    def test_set_get_overwrite(self):
        self.ledger.set_kv("quota_exhausted_at", "2026-11-01T06:00:00Z")
        self.assertEqual(
            self.ledger.get_kv("quota_exhausted_at"), "2026-11-01T06:00:00Z"
        )
        self.ledger.set_kv("quota_exhausted_at", "2026-11-02T00:00:00Z")
        self.assertEqual(
            self.ledger.get_kv("quota_exhausted_at"), "2026-11-02T00:00:00Z"
        )

    def test_none_value_deletes_key(self):
        self.ledger.set_kv("k", "v")
        self.ledger.set_kv("k", None)
        self.assertIsNone(self.ledger.get_kv("k"))


class ConcurrencyTests(LedgerTestBase):
    def test_twenty_parallel_record_event_calls(self):
        errors = []
        count = 20
        barrier = threading.Barrier(count)

        def worker(index):
            try:
                barrier.wait(timeout=20)
                self.ledger.record_event(
                    source="proxy",
                    api="chat",
                    model="sarvam-105b",
                    quantity=1000.0,
                    unit="tokens",
                    cost=0.015,
                    status=200,
                )
            except Exception as exc:  # noqa: BLE001 - collected for the assertion
                errors.append("thread %d: %r" % (index, exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual([t.is_alive() for t in threads], [False] * count)
        self.assertEqual(errors, [])
        self.assertEqual(self.ledger.calls_since(EPOCH_ISO), count)
        self.assertAlmostEqual(self.ledger.spend_since(EPOCH_ISO), 0.3)

    def test_parallel_writers_plus_reader(self):
        errors = []
        count = 10

        def writer(index):
            try:
                for _ in range(5):
                    self.ledger.record_event(source="proxy", api="tts", cost=0.003)
            except Exception as exc:  # noqa: BLE001
                errors.append("writer %d: %r" % (index, exc))

        def reader():
            try:
                for _ in range(20):
                    self.ledger.spend_since(EPOCH_ISO)
                    self.ledger.spend_by_api_since(EPOCH_ISO)
            except Exception as exc:  # noqa: BLE001
                errors.append("reader: %r" % (exc,))

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(count)]
        threads.append(threading.Thread(target=reader))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(errors, [])
        self.assertEqual(self.ledger.calls_since(EPOCH_ISO), count * 5)


class LifecycleTests(unittest.TestCase):
    def test_in_memory_ledger_works_across_threads(self):
        ledger = Ledger(":memory:")
        self.addCleanup(ledger.close)
        errors = []

        def worker():
            try:
                ledger.record_event(source="manual", api="chat", cost=1.0)
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(ledger.calls_since(EPOCH_ISO), 5)

    def test_close_is_idempotent(self):
        tmpdir = tempfile.mkdtemp(prefix="sarvam-ledger-")
        self.addCleanup(shutil.rmtree, tmpdir, True)
        ledger = Ledger(os.path.join(tmpdir, "usage.db"))
        ledger.close()
        ledger.close()

    def test_context_manager_closes(self):
        tmpdir = tempfile.mkdtemp(prefix="sarvam-ledger-")
        self.addCleanup(shutil.rmtree, tmpdir, True)
        with Ledger(os.path.join(tmpdir, "usage.db")) as ledger:
            ledger.record_event(source="manual", api="chat")
        self.assertEqual(ledger.calls_since(EPOCH_ISO), 1)


if __name__ == "__main__":
    unittest.main()
