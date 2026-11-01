# SPDX-License-Identifier: MIT
"""Status payload builder -- the widget's data contract (SPEC sections 10 and 17.5).

The JSON produced by :func:`build_status` is the contract between the daemon and
the Plasma widget.  The shape is exact; only the objects explicitly marked
extensible in the spec may grow new keys.  Schema 2 adds the dashboard source:
``balance.source``/``spend.source`` and the ``sarvam.dashboard`` block.

:func:`build_status` must never raise: a failing ledger read is reported inside
``errors`` with every number zeroed.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_VERSION = 2

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


def _empty_sarvam(
    configured: bool, currency: str, dashboard: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    return {
        "configured": configured,
        "currency": currency,
        "balance": {
            "known": False,
            "remaining": None,
            "starting": None,
            "spent_total": 0.0,
            "set_at": None,
            "source": "ledger",
        },
        "spend": {"today": 0.0, "month": 0.0, "source": "ledger"},
        "calls": {"today": 0},
        "by_api": [],
        "rate_limits": {"count_24h": 0, "last_at": None},
        "quota_exhausted": False,
        "quota_exhausted_at": None,
        "untracked_calls": {"today": 0, "total": 0},
        "dashboard": dashboard if dashboard is not None else _empty_dashboard(False),
    }


def _empty_dashboard(configured: bool) -> Dict[str, Any]:
    """The ``sarvam.dashboard`` block with nothing known yet."""
    return {
        "configured": bool(configured),
        "reachable": False,
        "as_of": None,
        "org_id": None,
        "currency": "INR",
        "window": {"start": None, "end": None},
        "spend": {"window": 0.0, "today": 0.0},
        "by_model": [],
        "error": None,
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


def _normalise_window(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {"start": None, "end": None}
    start = raw.get("start")
    end = raw.get("end")
    return {
        "start": start if isinstance(start, str) else None,
        "end": end if isinstance(end, str) else None,
    }


def _normalise_dashboard_spend(raw: Any) -> Dict[str, float]:
    if not isinstance(raw, dict):
        return {"window": 0.0, "today": 0.0}
    return {
        "window": money(raw.get("window")),
        "today": money(raw.get("today")),
    }


def _normalise_by_model(raw: Any) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    if not isinstance(raw, (list, tuple)):
        return entries
    for item in raw:
        if not isinstance(item, dict):
            continue
        model = item.get("model")
        entries.append(
            {
                "model": str(model) if model is not None else "",
                "cost": money(item.get("cost")),
            }
        )
    return entries


def _normalise_dashboard_error(raw: Any) -> Optional[Dict[str, str]]:
    """One ``{"source","code","message"}`` entry, or ``None``.

    Accepts the snapshot's singular ``error`` object (SPEC 17.3) and, defensively,
    a list of them.  A session value can never reach this point: the snapshot
    shape carries no credentials, and ``_scrub`` is applied as a second guard.
    """
    entry = raw
    if isinstance(raw, (list, tuple)):
        entry = raw[0] if raw else None
    if not isinstance(entry, dict):
        return None
    scrubbed = _scrub(entry)
    if not isinstance(scrubbed, dict):  # pragma: no cover - defensive
        return None
    return {
        "source": str(scrubbed.get("source") or "dashboard"),
        "code": str(scrubbed.get("code") or "error"),
        "message": str(scrubbed.get("message") or ""),
    }


def _dashboard_block(config: Any, snapshot: Optional[dict]) -> Dict[str, Any]:
    """The ``sarvam.dashboard`` object (always present, SPEC 17.5)."""
    section = getattr(config, "dashboard", None)
    configured = bool(getattr(section, "enabled", False))
    block = _empty_dashboard(configured)
    if not isinstance(snapshot, dict):
        return block

    block["configured"] = bool(snapshot.get("configured", configured))
    block["reachable"] = bool(snapshot.get("reachable"))
    as_of = snapshot.get("as_of")
    block["as_of"] = as_of if isinstance(as_of, str) else None
    org_id = snapshot.get("org_id")
    block["org_id"] = org_id if isinstance(org_id, str) else None
    currency = snapshot.get("currency")
    block["currency"] = str(currency) if isinstance(currency, str) and currency else "INR"
    block["window"] = _normalise_window(snapshot.get("window"))
    block["spend"] = _normalise_dashboard_spend(snapshot.get("spend"))
    block["by_model"] = _normalise_by_model(snapshot.get("by_model"))
    error = _normalise_dashboard_error(snapshot.get("error"))
    if error is None:
        error = _normalise_dashboard_error(snapshot.get("errors"))
    block["error"] = error
    return block


def _sarvam_block(
    config: Any,
    ledger: Any,
    now_utc: datetime,
    day_start_iso: str,
    month_start_iso: str,
    dashboard: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], Optional[Dict[str, str]]]:
    """Build the ``sarvam`` object, plus a ledger error entry when reads fail.

    Money sources follow SPEC 17.5: when the dashboard snapshot is reachable the
    balance and ``spend.today`` come from the dashboard (``source: "dashboard"``),
    otherwise from the ledger (``source: "ledger"``).
    """
    sarvam_cfg = getattr(config, "sarvam", None)
    configured = bool(getattr(sarvam_cfg, "enabled", True))
    currency = str(getattr(sarvam_cfg, "currency", "INR") or "INR")
    dashboard_block = dashboard if isinstance(dashboard, dict) else _empty_dashboard(
        bool(getattr(getattr(config, "dashboard", None), "enabled", False))
    )
    empty = _empty_sarvam(configured, currency, dashboard_block)
    dashboard_reachable = bool(dashboard_block.get("reachable"))

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

    source = "dashboard" if dashboard_reachable else "ledger"

    if starting_raw is not None:
        if dashboard_reachable:
            # The dashboard is authoritative for the window that started when the
            # starting balance was set (SPEC 17.5).
            dashboard_spend = dashboard_block.get("spend") or {}
            measured = _as_float(dashboard_spend.get("window")) or 0.0
            remaining = max(0.0, starting_raw - measured)
        else:
            measured = spend_total
            remaining = starting_raw - measured
        balance_block = {
            "known": True,
            "remaining": money(remaining),
            "starting": money(starting_raw),
            "spent_total": money(measured),
            "set_at": set_at if isinstance(set_at, str) else None,
            "source": source,
        }
    else:
        balance_block = {
            "known": False,
            "remaining": None,
            "starting": None,
            "spent_total": money(spend_total),
            "set_at": None,
            "source": source,
        }

    if dashboard_reachable:
        dashboard_spend = dashboard_block.get("spend") or {}
        today_value = _as_float(dashboard_spend.get("today"))
        if today_value is not None:
            spend_today = today_value
        # The ledger's month total is not comparable once the dashboard is the
        # source: it only sees proxied calls.  Prefer the dashboard's own
        # month-to-date figure when it is present.
        month_value = _as_float(dashboard_spend.get("month"))
        if month_value is not None:
            spend_month = month_value

    block = {
        "configured": configured,
        "currency": currency,
        "balance": balance_block,
        "spend": {
            "today": money(spend_today),
            "month": money(spend_month),
            "source": source,
        },
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
        "dashboard": dashboard_block,
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
    dashboard_snapshot: Optional[dict] = None,
) -> dict:
    """Build the status payload described by SPEC sections 10 and 17.5 (schema 2).

    Never raises: ledger failures surface as an ``errors`` entry with zeroed
    numbers, and any other unexpected failure degrades to a minimal payload.
    ``dashboard_snapshot`` is the dict produced by the dashboard client's
    ``snapshot()``; ``None`` means "no dashboard data" and leaves every money
    figure on the ledger with ``source: "ledger"``.
    """
    snapshot = cliproxy_snapshot if isinstance(cliproxy_snapshot, dict) else None
    dash_snapshot = dashboard_snapshot if isinstance(dashboard_snapshot, dict) else None
    try:
        dashboard_block = _dashboard_block(config, dash_snapshot)
    except Exception:  # noqa: BLE001 - a malformed snapshot must not kill the payload
        dashboard_block = _empty_dashboard(
            bool(getattr(getattr(config, "dashboard", None), "enabled", False))
        )
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
            config, ledger, now_utc, day_start_iso, month_start_iso, dashboard_block
        )
    except Exception as exc:  # noqa: BLE001
        sarvam_cfg = getattr(config, "sarvam", None)
        sarvam_block = _empty_sarvam(
            bool(getattr(sarvam_cfg, "enabled", True)),
            str(getattr(sarvam_cfg, "currency", "INR") or "INR"),
            dashboard_block,
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
