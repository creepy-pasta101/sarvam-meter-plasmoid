# Edge cases

The cases in SPEC.md §15, with the module that owns each one and what the user
actually sees, followed by the cases the dashboard source (SPEC.md §17) adds. This
table is the review artifact: check the named module for the named behaviour.

The cases that used to describe the recording reverse proxy (402/429 bookkeeping,
unmeasured chat calls, STT duration, TTS character counting, a 200 MB streamed
upload) were retired with it: nothing in this project relays API traffic any more,
so there is no exchange to measure. Their numbers are kept so cross-references
stay stable, and each says what replaced it.

Reading the columns:

- **case** — the contractual situation, abbreviated from the spec.
- **where it is handled** — the module and function that owns it. Paths are
  repo-relative.
- **what the user sees** — the observable result in the widget, the CLI or the
  HTTP API. No behaviour is described here that is not in the spec.

| # | Case | Where it is handled | What the user sees |
| --- | --- | --- | --- |
| 1 | Daemon down / connection refused | `plasmoid/io.github.creepy_pasta101.sarvammeter/contents/ui/main.qml` XHR `onerror`/non-200 handler: keeps the last payload, sets `offline = true`, doubles the poll interval up to 10x and resets on success. | The previous numbers stay on screen with an offline notice in the footer and a `!` badge on the compact representation. No empty widget, no error dialog. |
| 2 | Daemon returns non-JSON or a 500 | Same QML handler: the response is parsed defensively and a parse failure or non-200 is treated exactly like a connection failure. Server side, `sarvam_meter/daemon.py` only ever emits JSON built by `status.build_status`. | Last good payload retained, stale/offline notice shown. Numbers do not flip to zero. |
| 3 | Config missing | `sarvam_meter/cli.py` `init` writes the default document; `sarvam_meter/config.py` `load_config` raises `ConfigError` naming the path and telling you to run `sarvam-meter init`. | `sarvam-meter init` creates the file and prints its path. A daemon or `serve` started without a config refuses to start with a one-line message and exit code 2, not a traceback. |
| 4 | Config unreadable JSON | `sarvam_meter/config.py` `load_config` wraps `json` errors as `ConfigError("<path>: <reason>")`; `sarvam_meter/cli.py` maps `ConfigError` to exit code 2. | One stderr line naming the file and the parse reason, exit code 2. The file's contents are never echoed. |
| 5 | DB path unwritable | `sarvam_meter/ledger.py` `Ledger.__init__` fails; `sarvam_meter/status.py` `build_status` catches it and emits `errors: [{"source":"ledger","code":"read_failed",...}]` with zeroed numbers. `sarvam_meter/daemon.py` still binds and serves. | The widget loads and shows a ledger error, with `0` for spend and calls rather than a blank panel. `sarvam-meter doctor` marks the data dir / db check `FAIL`. |
| 6 | Port already in use | `sarvam_meter/daemon.py` `Daemon.start` lets the bind `OSError` propagate; `sarvam_meter/cli.py` turns it into a message and exit code 1. `doctor` probes the daemon's socket first. | A clear "address already in use" message naming the address and exit code 1 — no silent hang. `doctor` shows `FAIL daemon port free`. |
| 7 | `starting_balance` unset | `sarvam_meter/ledger.py` `get_balance` returns `None`; `sarvam_meter/status.py` sets `balance.known: false` with `remaining`/`starting`/`set_at` null and `spent_total` as the all-time measured spend. QML renders "spend only". | The full representation shows spend today/month and call counts without a balance figure, instead of a bogus `₹0.00 left`. |
| 8 | `402` from upstream, and `429` | Retired with the recording proxy. Nothing in the project sees an upstream status any more, so the payload carries no `rate_limits`, `quota_exhausted` or `quota_exhausted_at`. The reported balance (`sarvam.dashboard.balance`, `sarvam.balance.remaining`) is where an exhausted wallet shows up now. | The full representation shows the reported balance — including a known `0.00` — instead of an inferred "quota exhausted" flag. |
| 9 | Chat response without `usage` (and SSE without a usage chunk) | Retired with the recording proxy: there is no response to inspect. An unpriced manual entry is the remaining analogue: `sarvam-meter record` for an api with no published rate stores it with `cost = 0` and `estimated = 1`. | The entry is counted in `sarvam.calls` and listed in `sarvam.by_api` with cost 0, so an unpriced entry is visible rather than silently free. |
| 10 | STT audio whose duration cannot be parsed | Retired with the proxy and with `sarvam_meter/audio.py` (both deleted). `sarvam-meter record --api stt --quantity <seconds> --unit seconds` takes the duration from you. | You supply the duration; nothing tries to parse an audio file. |
| 11 | TTS text with non-ASCII or emoji | Retired with the proxy's `tts_char_count`. `sarvam-meter record --api tts --quantity <chars> --unit chars` takes the character count from you. | You supply the count; the rate card prices it. |
| 12 | `remainingFraction` null or missing | `sarvam_meter/cliproxy.py` `normalize_quota` / `normalize_account` set `"unknown": true` and `remaining_fraction: null`; `worst_fraction` skips unknown buckets; QML prints "unknown" instead of a bar. | The bucket reads "unknown" — never `0%` (which would look exhausted) and never `100%` (which would look full). The worst-fraction summary ignores it. |
| 13 | `resetTime` in the past | `sarvam_meter/cliproxy.py` `normalize_quota`: parseable timestamps are normalised to UTC `...Z`, unparseable ones are passed through verbatim; no timestamp is discarded or recomputed. QML prints `reset_at` as received. | The stale reset time is still shown, with the raw timestamp, so you can tell the upstream data is out of date. No reset is hidden or guessed forward. |
| 14 | CLIProxyAPI `401` / `404` / `501` / HTML body / empty providers | `sarvam_meter/cliproxy.py` `snapshot` error mapping: `401`/`403` → `unauthorized`, `404` → `endpoint_missing`, `501` on a quota fetch → per-account `error: "no quota provider"`, non-JSON body → `bad_response`; empty provider list yields accounts with no buckets. `snapshot` never raises. | A distinct message per cause in `errors` (and in the widget footer), and the Sarvam section keeps working. Nothing crashes, nothing retries in a tight loop. |
| 15 | Never poll CLIProxyAPI's destructive usage-queue endpoint | `sarvam_meter/cliproxy.py` `CliproxyClient` has no code path for `/usage-queue` (v0) or `/observability/usage/queue` (v8); `tests/test_cliproxy.py` asserts no request ever targets those paths. | Nothing in the UI, by design: polling that endpoint would silently delete records from CLIProxyAPI. Quota comes from the `quota/fetch` endpoint instead. |
| 16 | Concurrent writers on one database | `sarvam_meter/ledger.py`: WAL mode, `busy_timeout = 5000`, one connection per thread via `threading.local`, opened and reused per method. `tests/test_ledger.py` runs 20 parallel `record_event` calls, and `tests/test_integration.py` drives 10 concurrent records through a live daemon. | No `database is locked` errors under load; every event lands exactly once. A `record` from a terminal and a daemon read can happen at the same time. |
| 17 | Secrets must never appear in logs, `meta`, `/v1/config` or status | Redaction of `api-subscription-key`, `authorization` and `x-management-key` to `***` before any log line (`sarvam_meter/daemon.py`, `sarvam_meter/dashboard.py`); the ledger's `meta` may carry only api and model; `daemon.py` `/v1/config` blanks key material; `status.py` scrubs secret-shaped keys and carries no credentials. | Logs and `GET /v1/config` show `***` or empty strings where keys were. Nothing secret reaches the widget, the database, or a terminal. |
| 18 | Non-loopback CLIProxyAPI `base_url` refused unless `allow_remote` | `sarvam_meter/config.py` `load_config` SSRF guard: loopback is `127.0.0.0/8`, `::1`, `localhost`; a remote `cliproxy.base_url` without `cliproxy.allow_remote: true` raises `ConfigError`. | The config is rejected with a message explaining that a remote URL needs `allow_remote`, exit code 2. Setting `allow_remote: true` is an explicit, visible opt-in. |
| 19 | Two widget instances | `sarvam_meter/daemon.py`: `ThreadingHTTPServer` with `daemon_threads` on serves each poll in its own thread from the cached snapshot, with no per-client state. | Both widgets poll independently and show the same numbers. Two panels, two instances, or a manual `curl` alongside them all work; nothing is consumed or rate-limited per reader. |
| 20 | Large upload (200 MB) streamed, `max_body_bytes` enforced | Retired with the recording proxy: no request body passes through this project at all, so there is no upload ceiling to enforce and no `proxy.max_body_bytes` to set. | Nothing to see: your uploads go straight to Sarvam, as they always did when the proxy was off. |
| 21 | "Today" is the local calendar day, "month" is the local calendar month | `sarvam_meter/ledger.py` `local_day_start_iso` / `local_month_start_iso` compute the boundary in local time with `zoneinfo` and return UTC ISO8601 for string comparison against `events.ts`; `sarvam_meter/status.py` uses them for `spend.today`, `spend.month`, `calls.today`, `by_api`. | Spend rolls over at your local midnight, not at UTC midnight, and the month changes on the local 1st — matching the dashboard's calendar. |
| 22 | Plasma 6.8: no deprecated QML APIs, no `PlasmaCore.Units`, no custom contextual actions | `plasmoid/io.github.creepy_pasta101.sarvammeter/contents/ui/*.qml`: `PlasmoidItem` root, `Kirigami.Units` for spacing, Plasma 6 components, `XMLHttpRequest` only (no synchronous XHR), no `PlasmaComponents2`, no contextual actions, `metadata.json` declares `X-Plasma-API-Minimum-Version: 6.0`. | The widget loads and renders on Plasma 6.7 and 6.8 without deprecation warnings and without a migration break. |

## Cases added by the dashboard source (schema 2)

The dashboard source (SPEC.md §17) adds a second Sarvam reader, and with it a new
class of failure: a source that can be configured, reachable in principle, and
still return nothing usable. Each case below degrades to the ledger rather than to
a wrong number. The widget side lives in
`plasmoid/io.github.creepy_pasta101.sarvammeter/contents/ui/main.qml` and
`FullRepresentation.qml`; the daemon side in `sarvam_meter/dashboard.py`,
`sarvam_meter/status.py` and `sarvam_meter/daemon.py`.

| case | where it is handled | what the user sees |
| --- | --- | --- |
| Session expired: the dashboard answers `401`/`403` | `sarvam_meter/dashboard.py` `snapshot` maps `401`/`403` to `DashboardError(code="unauthorized")` and never raises; `status.py` leaves `sarvam.dashboard.reachable: false` and `spend.source`/`balance.source` at `"ledger"`; QML `dashboardErrorLine` renders the code only. | One muted `dashboard: unauthorized` line in the full representation, and the balance and spend figures are the ledger's, with the hint reading `local meter`. No dialog, no empty widget, and no session value anywhere on screen. |
| Session file missing or unreadable | `sarvam_meter/dashboard.py` `load_session` raises `DashboardError(code="no_session")`, which `snapshot` records as `error.code`; `sarvam-meter doctor` marks the `dashboard session` check `FAIL`. | `dashboard: no_session` on one muted line, ledger numbers elsewhere, and `doctor` names the file and the reason (missing, wrong mode, unexpected keys) — never a value. |
| Dashboard unreachable (DNS, TLS, timeout, Cloudflare challenge) | `sarvam_meter/dashboard.py` `snapshot` maps a transport failure to `unreachable` and a challenge or non-JSON body to `bad_response`; the daemon keeps the previous snapshot in its TTL cache and `GET /v1/status` never blocks on the refresh. | The last dashboard numbers stay on screen with their `as_of` time; if there was never a good snapshot, the ledger numbers show with `local meter`. The error line names `unreachable` or `bad_response`, and the daemon does not retry in a tight loop — the next attempt waits for `dashboard.poll_seconds`. |
| A number in the payload is not numeric (`"53.38x"`, `null`, a number where a string was expected) | `sarvam_meter/dashboard.py` `parse_summary` / `parse_timeseries` raise on a non-numeric string instead of coercing it to `0`, and `snapshot` turns that into `error.code = "bad_response"` with no figures; QML reads every cost through `numberOr(..., NaN)` and renders an em dash rather than `NaN`. | The snapshot is treated as failed, so no partially parsed figure is shown at all — a wrong number is worse than an absent one. The error line reads `dashboard: bad_response`. |
| An older daemon serves schema 1 (no `sarvam.dashboard`, no `spend.source`) | `plasmoid/.../ui/main.qml`: `dashboardObject` falls back to `{}` when the key is absent, `spendSource` falls back to `"ledger"`, `topModels` and `topModelMaxCost` fall back to `[]`/`0`, and `dashboardHasError` is `false` for a missing `error`. `FullRepresentation.qml` hides the by-model block and the error line. | The widget renders exactly the schema-1 presentation: no by-model rows, no dashboard error line, and the spend hint reads `local meter`. Nothing renders as `undefined`, and no binding throws. |
| Dashboard reachable but `by_model` is missing, empty, or all costs are `0` | `plasmoid/.../ui/main.qml` `byModelList` / `topModels` (guarded `asArray`, at most three entries) and `topModelMaxCost` (a `0` maximum); `FullRepresentation.qml` delegate `share` returns `0` when the maximum is not `> 0`. | The by-model block is hidden when there is nothing to draw. With zero costs the rows still show an em-dash cost and a zero-width `Bar` — never a divide-by-zero, and never a bar that reads as full. |

## Notes for reviewers

- Cases 1, 2, 19 and 22 live in the QML and daemon units; cases 3–8, 16, 17, 20
  and 21 in the Python units. This document does not change any of them — it only
  states where the spec says each is handled.
- The dashboard cases are a separate table because they are not in §15: they come
  from §17, which was added after 0.1.0. Every one of them ends the same way — the
  ledger numbers stay on screen and the dashboard's problem is reduced to a single
  muted line naming an error code. No case in that table shows a figure the daemon
  did not actually receive.
- Case 18 applies to `cliproxy.base_url`, the only configurable remote URL left;
  `dashboard.base_url` is remote by design and is instead required to be
  `https://` with no userinfo. See
  [configuration.md](configuration.md#validation-rules).
- Cases 8-11 and 20 are retired rather than solved: their subject (a relayed API
  exchange) no longer exists. The numbers stay so the cross-references above and
  in SPEC.md §15 keep pointing at the same case.

## Cases added by the 0.1.0 hardening pass

The adversarial review in [review-0.1.0.md](review-0.1.0.md) found and closed the
following. Each has a regression test in `tests/test_regressions.py`.

| case | where it is handled | what the user sees |
| --- | --- | --- |
| `$http_proxy` set in the environment | `cliproxy._default_transport` | the management key never leaves the machine through an environment proxy; the transport uses an explicit empty proxy map |
| An in-memory ledger (`Ledger(":memory:")`) | `status._count_estimated` + `Ledger.count_estimated_since` | the payload is no longer zeroed by a phantom `no such table` error |
| A price that overflows to infinity | `rates.cost_for`, `Ledger.record_event`, `cli.cmd_record` | the event is refused (exit 2 in the CLI) instead of poisoning every future `SUM(cost)` |
| A request body sent to a daemon route | `daemon._drain_body` | the body is drained and the connection closed, so the next request is never parsed as garbage |
| A host name that is not a literal IP | `config.is_loopback_host` | single-label names (e.g. `ip6-loopback`) are resolved and accepted if they are loopback; dotted names are treated as remote without a DNS round trip |
| Credentials inside `cliproxy.base_url` | `config._guard_loopback`, `CliproxyClient.__init__` | the config is rejected, so a password can never reach an error message or `/v1/status` |
| `SIGTERM` on `sarvam-meter serve` | `cli.cmd_serve` | the daemon and the ledger shut down cleanly and the WAL is checkpointed, so no `-wal`/`-shm` files are left behind |
| `serve --port` / a taken socket | `cli._with_port`, `cli.cmd_serve` | the override applies to the daemon section only, and a taken address fails loudly with exit 1 instead of dying in a background thread |
| A read-only ledger database | `cli.cmd_doctor` | `doctor` reports FAIL instead of a false OK |
| The reported balance is exactly `0.00` | `status._normalise_dashboard_balance` | `balance.known` stays true and the widget shows `₹0.00` — an exhausted wallet is a known state, not an unknown one |
| The balance endpoint answers but the usage windows do not | `dashboard.snapshot`, `status._sarvam_block` | `balance.source` is `"dashboard"` (the reported figure is shown) while `spend.source` stays `"ledger"` and `dashboard.reachable` is false |
| A malformed date passed to the client | `dashboard.snapshot` (fail-fast validation) | `bad_request` and no request is made at all |
| The daemon accepts the socket and never answers | `main.qml` `requestWatchdog` → `abortRequest()` | the widget shows offline after ~6 s and keeps backing off; plasmashell never blocks, because the request was asynchronous and is now aborted |
| The daemon returns a body larger than 512 KiB | `main.qml` size cap before `JSON.parse` | "daemon response too large", offline; the GUI thread never parses it |
| The widget is removed while a request is in flight | `main.qml` `Component.onDestruction` | the request is aborted and no callback outlives the widget |
| The settings dialog is closed mid-Test | `configGeneral.qml` `Component.onDestruction` | the probe is aborted; the dialog cannot be left waiting on a socket |
| The user clicks Test twice quickly | `configGeneral.qml` `testConnection()` | the first request is aborted, so its callbacks cannot clobber the second result |
| The reported balance has not been refreshed for a while | `main.qml` `balanceFreshnessLine` + `clockTimer` | the footer says `balance 2h ago · not refreshed`, so a stale figure cannot be mistaken for a live one |
| `balance_updated_at` is missing, unparsable, or in the future | `main.qml` `balanceAsOf` / `balanceAgeMs` | the freshness line is simply absent (or reads `just now` for a small negative age); nothing renders as `undefined` or `NaN` |
| The daemon serves schema 1 (no dashboard block) | `main.qml` `balanceAsOf` guard | no freshness line at all |
| Refresh is pressed while a request is already in flight | `main.qml` `startRequest()` single-flight guard | the click is ignored and the button is disabled while busy; nothing queues up |
| The daemon refuses `/v1/refresh` (or is slow to re-poll) | `main.qml` `forceRefresh()` fallback | the widget falls back to one `GET /v1/status`, so the spinner stops and the last known numbers stay on screen |
| No status has arrived yet | `FullRepresentation.qml` `PlasmaExtras.PlaceholderMessage` | the standard Plasma empty state: an icon, "Waiting for the daemon", and the reason |
| The panel item is clicked | `CompactRepresentation.qml` `MouseArea` → `Plasmoid.expanded` | the popup opens. Plasma does not expand it for you, so without this the widget is inert in the panel |
| The dashboard is enabled but no session has been captured | `cli._dashboard_session_check`, `cli._dashboard_reachable` | `doctor` prints `OK dashboard session: not configured (run: sarvam-meter session set ...)` and exits 0 — a setup step, not a fault. A session file with loose permissions is still a FAIL |
| The settings page sends a key the daemon will not accept | `daemon` `POST /v1/config` | `400` naming the key (`cliproxy.management_key is not writable`); the config file is left byte-identical |
| The settings page sends an out-of-range poll interval | `config.py` bounds + `configGeneral.qml` clamp | `400` from the daemon, and the page clamps to 120..21600 before sending |
| QtWebEngine is not installed | `configGeneral.qml` `Loader.status === Loader.Error` | the sheet is replaced by a message naming `qt6-qtwebengine` and the CLI fallback; the rest of the page is untouched |
| The webview collects no cookies (sign-in failed, or the flow changed) | `SessionCapture.qml` manual fallback | the sheet still accepts a pasted Cookie header, so capture is never a dead end |
| The user has never captured a session | `POST`/`GET /v1/session`, `dashboard` `no_session` | the widget shows spend only, `doctor` calls it "not configured" and names the command |
| A three-digit balance plus the offline badge in a panel | `CompactRepresentation.qml` `naturalWidth` | the item asks for the width it needs (measured with a hidden `Text`), capped at 14 grid units; if the panel clamps anyway the number elides instead of painting over the neighbouring applet |
| The unit is enabled before any config exists | `ExecStartPre=... init --if-missing` | a default config is written at 0600 on first start; an edited one is never overwritten |
