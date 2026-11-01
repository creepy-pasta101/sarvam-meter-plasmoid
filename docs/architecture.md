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
| daemon | `127.0.0.1:8787` | serves the status JSON the widget reads; caches the CLIProxyAPI and dashboard snapshots | `sarvam-meter serve` |
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
| `sarvam_meter/dashboard.py` | Sarvam dashboard adapter: `DashboardClient` (injectable transport, session from a `0600` file), `load_session`, `parse_summary`, `parse_timeseries`, `DashboardError`, and a `snapshot()` that never raises. See [dashboard-scrape.md](dashboard-scrape.md) and §4a below. |
| `sarvam_meter/status.py` | `build_status(config, ledger, cliproxy_snapshot=None, dashboard_snapshot=None, now=None)` — assembles the widget contract. Never raises. |
| `sarvam_meter/daemon.py` | `ThreadingHTTPServer` on `daemon.host:daemon.port` serving `/v1/status`, `/v1/health`, `/v1/refresh`, `/v1/config` (GET and POST); owns the background CLIProxyAPI refresh thread and its lock, and the dashboard snapshot's TTL cache. |
| `sarvam_meter/cli.py` | `argparse` front end (`init`, `serve`, `status`, `balance set/show`, `record`, `proxy`, `doctor`, `session set/show`, `dashboard`), exit codes 0/1/2, and the human-readable `status` output. |
| `sarvam_meter/__main__.py` | `python3 -m sarvam_meter` shim into `cli.main()`. |

Dependency direction is one way and shallow:

```
config --> (nobody)
rates  --> (nobody)
audio  --> (nobody)
ledger --> (nobody)
cliproxy --> (nobody)
dashboard --> (nobody)
proxy  --> config, rates, audio, ledger
status --> config, ledger
daemon --> config, ledger, cliproxy, dashboard, status
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
contract: the shape is exact, the schema version is `2`, and fields may be added
only inside objects marked extensible (`by_api[]`, `by_model[]`, `errors[]` and
`accounts[]` entries). This block is a complete, valid JSON document:

```json
{
  "schema": 2,
  "generated_at": "2026-11-01T10:00:00+05:30",
  "stale": false,
  "errors": [{"source": "cliproxy", "code": "unreachable", "message": "..."}],
  "sarvam": {
    "configured": true,
    "currency": "INR",
    "balance": {"known": true, "remaining": 365.77, "starting": 500.0,
                "spent_total": 134.23, "set_at": "2026-10-20T09:00:00Z",
                "source": "dashboard"},
    "spend": {"today": 12.3, "month": 134.23, "source": "dashboard"},
    "calls": {"today": 42},
    "by_api": [{"api": "chat", "spend": 8.0, "calls": 20}],
    "rate_limits": {"count_24h": 3, "last_at": "2026-10-31T18:00:00Z"},
    "quota_exhausted": false,
    "quota_exhausted_at": null,
    "untracked_calls": {"today": 0, "total": 0},
    "dashboard": {
      "configured": true, "reachable": true, "as_of": "2026-11-01T09:58:00+05:30",
      "org_id": "019f37be-...", "currency": "INR",
      "window": {"start": "2026-10-20", "end": "2026-11-01"},
      "spend": {"window": 134.23, "today": 12.3},
      "by_model": [{"model": "saaras:v4", "cost": 74.05},
                   {"model": "glm5.3-flash", "cost": 53.39},
                   {"model": "sarvam-105b", "cost": 6.64},
                   {"model": "saaras:v3-realtime", "cost": 0.16}],
      "error": null
    }
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

Schema 1 remains readable: the widget treats a payload with no `spend.source` and
no `sarvam.dashboard` as a ledger payload, so an older daemon still drives a newer
widget.

Annotation, field by field:

| Field | Type | Meaning |
| --- | --- | --- |
| `schema` | int | Contract version. `2` when the build knows about the dashboard source; `1` payloads stay readable by the widget. |
| `generated_at` | string | When the payload was built, in the daemon's local timezone with offset (`+05:30`). Displayed in the widget footer. |
| `stale` | bool | `true` when the CLIProxyAPI snapshot is missing while `cliproxy.enabled` is `true`. The widget shows a stale notice and keeps the numbers it has. |
| `errors` | array | Always present, possibly `[]`. Carries the CLIProxyAPI snapshot's errors when there are any. Each entry is `{source, code, message}`; sources seen are `cliproxy` and `ledger`. |
| `sarvam.configured` | bool | `sarvam.enabled` from the config. |
| `sarvam.currency` | string | Currency code from the config, used for symbol formatting (`INR` → `₹`). |
| `sarvam.balance.known` | bool | `false` until a starting balance has been set. When false, `remaining`, `starting` and `set_at` are `null` and only `spent_total` is meaningful. |
| `sarvam.balance.remaining` | number or null | `starting − spent_total`, 2 dp for display. |
| `sarvam.balance.starting` | number or null | The figure you entered with `sarvam-meter balance set`. |
| `sarvam.balance.spent_total` | number | Spend inside the measured window: the dashboard's window spend when the dashboard is reachable and a starting balance is set, else the all-time measured spend from the ledger. |
| `sarvam.balance.set_at` | string or null | UTC ISO8601 of the last `balance set`. Its date part is the start of the dashboard window. |
| `sarvam.balance.source` | string | `dashboard` or `ledger` — which source produced `remaining`/`spent_total`. Schema-2 addition. |
| `sarvam.spend.today` | number | Spend since local midnight: the dashboard's `spend.today` when reachable, else the ledger figure. |
| `sarvam.spend.month` | number | Measured spend since local month start (ledger). |
| `sarvam.spend.source` | string | `dashboard` or `ledger`. Drives the widget's muted hint. Schema-2 addition. |
| `sarvam.dashboard` | object | Always present in schema 2; `configured: false` when `dashboard.enabled` is false. See below. |
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

`sarvam.dashboard` is the raw snapshot from the adapter, minus the session:

| Field | Type | Meaning |
| --- | --- | --- |
| `configured` | bool | `dashboard.enabled` from the config. |
| `reachable` | bool | Whether the last snapshot got usable JSON back from the dashboard. |
| `as_of` | string | When that snapshot was taken. |
| `org_id` | string or null | The dashboard's own org identifier, as returned. |
| `currency` | string | Normalised from the payload's lowercase `"inr"` to `"INR"`. |
| `window` | object | `{start, end}`, both `YYYY-MM-DD`: the date part of `balance.set_at` and today. |
| `spend.window` | number | Dashboard cost for that window, 2 dp. |
| `spend.today` | number | Dashboard cost for today; `0.0` when only the today call failed. |
| `by_model` | array | `{model, cost}` per model for the window, sorted by cost descending. The widget draws at most three. |
| `error` | object or null | `null`, or `{source: "dashboard", code, message}`. Codes: `no_session`, `unauthorized`, `bad_response`, `unreachable`, `http_error`. |

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

## 4a. The dashboard source

The dashboard source is the second Sarvam reader: instead of measuring traffic on
this machine, it asks Sarvam's own dashboard what the organisation spent. It is
optional (`dashboard.enabled`, default `false`) and it never replaces local
metering — the ledger stays the fallback for every number it cannot supply. The
honest caveats are in [dashboard-scrape.md](dashboard-scrape.md); this section is
the mechanical description.

**Endpoints.** Two `GET`s, both requiring the signed-in session, both derived from
the dashboard's own front-end traffic and **not** from any published API:

```
GET /api/dhan/org/balance
GET /api/dhan/org/usage/summary?start=YYYY-MM-DD&end=YYYY-MM-DD&group_by=model&product=sarvam-api-platform
GET /api/dhan/org/usage/timeseries?start=YYYY-MM-DD&end=YYYY-MM-DD&group_by=product&product=sarvam-api-platform&granularity=auto
```

Unauthenticated they answer `401 application/json`; an unknown `/api/...` path
answers `404` with the SPA shell, which is how the real paths were told apart from
the invented ones. The shapes below were captured from a real signed-in session and
committed as fixtures in `tests/fixtures/` — those files, not this prose, are the
contract:

```json
// dhan_usage_summary_by_model.json  (group_by=model)
{"org_id": "019f37be-...",
 "range": {"start": "2026-10-02T18:30:00Z", "end": "2026-11-01T18:29:59Z"},
 "group_by": ["model"], "currency": "inr",
 "subtotal": "134.2312311905", "total_discount": "0",
 "total_cost": "134.2312311905", "total_org_cost": "134.2312311905",
 "usage": [{"unit": "token", "quantity": "13046343",
            "breakdown": [{"unit": "cached_token", "quantity": "11296128"},
                          {"unit": "input_token", "quantity": "1513846"},
                          {"unit": "output_token", "quantity": "236369"}]},
           {"unit": "second", "quantity": "9086.323125"}],
 "groups": [{"model": "glm5.3-flash", "total_cost": "53.388129", "currency": "inr", "usage": [...]},
            {"model": "saaras:v3-realtime", "total_cost": "0.1551666667", ...},
            {"model": "saaras:v4", "total_cost": "74.0498055238", ...},
            {"model": "sarvam-105b", "total_cost": "6.63813", ...}]}

// dhan_usage_timeseries_by_product.json  (group_by=product)
// same envelope plus "granularity", "available_granularities", groups[].product and
// points: [{"timestamp", "subtotal", "total_discount", "total_cost", "event_count"}]
```

Two properties of those payloads drive the parser:

- **Every number is a JSON string.** `parse_summary` / `parse_timeseries` convert
them to `float`; a non-numeric string raises, it is never silently turned into `0`.
- **`currency` arrives lowercase** (`"inr"`) and is normalised to `"INR"`.

**Snapshot.** `DashboardClient.snapshot(since_date, today)` makes two calls — the
window summary and today's summary — and never raises. A failure of the second keeps
the window result, leaves `spend.today` at `0.0`, and records the error; a failure of
the first yields `reachable: false` with an error and no numbers. The transport is
injectable and defaults to `urllib` with an empty proxy map, certificate
verification on and no redirect following, the same rules as `cliproxy.py`. Every
call is a `GET`; the adapter has no write path at all.

**TTL cache and refresh thread.** The daemon keeps the snapshot in memory with a TTL
of `dashboard.poll_seconds` (default 300 s, minimum 60 s), refreshed by the same
background-thread pattern as CLIProxyAPI: the thread wakes, and if the cached
snapshot is older than the TTL it takes the lock, calls `snapshot()`, and replaces
the cache. `GET /v1/status` only ever reads the cache, so a slow or hostile
dashboard cannot delay a widget poll, and `POST /v1/refresh` refreshes both sources.

**Balance arithmetic.** The dashboard reports spend, not a balance, so the widget's
`remaining` is derived:

```
window.start = date part of balance.set_at          # e.g. 2026-10-20
spent_total  = dashboard.spend.window               # spend since that date
remaining    = max(0, balance.starting - spent_total)
```

The clamp matters: a top-up or a credit adjustment made after `balance set` can push
measured spend above the starting figure, and a negative balance is not a thing the
user can act on. When no starting balance is set, `balance.known` is `false` and the
widget shows spend only, exactly as in schema 1. When the dashboard is unreachable,
all of this is skipped and the ledger arithmetic applies unchanged.

## 4b. The popup

`FullRepresentation.qml` is built the way Plasma's own applets are, so it inherits
the shell's margins, typography and control sizes instead of inventing its own:

```
PlasmaExtras.Representation (collapseMargins: true)
├── header: PlasmaExtras.PlasmoidHeading
│   └── RowLayout: title · BusyIndicator (while a request is in flight) · refresh
└── PlasmaComponents3.ScrollView
    ├── PlasmaExtras.PlaceholderMessage   (only when no status has arrived)
    ├── Sarvam section, per-API bars, per-model rows
    ├── Kirigami.Separator
    ├── CLIProxyAPI section
    └── Kirigami.Separator + footer
```

The header's refresh button calls `forceRefresh()`, which is a `POST
/v1/refresh` — not another `GET /v1/status`. A GET would only re-read the
daemon's cache; the dashboard is re-polled on its own TTL, so a plain re-read
would usually show the same numbers and look broken. The forced call can take a
while when the dashboard is slow, so a failure falls back to one `GET
/v1/status`: the spinner always stops and the user always ends up looking at
numbers. Both paths go through `startRequest()`, so the timeout, the watchdog,
the single-flight guard and the size cap apply to the button exactly as they do
to the timer.

## 4b2. Panel sizing

The compact representation does not let QtQuick size it from a Layout: the panel
asks the item for its `implicitWidth`. That width is computed from a hidden `Text`
measuring the value at the theme font, plus the badge slot (only while the badge is
shown) plus a small padding, capped at 14 grid units. The measuring `Text` is
declared *before* the properties that read it, because `implicitWidth` is evaluated
while the component is still being built and a forward reference to a child id
throws at that point. The label itself gets `Layout.fillWidth: true` and
`Layout.minimumWidth: 0`, so a panel that clamps the item makes the number elide
rather than overflow its bounds, and the badge is pinned to its width so it cannot
be squashed.

## 4c. Plasmashell safety

The applet is not a separate process: it is QML running on plasmashell's GUI
thread, next to every panel and every other widget. A blocking call or an
unbounded loop there freezes the whole desktop, so the request path is bounded
four ways, all of them in `contents/ui/main.qml`:

| guard | what it prevents |
| --- | --- |
| `xhr.open(..., true)` only | a synchronous request stalling the event loop |
| `requestTimeoutMs = 4000` | waiting on a daemon that stopped answering |
| a `requestWatchdog` timer that calls `abortRequest()` at timeout + 2 s | Qt's own timeout not covering every way a socket can go quiet; without it the widget would sit in `loading` for ever |
| single-flight (`if (root.loading) return`) | a slow daemon stacking requests until the shell runs out of handles |
| `maxResponseChars` checked before `JSON.parse` | a huge or corrupted body blocking the GUI thread inside the parser |
| `.slice()` on every delegate list | a pathological payload building thousands of delegates |
| `Component.onDestruction: abortRequest("")` | a callback outliving the widget |
| `clockTimer` (30 s, gated on having an `as_of`) | a stale balance reading looking live; the timer only bumps a tick that re-formats the age text |

The settings page has the same treatment for its Test button: a 5 s timeout, and
`abort()` both when a second test starts and when the page is destroyed.

`scripts/qml-safety-harness.py` loads the real `main.qml` in a real Qt engine
(namespace-substituted: `PlasmoidItem` becomes `Item`, `Plasmoid.configuration`
becomes a local object) and drives it against a daemon that hangs, floods the
socket, returns garbage, or is removed mid-request, asserting each time that an
independent timer kept ticking — i.e. that the GUI thread never blocked. CI runs
it in the `qml-safety` job. `tests/test_qml_safety.py` is the cheap static
counterpart that runs where Qt is not installed.

The one thing no guard can prevent is a genuine C++ crash in Qt itself; the applet
loads no C++ plugin, ships no compiled code, and performs no synchronous I/O, so
its failure modes are the ones above.

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
- **Dashboard TTL cache.** The dashboard snapshot is refreshed by the same thread
  pattern with a TTL of `dashboard.poll_seconds`; a poll inside the TTL is served
  from memory without touching the network. The dashboard is slow (a browser app's
  backend, behind bot management), so the refresh must never happen on the request
  path — and it does not.
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
- **`/v1/config` is sanitised, and writable only through a whitelist.** `GET`
  returns the config with key material blanked (`""`/`"***"`). `POST` accepts a
  fixed subset of keys (`dashboard.enabled/base_url/poll_seconds/session_file`,
  `cliproxy.enabled/base_url/api_version`, `proxy.enabled`,
  `daemon.refresh_seconds`), validates them with the same rules `load_config`
  uses, writes the file atomically at `0600` and reloads; anything else — notably
  `cliproxy.management_key` — is rejected whole with a `400` that names the key
  and leaves the file byte-identical. `PUT`/`PATCH`/`DELETE` are still `405`.
- **The CLIProxyAPI transport does not follow redirects** and keeps TLS
  verification on, so a compromised management endpoint cannot bounce the
  management key to a third party.
- **The dashboard session is a credential, and is treated as one.** It lives in a
  `0600` file (`dashboard.session_file`), is read per snapshot and never cached in
  the payload, is never logged, is never returned by `/v1/config`, and never
  appears in an error message or in `sarvam.dashboard.error` — the widget only ever
  shows the error *code*. The adapter issues `GET` requests only, so no dashboard
  write, purchase or settings call exists in the code at all.
- **`dashboard.base_url` is exempt from the loopback guard by design** (the
  dashboard is remote), so it is instead required to be `https://` with no
  userinfo, which keeps credentials out of the URL.
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
