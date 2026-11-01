# SPDX-License-Identifier: MIT
"""SQLite usage ledger (SPEC section 6).

Thread safety: every thread gets its own connection (``threading.local``), the
database runs in WAL mode with ``busy_timeout=5000``, and connections are opened
in autocommit mode so no transaction is left open across threads.  Twenty
concurrent writers therefore never raise ``database is locked``.

``:memory:`` databases cannot be shared between connections, so that one case
uses a single connection guarded by a lock (all statements are materialised
inside the lock).

All timestamps are stored as fixed-width UTC ISO8601 strings ending in ``Z``
(``2026-11-01T04:30:00Z``), so plain string comparison is a valid ordering.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

try:  # pragma: no cover - zoneinfo is stdlib on 3.9+
    from zoneinfo import ZoneInfo  # noqa: F401  (re-exported for callers/tests)
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

#: Fixed-width UTC timestamp format used for every stored time.
ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  source TEXT NOT NULL,
  api TEXT NOT NULL,
  model TEXT,
  quantity REAL,
  unit TEXT,
  cost REAL NOT NULL DEFAULT 0,
  status INTEGER,
  estimated INTEGER NOT NULL DEFAULT 0,
  meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS balance (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  starting REAL NOT NULL,
  currency TEXT NOT NULL,
  set_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


def utc_now_iso() -> str:
    """Current UTC time as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return datetime.now(timezone.utc).strftime(ISO_FORMAT)


def _to_utc_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(ISO_FORMAT)


def _local_moment(now: Optional[datetime]) -> datetime:
    """Return ``now`` as an aware datetime in the local timezone.

    An aware ``now`` is bucketed in its own timezone, so callers and tests can
    pass an explicit ``Asia/Kolkata`` datetime and stay independent of the
    machine's TZ.  A naive ``now`` is interpreted as local time.
    """
    if now is None:
        return datetime.now().astimezone()
    if not isinstance(now, datetime):
        raise TypeError("now must be a datetime, got %r" % (type(now).__name__,))
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        return now.astimezone()
    return now


def local_day_start_iso(now: Optional[datetime] = None) -> str:
    """UTC ISO8601 of the start of the local calendar day containing ``now``."""
    local = _local_moment(now)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return _to_utc_iso(start)


def local_month_start_iso(now: Optional[datetime] = None) -> str:
    """UTC ISO8601 of the start of the local calendar month containing ``now``."""
    local = _local_moment(now)
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return _to_utc_iso(start)


class Ledger:
    """Append-only usage ledger plus the single balance row and a small kv store."""

    def __init__(self, db_path: str):
        if not isinstance(db_path, str) or not db_path.strip():
            raise ValueError("db_path must be a non-empty string")
        self._path = os.path.expanduser(db_path)
        self._memory = self._path == ":memory:"
        self._local = threading.local()
        self._lock = threading.Lock()
        self._shared: Optional[sqlite3.Connection] = None
        # Every connection we ever opened, so close_all() can release them.
        self._conns: List[sqlite3.Connection] = []
        if self._memory:
            self._shared = self._new_conn()
        else:
            parent = os.path.dirname(os.path.abspath(self._path))
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
        self._ensure_schema()

    # -- connection plumbing -------------------------------------------------

    @property
    def db_path(self) -> str:
        return self._path

    def _new_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._path, timeout=5.0, check_same_thread=False, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:  # pragma: no cover - already WAL / read-only fs
            pass
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._conns.append(conn)
        return conn

    def _conn(self) -> sqlite3.Connection:
        if self._memory:
            assert self._shared is not None
            return self._shared
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_conn()
            self._local.conn = conn
        return conn

    def _execute(
        self, sql: str, params: Tuple[Any, ...] = ()
    ) -> Tuple[List[sqlite3.Row], Optional[int], int]:
        """Run one statement, returning (rows, lastrowid, rowcount)."""
        conn = self._conn()
        if self._memory:
            with self._lock:
                cursor = conn.execute(sql, params)
                try:
                    rows = cursor.fetchall()
                    return rows, cursor.lastrowid, cursor.rowcount
                finally:
                    cursor.close()
        cursor = conn.execute(sql, params)
        try:
            return cursor.fetchall(), cursor.lastrowid, cursor.rowcount
        finally:
            cursor.close()

    def _ensure_schema(self) -> None:
        conn = self._conn()
        if self._memory:
            with self._lock:
                conn.executescript(SCHEMA)
            return
        conn.executescript(SCHEMA)

    # -- writes --------------------------------------------------------------

    def record_event(
        self,
        *,
        source: str,
        api: str,
        model: Optional[str] = None,
        quantity: Optional[float] = None,
        unit: Optional[str] = None,
        cost: float = 0.0,
        status: Optional[int] = None,
        estimated: bool = False,
        meta: Optional[dict] = None,
        ts: Optional[str] = None,
    ) -> int:
        """Insert one usage event and return its row id."""
        if not isinstance(source, str) or not source:
            raise ValueError("source must be a non-empty string")
        if not isinstance(api, str) or not api:
            raise ValueError("api must be a non-empty string")
        stamp = ts if ts is not None else utc_now_iso()
        cost_value = 0.0 if cost is None else float(cost)
        if not math.isfinite(cost_value):
            # One inf/nan row would poison every SUM(cost) and blank the widget.
            raise ValueError("cost must be finite, got %r" % (cost,))
        cost_value = round(cost_value, 6)
        quantity_value = None if quantity is None else float(quantity)
        status_value = None if status is None else int(status)
        meta_json = (
            None
            if meta is None
            else json.dumps(meta, sort_keys=True, ensure_ascii=False)
        )
        _rows, lastrowid, _count = self._execute(
            "INSERT INTO events"
            " (ts, source, api, model, quantity, unit, cost, status, estimated, meta)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                stamp,
                source,
                api,
                model,
                quantity_value,
                unit,
                cost_value,
                status_value,
                1 if estimated else 0,
                meta_json,
            ),
        )
        return int(lastrowid or 0)

    def set_balance(self, starting: float, currency: str = "INR") -> None:
        """Upsert the single balance row (id = 1)."""
        self._execute(
            "INSERT OR REPLACE INTO balance (id, starting, currency, set_at)"
            " VALUES (1, ?, ?, ?)",
            (float(starting), str(currency), utc_now_iso()),
        )

    def set_kv(self, key: str, value: Optional[str]) -> None:
        """Store ``key`` -> ``value``; a ``None`` value deletes the key."""
        if value is None:
            self._execute("DELETE FROM kv WHERE k = ?", (str(key),))
            return
        self._execute(
            "INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (str(key), str(value))
        )

    def purge_before(self, before_iso: str) -> int:
        """Delete events older than ``before_iso``; return the number removed."""
        _rows, _last, count = self._execute(
            "DELETE FROM events WHERE ts < ?", (before_iso,)
        )
        return int(count or 0)

    # -- reads ---------------------------------------------------------------

    def get_balance(self) -> Optional[dict]:
        rows, _last, _count = self._execute(
            "SELECT starting, currency, set_at FROM balance WHERE id = 1"
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "starting": float(row["starting"]),
            "currency": row["currency"],
            "set_at": row["set_at"],
        }

    def spend_since(self, since_iso: str) -> float:
        rows, _last, _count = self._execute(
            "SELECT COALESCE(SUM(cost), 0.0) FROM events WHERE ts >= ?", (since_iso,)
        )
        total = rows[0][0] if rows else 0.0
        return round(float(total or 0.0), 6)

    def calls_since(self, since_iso: str) -> int:
        rows, _last, _count = self._execute(
            "SELECT COUNT(*) FROM events WHERE ts >= ?", (since_iso,)
        )
        return int((rows[0][0] if rows else 0) or 0)

    def spend_by_api_since(self, since_iso: str) -> List[dict]:
        rows, _last, _count = self._execute(
            "SELECT api, COALESCE(SUM(cost), 0.0) AS spend, COUNT(*) AS calls"
            " FROM events WHERE ts >= ? GROUP BY api"
            " ORDER BY spend DESC, api ASC",
            (since_iso,),
        )
        return [
            {
                "api": row["api"],
                "spend": round(float(row["spend"] or 0.0), 6),
                "calls": int(row["calls"] or 0),
            }
            for row in rows
        ]

    def count_status_since(self, status: int, since_iso: str) -> int:
        rows, _last, _count = self._execute(
            "SELECT COUNT(*) FROM events WHERE status = ? AND ts >= ?",
            (int(status), since_iso),
        )
        return int((rows[0][0] if rows else 0) or 0)

    def count_estimated_since(self, since_iso: str) -> int:
        """Count events flagged ``estimated`` (unmeasurable or unpriced)."""
        rows, _last, _count = self._execute(
            "SELECT COUNT(*) FROM events WHERE estimated = 1 AND ts >= ?",
            (since_iso,),
        )
        return int((rows[0][0] if rows else 0) or 0)

    def count_estimated(self) -> int:
        """Count every ``estimated`` event, all time."""
        rows, _last, _count = self._execute(
            "SELECT COUNT(*) FROM events WHERE estimated = 1"
        )
        return int((rows[0][0] if rows else 0) or 0)

    def last_status_ts(self, status: int) -> Optional[str]:
        rows, _last, _count = self._execute(
            "SELECT MAX(ts) FROM events WHERE status = ?", (int(status),)
        )
        if not rows or rows[0][0] is None:
            return None
        return str(rows[0][0])

    def get_kv(self, key: str) -> Optional[str]:
        rows, _last, _count = self._execute(
            "SELECT v FROM kv WHERE k = ?", (str(key),)
        )
        if not rows:
            return None
        return None if rows[0][0] is None else str(rows[0][0])

    # -- lifecycle -----------------------------------------------------------

    def close_all(self) -> None:
        """Close every connection this ledger opened, on any thread.

        Called on shutdown so the ``-wal``/``-shm`` side files are removed and
        the WAL is checkpointed.  Safe to call more than once.
        """
        with self._lock:
            conns = list(self._conns)
            self._conns = []
        self._shared = None
        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover - already closed
                pass
        self._local = threading.local()

    def close(self) -> None:
        """Close this thread's connection (and the shared in-memory one)."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            self._local.conn = None
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover
                pass
        shared = self._shared
        if shared is not None:
            self._shared = None
            try:
                shared.close()
            except sqlite3.Error:  # pragma: no cover
                pass

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


__all__ = [
    "ISO_FORMAT",
    "Ledger",
    "SCHEMA",
    "local_day_start_iso",
    "local_month_start_iso",
    "utc_now_iso",
]
