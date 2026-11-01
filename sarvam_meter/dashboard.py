# SPDX-License-Identifier: MIT
"""Sarvam dashboard adapter -- the authoritative usage/spend source (SPEC 17).

Reads the numbers the Sarvam dashboard itself shows, over the user's own
signed-in session.  The two captured payloads committed under
``tests/fixtures/`` are the hard contract: nothing below is guessed beyond them.

Confirmed endpoints (SPEC 17.1), base ``https://indus.sarvam.ai``::

    GET /api/dhan/org/usage/summary?start=&end=&group_by=model&product=
    GET /api/dhan/org/usage/timeseries?start=&end=&group_by=product&product=&granularity=auto

Facts encoded here:

* every number arrives as a JSON **string** and is parsed to ``float``; a
  non-numeric string is an error, never a silent ``0``;
* ``currency`` arrives lowercase (``"inr"``) and is normalised to ``"INR"``;
* ``groups[].model`` + ``groups[].total_cost`` are the per-model rows, in
  payload order;
* a ``401``/``403`` means the stored session expired, not that the host is down.

The session (``Cookie`` and ``Authorization`` only) is read from a ``0600`` JSON
file by :func:`load_session`.  A session value is never logged, never returned in
a snapshot and never written into an error message: every message is scrubbed
before it leaves this module.

The HTTP layer is an injectable callable so tests never touch the network::

    transport(method, url, headers, timeout) -> (status, headers, body_bytes)

The default transport mirrors :mod:`sarvam_meter.cliproxy`: certificate
verification on, redirects disabled, an explicit empty ``ProxyHandler({})`` (so
``$http_proxy`` can never receive the session cookie) and a hard timeout.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "DashboardError",
    "DashboardClient",
    "load_session",
    "parse_summary",
    "parse_timeseries",
    "SESSION_HEADERS",
    "SUMMARY_PATH",
    "TIMESERIES_PATH",
]

#: API paths below ``base_url`` (SPEC 17.1).
SUMMARY_PATH = "/api/dhan/org/usage/summary"
TIMESERIES_PATH = "/api/dhan/org/usage/timeseries"

#: The only header names a session file may carry (matched case-insensitively).
SESSION_HEADERS = ("cookie", "authorization")

#: Header names whose values must never reach a log line or an error message.
SECRET_HEADERS = SESSION_HEADERS

#: Default ``product`` query value.
DEFAULT_PRODUCT = "sarvam-api-platform"

#: Date format used by both endpoints.
DATE_FORMAT = "%Y-%m-%d"


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


class DashboardError(Exception):
    """Any failure while reading the dashboard.

    ``status`` is the HTTP status when the server answered at all, else ``None``
    (connection refused, timeout, DNS failure, ...).  ``code`` is the short
    machine-readable tag used in ``snapshot()["error"]["code"]``: ``no_session``,
    ``unauthorized``, ``bad_response``, ``unreachable``, ``http_error``,
    ``bad_request`` or ``config``.
    """

    def __init__(
        self,
        message: str,
        status: Optional[int] = None,
        code: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code or "error"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


# ---------------------------------------------------------------------------
# redaction (a session value never leaves this module)
# ---------------------------------------------------------------------------


def _safe_url(url: str) -> str:
    """Return *url* with any userinfo removed, for error messages."""
    try:
        parts = urllib.parse.urlsplit(str(url))
    except ValueError:  # pragma: no cover - defensive
        return "<url>"
    if parts.username is None and parts.password is None:
        return str(url)
    host = parts.hostname or ""
    if parts.port:
        host = "%s:%d" % (host, parts.port)
    return urllib.parse.urlunsplit(
        (parts.scheme, host, parts.path, parts.query, parts.fragment)
    )


def _redact_headers(headers: Optional[dict]) -> dict:
    """Return a copy of *headers* with every secret value replaced by ``***``."""
    out = {}
    for name, value in (headers or {}).items():
        if str(name).lower() in SECRET_HEADERS:
            out[name] = "***"
        else:
            out[name] = value
    return out


def _scrub(text: str, secrets) -> str:
    """Replace every session value occurring in *text* with ``***``.

    Applied to every message this module emits, so even a transport that raises
    with the request headers embedded in its own exception cannot leak the
    cookie into a snapshot or a log line.  Very short values are left alone
    (they would shred unrelated words and reveal nothing).
    """
    if not isinstance(text, str):
        text = str(text)
    for value in secrets or ():
        if isinstance(value, str) and len(value) >= 4 and value in text:
            text = text.replace(value, "***")
    return text


# ---------------------------------------------------------------------------
# default transport (urllib, verification on, no redirects)
# ---------------------------------------------------------------------------


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """A redirect handler that refuses to follow anything."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _default_transport(
    method: str,
    url: str,
    headers: dict,
    timeout: float,
) -> Tuple[int, dict, bytes]:
    """Perform one HTTP request and return ``(status, headers, body_bytes)``.

    Certificate verification is on (``ssl.create_default_context()``), redirects
    are not followed, and ``timeout`` is a hard per-request deadline.  HTTP error
    statuses are returned as data, not raised; only transport-level failures
    (refused connection, timeout, TLS error) propagate.
    """
    context = ssl.create_default_context()
    # An explicit empty ProxyHandler is essential: build_opener() would otherwise
    # install a default ProxyHandler that honours $http_proxy/$HTTP_PROXY, sending
    # the session cookie to whatever proxy the environment names.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
        _NoRedirectHandler(),
    )
    request = urllib.request.Request(
        url, data=None, headers=dict(headers or {}), method=method
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            status = int(getattr(response, "status", None) or response.getcode())
            response_headers = dict(response.headers.items()) if response.headers else {}
            payload = response.read()
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        response_headers = dict(exc.headers.items()) if exc.headers else {}
        try:
            payload = exc.read()
        except Exception:  # pragma: no cover - defensive
            payload = b""
        finally:
            exc.close()
    return status, response_headers, payload or b""


# ---------------------------------------------------------------------------
# session file
# ---------------------------------------------------------------------------


def load_session(path: str) -> Dict[str, str]:
    """Read the ``0600`` session file and return ``{lower-name: value}``.

    Only ``Cookie`` and ``Authorization`` are accepted (case-insensitive); every
    other key is dropped.  A missing, unreadable or non-object file raises
    :class:`DashboardError` with ``code="no_session"``.  A value is never echoed
    in the message -- only the path (which is not a secret) is named.
    """
    if not isinstance(path, str) or not path.strip():
        raise DashboardError("dashboard session file path is empty", code="no_session")

    expanded = os.path.expanduser(path.strip())
    try:
        with open(expanded, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise DashboardError(
            "cannot read dashboard session file %s (%s)" % (expanded, type(exc).__name__),
            code="no_session",
        )

    try:
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise DashboardError(
            "dashboard session file %s is not valid JSON" % expanded,
            code="no_session",
        )

    if not isinstance(document, dict):
        raise DashboardError(
            "dashboard session file %s is not a JSON object" % expanded,
            code="no_session",
        )

    session: Dict[str, str] = {}
    for name, value in document.items():
        key = str(name).strip().lower()
        if key not in SESSION_HEADERS:
            continue
        if not isinstance(value, str) or not value.strip():
            continue
        session[key] = value.strip()

    return session


# ---------------------------------------------------------------------------
# payload parsing
# ---------------------------------------------------------------------------


def _load_object(payload: Any) -> dict:
    """Decode *payload* (bytes, str or dict) into a JSON object or fail."""
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, (bytes, bytearray)):
        try:
            text = bytes(payload).decode("utf-8", "replace")
        except Exception:  # pragma: no cover - defensive
            raise DashboardError(
                "dashboard payload could not be decoded as UTF-8", code="bad_response"
            )
    elif isinstance(payload, str):
        text = payload
    else:
        raise DashboardError(
            "dashboard payload has an unexpected type (%s)" % type(payload).__name__,
            code="bad_response",
        )

    if not text.strip():
        raise DashboardError("dashboard returned an empty body", code="bad_response")
    try:
        document = json.loads(text)
    except ValueError:
        raise DashboardError("dashboard returned a non-JSON body", code="bad_response")

    if not isinstance(document, dict):
        raise DashboardError(
            "dashboard payload is not a JSON object (%s)" % type(document).__name__,
            code="bad_response",
        )
    return document


def _to_float(value: Any, field: str) -> float:
    """Parse one numeric field to ``float``; a non-number is ``bad_response``.

    Every number in these payloads arrives as a JSON string, so a string is the
    normal case -- but a string that does not parse is an error, never a silent
    ``0``.
    """
    if isinstance(value, bool):
        raise DashboardError(
            "dashboard payload field %s is not a number" % field, code="bad_response"
        )
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise DashboardError(
                "dashboard payload field %s is not a number" % field, code="bad_response"
            )
        try:
            number = float(text)
        except ValueError:
            raise DashboardError(
                "dashboard payload field %s is not a number" % field, code="bad_response"
            )
    else:
        raise DashboardError(
            "dashboard payload field %s is not a number" % field, code="bad_response"
        )
    if number != number or number in (float("inf"), float("-inf")):
        raise DashboardError(
            "dashboard payload field %s is not a finite number" % field, code="bad_response"
        )
    return number


def _to_int(value: Any, field: str) -> int:
    """Parse one integral field (``event_count``); a non-number is ``bad_response``."""
    number = _to_float(value, field)
    return int(round(number))


def _text(value: Any) -> str:
    """Return *value* as a string (``""`` when it is absent)."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def _normalise_currency(value: Any) -> str:
    """Upper-case a currency code (``"inr"`` -> ``"INR"``); absent -> ``""``."""
    if not isinstance(value, str):
        return ""
    return value.strip().upper()


def _parse_range(raw: Any) -> Dict[str, str]:
    """Return ``{"start", "end"}`` from the payload ``range`` object."""
    source = raw if isinstance(raw, dict) else {}
    return {"start": _text(source.get("start")), "end": _text(source.get("end"))}


def _parse_group_by(raw: Any, fallback: str) -> str:
    """Return the single ``group_by`` dimension as a string.

    The payload carries a list (``["model"]``); the parsed shape carries one
    string, so the first entry wins.
    """
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item.strip():
                return item.strip()
        return fallback
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return fallback


def _parse_usage(raw: Any) -> List[dict]:
    """Normalise the ``usage`` list, parsing every ``quantity`` to ``float``."""
    out: List[dict] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        breakdown: List[dict] = []
        raw_breakdown = item.get("breakdown")
        if isinstance(raw_breakdown, list):
            for part in raw_breakdown:
                if not isinstance(part, dict):
                    continue
                breakdown.append(
                    {
                        "unit": _text(part.get("unit")),
                        "quantity": _to_float(part.get("quantity"), "usage.breakdown.quantity"),
                    }
                )
        out.append(
            {
                "unit": _text(item.get("unit")),
                "quantity": _to_float(item.get("quantity"), "usage.quantity"),
                "breakdown": breakdown,
            }
        )
    return out


def _common(payload: Any, group_by_fallback: str) -> Tuple[dict, dict]:
    """Parse the fields both payloads share; return ``(parsed, document)``."""
    document = _load_object(payload)
    if "total_cost" not in document:
        raise DashboardError(
            "dashboard payload has no total_cost", code="bad_response"
        )
    total_cost = _to_float(document.get("total_cost"), "total_cost")

    subtotal_raw = document.get("subtotal")
    if subtotal_raw is None:
        subtotal = 0.0
    else:
        subtotal = _to_float(subtotal_raw, "subtotal")

    parsed = {
        "org_id": _text(document.get("org_id")),
        "currency": _normalise_currency(document.get("currency")),
        "group_by": _parse_group_by(document.get("group_by"), group_by_fallback),
        "range": _parse_range(document.get("range")),
        "total_cost": total_cost,
        "subtotal": subtotal,
        "usage": _parse_usage(document.get("usage")),
    }
    return parsed, document


def parse_summary(payload: Any) -> dict:
    """Parse a ``summary`` payload (bytes, JSON text or an already-decoded dict).

    Returns the SPEC 17.3 shape, including ``by_model`` (payload order preserved,
    ``model`` + ``total_cost`` taken from each group).  A non-numeric number, a
    missing ``total_cost`` or JSON that is not an object raises
    :class:`DashboardError` with ``code="bad_response"``.
    """
    parsed, document = _common(payload, "model")

    by_model: List[dict] = []
    groups = document.get("groups")
    if isinstance(groups, list):
        for group in groups:
            if not isinstance(group, dict):
                continue
            by_model.append(
                {
                    "model": _text(group.get("model")),
                    "cost": _to_float(group.get("total_cost"), "groups.total_cost"),
                    "usage": _parse_usage(group.get("usage")),
                }
            )

    parsed["by_model"] = by_model
    return parsed


def parse_timeseries(payload: Any) -> dict:
    """Parse a ``timeseries`` payload; the same shape as :func:`parse_summary`
    minus ``by_model``, plus ``points`` and ``granularity``.
    """
    parsed, document = _common(payload, "product")

    points: List[dict] = []
    raw_points = document.get("points")
    if isinstance(raw_points, list):
        for point in raw_points:
            if not isinstance(point, dict):
                continue
            points.append(
                {
                    "timestamp": _text(point.get("timestamp")),
                    "cost": _to_float(point.get("total_cost"), "points.total_cost"),
                    "event_count": _to_int(point.get("event_count"), "points.event_count"),
                }
            )

    parsed["granularity"] = _text(document.get("granularity"))
    parsed["points"] = points
    return parsed


# ---------------------------------------------------------------------------
# date validation
# ---------------------------------------------------------------------------


def _validate_date(value: Any, field: str) -> str:
    """Return *value* as a strict ``YYYY-MM-DD`` string or raise ``bad_request``.

    Garbage is rejected here rather than sent to the dashboard, so a caller's bug
    can never become a malformed request against a remote host.
    """
    if not isinstance(value, str):
        raise DashboardError(
            "dashboard %s must be a YYYY-MM-DD string" % field, code="bad_request"
        )
    text = value.strip()
    try:
        moment = datetime.strptime(text, DATE_FORMAT)
    except ValueError:
        raise DashboardError(
            "dashboard %s %r is not a YYYY-MM-DD date" % (field, text),
            code="bad_request",
        )
    # strptime accepts "2026-1-1"; require the exact zero-padded form.
    if moment.strftime(DATE_FORMAT) != text:
        raise DashboardError(
            "dashboard %s %r is not a YYYY-MM-DD date" % (field, text),
            code="bad_request",
        )
    return text


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------


class DashboardClient:
    """Minimal Sarvam dashboard client (SPEC 17.3)."""

    def __init__(
        self,
        base_url: str,
        session: Optional[dict],
        *,
        timeout: float = 15.0,
        product: str = DEFAULT_PRODUCT,
        transport=None,
    ) -> None:
        base = (base_url or "").strip()
        if not base:
            raise DashboardError("dashboard base_url is empty", code="config")
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme not in ("http", "https"):
            raise DashboardError(
                "dashboard base_url must be an http:// or https:// URL", code="config"
            )
        if not parsed.hostname:
            raise DashboardError("dashboard base_url has no host", code="config")
        if parsed.username is not None or parsed.password is not None:
            # Credentials in the URL leak into error messages and /v1/status.
            raise DashboardError(
                "dashboard base_url must not carry credentials", code="config"
            )

        self.base_url = base.rstrip("/")
        self.product = (product or DEFAULT_PRODUCT).strip() or DEFAULT_PRODUCT
        self.timeout = float(timeout)
        self._session = self._clean_session(session)
        self._transport = transport if transport is not None else _default_transport

    # -- introspection ------------------------------------------------------

    @staticmethod
    def _clean_session(session: Optional[dict]) -> Dict[str, str]:
        """Keep only the two accepted headers, keyed by lower-case name."""
        out: Dict[str, str] = {}
        if not isinstance(session, dict):
            return out
        for name, value in session.items():
            key = str(name).strip().lower()
            if key not in SESSION_HEADERS:
                continue
            if not isinstance(value, str) or not value.strip():
                continue
            out[key] = value.strip()
        return out

    @property
    def configured(self) -> bool:
        """True when a session header is available (otherwise nothing is polled)."""
        return bool(self._session)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "DashboardClient(base_url=%r, product=%r, session=%r)" % (
            self.base_url,
            self.product,
            sorted(self._session),
        )

    # -- plumbing -----------------------------------------------------------

    def _secrets(self) -> List[str]:
        return [value for value in self._session.values()]

    def _headers(self) -> dict:
        """Build the request headers, including both session headers."""
        headers = {
            "Accept": "application/json",
            "User-Agent": "sarvam-meter/0.1.0",
        }
        if "cookie" in self._session:
            headers["Cookie"] = self._session["cookie"]
        if "authorization" in self._session:
            headers["Authorization"] = self._session["authorization"]
        return headers

    def _url(self, path: str, start: str, end: str, group_by: str,
             granularity: Optional[str] = None) -> str:
        params = [
            ("start", start),
            ("end", end),
            ("group_by", group_by),
            ("product", self.product),
        ]
        if granularity is not None:
            params.append(("granularity", granularity))
        return "%s%s?%s" % (self.base_url, path, urllib.parse.urlencode(params))

    def _request(self, url: str) -> Tuple[int, dict, bytes]:
        """Perform one GET; transport failures become ``unreachable``."""
        headers = self._headers()
        try:
            result = self._transport("GET", url, headers, self.timeout)
            status, response_headers, raw = result
        except DashboardError:
            raise
        except Exception as exc:  # noqa: BLE001 - transport failures are data
            raise DashboardError(
                _scrub(
                    "dashboard GET %s failed: %s: %s"
                    % (_safe_url(url), type(exc).__name__, exc),
                    self._secrets(),
                ),
                status=None,
                code="unreachable",
            )

        try:
            status_int = int(status)
        except (TypeError, ValueError):
            raise DashboardError(
                "dashboard returned a non-integer status for %s" % _safe_url(url),
                status=None,
                code="bad_response",
            )

        if raw is None:
            raw = b""
        elif isinstance(raw, str):
            raw = raw.encode("utf-8")
        elif isinstance(raw, bytearray):
            raw = bytes(raw)

        return status_int, dict(response_headers or {}), raw

    def _request_json(self, url: str) -> dict:
        """Like :meth:`_request` but maps HTTP errors and requires a JSON object."""
        status, _headers, raw = self._request(url)

        if status in (401, 403):
            raise DashboardError(
                "dashboard rejected the stored session (HTTP %d); "
                "run `sarvam-meter session set` again" % status,
                status=status,
                code="unauthorized",
            )
        if status < 200 or status >= 300:
            raise DashboardError(
                "dashboard returned HTTP %d for %s" % (status, _safe_url(url)),
                status=status,
                code="http_error",
            )
        return _load_object(raw)

    # -- public API ---------------------------------------------------------

    def usage_summary(
        self, start_date: str, end_date: str, group_by: str = "model"
    ) -> dict:
        """Fetch and parse the ``summary`` payload for one date window."""
        start = _validate_date(start_date, "start date")
        end = _validate_date(end_date, "end date")
        url = self._url(SUMMARY_PATH, start, end, group_by or "model")
        return parse_summary(self._request_json(url))

    def usage_timeseries(
        self,
        start_date: str,
        end_date: str,
        group_by: str = "product",
        granularity: str = "auto",
    ) -> dict:
        """Fetch and parse the ``timeseries`` payload for one date window."""
        start = _validate_date(start_date, "start date")
        end = _validate_date(end_date, "end date")
        url = self._url(
            TIMESERIES_PATH,
            start,
            end,
            group_by or "product",
            granularity or "auto",
        )
        return parse_timeseries(self._request_json(url))

    def snapshot(self, *, since_date: str, today: str) -> dict:
        """Build the widget-facing snapshot.  Never raises.

        Three calls: the window summary (``since_date``..``today``), today's own
        summary, and the month-to-date summary (the first of the current month
        .. ``today``).  The month figure exists because the ledger's own month
        total is not comparable once the dashboard is the source.  A failure of
        the today or month call keeps the window result, leaves that field at
        ``0.0`` and records the error.  ``reachable`` is True only when the
        window call produced usable numbers.
        """
        snapshot = {
            "configured": self.configured,
            "reachable": False,
            "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "org_id": None,
            "currency": "INR",
            "window": {"start": _text(since_date), "end": _text(today)},
            "spend": {"window": 0.0, "today": 0.0, "month": 0.0},
            "by_model": [],
            "usage": [],
            "error": None,
        }
        if not snapshot["configured"]:
            return snapshot

        try:
            window = self.usage_summary(since_date, today, group_by="model")
        except DashboardError as exc:
            snapshot["error"] = self._error(exc)
            return snapshot
        except Exception as exc:  # noqa: BLE001 - snapshot must never raise
            snapshot["error"] = self._error(
                DashboardError(
                    "%s: %s" % (type(exc).__name__, exc), code="error"
                )
            )
            return snapshot

        snapshot["reachable"] = True
        snapshot["org_id"] = window.get("org_id") or None
        snapshot["currency"] = window.get("currency") or "INR"
        snapshot["window"] = {
            "start": _text(since_date),
            "end": _text(today),
        }
        snapshot["spend"]["window"] = float(window.get("total_cost") or 0.0)
        snapshot["by_model"] = [
            {"model": entry.get("model", ""), "cost": float(entry.get("cost") or 0.0)}
            for entry in window.get("by_model") or []
            if isinstance(entry, dict)
        ]
        snapshot["usage"] = list(window.get("usage") or [])

        try:
            today_summary = self.usage_summary(today, today, group_by="model")
        except DashboardError as exc:
            snapshot["error"] = self._error(exc)
            return snapshot
        except Exception as exc:  # noqa: BLE001 - snapshot must never raise
            snapshot["error"] = self._error(
                DashboardError(
                    "%s: %s" % (type(exc).__name__, exc), code="error"
                )
            )
            return snapshot

        snapshot["spend"]["today"] = float(today_summary.get("total_cost") or 0.0)

        month_start = "%s-01" % _text(today)[:7]
        try:
            month_summary = self.usage_summary(month_start, today, group_by="model")
        except DashboardError as exc:
            snapshot["error"] = self._error(exc)
            return snapshot
        except Exception as exc:  # noqa: BLE001 - snapshot must never raise
            snapshot["error"] = self._error(
                DashboardError(
                    "%s: %s" % (type(exc).__name__, exc), code="error"
                )
            )
            return snapshot

        snapshot["spend"]["month"] = float(month_summary.get("total_cost") or 0.0)
        return snapshot

    # -- helpers ------------------------------------------------------------

    def _error(self, exc: "DashboardError") -> dict:
        """Turn a :class:`DashboardError` into the snapshot error entry."""
        return {
            "source": "dashboard",
            "code": exc.code or "error",
            "message": _scrub(exc.message, self._secrets()),
        }
