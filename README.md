# sarvam-meter-plasmoid

A KDE Plasma 6 widget, plus a small local Python daemon, that reports Sarvam AI
credit usage and CLIProxyAPI OAuth quota in your panel.

Version 0.1.0. MIT licensed.

## The honest part first

Sarvam AI publishes **no** usage or credits REST API. Balance and usage exist only
in their web dashboard, so no program on your machine can *ask* Sarvam how many
credits you have left.

This project does not pretend otherwise. It meters usage **locally**:

- the daemon can run as a **recording reverse proxy** in front of
  `https://api.sarvam.ai`;
- for each call it relays it measures the billable quantity (chat tokens, TTS
  characters, STT seconds), prices it against a rate card, and writes it to a
  local **SQLite ledger**;
- you enter a **starting credit balance** once, and the widget shows
  `remaining = starting balance - measured spend`, plus spend today and this
  month.

Every Sarvam number the widget shows is a measurement of traffic that went
through your own machine, not a reading of Sarvam's billing system. Calls that do
not pass through the proxy are invisible to it (see
[What is not tracked](#what-is-not-tracked)).

The second source is different, and it is real: CLIProxyAPI's management API
exposes actual quota for the OAuth accounts it proxies, and the widget shows the
remaining fraction per quota window per account.

### The dashboard source (optional)

The third source reads the numbers Sarvam's own dashboard shows, using a session
you capture once from your browser. It adds what local metering structurally
cannot know: the **authoritative, org-wide spend** — every call from every machine
and every client, not just the traffic that happened to go through this proxy —
plus the per-model cost breakdown for the window, and a balance derived from the
dashboard's spend since the date you set your starting balance.

It is opt-in, because it is honest about what it is: an unsupported internal
endpoint behind Cloudflare bot management, reached with a session cookie that
expires and that only you can renew. It polls every five minutes by default, it is
read-only, and when it cannot reach the dashboard the widget says so on one muted
line (`dashboard: unauthorized`, `dashboard: no_session`, and so on) and keeps
showing the ledger numbers.

To enable it: capture the cookie from a signed-in `https://indus.sarvam.ai`,
`sarvam-meter session set --cookie-file <file>` (written `0600`), then set
`"dashboard": {"enabled": true}` in the config and restart the daemon. Details:
[docs/dashboard-scrape.md](docs/dashboard-scrape.md) and
[docs/configuration.md](docs/configuration.md#dashboard).

**Local metering remains the fallback and is never switched off.** The dashboard
does not replace the recording proxy: it cannot see individual calls, it cannot
price a request, and it stops working the day Sarvam changes a private endpoint or
your session lapses. When it works, the widget labels the spend line `dashboard`;
when it does not, the label reads `local meter` and nothing else changes.

## Features

- Remaining Sarvam balance, spend today, spend this month, call count, from the
  local ledger.
- Per-API breakdown for `chat`, `stt`, `tts`, `translate`, `transliterate`, `lid`
  and everything else.
- Recording proxy that meters chat tokens (including SSE streams), TTS characters
  counted in code points, STT seconds derived from the uploaded audio file, and
  request counts.
- Distinguishes upstream `402` (credits/quota exhausted) from `429` (rate
  limited, counted over the last 24 hours).
- Flags calls it could not measure instead of silently reporting them as free.
- CLIProxyAPI quota per OAuth account: remaining fraction per window, reset time,
  plan, plus a worst-account summary for the compact view.
- Optional dashboard source: authoritative org-wide spend, spend today and the top
  models by cost, with the widget showing which source each figure came from and
  falling back to the ledger when the dashboard is unreachable.
- Plasma 6.7 and 6.8 compatible QML, compact and full representations.
- Python standard library only, no runtime dependencies; Python 3.9+.
- Loopback-only binds, secret redaction, and an SSRF guard on remote URLs.

## How it works

Two servers, two ports, one config file.

```
  client (Sarvam SDK / curl, base_url = http://127.0.0.1:8788)
      |
      v
  recording proxy  127.0.0.1:8788  ---------------->  https://api.sarvam.ai
      |  measures billable quantity, prices it,
      |  records one event per upstream attempt
      v
  SQLite ledger  ~/.local/share/sarvam-meter/usage.db
      ^
      |  reads
  daemon  127.0.0.1:8787  ------------------------->  CLIProxyAPI management API
      ^                                               (real OAuth account quota)
      |  GET /v1/status  (loopback HTTP only)
  Plasma widget (plasmoid)
```

- The **widget** does no network work beyond `GET` on the local daemon.
- The **daemon** serves the status JSON, caches the CLIProxyAPI snapshot, and
  re-polls it in a background thread. When the optional dashboard source is
  enabled it caches that snapshot too, with a TTL of `dashboard.poll_seconds`.
- The **proxy** is off by default. Nothing is metered until you enable it and
  point a client at it.

Details: [docs/architecture.md](docs/architecture.md).

## Installation

### Fedora RPM from CI

CI (`.github/workflows/ci.yml`) builds the package in a `fedora:latest` container
on every push and pull request, and uploads the resulting `.rpm` files with
`actions/upload-artifact@v4`; the plasmoid is zipped into
`sarvam-meter-<version>.plasmoid` as a separate artifact. Tagged builds (`v*`)
attach the same artifacts to a GitHub Release.

There is no distribution-package version of this project and no published
release yet: take the RPM from the artifacts of a CI run you trust, then

```
sudo dnf install ./plasma-applet-sarvam-meter-0.1.0-1.<dist>.noarch.rpm
systemctl --user enable --now sarvam-meter.service
```

The package installs the Python package, a `/usr/bin/sarvam-meter` wrapper, the
plasmoid, a systemd **user** unit, and the docs under `/usr/share/doc/`.

### Developer install

From a checkout:

```
./scripts/install-dev.sh
```

That script sets up a development install from the checkout; if you prefer to do
it by hand, install the package (`python3 -m pip install .`, which provides the
`sarvam-meter` entry point) and install the plasmoid directory
`plasmoid/io.github.creepy_pasta101.sarvammeter/` as a user plasmoid under
`~/.local/share/plasma/plasmoids/`. The repo also carries `scripts/smoke.sh` and
`scripts/build-plasmoid.sh`.

## Quick start

1. Write a default config (prints the path it wrote):

   ```
   sarvam-meter init
   ```

2. Record the balance you currently see in the Sarvam dashboard:

   ```
   sarvam-meter balance set 500     # optional once the dashboard source is on
   ```

3. Turn the recording proxy on. In `~/.config/sarvam-meter/config.json` set
   `"proxy": { "enabled": true }`, then start both servers:

   ```
   sarvam-meter serve --with-proxy
   ```

   (`systemctl --user start sarvam-meter` does the same via the packaged unit,
   which runs `sarvam-meter serve --with-proxy`.)

4. Point your Sarvam client at the proxy by changing its base URL:

   ```
   base_url = "http://127.0.0.1:8788"     # was https://api.sarvam.ai
   ```

   Requests are forwarded to `https://api.sarvam.ai` unchanged, so your API key
   and payloads behave exactly as before.

5. Add the **Sarvam Meter** widget to a Plasma panel. Its default daemon URL is
   `http://127.0.0.1:8787`, which is where the daemon listens.

Check the state at any time:

```
sarvam-meter status          # one line per source
sarvam-meter doctor          # config, data dir, db, ports, cliproxy
curl -s http://127.0.0.1:8787/v1/status | python3 -m json.tool
```

## Configuration

The config lives at `$SARVAM_METER_CONFIG`, else
`~/.config/sarvam-meter/config.json`; the database at
`~/.local/share/sarvam-meter/usage.db`. Every key, its type, default and
validation rule is in [docs/configuration.md](docs/configuration.md). The
default document is mirrored in `packaging/config.example.json`.

CLIProxyAPI is the only other integration to configure: enable it, set
`base_url`, and set the management key (or export
`SARVAM_METER_CLIPROXY_KEY`). See [docs/cliproxyapi.md](docs/cliproxyapi.md).

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Widget shows a `!` badge and stale numbers | The daemon is down or unreachable. The widget keeps the last good payload and backs off up to 10x its refresh interval. Check `systemctl --user status sarvam-meter` or run `sarvam-meter doctor`. |
| `sarvam-meter serve` exits 1 with an `OSError` about the address | Port already in use. `doctor` reports the daemon port and the proxy port (when enabled) as `OK`/`FAIL`. Change `daemon.port` / `proxy.port`, or stop the process holding the port. |
| `quota_exhausted` is true and the compact view shows `!` | Upstream returned `402`. Sarvam is refusing calls for lack of credits; the timestamp is kept in `quota_exhausted_at`. This is not the same as a `429`, which only increments `rate_limits.count_24h` and does not mark the account exhausted. |
| Spend does not move after a chat call | The response carried no `usage` object (and the stream carried no usage chunk). The call is recorded with `estimated = 1`, `quantity = null`, `cost = 0` and counted in `untracked_calls`. When you stream, the stream itself must carry a usage chunk for the tokens to be counted. |
| STT calls never reduce the balance | The uploaded audio duration could not be parsed, so the call is untracked in the same way. Duration is derived from WAV, FLAC, OGG, MP3 and MP4/M4A payloads only; anything else (or a malformed file) yields no measurement. |
| The full view shows `dashboard: unauthorized` (or `no_session`) | The captured dashboard session expired or is missing. Re-capture the cookie from a signed-in browser and run `sarvam-meter session set --cookie-file <file>`; until then the widget shows ledger numbers, labelled `local meter`. `sarvam-meter doctor` reports the session file's mode and which headers are present. |
| `dashboard: unreachable` or `dashboard: bad_response` | The dashboard did not answer, or answered with something that is not its usual JSON (a Cloudflare challenge, or a changed endpoint). Nothing to fix locally: the widget keeps the last good dashboard snapshot and falls back to the ledger on the next one. Raise `dashboard.poll_seconds` if it happens often. |
| CLIProxyAPI section shows an error | `unauthorized` (401/403) means the management key is wrong or missing; `endpoint_missing` (404) means the configured `api_version` does not match the server; a per-account `no quota provider` (501) means that account has no quota provider behind it. All are non-fatal and appear in `errors`. |
| Everything is zero but traffic works | The client is not going through the proxy. Only requests to `127.0.0.1:8788` are metered. |

## What is not tracked

- **Calls that bypass the proxy.** Anything sent straight to
  `https://api.sarvam.ai`, or through another machine, is never seen.
- **Batch jobs launched elsewhere.** Server-side or scheduled work that does not
  originate from a client pointed at the proxy is invisible.
- **Unpublished rates.** `translate`, `transliterate` and `lid` have no published
  list price in the shipped rate card, so they are recorded with cost `0` and
  reported as untracked rather than guessed at. If you know your contracted price,
  add a rate override (see [docs/configuration.md](docs/configuration.md)).
- **Sarvam's own view of your balance.** If you top up, change plan, or spend
  outside this machine, re-run `sarvam-meter balance set <amount>` with the number
  the dashboard shows — unless the dashboard source is enabled and reachable, in
  which case the spend side of that figure comes from the dashboard itself.

The ledger is a measurement tool, not a billing record. Sarvam's dashboard is the
authority, and with the optional dashboard source enabled the widget reads it
directly; without it, the ledger is the best estimate this machine can make.

## Documentation

- [docs/architecture.md](docs/architecture.md) — modules, database schema, status
  JSON contract, threading, streaming, security posture.
- [docs/configuration.md](docs/configuration.md) — every config key, env
  overrides, validation, rate overrides, widget settings.
- [docs/cliproxyapi.md](docs/cliproxyapi.md) — the CLIProxyAPI adapter and its
  endpoints.
- [docs/dashboard-scrape.md](docs/dashboard-scrape.md) — the optional dashboard
  source: the captured endpoints, the session file, and the caveats.
- [docs/edge-cases.md](docs/edge-cases.md) — the 22 contractual edge cases, the
  dashboard cases added by §17, and where each is handled.
- [SPEC.md](SPEC.md) — the contract the implementation is written against.

## Licence

MIT. See [LICENSE](LICENSE). Copyright (c) 2026 Suhan G Paradkar.
