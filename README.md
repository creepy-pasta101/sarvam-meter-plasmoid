# sarvam-meter-plasmoid

A KDE Plasma 6 widget, plus a small local Python daemon, that reports Sarvam AI
credit usage and CLIProxyAPI OAuth quota in your panel.

Version 0.1.0. MIT licensed.

## The honest part first

Sarvam AI publishes **no** usage or credits REST API. Balance and usage exist only
in their web dashboard, so no program on your machine can *ask* Sarvam how many
credits you have left.

This project therefore does not measure your API traffic, and it does not pretend
to. **It relays nothing and listens on no TCP port.** There is no reverse proxy
here, no local meter in front of `https://api.sarvam.ai`, and no client of yours
has to be pointed anywhere else. It is a **dashboard scraper**: it reads the
numbers Sarvam's own dashboard shows, using a session you capture once from your
browser, and it reports the two things only Sarvam knows:

- the **authoritative, org-wide spend** — every call from every machine and every
  client, not just the traffic one machine happened to see — with the per-model
  cost breakdown for the window;
- the **real credit balance**, read from the balance endpoint rather than derived
  from anything.

The second source is different, and it is real too: CLIProxyAPI's management API
exposes actual quota for the OAuth accounts it proxies, and the widget shows the
remaining fraction per quota window per account.

### The dashboard source

The dashboard is opt-in, because it is honest about what it is: an unsupported
internal endpoint behind Cloudflare bot management, reached with a session cookie
that expires and that only you can renew. It polls every five minutes by default,
it is read-only, and when it cannot reach the dashboard the widget says so on one
muted line (`dashboard: unauthorized`, `dashboard: no_session`, and so on) and
keeps showing whatever it last knew.

To enable it: capture the cookie from a signed-in `https://indus.sarvam.ai`,
`sarvam-meter session set --cookie-file <file>` (written `0600`), then set
`"dashboard": {"enabled": true}` in the config and restart the daemon. Details:
[docs/dashboard-scrape.md](docs/dashboard-scrape.md) and
[docs/configuration.md](docs/configuration.md#dashboard).

### The ledger

A local SQLite ledger still exists, and so does `sarvam-meter record`, for the
entries you want to track by hand: `record` prices one event against the rate card
and stores it. `sarvam-meter status` shows what is in the ledger, and the widget
shows it too whenever the dashboard cannot supply a figure. What the ledger can
never do is see a call you did not tell it about — nothing in this project sits in
front of your API traffic any more.

## Features

- The **reported Sarvam credit balance**, spend today, spend this month and the
  top models by cost, scraped from the Sarvam dashboard.
- CLIProxyAPI quota per OAuth account: remaining fraction per window, reset time,
  plan, plus a worst-account summary for the compact view.
- Manual entries: `sarvam-meter record --api ... --quantity ... --unit ...` prices
  an event against the rate card and stores it in the local ledger, with a per-API
  breakdown and a call count.
- The widget labels which source each money figure came from and falls back to the
  ledger when the dashboard is unreachable.
- A popup built like Plasma's own applets (`PlasmaExtras.Representation` + a
  `PlasmoidHeading` header), with a refresh button that makes the daemon
  re-poll its sources and a spinner while it does.
- The widget's settings page configures the **daemon** too — dashboard on/off, the
  dashboard poll interval (2 minutes to 6 hours), CLIProxyAPI on/off and its base
  URL — over `config.set`, without editing a file by hand.
- Plasma 6.7 and 6.8 compatible QML, compact and full representations.
- Python standard library only, no runtime dependencies; Python 3.9+.
- Loopback-only binds, secret redaction, and an SSRF guard on remote URLs.

## How it works

One daemon, one config file, one database — and no traffic of yours passing
through any of it.

```
  Sarvam dashboard (indus.sarvam.ai)  <--- GET, session cookie, every 5 min
          |                                 (authoritative spend + balance)
          v
  daemon (local socket)  --------------->  CLIProxyAPI management API
      |  serves the status JSON               (real OAuth account quota)
      |  over a local socket only
      v
  Plasma widget (plasmoid)      SQLite ledger <--- sarvam-meter record (manual)
```

- The **widget** does no network work beyond reading the daemon's status over a
  local socket on this machine.
- The **daemon** serves the status JSON, caches the CLIProxyAPI snapshot, and
  re-polls it in a background thread. When the dashboard source is enabled it
  caches that snapshot too, with a TTL of `dashboard.poll_seconds`.
- The **ledger** holds only what `sarvam-meter record` wrote. Nothing appends to
  it behind your back.

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

2. Capture your dashboard session once, so the widget can show the real credit
   balance. The easiest way is in the widget: **right-click the panel item >
   Configure Sarvam Meter > Capture session…**, which opens the Sarvam sign-in
   page in a webview and hands the cookies to the daemon (needs
   `qt6-qtwebengine`; without it the page tells you and falls back). From a
   terminal, the same thing is:

   ```
   sarvam-meter session set --cookie-file ~/.cache/sarvam-cookie.txt
   ```

   A starting balance is not needed for that: the widget reports the figure
   Sarvam reports. `sarvam-meter balance set <amount>` still exists but only feeds
   the "spend only" presentation.

3. Start the daemon — by hand, or as a systemd user service:

   ```
   sarvam-meter serve
   ```

   `serve` starts the daemon and nothing else; there is no proxy to enable.

   The RPM ships a user unit, so a packaged install is:

   ```
   systemctl --user daemon-reload
   systemctl --user enable --now sarvam-meter.service
   journalctl --user -u sarvam-meter.service -f
   ```

   (`loginctl enable-linger "$USER"` keeps it running while you are logged out.)

4. Add the **Sarvam Meter** widget to a Plasma panel. Its default daemon URL
   points at the daemon on this machine; nothing else on your network is involved.

   Optionally, log usage you want tracked by hand:

   ```
   sarvam-meter record --api chat --model sarvam-105b --quantity 1500 --unit tokens
   ```

Check the state at any time:

```
sarvam-meter status          # one line per source
sarvam-meter doctor          # config, data dir, db, daemon socket, cliproxy, dashboard
sarvam-meter dashboard       # one dashboard snapshot
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
| `sarvam-meter serve` exits 1 with an `OSError` about the address | The daemon's socket is already in use — usually a second daemon. `doctor` reports it as `FAIL daemon port free`. Stop the process holding it, or change the `daemon` bind settings. |
| The full view shows `dashboard: unauthorized` (or `no_session`) | The captured dashboard session expired or is missing. Re-capture the cookie from a signed-in browser and run `sarvam-meter session set --cookie-file <file>`; until then the widget shows the ledger numbers. `sarvam-meter doctor` reports the session file's mode and which headers are present. |
| `dashboard: unreachable` or `dashboard: bad_response` | The dashboard did not answer, or answered with something that is not its usual JSON (a Cloudflare challenge, or a changed endpoint). Nothing to fix locally: the widget keeps the last good dashboard snapshot and falls back to the ledger on the next one. Raise `dashboard.poll_seconds` if it happens often. |
| CLIProxyAPI section shows an error | `unauthorized` (401/403) means the management key is wrong or missing; `endpoint_missing` (404) means the configured `api_version` does not match the server; a per-account `no quota provider` (501) means that account has no quota provider behind it. All are non-fatal and appear in `errors`. |
| Spend is zero but you have been calling Sarvam | Expected: this project does not see your API traffic. Enable the dashboard source and capture a session — that is the only way any figure arrives. `sarvam-meter record` is the manual alternative. |
| An entry you recorded never reduces the balance | The balance is only ever the figure Sarvam reports; local entries never move it. They do count towards the ledger's spend and call figures. |

## What is not tracked

- **Per-call detail.** The dashboard aggregates; this project reads the
  aggregates. It cannot tell you which request cost what, because nothing here
  sits in front of your traffic.
- **Anything the dashboard does not show.** If Sarvam's dashboard does not
  display it, this project cannot report it.
- **Unpublished rates.** `translate`, `transliterate` and `lid` have no published
  list price in the shipped rate card, so a `record` entry for them is stored with
  cost `0` rather than guessed at. If you know your contracted price, add a rate
  override (see [docs/configuration.md](docs/configuration.md)).
- **Your balance when the session has lapsed.** There is no fallback derivation:
  the balance stays unknown (`balance.known: false`) and the widget shows spend
  only, rather than a wrong "left" figure.

The ledger is a notebook, not a billing record. Sarvam's dashboard is the
authority, and with the dashboard source enabled the widget reads it directly.

## Documentation

- [docs/architecture.md](docs/architecture.md) — modules, database schema, status
  JSON contract, threading, security posture.
- [docs/configuration.md](docs/configuration.md) — every config key, env
  overrides, validation, rate overrides, widget settings.
- [docs/cliproxyapi.md](docs/cliproxyapi.md) — the CLIProxyAPI adapter and its
  endpoints.
- [docs/dashboard-scrape.md](docs/dashboard-scrape.md) — the dashboard source: the
  captured endpoints, the session file, and the caveats.
- [docs/edge-cases.md](docs/edge-cases.md) — the contractual edge cases, the
  dashboard cases added by §17, and where each is handled.
- [SPEC.md](SPEC.md) — the contract the implementation is written against.

## Licence

MIT. See [LICENSE](LICENSE). Copyright (c) 2026 Suhan G Paradkar.
