# Configuration

Everything is one JSON document, read once at startup. There is no hot reload:
change the file and restart the daemon.

## File locations

| What | Path |
| --- | --- |
| Config | `$SARVAM_METER_CONFIG` if set, else `~/.config/sarvam-meter/config.json` |
| Database | `db_path` from the config; default `~/.local/share/sarvam-meter/usage.db` |
| Data directory | `~/.local/share/sarvam-meter/` (created on demand) |
| Dashboard session | `dashboard.session_file`; default `~/.config/sarvam-meter/session.json` (mode `0600`, see [the session file](#the-dashboard-session-file)) |
| Example config shipped in the package | `/usr/share/doc/plasma-applet-sarvam-meter/config.example.json` |

`sarvam-meter init` writes the default document to the config path, creating
parent directories, with the file mode `0600` and the directory mode `0700`, then
prints the path it used. The packaged systemd user unit sets
`SARVAM_METER_CONFIG=%h/.config/sarvam-meter/config.json`.

## Environment overrides

| Variable | Effect |
| --- | --- |
| `SARVAM_METER_CONFIG` | Path to the config file. Takes precedence over the default location. |
| `SARVAM_METER_CLIPROXY_KEY` | If non-empty, replaces `cliproxy.management_key`. This is the recommended way to keep the management key out of the config file. |

Nothing else is configurable by environment. In particular there is no way to
override the daemon's socket settings except by editing the config, or by the
`--port` flag on `serve` for that one run.

## The default document

This is exactly what `sarvam-meter init` writes, and what
`packaging/config.example.json` must contain:

```json
{
  "version": 1,
  "db_path": "~/.local/share/sarvam-meter/usage.db",
  "daemon": { "host": "127.0.0.1", "port": 8787, "refresh_seconds": 60, "request_timeout_seconds": 10 },
  "sarvam": { "enabled": true, "currency": "INR", "starting_balance": null,
              "rate_overrides": {} },
  "cliproxy": { "enabled": false, "base_url": "http://127.0.0.1:8317",
                "management_key": "", "api_version": "v8", "quota_fetch": true,
                "quota_fetch_interval_seconds": 300, "include_api_key_usage": false,
                "allow_remote": false },
  "dashboard": { "enabled": true, "base_url": "https://indus.sarvam.ai",
                 "session_file": "~/.config/sarvam-meter/session.json",
                 "poll_seconds": 300, "product": "sarvam-api-platform" }
}
```

## Keys

### Top level

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `version` | int | `1` | Config document version. Must be `1`; anything else is rejected. |
| `db_path` | string | `~/.local/share/sarvam-meter/usage.db` | SQLite ledger path. `~` is expanded. Parent directories are created by the ledger. |
| `daemon` | object | see below | The daemon that serves the widget. |
| `sarvam` | object | see below | Balance, currency, rate card overrides. |
| `cliproxy` | object | see below | The CLIProxyAPI adapter. |
| `dashboard` | object | see below | The Sarvam dashboard source (unsupported internal API; off by default). |

Unknown top-level keys are ignored (a newer config opened by an older build must
not fail). Missing keys take their defaults: the loaded document is deep-merged
over the default.

### `daemon`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `host` | string | `127.0.0.1` | Bind address for the daemon's local socket. Keep it on loopback; the status API has no authentication. |
| `port` | int | `8787` | Bind port of that socket, `1..65535`. This is what the widget reads. It is the daemon's own endpoint only: nothing of yours is relayed through it. |
| `refresh_seconds` | int | `60` | Refresh cadence in seconds, `>= 5`. The widget's own poll interval is a separate setting (`refreshSeconds`, below). |
| `request_timeout_seconds` | int | `10` | Timeout for outbound requests the daemon makes (the CLIProxyAPI calls), `>= 1`. |

Example — move the daemon because 8787 is taken:

```json
"daemon": { "host": "127.0.0.1", "port": 18787, "refresh_seconds": 30, "request_timeout_seconds": 10 }
```

Then set the widget's daemon URL to match.

There is no `proxy` section. It was removed with the recording reverse proxy: this
project relays no API traffic, so it has no upstream to forward to and no port for
a client to point at. A config file that still carries a `proxy` object is not an
error — unknown sections are ignored — but nothing reads it.

### `sarvam`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | Show the Sarvam section at all. |
| `currency` | string | `INR` | Three-letter uppercase currency code, used for formatting. |
| `starting_balance` | number or null | `null` | The credit balance you saw in the Sarvam dashboard (not needed when the dashboard source is enabled — it reads the real figure). `null` means "not set": the widget shows spend only, and `balance.known` is `false`. `sarvam-meter balance set <amount>` stores the same figure in the ledger (table `balance`), which is what the status payload reports. |
| `rate_overrides` | object | `{}` | Rate card overrides, see below. |

### `cliproxy`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Turn the CLIProxyAPI source on. |
| `base_url` | string | `http://127.0.0.1:8317` | Management API root, e.g. `http://127.0.0.1:8317`. Must be `http://`/`https://` with a host. |
| `management_key` | string | `""` | The CLIProxyAPI management key. Prefer `SARVAM_METER_CLIPROXY_KEY`. |
| `api_version` | string | `v8` | `v8` or `v0` — which management API paths to use. |
| `quota_fetch` | bool | `true` | Whether to fetch per-account quota (`POST .../quota/fetch`). With this off, accounts are listed but carry no buckets. |
| `quota_fetch_interval_seconds` | int | `300` | How often the daemon's background thread re-polls. |
| `include_api_key_usage` | bool | `false` | Whether to also fetch API-key request counters (`.../usage/api-keys`). Off by default; it is extra traffic for data the widget does not draw by default. |
| `allow_remote` | bool | `false` | Required for a non-loopback `base_url`. See the SSRF guard below. |

Example — a CLIProxyAPI on another machine on your LAN, with the key from the
environment:

```json
"cliproxy": { "enabled": true, "base_url": "http://192.168.1.20:8317",
              "management_key": "", "api_version": "v8", "quota_fetch": true,
              "quota_fetch_interval_seconds": 300, "include_api_key_usage": false,
              "allow_remote": true }
```

```sh
export SARVAM_METER_CLIPROXY_KEY='<management key>'
```

Details of what those endpoints return: [cliproxyapi.md](cliproxyapi.md).

### `dashboard`

The dashboard source reads the authoritative, organisation-wide spend figures from
the Sarvam web dashboard using a session you captured from your own browser. It is
**on by default** and it is **unsupported** (an internal endpoint behind bot
management). It is the only way this project learns real spend or the real credit
balance. Read [dashboard-scrape.md](dashboard-scrape.md) before you rely on it.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | Scraping is the default source: it is the only way to get the real credit balance. With it off, `sarvam.dashboard.configured` is `false`, `spend.source` stays `ledger`, no request is ever made and the balance is unknown. |
| `base_url` | string | `https://indus.sarvam.ai` | Dashboard origin. Must be `https://` with a host and **no** userinfo (no `user:pass@`). This is a remote host by design, so the loopback/SSRF guard that applies to `cliproxy.base_url` does **not** apply here. `https://dashboard.sarvam.ai` `308`-redirects to `https://indus.sarvam.ai`, so use the latter. |
| `session_file` | string | `~/.config/sarvam-meter/session.json` | Where the captured session lives. `~` is expanded. It must be mode `0600`; `sarvam-meter doctor` checks this. |
| `poll_seconds` | int | `300` | How often the daemon re-reads the dashboard, **`120`..`21600`** (every 2 minutes to every 6 hours). The floor is deliberate: the dashboard sits behind bot management, and a minute-by-minute poll is exactly the traffic it exists to notice. |
| `product` | string | `sarvam-api-platform` | Non-empty product filter sent as the `product` query parameter on both usage endpoints. |

Example — enable it, poll every five minutes, and keep the session somewhere else:

```json
"dashboard": { "enabled": true, "base_url": "https://indus.sarvam.ai",
               "session_file": "~/.config/sarvam-meter/session.json",
               "poll_seconds": 300, "product": "sarvam-api-platform" }
```

Enabling it is three steps:

```sh
# 1. capture the session once, from your own signed-in browser
sarvam-meter session set --cookie-file ~/.cache/sarvam-cookie.txt

# 2. set "dashboard": {"enabled": true, ...} in the config, then
systemctl --user restart sarvam-meter     # or: sarvam-meter serve

# 3. confirm what the daemon sees
sarvam-meter doctor                       # checks the session file and reachability
sarvam-meter dashboard                    # one snapshot, --json for the raw shape
```

#### The dashboard session file

`session_file` is a small JSON document holding the two headers the dashboard's own
API expects:

```json
{"Cookie": "...", "Authorization": "Bearer ..."}
```

- **Only those two keys are accepted** (matched case-insensitively). Anything else
  in the file is ignored.
- **The file must be mode `0600`.** `sarvam-meter session set` writes it that way;
  if you create it by hand, `chmod 600` it. `sarvam-meter doctor` reports the mode.
- **Values are never shown.** They are never logged, never returned by
  `GET /v1/config` (rendered `"***"`), never written into the status payload and
  never into an error message. `sarvam-meter session show` prints the path, the
  file mode and *which* headers are present — never a value.
- **A missing or unreadable file is not fatal.** The snapshot comes back with
  `error.code = "no_session"`, the widget shows one muted `dashboard: no_session`
  line, and the balance and spend figures stay on the ledger.

Capturing the session, from a browser where you are already signed in:

1. Open `https://indus.sarvam.ai` and sign in.
2. DevTools → Network, pick any request to that origin, and copy the whole `Cookie`
   request header value.
3. Save it where only you can read it, then hand it to the CLI:

   ```sh
   printf '%s' 'PASTE_THE_COOKIE_HEADER_HERE' > ~/.cache/sarvam-cookie.txt
   chmod 600 ~/.cache/sarvam-cookie.txt
   sarvam-meter session set --cookie-file ~/.cache/sarvam-cookie.txt
   # or pass it directly:  sarvam-meter session set --cookie '...'
   # and, if your session also carries one:  --authorization 'Bearer ...'
   ```

   `session set` writes `session_file` with mode `0600`. Delete the intermediate
   cookie file afterwards.

The session expires when the dashboard decides it does — typically after days, or
immediately if you sign out or change your password. There is no refresh flow: when
it expires the daemon gets `401`/`403`, records `unauthorized`, and you repeat step 3.

#### Schema 2 in the status payload

The dashboard source is why the status payload moved to `schema: 2`. Three things
change for a reader of `GET /v1/status` (the full annotated shape is in
[architecture.md](architecture.md#4-status-json-contract)):

- `sarvam.spend` gains `source`: `"dashboard"` when a dashboard snapshot is
  reachable, `"ledger"` otherwise.
- `sarvam.balance` gains a `source` field: `"dashboard"` when the balance endpoint
  reported the credits left, otherwise it says where the *spend* came from
  (`"dashboard"` or `"ledger"`). The balance is never derived from local spend any
  more — see below.
- `sarvam.dashboard.balance` carries the reported figure:
  `{"known", "amount", "currency", "as_of", "state", "billing_model", "unit"}`.

### Capturing the session

The settings page has a **Capture session…** button: it opens
`https://indus.sarvam.ai/login` in a `WebEngineView`, waits for you to sign in,
reads the `sarvam.ai` cookies out of the web engine's cookie store (the session
cookie is HttpOnly, so `document.cookie` cannot see it) and POSTs them to the
daemon's `POST /v1/session`. Nothing but the cookie header leaves the page, and
no value is ever displayed or logged.

QtWebEngine is not installed on most KDE systems. It is a *Recommends* of the
RPM, not a requirement: the sheet is loaded through a `Loader` whose error path
names the package (`qt6-qtwebengine` on Fedora) and the CLI fallback, so the
settings page keeps working without it. The CLI equivalent is
`sarvam-meter session set --cookie-file PATH`, and `GET /v1/session` reports
whether a session is captured without ever returning a value.

`as_of` is worth looking at: it is `balance_updated_at` from the balance endpoint,
which is when the *balance value* last changed, not when the widget last asked.
Sarvam updates it lazily, so a reading can be an hour old while the widget polled a
minute ago. The applet therefore prints the age under the balance — `balance 4m
ago`, or `balance 2h ago · not refreshed` past 30 minutes — refreshed by a 30 s
timer with no network involved.
- `sarvam.dashboard` is **always present** — `configured: false` when the section is
  disabled — and carries the snapshot:

```json
"sarvam": {
  "spend": {"today": 12.3, "month": 87.4, "source": "dashboard"},
  "balance": {"known": true, "remaining": 80.04, "starting": null,
              "spent_total": 134.23, "set_at": null, "source": "dashboard"},
  "dashboard": {
    "configured": true, "reachable": true, "as_of": "...",
    "org_id": "...", "currency": "INR",
    "window": {"start": "2026-10-20", "end": "2026-11-01"},
    "spend": {"window": 134.23, "today": 12.3, "month": 134.23},
    "balance": {"known": true, "amount": 80.03533, "currency": "INR",
                "as_of": "2026-11-01T05:55:39.713767Z", "state": "provisioned",
                "billing_model": "prepaid", "unit": "rupees"},
    "by_model": [{"model": "saaras:v4", "cost": 74.05}],
    "error": null
  }
}
```

`GET /api/dhan/org/balance` reports the credits left, so **once the dashboard source
is on you do not need `sarvam-meter balance set` at all**: `balance.remaining` is the
reported figure verbatim and `balance.source` is `"dashboard"`. A reported `0.00` is a
known, exhausted balance — the widget shows it as such rather than as "unknown".

Without a reported balance the widget shows spend only: `known` is `false`,
`remaining`/`starting` are `null`, and `spent_total` carries the ledger's all-time
total with `source: "ledger"`. **The balance is never computed from local
entries** — a local estimate disagreed with the real wallet (top-ups, other
machines, calls made elsewhere), and a wrong "left" number is worse than none.
`sarvam-meter balance set` still records a starting balance and `sarvam-meter
record` still writes ledger entries, but neither produces a `remaining` figure.
`spend.today` and `spend.month` likewise come from the dashboard when it is reachable
and from the ledger otherwise.

A dashboard that is configured but not reachable is not something you have to act
on: `sarvam.dashboard.reachable` is `false`, `sarvam.dashboard.error` carries
`{source, code, message}`, and every figure falls back to the ledger. The error codes
are `no_session`, `unauthorized`, `bad_response`, `unreachable` and `http_error`.

## Validation rules

Validation runs on load. Every failure raises `ConfigError` whose message names
the offending key, so you can fix it without reading source.

| Rule | Failure |
| --- | --- |
| `version` must be `1` | `ConfigError` naming `version` |
| `daemon.port` in `1..65535` | `ConfigError` naming `daemon.port` |
| `daemon.refresh_seconds >= 5` | `ConfigError` naming the key |
| `daemon.request_timeout_seconds >= 1` | `ConfigError` naming the key |
| `cliproxy.api_version` in `{"v8","v0"}` | `ConfigError` naming the key |
| `sarvam.currency` is a 3-letter uppercase code | `ConfigError` naming the key |
| `sarvam.starting_balance` is `null` or a finite number `>= 0` | `ConfigError` naming the key |
| `cliproxy.base_url` is an `http(s)://` URL with a host | `ConfigError` naming `cliproxy.base_url` |
| `dashboard.base_url` is `https://` with a host and no userinfo | `ConfigError` naming `dashboard.base_url` |
| `dashboard.poll_seconds >= 60` | `ConfigError` naming the key |
| `dashboard.product` is a non-empty string | `ConfigError` naming the key |
| SSRF guard: a non-loopback `cliproxy.base_url` requires `cliproxy.allow_remote: true`. Loopback is `127.0.0.0/8`, `::1`, `localhost` | `ConfigError` explaining that a remote URL needs `allow_remote` |
| The SSRF guard does **not** apply to `dashboard.base_url` — the dashboard is a remote host by design. It is instead required to be `https://` with no userinfo, so credentials cannot be smuggled into the URL | `ConfigError` naming `dashboard.base_url` |
| Unparseable JSON | `ConfigError("<path>: <reason>")` |

What the user actually sees, per entry point:

- **`sarvam-meter` CLI** — the message on stderr, exit code `2` for a config or
  usage error (exit `1` is a runtime error). The config path appears in the
  message; file contents never do.
- **Daemon** — refuses to start, printing the same message, instead of dying with
  a traceback. A missing config file is the exception: `init` writes defaults, and
  a daemon started without a config gets a clear "no config, run `sarvam-meter
  init`" message rather than a stack trace.
- **Database path unwritable** — the daemon still starts and still serves
  `/v1/status`, but the payload carries a `ledger` error entry and zeroed numbers.
- **Port already in use** — `OSError` surfaced as a clear message, exit `1`.

Two rules worth spelling out because they are easy to trip over:

- **Unknown keys are ignored, missing keys are defaulted.** A typo such as
  `"startin_balance"` will not error — it is silently ignored and the default
  (`null`) is used. Run `sarvam-meter doctor` or `GET /v1/config` to see the
  effective configuration.
- **`allow_remote` is per section, and only `cliproxy` has it.** `cliproxy.base_url`
  is the only configurable remote URL left, so it is the only one the guard
  applies to. `dashboard.base_url` is remote by design and is instead required to
  be `https://` with no userinfo.

## Rate card overrides

Costs come from a hardcoded pay-as-you-go rate card in `sarvam_meter/rates.py`
(list prices, INR). The shipped card:

| api | model | price |
| --- | --- | --- |
| `chat` | `default`, `sarvam-105b`, `sarvam-105b-conversations` | 15.0 per 1M input, 5.0 per 1M cached input, 60.0 per 1M output |
| `chat` | `glm5.3` | 126.0 / 20.0 / 390.0 |
| `chat` | `glm5.3-flash` | 13.5 / 2.5 / 45.0 |
| `chat` | `gemma4` | 35.0 / 10.0 / 90.0 |
| `chat` | `deepseekv4.1-flash` | 25.0 / 0.5 / 105.0 |
| `stt` | `default` | 0.49 per minute |
| `tts` | `default` | 3.00 per 1k characters |
| `translate` | `default` | not published (`null`) |
| `transliterate` | `default` | not published (`null`) |
| `lid` | `default` | not published (`null`) |
| `other` | `default` | — |

Model lookup is exact match first, then the `"default"` entry for that api.
`cached_quantity` and `out_quantity` apply to `chat` only. Output tokens cost four
times input tokens on the Sarvam models, so a caller that prices a chat call must
pass `out_quantity`; without it the blended path prices every non-cached token at
the input rate and understates the call by up to 4x. A price that would overflow to
infinity raises `ValueError` rather than being stored.

`sarvam.rate_overrides` is handed to `rate_card(overrides)` and merged over the
default card; keys follow the same `api` → `model` → price shape as the card
above. (SPEC.md fixes the `rate_card(overrides)` signature and the card's shape
but not a deeper merge rule, so keep overrides to the same two levels.)

Example — you negotiated a cheaper TTS rate and you know your translate price:

```json
"sarvam": {
  "enabled": true,
  "currency": "INR",
  "starting_balance": 500.0,
  "rate_overrides": {
    "tts":       { "default": { "per_1k_chars": 2.50 } },
    "translate": { "default": { "per_1k_chars": 0.20 } }
  }
}
```

**A price of `null` means "not published", and that is deliberate.** For such an
api, `cost_for` returns `0.0` and `rate_known` returns `False`. `sarvam-meter
record` still stores the entry — with the quantity you gave it — but with cost `0`
and `estimated = 1`, so the ledger says plainly that the entry is not priced. The
same happens for an api name the card does not know at all. Nothing is ever priced
by guesswork.

A negative or non-finite quantity raises `ValueError` in `cost_for` — that is a
programming error in a caller, not something a user can trigger through config.

## Widget settings

The plasmoid has five settings of its own, independent of the daemon config
(`plasmoid/io.github.creepy_pasta101.sarvammeter/contents/config/main.xml`):

| Setting | Type | Default | Meaning |
| --- | --- | --- | --- |
| `daemonUrl` | string | `http://127.0.0.1:8787` | Base URL of the daemon. Must be `http://` or `https://`; the settings page rejects anything else inline instead of saving it. |
| `refreshSeconds` | int | `60` | Widget poll interval. On error the interval doubles, up to 10x, and resets on success. |
| `compactMode` | string | `sarvam` | What the compact representation shows: `sarvam` (remaining balance) or `cliproxy` (worst remaining fraction). |
| `warnBelowPercent` | int | `20` | Below this percentage the compact value turns red. |
| `showApiBreakdown` | bool | `true` | Whether the full representation draws a bar per top-3 `by_api` entry. |

The settings page has a **Test** button that issues an asynchronous
`GET <daemonUrl>/v1/health` and reports OK or FAIL, so a wrong URL is caught
before it becomes an offline widget.

The dashboard source adds **no** widget setting. The widget reads
`sarvam.spend.source` and `sarvam.dashboard` out of the payload and shows a muted
`dashboard` or `local meter` hint beside the spend line, up to three by-model rows
when `sarvam.dashboard.reachable` is true, and one `dashboard: <code>` line when
`sarvam.dashboard.error` is set. If the daemon is older than schema 2 and sends no
dashboard block at all, the widget renders the ledger presentation and says
`local meter` — nothing is undefined and nothing errors.
