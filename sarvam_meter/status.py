# SPDX-License-Identifier: MIT
"""Status payload builder -- the widget's data contract (SPEC section 10).

The JSON produced by :func:`build_status` is the contract between the daemon and
the Plasma widget.  The shape is exact; only the objects explicitly marked
extensible in the spec may grow new keys.

:func:`build_status` must never raise: a failing ledger read is reported inside
``errors`` with every number zeroed.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_VERSION = 1

#: Lower bound used when a query has to cover "all time".
EPOCH_ISO = "0001-01-01T00:00:00Z"

#: Key names that must never be echoed into the payload (defence in depth; the
#: normalized cliproxy account shape contains none of them).
_SECRET_KEYS = frozenset(
    [
        "api_key",
        "apikey",
        "api_subscription_key",
        "api-subscription-key",
        "authorization",
        "access_token",
        "management_key",
        "password",
        "refresh_token",
        "secret",
        "token",
        "x-management-key",
    ]
)


# ---------------------------------------------------------------------------
# small numeric / time helpers
# ---------------------------------------------------------------------------


def _as_float(value: Any) -> Optional[float]:
    """Return *value* as a finite float, or ``None`` when it is not numeric."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def money(value: Any) -> float:
    """Round a monetary amount to 2 decimal places (display precision)."""
    number = _as_float(value)
    if number is None:
        return 0.0
    rounded = round(number, 2)
    return 0.0 if rounded == 0 else rounded


def fraction(value: Any) -> Optional[float]:
    """Round a 0..1 fraction to 4 decimal places, clamping out-of-range values."""
    number = _as_float(value)
    if number is None:
        return None
    if number < 0.0:
        number = 0.0
    elif number > 1.0:
        number = 1.0
    return round(number, 4)


def _utc_iso(moment: datetime) -> str:
    """Fixed-width UTC ISO8601 with a ``Z`` suffix (string-comparable)."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _time_context(now: Optional[datetime]) -> Tuple[str, str, str, datetime]:
    """Return ``(generated_at, day_start_iso, month_start_iso, now_utc)``.

    A naive ``now`` is interpreted as local time, exactly like
    ``datetime.astimezone()`` does.  "Today" and "this month" are the *local*
    calendar day/month of ``now``, converted back to UTC ISO for the ledger.
    """
    if now is None:
        now = datetime.now(timezone.utc).astimezone()
    elif now.tzinfo is None:
        now = now.astimezone()

    local = now
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)
    return (
        local.isoformat(timespec="seconds"),
        _utc_iso(day_start),
        _utc_iso(month_start),
        local.astimezone(timezone.utc),
    )


def _scrub(value: Any) -> Any:
    """Recursively drop secret-looking keys from a structure."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in _SECRET_KEYS:
                continue
            out[key] = _scrub(item)
        return out
    if isinstance(value, (list, tuple)):
        return [_scrub(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# untracked (estimated) call counting
# ---------------------------------------------------------------------------


def _ledger_db_path(ledger: Any, config: Any) -> Optional[str]:
    for attr in ("db_path", "path", "_db_path", "_path"):
        try:
            candidate = getattr(ledger, attr, None)
        except Exception:
            candidate = None
        if isinstance(candidate, str) and candidate:
            return candidate
    try:
        candidate = getattr(config, "db_path", None)
    except Exception:
        candidate = None
    return candidate if isinstance(candidate, str) and candidate else None


def _count_estimated(ledger: Any, config: Any, since_iso: Optional[str]) -> int:
    """Count events recorded with ``estimated = 1``.

    SPEC section 6's ``Ledger`` API has no method for this, so the count is
    taken from the ledger database directly (a single indexed-friendly COUNT).
    A future ``Ledger.count_estimated_since()`` is used when present.
    """
    boundary = since_iso or EPOCH_ISO
    for method_name in ("count_estimated_since", "count_estimated"):
        method = getattr(ledger, method_name, None)
        if callable(method):
            try:
                return int(method(boundary))
            except TypeError:
                continue
    path = _ledger_db_path(ledger, config)
    if not path or path == ":memory:":
        # An in-memory ledger cannot be reached by a second connection; opening
        # it again would create an empty database and zero the whole payload.
        return 0
    sql = "SELECT COUNT(*) FROM events WHERE estimated = 1 AND ts >= ?"
    connection = sqlite3.connect(path, timeout=5.0)
    try:
        row = connection.execute(sql, (boundary,)).fetchone()
    finally:
        connection.close()
    return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# payload blocks
# ---------------------------------------------------------------------------


def _empty_sarvam(configured: bool, currency: str) -> Dict[str, Any]:
    return {
        "configured": configured,
        "currency": currency,
        "balance": {
            "known": False,
            "remaining": None,
            "starting": None,
            "spent_total": 0.0,
            "set_at": None,
        },
        "spend": {"today": 0.0, "month": 0.0},
        "calls": {"today": 0},
        "by_api": [],
        "rate_limits": {"count_24h": 0, "last_at": None},
        "quota_exhausted": False,
        "quota_exhausted_at": None,
        "untracked_calls": {"today": 0, "total": 0},
    }


def _normalise_by_api(raw: Any) -> List[Dict[str, Any]]:
    entries = []
    if not isinstance(raw, (list, tuple)):
        return entries
    for item in raw:
        if not isinstance(item, dict):
            continue
        entries.append(
            {
                "api": str(item.get("api") or "other"),
                "spend": money(item.get("spend")),
                "calls": int(_as_float(item.get("calls")) or 0),
            }
        )
    return entries


def _sarvam_block(
    config: Any,
    ledger: Any,
    now_utc: datetime,
    day_start_iso: str,
    month_start_iso: str,
) -> Tuple[Dict[str, Any], Optional[Dict[str, str]]]:
    """Build the ``sarvam`` object, plus a ledger error entry when reads fail."""
    sarvam_cfg = getattr(config, "sarvam", None)
    configured = bool(getattr(sarvam_cfg, "enabled", True))
    currency = str(getattr(sarvam_cfg, "currency", "INR") or "INR")
    empty = _empty_sarvam(configured, currency)

    since_24h = _utc_iso(now_utc - timedelta(hours=24))
    try:
        balance = ledger.get_balance()
        spend_today = _as_float(ledger.spend_since(day_start_iso)) or 0.0
        spend_month = _as_float(ledger.spend_since(month_start_iso)) or 0.0
        spend_total = _as_float(ledger.spend_since(EPOCH_ISO)) or 0.0
        calls_today = int(_as_float(ledger.calls_since(day_start_iso)) or 0)
        by_api = ledger.spend_by_api_since(month_start_iso)
        rate_limit_count = int(
            _as_float(ledger.count_status_since(429, since_24h)) or 0
        )
        rate_limit_last = ledger.last_status_ts(429)
        quota_at = ledger.get_kv("quota_exhausted_at")
        untracked_today = _count_estimated(ledger, config, day_start_iso)
        untracked_total = _count_estimated(ledger, config, None)
    except Exception as exc:  # noqa: BLE001 - status must never raise
        message = str(exc) or exc.__class__.__name__
        return empty, {"source": "ledger", "code": "read_failed", "message": message}

    starting_raw = None
    set_at = None
    if isinstance(balance, dict):
        starting_raw = _as_float(balance.get("starting"))
        set_at = balance.get("set_at")

    if starting_raw is not None:
        balance_block = {
            "known": True,
            "remaining": money(starting_raw - spend_total),
            "starting": money(starting_raw),
            "spent_total": money(spend_total),
            "set_at": set_at if isinstance(set_at, str) else None,
        }
    else:
        balance_block = {
            "known": False,
            "remaining": None,
            "starting": None,
            "spent_total": money(spend_total),
            "set_at": None,
        }

    block = {
        "configured": configured,
        "currency": currency,
        "balance": balance_block,
        "spend": {"today": money(spend_today), "month": money(spend_month)},
        "calls": {"today": calls_today},
        "by_api": _normalise_by_api(by_api),
        "rate_limits": {
            "count_24h": rate_limit_count,
            "last_at": rate_limit_last if isinstance(rate_limit_last, str) else None,
        },
        "quota_exhausted": bool(quota_at),
        "quota_exhausted_at": quota_at if isinstance(quota_at, str) else None,
        "untracked_calls": {
            "today": int(untracked_today),
            "total": int(untracked_total),
        },
    }
    return block, None


def worst_fraction(accounts: Any) -> Optional[float]:
    """Minimum known bucket ``remaining_fraction`` over all accounts (4 dp)."""
    worst = None
    if not isinstance(accounts, (list, tuple)):
        return None
    for account in accounts:
        if not isinstance(account, dict):
            continue
        buckets = account.get("buckets")
        if not isinstance(buckets, (list, tuple)):
            continue
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            value = fraction(bucket.get("remaining_fraction"))
            if value is None:
                continue
            if worst is None or value < worst:
                worst = value
    return worst


def _cliproxy_block(config: Any, snapshot: Optional[dict]) -> Dict[str, Any]:
    cliproxy_cfg = getattr(config, "cliproxy", None)
    configured = bool(getattr(cliproxy_cfg, "enabled", False))
    api_version = str(getattr(cliproxy_cfg, "api_version", "v8") or "v8")

    accounts: List[Dict[str, Any]] = []
    reachable = False
    if isinstance(snapshot, dict):
        api_version = str(snapshot.get("api_version") or api_version)
        reachable = bool(snapshot.get("reachable"))
        raw_accounts = snapshot.get("accounts")
        if isinstance(raw_accounts, (list, tuple)):
            for account in raw_accounts:
                if isinstance(account, dict):
                    scrubbed = _scrub(account)
                    if isinstance(scrubbed, dict):
                        accounts.append(scrubbed)

    return {
        "configured": configured,
        "reachable": reachable,
        "api_version": api_version,
        "worst_fraction": worst_fraction(accounts),
        "accounts": accounts,
    }


def _snapshot_errors(snapshot: Optional[dict]) -> List[Dict[str, str]]:
    errors: List[Dict[str, str]] = []
    if not isinstance(snapshot, dict):
        return errors
    raw = snapshot.get("errors")
    if not isinstance(raw, (list, tuple)):
        return errors
    for item in raw:
        if not isinstance(item, dict):
            continue
        errors.append(
            {
                "source": str(item.get("source") or "cliproxy"),
                "code": str(item.get("code") or "error"),
                "message": str(item.get("message") or ""),
            }
        )
    return errors


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------


def build_status(
    config: Any,
    ledger: Any,
    cliproxy_snapshot: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Build the status payload described by SPEC section 10.

    Never raises: ledger failures surface as an ``errors`` entry with zeroed
    numbers, and any other unexpected failure degrades to a minimal payload.
    """
    snapshot = cliproxy_snapshot if isinstance(cliproxy_snapshot, dict) else None
    try:
        generated_at, day_start_iso, month_start_iso, now_utc = _time_context(now)
    except Exception:  # noqa: BLE001 - a broken `now` must not kill the payload
        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        now_utc = datetime.now(timezone.utc)
        day_start_iso = _utc_iso(now_utc.replace(hour=0, minute=0, second=0, microsecond=0))
        month_start_iso = _utc_iso(
            now_utc.replace(hour=0, minute=0, second=0, microsecond=0).replace(day=1)
        )

    errors: List[Dict[str, str]] = []

    try:
        sarvam_block, ledger_error = _sarvam_block(
            config, ledger, now_utc, day_start_iso, month_start_iso
        )
    except Exception as exc:  # noqa: BLE001
        sarvam_cfg = getattr(config, "sarvam", None)
        sarvam_block = _empty_sarvam(
            bool(getattr(sarvam_cfg, "enabled", True)),
            str(getattr(sarvam_cfg, "currency", "INR") or "INR"),
        )
        ledger_error = {
            "source": "ledger",
            "code": "read_failed",
            "message": str(exc) or exc.__class__.__name__,
        }

    if ledger_error is not None:
        errors.append(ledger_error)
    errors.extend(_snapshot_errors(snapshot))

    try:
        cliproxy_block = _cliproxy_block(config, snapshot)
        enabled = bool(getattr(getattr(config, "cliproxy", None), "enabled", False))
    except Exception as exc:  # noqa: BLE001
        cliproxy_block = {
            "configured": False,
            "reachable": False,
            "api_version": "v8",
            "worst_fraction": None,
            "accounts": [],
        }
        enabled = False
        errors.append(
            {
                "source": "cliproxy",
                "code": "read_failed",
                "message": str(exc) or exc.__class__.__name__,
            }
        )

    return {
        "schema": SCHEMA_VERSION,
        "generated_at": generated_at,
        "stale": bool(enabled and snapshot is None),
        "errors": errors,
        "sarvam": sarvam_block,
        "cliproxy": cliproxy_block,
    }
