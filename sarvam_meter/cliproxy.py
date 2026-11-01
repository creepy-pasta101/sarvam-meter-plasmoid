# SPDX-License-Identifier: MIT
"""CLIProxyAPI management-API adapter (SPEC section 9).

Talks to a local CLIProxyAPI (``router-for-me/CLIProxyAPI``) management API to
report OAuth-account quotas.  Facts encoded here were taken from the CLIProxyAPI
Go source (v8) and are treated as a hard contract:

* both auth headers are sent: ``Authorization: Bearer <key>`` **and**
  ``X-Management-Key: <key>``;
* v8 base path is ``/v8/management``, v0 base path is ``/v0/management``;
* account list, quota-provider list, quota fetch and API-key usage live at the
  paths in ``V8_PATHS`` / ``V0_PATHS``;
* payload field names arrive camelCase *or* snake_case and both are accepted;
* the destructive usage-queue endpoints are **never** requested -- their
  handlers *pop* records off a queue, so polling them destroys data.  Any URL
  containing ``queue`` is refused before it reaches the transport.

The HTTP layer is an injectable callable so tests never touch the network::

    transport(method, url, headers, body_bytes, timeout) -> (status, headers, body_bytes)

The default transport uses :mod:`urllib.request` with certificate verification
on, redirects disabled and a hard timeout.
"""

from __future__ import annotations

import ipaddress
import json
import math
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "CliproxyError",
    "CliproxyClient",
    "normalize_account",
    "normalize_quota",
    "worst_fraction",
    "V8_PATHS",
    "V0_PATHS",
]

#: Path suffixes (below ``/<version>/management``) for api_version ``v8``.
V8_PATHS = {
    "accounts": "/credentials",
    "providers": "/credentials/quota/providers",
    "quota_fetch": "/credentials/quota/fetch",
    "api_key_usage": "/observability/usage/api-keys",
}

#: Path suffixes (below ``/<version>/management``) for api_version ``v0``.
V0_PATHS = {
    "accounts": "/auth-files",
    "providers": "/quota/providers",
    "quota_fetch": "/quota/fetch",
    "api_key_usage": "/api-key-usage",
}

#: Header names that must never reach a log line.
SECRET_HEADERS = ("api-subscription-key", "authorization", "x-management-key")

#: Substrings that identify the destructive usage-queue endpoints.
_FORBIDDEN_PATH_MARKERS = ("queue",)

#: Account status values the widget understands.
ACCOUNT_STATUSES = ("active", "disabled", "unavailable", "cooldown", "error")


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


class CliproxyError(Exception):
    """Any failure while talking to CLIProxyAPI.

    ``status`` is the HTTP status when the server answered at all, else ``None``
    (connection refused, timeout, DNS failure, ...).  ``code`` is the short
    machine-readable tag used in ``snapshot()["errors"]``.
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
# header redaction (never log the management key)
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
    return urllib.parse.urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def _redact_headers(headers: Optional[dict]) -> dict:
    """Return a copy of *headers* with every secret value replaced by ``***``."""
    out = {}
    for name, value in (headers or {}).items():
        if str(name).lower() in SECRET_HEADERS:
            out[name] = "***"
        else:
            out[name] = value
    return out


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
    body_bytes: bytes,
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
    # the management key to whatever proxy the environment names.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
        _NoRedirectHandler(),
    )
    data = body_bytes if body_bytes else None
    request = urllib.request.Request(url, data=data, headers=dict(headers or {}), method=method)
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
# small helpers
# ---------------------------------------------------------------------------


def _is_loopback(host: Optional[str]) -> bool:
    """True when *host* names the loopback interface."""
    if not host:
        return False
    name = host.strip().strip("[]").lower()
    if name in ("localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _first_str(*values: Any) -> Optional[str]:
    """Return the first value that is a non-empty string."""
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _first_present(mapping: dict, *keys: str) -> Any:
    """Return the first value present under any of *keys* (camel/snake tolerant)."""
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _as_fraction(value: Any) -> Optional[float]:
    """Return *value* clamped to ``0.0..1.0`` or ``None`` when not a real number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    if number < 0.0:
        return 0.0
    if number > 1.0:
        return 1.0
    return number


def _normalize_reset_time(value: Any) -> Any:
    """Normalise a reset timestamp to UTC ``...Z``, else pass it through verbatim."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        seconds = float(value)
        if not math.isfinite(seconds):
            return value
        if abs(seconds) > 1e11:  # milliseconds
            seconds = seconds / 1000.0
        try:
            moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return value
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    candidate = text
    if candidate.endswith("Z") or candidate.endswith("z"):
        candidate = candidate[:-1] + "+00:00"
    moment = None
    try:
        moment = datetime.fromisoformat(candidate)
    except ValueError:
        moment = None
    if moment is None:
        return value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------


def normalize_account(raw: dict) -> dict:
    """Normalise one raw CLIProxyAPI account record into the widget shape.

    Returns ``{"auth_index", "id", "name", "provider", "label", "status",
    "plan", "buckets", "summary", "error"}``.  ``buckets``/``summary`` are
    filled in later by :meth:`CliproxyClient.snapshot` once quota is fetched.
    """
    source = raw if isinstance(raw, dict) else {}

    auth_index = _first_str(
        source.get("auth_index"),
        source.get("authIndex"),
        source.get("id"),
    )
    account_id = _first_str(source.get("id"), source.get("auth_index"), source.get("authIndex"))
    name = _first_str(source.get("name"), source.get("label"), auth_index) or ""
    label = _first_str(source.get("label"), source.get("name")) or ""
    provider = _first_str(source.get("provider"), source.get("type")) or ""

    raw_status = source.get("status")
    status = None
    if isinstance(raw_status, str) and raw_status.strip():
        candidate = raw_status.strip().lower()
        if candidate in ACCOUNT_STATUSES:
            status = candidate
    if status is None:
        if source.get("disabled") is True:
            status = "disabled"
        elif source.get("unavailable") is True:
            status = "unavailable"
        else:
            status = "active"

    plan = _first_str(
        source.get("plan"),
        source.get("tier_name"),
        source.get("tierName"),
    )

    buckets = source.get("buckets")
    if not isinstance(buckets, list):
        buckets = []
    summary = source.get("summary")
    if not isinstance(summary, list):
        summary = []

    error = source.get("error")
    if error is not None and not isinstance(error, str):
        error = str(error)

    return {
        "auth_index": auth_index,
        "id": account_id,
        "name": name,
        "provider": provider,
        "label": label,
        "status": status,
        "plan": plan,
        "buckets": buckets,
        "summary": summary,
        "error": error,
    }


def _normalize_bucket(raw: dict) -> dict:
    """Normalise one quota bucket; a missing/null/non-numeric fraction is unknown."""
    window = _first_str(raw.get("window"), raw.get("name"), raw.get("key")) or ""
    fraction_raw = _first_present(raw, "remainingFraction", "remaining_fraction")
    fraction = _as_fraction(fraction_raw)
    reset_raw = _first_present(raw, "resetTime", "reset_time", "resetAt", "reset_at")
    description = _first_str(raw.get("description"), raw.get("label")) or ""
    return {
        "window": window,
        "remaining_fraction": fraction,
        "reset_at": _normalize_reset_time(reset_raw),
        "description": description,
        "unknown": fraction is None,
    }


def _normalize_summary_row(raw: dict) -> dict:
    """Normalise one ``summary`` row, preserving ``format``/``currency``."""
    value = _first_present(raw, "value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        value = None
    elif isinstance(value, float) and not math.isfinite(value):
        value = None

    fmt = _first_str(raw.get("format")) or "number"
    currency = _first_str(raw.get("currency"))
    if currency is not None:
        if len(currency) == 3 and currency.isalpha():
            currency = currency.upper()

    return {
        "key": _first_str(raw.get("key"), raw.get("name")) or "",
        "label": _first_str(raw.get("label"), raw.get("key")) or "",
        "value": value,
        "unit": _first_str(raw.get("unit")) or "",
        "format": fmt,
        "currency": currency,
    }


def normalize_quota(raw: dict) -> dict:
    """Normalise a CLIProxyAPI quota payload (camelCase or snake_case).

    Returns ``{"plan", "subscription", "summary", "buckets",
    "server_time_offset_ms", "raw_provider"}`` where ``buckets`` is the flattened
    list of every group's buckets in the widget's bucket shape.
    """
    source = raw if isinstance(raw, dict) else {}

    subscription_raw = source.get("subscription")
    subscription = subscription_raw if isinstance(subscription_raw, dict) else {}
    plan = _first_str(
        subscription.get("plan"),
        subscription.get("tierName"),
        subscription.get("tier_name"),
        subscription.get("tierId"),
        subscription.get("tier_id"),
    )

    buckets: List[dict] = []
    groups = source.get("groups")
    if isinstance(groups, list):
        for group in groups:
            if not isinstance(group, dict):
                continue
            group_buckets = group.get("buckets")
            if not isinstance(group_buckets, list):
                continue
            for bucket in group_buckets:
                if isinstance(bucket, dict):
                    buckets.append(_normalize_bucket(bucket))
    top_buckets = source.get("buckets")
    if not buckets and isinstance(top_buckets, list):
        for bucket in top_buckets:
            if isinstance(bucket, dict):
                buckets.append(_normalize_bucket(bucket))

    summary: List[dict] = []
    summary_raw = source.get("summary")
    if isinstance(summary_raw, list):
        for row in summary_raw:
            if isinstance(row, dict):
                summary.append(_normalize_summary_row(row))

    offset = _first_present(source, "serverTimeOffsetMs", "server_time_offset_ms")
    if isinstance(offset, bool) or not isinstance(offset, (int, float)):
        offset = None
    elif not math.isfinite(float(offset)):
        offset = None

    return {
        "plan": plan,
        "subscription": dict(subscription),
        "summary": summary,
        "buckets": buckets,
        "server_time_offset_ms": offset,
        "raw_provider": _first_str(source.get("provider")),
    }


def worst_fraction(accounts: list) -> Optional[float]:
    """Return the smallest known bucket fraction across *accounts*, else ``None``."""
    worst: Optional[float] = None
    if not isinstance(accounts, (list, tuple)):
        return None
    for account in accounts:
        if not isinstance(account, dict):
            continue
        buckets = account.get("buckets")
        if not isinstance(buckets, list):
            continue
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            if bucket.get("unknown"):
                continue
            fraction = bucket.get("remaining_fraction")
            if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
                continue
            value = float(fraction)
            if not math.isfinite(value):
                continue
            worst = value if worst is None else min(worst, value)
    return worst


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------


class CliproxyClient:
    """Minimal CLIProxyAPI management-API client (SPEC section 9)."""

    def __init__(
        self,
        base_url: str,
        management_key: str,
        api_version: str = "v8",
        timeout: float = 10.0,
        allow_remote: bool = False,
        transport=None,
        include_api_key_usage: bool = False,
    ) -> None:
        base = (base_url or "").strip()
        if not base:
            raise CliproxyError("cliproxy base_url is empty", code="config")
        if not (base.startswith("http://") or base.startswith("https://")):
            raise CliproxyError(
                "cliproxy base_url must be an http:// or https:// URL", code="config"
            )
        parsed = urllib.parse.urlsplit(base)
        if not parsed.hostname:
            raise CliproxyError("cliproxy base_url has no host", code="config")
        if parsed.username is not None or parsed.password is not None:
            # Credentials in the URL leak into error messages and /v1/status.
            raise CliproxyError(
                "cliproxy base_url must not carry credentials", code="config"
            )
        if not allow_remote and not _is_loopback(parsed.hostname):
            raise CliproxyError(
                "cliproxy base_url %r is not loopback; set allow_remote to permit it" % base,
                code="config",
            )
        if api_version not in ("v8", "v0"):
            raise CliproxyError(
                "unsupported cliproxy api_version %r (expected 'v8' or 'v0')" % (api_version,),
                code="config",
            )

        self.base_url = base.rstrip("/")
        self.management_key = management_key or ""
        self.api_version = api_version
        self.timeout = float(timeout)
        self.allow_remote = bool(allow_remote)
        self.include_api_key_usage = bool(include_api_key_usage)
        self._transport = transport if transport is not None else _default_transport
        self._paths = V8_PATHS if api_version == "v8" else V0_PATHS

    # -- introspection ------------------------------------------------------

    @property
    def configured(self) -> bool:
        """True when a management key is available (otherwise nothing is polled)."""
        return bool(self.management_key.strip())

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "CliproxyClient(base_url=%r, api_version=%r, key=%r)" % (
            self.base_url,
            self.api_version,
            "***" if self.management_key else "",
        )

    # -- plumbing -----------------------------------------------------------

    def _url(self, key: str) -> str:
        return "%s/%s/management%s" % (self.base_url, self.api_version, self._paths[key])

    def _headers(self, has_body: bool) -> dict:
        headers = {
            "Accept": "application/json",
            "User-Agent": "sarvam-meter/0.1.0",
        }
        if self.management_key:
            headers["Authorization"] = "Bearer " + self.management_key
            headers["X-Management-Key"] = self.management_key
        if has_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _request(
        self,
        method: str,
        key: str,
        payload: Optional[dict] = None,
    ) -> Tuple[str, int, dict, bytes]:
        """Perform one management request; return (url, status, headers, body)."""
        url = self._url(key)
        lowered = url.lower()
        for marker in _FORBIDDEN_PATH_MARKERS:
            if marker in lowered:
                raise CliproxyError(
                    "refusing to request the destructive usage-queue endpoint %r" % url,
                    code="forbidden_endpoint",
                )

        body = b""
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        headers = self._headers(bool(body))

        try:
            status, response_headers, raw = self._transport(
                method, url, headers, body, self.timeout
            )
        except CliproxyError:
            raise
        except Exception as exc:  # noqa: BLE001 - transport failures are data
            raise CliproxyError(
                "cliproxy %s %s failed: %s: %s"
                % (method, _safe_url(url), type(exc).__name__, exc),
                status=None,
                code="unreachable",
            )

        try:
            status_int = int(status)
        except (TypeError, ValueError):
            raise CliproxyError(
                "cliproxy returned a non-integer status for %s" % url,
                status=None,
                code="bad_response",
            )

        if raw is None:
            raw = b""
        elif isinstance(raw, str):
            raw = raw.encode("utf-8")
        elif isinstance(raw, bytearray):
            raw = bytes(raw)

        return url, status_int, dict(response_headers or {}), raw

    def _request_json(
        self,
        method: str,
        key: str,
        payload: Optional[dict] = None,
    ) -> Any:
        """Like :meth:`_request` but maps HTTP errors and decodes JSON."""
        url, status, _headers, raw = self._request(method, key, payload)

        if status in (401, 403):
            raise CliproxyError(
                "cliproxy rejected the management key (HTTP %d)" % status,
                status=status,
                code="unauthorized",
            )
        if status == 404:
            raise CliproxyError(
                "cliproxy endpoint missing: %s (HTTP 404)" % url,
                status=status,
                code="endpoint_missing",
            )
        if status < 200 or status >= 300:
            raise CliproxyError(
                "cliproxy returned HTTP %d for %s" % (status, url),
                status=status,
                code="http_error",
            )

        if not raw.strip():
            return None
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            raise CliproxyError(
                "cliproxy returned a non-JSON body for %s" % url,
                status=status,
                code="bad_response",
            )

    # -- public API ---------------------------------------------------------

    def health(self) -> dict:
        """Probe the management API; never raises.

        Returns ``{"reachable", "ok", "status", "api_version", "error"}``.
        ``reachable`` is True whenever the server answered at all -- a 401 still
        means the daemon is up, it just rejected our key.
        """
        info = {
            "reachable": False,
            "ok": False,
            "status": None,
            "api_version": self.api_version,
            "error": None,
        }
        try:
            _url, status, _headers, _raw = self._request("GET", "accounts")
        except CliproxyError as exc:
            info["error"] = exc.message
            return info
        info["reachable"] = True
        info["status"] = status
        info["ok"] = 200 <= status < 300
        if not info["ok"]:
            info["error"] = "cliproxy returned HTTP %d" % status
        return info

    def list_accounts(self) -> list:
        """Return the normalised account list.

        The payload may put the list under ``files`` (v8/v0) or ``accounts``
        (v8), or be a bare JSON array.  An empty list is not an error.
        """
        data = self._request_json("GET", "accounts")
        if data is None:
            return []

        raw_accounts: list = []
        if isinstance(data, list):
            raw_accounts = data
        elif isinstance(data, dict):
            for key in ("files", "accounts", "data", "items"):
                candidate = data.get(key)
                if isinstance(candidate, list):
                    raw_accounts = candidate
                    break
        else:
            raise CliproxyError(
                "cliproxy account list has an unexpected shape (%s)" % type(data).__name__,
                code="bad_response",
            )

        return [normalize_account(item) for item in raw_accounts if isinstance(item, dict)]

    def quota_providers(self) -> list:
        """Return the raw quota-provider entries (``{"providers": [...]}``)."""
        data = self._request_json("GET", "providers")
        if data is None:
            return []
        if isinstance(data, list):
            return list(data)
        if isinstance(data, dict):
            for key in ("providers", "data", "items"):
                candidate = data.get(key)
                if isinstance(candidate, list):
                    return list(candidate)
        return []

    def fetch_quota(self, auth_index: str, provider: Optional[str] = None) -> Optional[dict]:
        """Fetch and normalise the quota for one account.

        Returns the :func:`normalize_quota` shape, or ``None`` for an empty body.
        A ``501`` (no quota provider for this account) raises a
        :class:`CliproxyError` whose message is ``"no quota provider"``.
        """
        payload: Dict[str, Any] = {"auth_index": auth_index}
        if provider:
            payload["provider"] = provider
        try:
            data = self._request_json("POST", "quota_fetch", payload)
        except CliproxyError as exc:
            if exc.status == 501:
                raise CliproxyError(
                    "no quota provider", status=501, code="no_quota_provider"
                )
            raise
        if not isinstance(data, dict):
            return None
        return normalize_quota(data)

    def api_key_usage(self) -> Optional[dict]:
        """Return API-key usage aggregates, or ``None`` when unsupported."""
        try:
            data = self._request_json("GET", "api_key_usage")
        except CliproxyError as exc:
            if exc.status == 404:
                return None
            raise
        if not isinstance(data, dict):
            return None
        return data

    def snapshot(self) -> dict:
        """Build the widget-facing snapshot.  Never raises.

        ``{"configured", "reachable", "api_version", "accounts", "errors"}``.
        Each error entry is ``{"source": "cliproxy", "code": ..., "message": ...}``.
        One failing account never fails the snapshot.
        """
        snapshot = {
            "configured": self.configured,
            "reachable": False,
            "api_version": self.api_version,
            "accounts": [],
            "errors": [],
        }
        if not snapshot["configured"]:
            return snapshot

        try:
            accounts = self.list_accounts()
        except CliproxyError as exc:
            snapshot["errors"].append(
                {
                    "source": "cliproxy",
                    "code": exc.code or "error",
                    "message": exc.message,
                }
            )
            return snapshot
        except Exception as exc:  # noqa: BLE001 - snapshot must never raise
            snapshot["errors"].append(
                {
                    "source": "cliproxy",
                    "code": "error",
                    "message": "%s: %s" % (type(exc).__name__, exc),
                }
            )
            return snapshot

        snapshot["reachable"] = True

        for account in accounts:
            entry = dict(account)
            entry["buckets"] = []
            entry["summary"] = []
            entry["error"] = None

            auth_index = entry.get("auth_index")
            if not auth_index:
                entry["error"] = "account has no auth_index"
                snapshot["accounts"].append(entry)
                continue

            try:
                quota = self.fetch_quota(auth_index, entry.get("provider") or None)
            except CliproxyError as exc:
                if exc.status == 501:
                    entry["error"] = "no quota provider"
                else:
                    entry["error"] = exc.message
                snapshot["accounts"].append(entry)
                continue
            except Exception as exc:  # noqa: BLE001 - one bad account is isolated
                entry["error"] = "%s: %s" % (type(exc).__name__, exc)
                snapshot["accounts"].append(entry)
                continue

            if quota:
                entry["buckets"] = quota.get("buckets") or []
                entry["summary"] = quota.get("summary") or []
                if quota.get("plan"):
                    entry["plan"] = quota["plan"]
            snapshot["accounts"].append(entry)

        if self.include_api_key_usage:
            # Optional and non-fatal: an older CLIProxyAPI build may not have it.
            try:
                usage = self.api_key_usage()
            except Exception:  # noqa: BLE001 - never fail the snapshot over it
                usage = None
            if usage is not None:
                snapshot["api_key_usage"] = usage

        return snapshot
