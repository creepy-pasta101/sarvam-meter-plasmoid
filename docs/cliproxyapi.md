# CLIProxyAPI

The second data source. Unlike Sarvam, this one is a real quota API: the
management interface of CLIProxyAPI reports actual remaining quota for the OAuth
accounts it proxies.

## What CLIProxyAPI is

[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) is a local proxy server
that fronts subscription-based OAuth accounts — Claude, Codex, Gemini-CLI style
plans — behind an API. Because those are subscriptions rather than metered API
keys, "how much is left" is only known to the proxy, not to any public endpoint.
Its management API exposes that as per-account quota, which is what this project
reads.

`sarvam_meter/cliproxy.py` is a read-only client for that management API. It never
proxies traffic through CLIProxyAPI, and it never changes anything there.

## Enabling it

1. Run CLIProxyAPI and note its address (the default in this project's config is
   `http://127.0.0.1:8317`) and its management key.
2. Put the key in the environment rather than the config file:

   ```sh
   export SARVAM_METER_CLIPROXY_KEY='<management key>'
   ```

   `SARVAM_METER_CLIPROXY_KEY`, when non-empty, replaces
   `cliproxy.management_key`.
3. Enable the section in `~/.config/sarvam-meter/config.json`:

   ```json
   "cliproxy": { "enabled": true, "base_url": "http://127.0.0.1:8317",
                 "management_key": "", "api_version": "v8", "quota_fetch": true,
                 "quota_fetch_interval_seconds": 300, "include_api_key_usage": false,
                 "allow_remote": false }
   ```

4. Restart the daemon. `sarvam-meter doctor` reports whether CLIProxyAPI is
   reachable when it is enabled, and `POST /v1/refresh` on the daemon forces an
   immediate re-poll instead of waiting for the next interval.

`api_version` selects the path family: `v8` (default) or `v0`. Set it to whichever
management API your CLIProxyAPI build serves; a mismatch shows up as
`endpoint_missing` rather than as a crash.

`base_url` must be loopback unless `allow_remote` is `true`. Loopback means
`127.0.0.0/8`, `::1`, or `localhost`.

## Endpoints the adapter uses

Both headers are sent on every request:

```
Authorization: Bearer <management-key>
X-Management-Key: <management-key>
```

| Operation | `api_version: v8` | `api_version: v0` |
| --- | --- | --- |
| Base path | `/v8/management` | `/v0/management` |
| Account list | `GET /v8/management/credentials` | `GET /v0/management/auth-files` |
| Quota providers | `GET /v8/management/credentials/quota/providers` | `GET /v0/management/quota/providers` |
| Quota fetch | `POST /v8/management/credentials/quota/fetch` | `POST /v0/management/quota/fetch` |
| API-key usage (optional) | `GET /v8/management/observability/usage/api-keys` | `GET /v0/management/api-key-usage` |

The account list response is expected to look like

```json
{"files": [{"id": "...", "auth_index": "...", "name": "...", "type": "...",
            "provider": "claude", "label": "...", "status": "active",
            "status_message": "", "disabled": false, "unavailable": false,
            "source": "...", "size": 0}]}
```

v8 builds may return the same array under `"accounts"` instead of `"files"`, and
some builds return a bare JSON array. All three are accepted.

The quota fetch is a `POST` with body `{"auth_index": "<idx>", "provider":
"<optional>"}`, where `auth_index` comes from the account list. API-key usage,
when `include_api_key_usage` is on, returns
`{provider: {"base_url|key": {"success": N, "failed": N, "recent_requests": [...]}}}`.

## The one endpoint that must never be called

> **Warning: `/usage-queue` (v0) and `/observability/usage/queue` (v8) are
> destructive.** The handler *pops* records off a queue, so every poll permanently
> removes data from the proxy. There is no read-only mode and no replay.
>
> The adapter refuses to call these paths, never polls them, and a unit test
> asserts that the client never requests them — even when a snapshot is refreshed
> repeatedly. If you are extending the adapter, keep that test passing.

## Normalized quota shape

`fetch_quota()` returns the management API's normalized quota, which the adapter
passes through `normalize_quota()` into the account's `buckets` and `summary`.
The raw shape from CLIProxyAPI is:

```json
{"subscription": {"plan": "...", "tierName": "...", "tierId": "..."},
 "summary": [{"key": "...", "label": "...", "value": 1.0, "unit": "...", "format": "number|currency", "currency": "USD"}],
 "serverTimeOffsetMs": 0,
 "groups": [{"displayName": "...", "buckets": [{"window": "5h", "remainingFraction": 0.62,
             "resetTime": "2026-11-01T10:00:00Z", "description": "..."}]}]}
```

and each account in the status payload ends up as:

```json
{"auth_index": "...", "id": "...", "name": "...", "provider": "claude",
 "label": "...", "status": "active", "plan": "Max",
 "buckets": [{"window": "5h", "remaining_fraction": 0.62,
              "reset_at": "2026-11-01T10:00:00Z", "description": "", "unknown": false}],
 "summary": [{"key": "...", "label": "...", "value": 1.0, "unit": "...",
              "format": "currency", "currency": "USD"}],
 "error": null}
```

Normalization rules:

- **camelCase or snake_case, both accepted.** `remainingFraction` /
  `remaining_fraction`, `resetTime` / `reset_time`, `displayName` /
  `display_name`, `tierName` / `tier_name`, `serverTimeOffsetMs` /
  `server_time_offset_ms`. Different CLIProxyAPI builds emit different casing, and
  the adapter does not care.
- `remaining_fraction` is clamped to `0.0..1.0`.
- A missing, null or non-numeric fraction yields `"unknown": true` and
  `"remaining_fraction": null`. It is never faked as `0` (which would look like an
  exhausted account) or `1` (which would look like a full one). The widget prints
  "unknown" for those buckets and leaves them out of the worst-fraction minimum.
- `resetTime` is normalized to UTC `...Z` when it parses; otherwise it is passed
  through verbatim, so a past or oddly formatted timestamp is still visible.
- `status` is taken from the account's `status` string when present, otherwise
  derived from the `disabled` / `unavailable` flags.
- An account whose quota fetch fails keeps `"buckets": []` and an `error` string.
  One failing account never fails the snapshot.

`worst_fraction(accounts)` is the minimum of all known bucket fractions, and
`null` when nothing is known.

## Error handling

`snapshot()` never raises. It returns
`{"configured", "reachable", "api_version", "accounts", "errors"}` and maps
failures into entries the widget displays:

| Condition | Result |
| --- | --- |
| connection refused, timeout | `reachable: false`, `errors: [{"source": "cliproxy", "code": "unreachable", "message": "..."}]` |
| `401` or `403` | `code: "unauthorized"` — the management key is missing or wrong |
| `404` | `code: "endpoint_missing"` — wrong `api_version` for this server, or an older build |
| `501` from a quota fetch | per-account `error: "no quota provider"` — the account has no quota provider behind it |
| non-JSON body (e.g. an HTML error page) | `code: "bad_response"` |

None of these is fatal: the Sarvam section keeps working, the widget shows the
error in its footer, and the last good snapshot stays on screen (`stale: true`
when no snapshot exists yet).

The transport is injectable — `CliproxyClient(..., transport=callable)` — so the
test suite runs with a fake transport and no network at all. The default transport
uses `urllib.request` with TLS verification on and follows no redirects.

## What the widget shows

For each account, one row:

- account name / label and provider,
- the plan from `subscription.plan` when present,
- one bar per bucket: remaining fraction as a filled bar, the `window` label as
  CLIProxyAPI reports it (for example `5h`), and the reset time,
- `summary` entries in their declared format (`number` or `currency` with the
  currency code),
- the word "unknown" instead of a bar when `remaining_fraction` is null,
- the account's `error` string when its fetch failed.

The whole section lives in a `ScrollView`, so a dozen accounts do not overflow the
popup. In the compact panel representation, `compactMode: cliproxy` shows the
single worst remaining fraction across all accounts (`worst_fraction`). The `!`
badge is driven by the widget being offline and by `sarvam.quota_exhausted`, not
by CLIProxyAPI state; a missing or stale snapshot is reported in the footer and in
`errors`.

Refresh cadence is `cliproxy.quota_fetch_interval_seconds` (default 300) in the
daemon's background thread; the widget just re-reads the cached status payload, so
polling the widget costs CLIProxyAPI nothing.
