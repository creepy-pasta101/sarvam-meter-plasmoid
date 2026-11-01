# Configuration

Everything is one JSON document, read once at startup. There is no hot reload:
change the file and restart the daemon.

## File locations

| What | Path |
| --- | --- |
| Config | `$SARVAM_METER_CONFIG` if set, else `~/.config/sarvam-meter/config.json` |
| Database | `db_path` from the config; default `~/.local/share/sarvam-meter/usage.db` |
| Data directory | `~/.local/share/sarvam-meter/` (created on demand) |
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
override the daemon or proxy port except by editing the config, or by the
`--port` flag on `serve` / `proxy` for that one run.

## The default document

This is exactly what `sarvam-meter init` writes, and what
`packaging/config.example.json` must contain:

```json
{
  "version": 1,
  "db_path": "~/.local/share/sarvam-meter/usage.db",
  "daemon": { "host": "127.0.0.1", "port": 8787, "refresh_seconds": 60, "request_timeout_seconds": 10 },
  "proxy":  { "enabled": false, "host": "127.0.0.1", "port": 8788,
              "upstream_base": "https://api.sarvam.ai", "max_body_bytes": 268435456 },
  "sarvam": { "enabled": true, "currency": "INR", "starting_balance": null,
              "rate_overrides": {} },
  "cliproxy": { "enabled": false, "base_url": "http://127.0.0.1:8317",
                "management_key": "", "api_version": "v8", "quota_fetch": true,
                "quota_fetch_interval_seconds": 300, "include_api_key_usage": false,
                "allow_remote": false }
}
```

## Keys

### Top level

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `version` | int | `1` | Config document version. Must be `1`; anything else is rejected. |
| `db_path` | string | `~/.local/share/sarvam-meter/usage.db` | SQLite ledger path. `~` is expanded. Parent directories are created by the ledger. |
| `daemon` | object | see below | The status server. |
| `proxy` | object | see below | The recording reverse proxy. |
| `sarvam` | object | see below | Balance, currency, rate card overrides. |
| `cliproxy` | object | see below | The CLIProxyAPI adapter. |

Unknown top-level keys are ignored (a newer config opened by an older build must
not fail). Missing keys take their defaults: the loaded document is deep-merged
over the default.

### `daemon`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `host` | string | `127.0.0.1` | Bind address for the status server. Keep it on loopback; the API has no authentication. |
| `port` | int | `8787` | Bind port, `1..65535`. This is the port the widget polls. |
| `refresh_seconds` | int | `60` | Refresh cadence in seconds, `>= 5`. The widget's own poll interval is a separate setting (`refreshSeconds`, below). |
| `request_timeout_seconds` | int | `10` | Timeout for outbound requests the daemon makes (the CLIProxyAPI calls), `>= 1`. |

Example — move the daemon because 8787 is taken:

```json
"daemon": { "host": "127.0.0.1", "port": 18787, "refresh_seconds": 30, "request_timeout_seconds": 10 }
```

Then set the widget's daemon URL to `http://127.0.0.1:18787`.

### `proxy`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Whether the recording proxy is part of `serve`. Off means nothing is metered. |
| `host` | string | `127.0.0.1` | Bind address for the proxy. |
| `port` | int | `8788` | Bind port, `1..65535`. This is the URL your Sarvam client must point at. |
| `upstream_base` | string | `https://api.sarvam.ai` | Where requests are forwarded. Must be an `http://` or `https://` URL with a host. |
| `max_body_bytes` | int | `268435456` | Largest accepted request body (256 MiB). Bigger uploads get `413` and are not forwarded. |

Example — raise the upload ceiling for long audio and listen on a different port:

```json
"proxy": { "enabled": true, "host": "127.0.0.1", "port": 18788,
           "upstream_base": "https://api.sarvam.ai", "max_body_bytes": 536870912 }
```

### `sarvam`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | Show the Sarvam section at all. |
| `currency` | string | `INR` | Three-letter uppercase currency code, used for formatting. |
| `starting_balance` | number or null | `null` | The credit balance you saw in the Sarvam dashboard. `null` means "not set": the widget shows spend only, and `balance.known` is `false`. `sarvam-meter balance set <amount>` stores the same figure in the ledger (table `balance`), which is what the status payload reports. |
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

## Validation rules

Validation runs on load. Every failure raises `ConfigError` whose message names
the offending key, so you can fix it without reading source.

| Rule | Failure |
| --- | --- |
| `version` must be `1` | `ConfigError` naming `version` |
| `daemon.port`, `proxy.port` in `1..65535` | `ConfigError` naming the port key |
| `daemon.refresh_seconds >= 5` | `ConfigError` naming the key |
| `daemon.request_timeout_seconds >= 1` | `ConfigError` naming the key |
| `cliproxy.api_version` in `{"v8","v0"}` | `ConfigError` naming the key |
| `sarvam.currency` is a 3-letter uppercase code | `ConfigError` naming the key |
| `sarvam.starting_balance` is `null` or a finite number `>= 0` | `ConfigError` naming the key |
| `proxy.upstream_base` and `cliproxy.base_url` are `http(s)://` URLs with a host | `ConfigError` naming the key |
| SSRF guard: a non-loopback `cliproxy.base_url` requires `cliproxy.allow_remote: true`. Loopback is `127.0.0.0/8`, `::1`, `localhost` | `ConfigError` explaining that a remote URL needs `allow_remote` |
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
- **`allow_remote` is per section, and only `cliproxy` has it.** SPEC.md applies
  the same guard to `proxy.upstream_base`, but `ProxyConfig` defines no
  `allow_remote` field and the shipped `upstream_base` default is itself remote,
  so in practice the guard is enforced for `cliproxy.base_url`. Do not widen
  `upstream_base` casually.

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
times input tokens on the Sarvam models, so the recording proxy always passes
`out_quantity`; without it the blended path prices every non-cached token at the
input rate and understates the call by up to 4x. A price that would overflow to
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
api, `cost_for` returns `0.0` and `rate_known` returns `False`. The event is
still recorded — with the measured quantity — but with cost `0` and flagged as an
untracked call, so the widget's `untracked_calls` counter tells you the balance
figure is incomplete. The same happens for an api name the card does not know at
all. Nothing is ever priced by guesswork.

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
