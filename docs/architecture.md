# Architecture

This document describes how `sarvam-meter-plasmoid` is put together: what each
module owns, the on-disk schema, the JSON the widget consumes, the threading
model, how the recording proxy streams, and the security posture.

The authoritative contract is [SPEC.md](../SPEC.md). Where this document and the
spec disagree, the spec wins.

## 1. Process model

Two independent servers, one config document, one database:

| Process | Default bind | Purpose | Started by |
| --- | --- | --- | --- |
| daemon | `127.0.0.1:8787` | serves the status JSON the widget reads; caches the CLIProxyAPI snapshot | `sarvam-meter serve` |
| recording proxy | `127.0.0.1:8788` | reverse proxy for `https://api.sarvam.ai` that measures and prices each call | `sarvam-meter serve --with-proxy`, or `sarvam-meter proxy` |

The proxy is disabled by default (`proxy.enabled: false`). The daemon and the
proxy can run in one process (`serve --with-proxy`) or in two separate processes;
either way they share the same SQLite ledger file, which is what makes
cross-process concurrency safe (WAL, §5).

The widget is a separate, dumb consumer: it issues `GET` against the daemon and
does nothing else on the network.

```
client --> proxy:8788 --> api.sarvam.ai
             |  one events row per upstream attempt
             v
        usage.db  <-- daemon:8787 <-- widget
             ^
             +-- daemon --> CLIProxyAPI management API
```

## 2. Module responsibilities

| Module | Responsibility |
| --- | --- |
| `sarvam_meter/config.py` | Defaults, load, deep-merge, validation, env overrides, SSRF guard, `write_default_config` (0600 file / 0700 dir). Defines `Config`, `DaemonConfig`, `ProxyConfig`, `SarvamConfig`, `CliproxyConfig`, `ConfigError`, `DEFAULT_CONFIG_PATH`. |
| `sarvam_meter/rates.py` | The rate card (`DEFAULT_RATE_CARD`), `rate_card(overrides)`, `cost_for(...)`, `rate_known(...)`. Pure functions, no I/O. Money is INR floats, rounded to 6 decimals on write. |
| `sarvam_meter/ledger.py` | SQLite storage: schema creation, `record_event`, balance upsert/read, spend and call aggregates, status counters, `kv` scratch space, `purge_before`. Thread-safe via per-thread connections. |
| `sarvam_meter/audio.py` | `duration_seconds(data, content_type)` and `container_of(data)` — derives STT duration from WAV / FLAC / OGG / MP3 / MP4 bytes, because Sarvam's STT response carries no duration. |
| `sarvam_meter/proxy.py` | The recording reverse proxy: `ProxyServer`, `classify`, `chat_usage`, `chat_stream_usage`, `tts_char_count`, `record_from_exchange`. Streaming relay, metering, error recording. |
| `sarvam_meter/cliproxy.py` | CLIProxyAPI management-API adapter: `CliproxyClient`, `normalize_account`, `normalize_quota`, `worst_fraction`, `CliproxyError`. See [cliproxyapi.md](cliproxyapi.md). |
| `sarvam_meter/status.py` | `build_status(config, ledger, cliproxy_snapshot=None, now=None)` — assembles the widget contract. Never raises. |
| `sarvam_meter/daemon.py` | `ThreadingHTTPServer` on `daemon.host:daemon.port` serving `/v1/status`, `/v1/health`, `/v1/refresh`, `/v1/config`; owns the background CLIProxyAPI refresh thread and its lock. |
| `sarvam_meter/cli.py` | `argparse` front end (`init`, `serve`, `status`, `balance set/show`, `record`, `proxy`, `doctor`), exit codes 0/1/2, and the human-readable `status` output. |
| `sarvam_meter/__main__.py` | `python3 -m sarvam_meter` shim into `cli.main()`. |

Dependency direction is one way and shallow:

```
config --> (nobody)
rates  --> (nobody)
audio  --> (nobody)
ledger --> (nobody)
cliproxy --> (nobody)
proxy  --> config, rates, audio, ledger
status --> config, ledger
daemon --> config, ledger, cliproxy, status
cli    --> all of the above
```

Everything is Python standard library only, Python 3.9+.

## 3. Database schema

One SQLite file, default `~/.local/share/sarvam-meter/usage.db` (`db_path`,
`~`-expanded). The schema is applied idempotently on `Ledger.__init__`.

```sql
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,            -- UTC ISO8601, e.g. 2026-11-01T04:30:00Z
  source TEXT NOT NULL,        -- 'proxy' | 'manual' | 'cliproxy'
  api TEXT NOT NULL,
  model TEXT,
  quantity REAL,               -- NULL when unmeasurable
  unit TEXT,
  cost REAL NOT NULL DEFAULT 0,
  status INTEGER,              -- upstream HTTP status, NULL if not applicable
  estimated INTEGER NOT NULL DEFAULT 0,   -- 1 when quantity/cost is an estimate
  meta TEXT                    -- JSON object or NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS balance (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  starting REAL NOT NULL, currency TEXT NOT NULL, set_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
```

Notes that matter for anyone reading the numbers:

- **One row per upstream attempt.** A call that upstream answered with 4xx/5xx is
  still a row, with `cost = 0` and `status` set. A network failure that never
  reached upstream is not a row at all (the proxy answers `502` instead).
- **`ts` is fixed-width UTC ISO8601 with `Z`**, so time range queries compare
  strings (`ts >= ?`), which is a valid ordering. Local-day and local-month
  boundaries are computed with `zoneinfo` by `local_day_start_iso()` /
  `local_month_start_iso()` and then converted to UTC before use — "today" is
  your local calendar day, not UTC's.
- **`quantity` is NULL when unmeasurable** and `estimated = 1` then says so.
  Nothing is ever back-filled with a guess.
- **`unit`** is one of `tokens`, `chars`, `seconds`, `pages`, `requests`.
- **`meta`** is a JSON object serialised with `sort_keys=True`,
  `ensure_ascii=False`, and may only carry the api name, model, path and status —
  never a header value, never a key.
- **`kv`** is small internal state, notably `quota_exhausted_at`, set when the
  proxy sees a `402` from upstream.
- `set_balance` upserts the single row `id = 1`; `get_balance` returns `None`
  when it has never been set. That `None` is what makes `balance.known` false.
- `purge_before(iso)` deletes older events and returns the number of rows removed.

## 4. Status JSON contract

`GET /v1/status` on the daemon returns `build_status(...)`. This is the widget's
contract: the shape is exact, the schema version is `1`, and fields may be added
only inside objects marked extensible (`by_api[]` entries, `errors[]` entries,
`accounts[]` entries). This block is a complete, valid JSON document:

```json
{
  "schema": 1,
  "generated_at": "2026-11-01T10:00:00+05:30",
  "stale": false,
  "errors": [{"source": "cliproxy", "code": "unreachable", "message": "..."}],
  "sarvam": {
    "configured": true,
    "currency": "INR",
    "balance": {"known": true, "remaining": 412.6, "starting": 500.0,
                "spent_total": 87.4, "set_at": "2026-10-20T09:00:00Z"},
    "spend": {"today": 12.3, "month": 87.4},
    "calls": {"today": 42},
    "by_api": [{"api": "chat", "spend": 8.0, "calls": 20}],
    "rate_limits": {"count_24h": 3, "last_at": "2026-10-31T18:00:00Z"},
    "quota_exhausted": false,
    "quota_exhausted_at": null,
    "untracked_calls": {"today": 0, "total": 0}
  },
  "cliproxy": {
    "configured": false,
    "reachable": false,
    "api_version": "v8",
    "worst_fraction": null,
    "accounts": []
  }
}
```

Annotation, field by field:

| Field | Type | Meaning |
| --- | --- | --- |
| `schema` | int | Contract version. Always `1`. |
| `generated_at` | string | When the payload was built, in the daemon's local timezone with offset (`+05:30`). Displayed in the widget footer. |
| `stale` | bool | `true` when the CLIProxyAPI snapshot is missing while `cliproxy.enabled` is `true`. The widget shows a stale notice and keeps the numbers it has. |
| `errors` | array | Always present, possibly `[]`. Carries the CLIProxyAPI snapshot's errors when there are any. Each entry is `{source, code, message}`; sources seen are `cliproxy` and `ledger`. |
| `sarvam.configured` | bool | `sarvam.enabled` from the config. |
| `sarvam.currency` | string | Currency code from the config, used for symbol formatting (`INR` → `₹`). |
| `sarvam.balance.known` | bool | `false` until a starting balance has been set. When false, `remaining`, `starting` and `set_at` are `null` and only `spent_total` is meaningful. |
| `sarvam.balance.remaining` | number or null | `starting − spent_total`, 2 dp for display. |
| `sarvam.balance.starting` | number or null | The figure you entered with `sarvam-meter balance set`. |
| `sarvam.balance.spent_total` | number | All-time measured spend from the ledger, whether or not a starting balance is known. |
| `sarvam.balance.set_at` | string or null | UTC ISO8601 of the last `balance set`. |
| `sarvam.spend.today` | number | Measured spend since local midnight. |
| `sarvam.spend.month` | number | Measured spend since local month start. |
| `sarvam.calls.today` | int | Recorded events since local midnight. |
| `sarvam.by_api` | array | `{api, spend, calls}` for the month, sorted by spend descending. The full representation draws a bar per top-3 entry. |
| `sarvam.rate_limits.count_24h` | int | Number of events with upstream status `429` in the last 24 hours. |
| `sarvam.rate_limits.last_at` | string or null | Timestamp of the most recent `429`. |
| `sarvam.quota_exhausted` | bool | Set from `kv` key `quota_exhausted_at`: a `402` was seen. Drives the `!` badge. |
| `sarvam.quota_exhausted_at` | string or null | When that `402` was seen. |
| `sarvam.untracked_calls` | object | `{today, total}` counts of calls the ledger could not measure or price — the "we do not know" bucket, surfaced instead of being hidden. |
| `cliproxy.configured` | bool | `cliproxy.enabled` from the config. |
| `cliproxy.reachable` | bool | Whether the last refresh reached the management API. |
| `cliproxy.api_version` | string | `v8` or `v0`, echoed from the config so a mismatch is visible in the UI. |
| `cliproxy.worst_fraction` | number or null | Minimum remaining fraction across all known account buckets, 4 dp. `null` when no fraction is known. This is what the compact representation shows when `compactMode` is `cliproxy`. |
| `cliproxy.accounts` | array | Normalized accounts (shape below). |

Each `cliproxy.accounts[]` entry is the normalized account from the adapter:

```json
{"auth_index":"...","id":"...","name":"...","provider":"claude","label":"...",
 "status":"active|disabled|unavailable|cooldown|error","plan":"Max",
 "buckets":[{"window":"5h","remaining_fraction":0.62,"reset_at":"...","description":"","unknown":false}],
 "summary":[{"key":"...","label":"...","value":1.0,"unit":"...","format":"currency","currency":"USD"}],
 "error":null}
```

`remaining_fraction` is clamped to `0.0..1.0`; when the upstream value is
missing, null or non-numeric the bucket carries `"unknown": true` and
`remaining_fraction: null` rather than a fabricated `0` or `1`. Accounts whose
quota fetch failed keep `"buckets": []` and an `error` string, and do not fail
the rest of the snapshot.

Rounding: money to 2 dp for display, quantities and fractions to 4 dp.

`build_status` never raises. A ledger failure is caught and reported as
`errors: [{"source": "ledger", "code": "read_failed", "message": ...}]` with the
numeric fields zeroed, so the widget degrades to "unknown" instead of crashing.

## 5. Threading model

Both servers are `ThreadingHTTPServer` instances with `SO_REUSEADDR` on and
`daemon_threads` on, so each request runs in its own thread and a slow client
cannot block the accept loop.

- **SQLite.** `Ledger` holds one connection per thread in a `threading.local`,
  opened lazily and reused; no connection is shared between threads. The database
  runs in WAL mode with `busy_timeout = 5000`, so concurrent writers queue
  instead of failing with `database is locked`. A test drives 20 parallel
  `record_event` calls to prove this.
- **Daemon refresh thread.** When `cliproxy.enabled` is true, `Daemon.start()`
  launches one background thread that re-polls CLIProxyAPI every
  `cliproxy.quota_fetch_interval_seconds`. The cached snapshot is replaced under a
  lock; `GET /v1/status` only reads the cached snapshot, so a slow or hanging
  management API never delays a widget poll. `POST /v1/refresh` forces a re-poll
  (taking the same lock) and returns the new status.
- **Proxy threads.** Each proxied request is one server thread that streams the
  request upstream, streams the response back, and records one ledger event on
  its own connection. Nothing is shared between requests except the `Config`
  (frozen dataclasses) and the `Ledger` object itself.
- **Nothing mutates from outside.** The daemon answers `405` to any
  `PUT`/`PATCH`/`DELETE`, so a stray local process cannot change state through
  HTTP; state changes come from the CLI.

## 6. The proxy's streaming design

The proxy must not buffer a 200 MB audio upload, and must not buffer a long SSE
chat stream. So:

**Request path.** The client body is read in 64 KiB chunks and fed to the
upstream request as a file-like object (or over a raw `http.client` connection),
so peak memory is one chunk regardless of payload size. The running total is
checked against `proxy.max_body_bytes` (default 268435456, i.e. 256 MiB); going
over yields `413` and no upstream call. Only `POST` and `GET` are forwarded —
anything else gets `405`. A path that does not start with `/`, or that contains
`..` or a scheme such as `http:`, gets `400`.

**Header handling.** Request headers are forwarded except `Host`,
`Content-Length`, `Transfer-Encoding`, `Connection` and `Accept-Encoding`;
`Accept-Encoding` is replaced with `identity` so the byte stream stays
observable. No `X-Forwarded-For` is added.

**Response path.** Bytes are forwarded to the client as they arrive; an upstream
`Transfer-Encoding: chunked` response is relayed chunked rather than being
de-chunked and re-sent with a `Content-Length`, because that would mean buffering
the whole body. For `text/event-stream` the relay flushes per line and keeps a
**bounded tail buffer of the last 64 KiB of `data:` payloads** — enough to catch
the usage chunk at the end of a stream, bounded so a long stream cannot grow
memory. `chat_stream_usage(lines)` scans that tail for `data: {...}` chunks and
extracts `in_tokens` / `out_tokens` / `cached_tokens`.

**Recording.** One `record_from_exchange(...)` call per request that reached
upstream, whatever the status:

| api | quantity | unit | notes |
| --- | --- | --- | --- |
| `chat` | `in + out` tokens | `tokens` | priced with `cost_for("chat", model, in-cached, "tokens", cached_quantity=cached, out_quantity=out)` |
| `stt` | `audio.duration_seconds(uploaded bytes)` | `seconds` | `None` when unparseable |
| `tts` | code-point length of the request `text` | `chars` | emoji and non-ASCII count as characters, not bytes |
| `translate`, `transliterate` | code-point length of the request `input` | `chars` | cost 0, rate unpublished |
| `lid` | 1 | `requests` | |
| `other` | — | — | not metered (`metered: false`) |

Model resolution order is the response `model` field, then the request JSON
`model`, then `None`. A missing `usage` object (or a stream with no usage chunk)
gives `estimated = 1`, `quantity = NULL`, `cost = 0`. On `402` the proxy writes
`kv["quota_exhausted_at"] = now`; on `429` it does nothing special, because the
count is derived from `events` at read time. If upstream cannot be reached at
all, the client gets `502` with
`{"error":{"message":"upstream unreachable","code":"meter_upstream_error"}}` and
**nothing is recorded** — an unreachable attempt is not a call.

**Bind failures are loud.** A port already in use surfaces as `OSError` from the
caller of `serve_forever()`, not a silent hang; the CLI turns it into a clear
message and exit code 1.

## 7. Security posture

- **Loopback by default.** Both servers bind `127.0.0.1` in the shipped config.
  Nothing listens on an external interface unless you change `host`.
- **SSRF guard.** A non-loopback `cliproxy.base_url` is rejected at config load
  unless `cliproxy.allow_remote` is `true`. Loopback means `127.0.0.0/8`, `::1`,
  and `localhost`. This stops a typo or a pasted URL from turning the daemon into
  a probe of arbitrary internal hosts.
- **Secrets are redacted, not logged.** Any header named `api-subscription-key`,
  `authorization` or `x-management-key` is replaced with `***` before it can
  reach a log line. The proxy stores no header values at all: `meta` may contain
  only the api name, model, path and status.
- **`/v1/config` is sanitised and read-only.** It returns the config with key
  material blanked (`""`/`"***"`), and the daemon rejects mutating methods with
  `405`.
- **The CLIProxyAPI transport does not follow redirects** and keeps TLS
  verification on, so a compromised management endpoint cannot bounce the
  management key to a third party.
- **No third-party runtime dependencies**, which keeps the supply chain to
  CPython itself.
- **The ledger holds no credentials** — only api names, models, quantities, costs
  and timestamps. It is still local data about your usage; it lives in your home
  directory with a 0600 config file and 0700 config directory created by
  `sarvam-meter init`.

Note for reviewers: SPEC.md states the SSRF guard covers `proxy.upstream_base`
"unless `allow_remote` is true for that section", but `ProxyConfig` defines no
`allow_remote` field, and the shipped `upstream_base` default is the non-loopback
`https://api.sarvam.ai`. The implementation therefore applies the guard where the
flag exists (`cliproxy`) and does not reject the default upstream. See
[edge-cases.md](edge-cases.md) case 18.
