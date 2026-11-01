# The dashboard source

Sarvam publishes no usage or credit-balance REST endpoint, so the dashboard — the
source this document is about — is where every real figure comes from: it reads the
numbers the dashboard itself shows, from the user's own signed-in session. It is
implemented, it is **on by default**, and it is **unsupported**.

Nothing in this project measures API traffic: it relays nothing and listens on no
TCP port. What the dashboard cannot supply, the local ledger's manual
`sarvam-meter record` entries supply, and nothing more.

The plumbing is described in [architecture.md](architecture.md#4a-the-dashboard-source);
the config keys are in [configuration.md](configuration.md#dashboard). This document
records what the dashboard is, what was actually captured from it, and what the
source is and is not good for.

## What the dashboard is

- `https://dashboard.sarvam.ai` answers `308` and redirects to
  `https://indus.sarvam.ai`.
- Unauthenticated requests are redirected to `/login?return_to=%2F`.
- The app is a Next.js single-page application (it serves `/_next/image` and
  streams its payload inline), behind Cloudflare bot management (`__cf_bm` cookie,
  `SameSite=None; Secure; Domain=sarvam.ai`).
- Cloudflare answers a blocked client with its "you have been blocked" page,
  sometimes with HTTP `200` and sometimes `403`, so a status code alone cannot tell
  a real page from a challenge.
- The endpoint list is **not documented**, but it is partly discoverable: the
  JavaScript chunks name their own endpoints, and an unknown `/api/...` path falls
  through to the app router and returns the SPA shell with `404`, while a real
  endpoint answers `401 application/json` when unauthenticated. That difference is
  a reliable existence test, and it is how the paths below were separated from the
  guessed ones.

## What was captured

Discovery ran against a real signed-in session with
`scripts/probe-dashboard.py`, and the two usage responses were captured verbatim.
They are committed as fixtures and are the contract for the parser:

| fixture | request |
| --- | --- |
| `tests/fixtures/dhan_usage_summary_by_model.json` | `GET /api/dhan/org/usage/summary?start=YYYY-MM-DD&end=YYYY-MM-DD&group_by=model&product=sarvam-api-platform` |
| `tests/fixtures/dhan_org_balance.json` | `GET /api/dhan/org/balance` — the authoritative credits left |
| `tests/fixtures/dhan_usage_timeseries_by_product.json` | `GET /api/dhan/org/usage/timeseries?start=YYYY-MM-DD&end=YYYY-MM-DD&group_by=product&product=sarvam-api-platform&granularity=auto` |

Both need the session. Unauthenticated they answer `401 application/json`; an
unknown `/api/...` path answers `404` with the SPA shell.

Summary payload (`group_by=model`), shape as captured:

```json
{
  "org_id": "019f37be-...",
  "range": {"start": "2026-10-02T18:30:00Z", "end": "2026-11-01T18:29:59Z"},
  "group_by": ["model"],
  "subtotal": "134.2312311905", "total_discount": "0",
  "total_cost": "134.2312311905", "total_org_cost": "134.2312311905",
  "currency": "inr",
  "usage": [
    {"unit": "token", "quantity": "13046343",
     "breakdown": [{"unit": "cached_token", "quantity": "11296128"},
                   {"unit": "input_token", "quantity": "1513846"},
                   {"unit": "output_token", "quantity": "236369"}]},
    {"unit": "second", "quantity": "9086.323125"}
  ],
  "groups": [
    {"model": "glm5.3-flash", "subtotal": "53.388129", "total_cost": "53.388129", "currency": "inr", "usage": ["..."]},
    {"model": "saaras:v3-realtime", "total_cost": "0.1551666667", "usage": ["..."]},
    {"model": "saaras:v4", "total_cost": "74.0498055238", "usage": ["..."]},
    {"model": "sarvam-105b", "total_cost": "6.63813", "usage": ["..."]}
  ]
}
```

The timeseries payload (`group_by=product`) has the same envelope plus
`granularity`, `available_granularities`, `groups[].product`, and
`points: [{"timestamp", "subtotal", "total_discount", "total_cost", "event_count"}]`
— 28 hourly points in the captured response — and no per-model groups.

Two details of those payloads are easy to get wrong, so they are stated as rules:

- **Every number arrives as a JSON string** (`"53.388129"`, `"9086.323125"`). They
  are parsed to `float`; a non-numeric string is an error, never a silent `0`.
- **`currency` arrives lowercase** (`"inr"`) and is normalised to `"INR"`.

Nothing in this document, and nothing in `sarvam_meter/dashboard.py`, is guessed
beyond those two fixtures.

## What the source does, and what it does not do

It reads, per snapshot:

- the window summary — total cost, the per-model groups, and the usage units;
- today's summary, for the "spend today" figure.

`/api/dhan/org/balance` is the endpoint that matters most: it reports the credits
left directly, so no starting balance and no arithmetic are involved. The usage
endpoints supply the spend windows, the per-model split and the token/audio
quantities. It produces `sarvam.spend.source = "dashboard"`, `sarvam.balance.source =
"dashboard"`, a `sarvam.dashboard` block with `by_model`, and, when it cannot read
the dashboard, one error code. It does **not**:

- replace the ledger or the `record` subcommand, or claim to;
- see per-call detail, individual requests, or anything Sarvam does not aggregate;
- write anything: every request is a `GET`, and the adapter has no code path for a
  purchase, top-up or settings call;
- automate login, or store a password (see below).

## The session

Authentication cannot be automated safely: the login flow is a browser flow behind
Cloudflare, and storing a password to replay it is both fragile and a
credential-handling problem. The workable route is a session cookie you capture once
from your own browser and store in a `0600` file:

```json
{"Cookie": "...", "Authorization": "Bearer ..."}
```

Only those two keys are accepted (case-insensitively). `sarvam-meter session set`
writes the file with mode `0600`; `sarvam-meter session show` prints the path, the
mode and which headers are present — never a value. The values are never logged,
never returned by `GET /v1/config`, and never written into the status payload or an
error message. A missing or unreadable file is `no_session`.

To capture the session:

```bash
# Sign in to https://indus.sarvam.ai in a browser.
# DevTools -> Network -> any request to that origin -> copy the Cookie header value.
printf '%s' 'PASTE_THE_COOKIE_HEADER_HERE' > ~/.cache/sarvam-cookie.txt
chmod 600 ~/.cache/sarvam-cookie.txt
sarvam-meter session set --cookie-file ~/.cache/sarvam-cookie.txt
rm ~/.cache/sarvam-cookie.txt
```

The session expires when the dashboard decides it does. When it does, the daemon
gets `401`/`403`, records `unauthorized`, the widget shows one muted
`dashboard: unauthorized` line, and the numbers fall back to the ledger until you
capture a fresh session. There is no refresh flow and none is planned.

## The caveats that did not go away

Being implemented does not make the endpoint supported, and these are the reasons
the source is quiet about failure:

1. **It is unsupported.** `/api/dhan/org/usage/*` is an internal endpoint of a
   private front-end, not a published API. It can change shape or disappear without
   notice, and when it does the parser reports `bad_response` rather than guessing
   at the new shape.
2. **It is behind Cloudflare bot management.** A widget polling these paths is
   exactly the traffic bot management exists to notice, which is why the default
   poll is 5 minutes and the config refuses anything under 60 seconds. A challenge
   page looks like `unreachable` or `bad_response`; it is not retried in a tight
   loop, and the backoff on the widget side is unchanged.
3. **The session expires.** There is no way to renew it except by hand, from a
   browser. A dashboard that worked yesterday can report `unauthorized` today.
4. **It polls every 5–10 minutes.** The dashboard's numbers are already
   aggregate-and-cached on its side; polling faster buys nothing and costs
   goodwill. Treat the figures as "as of the last successful snapshot"
   (`sarvam.dashboard.as_of`).
5. **It cannot be tested offline.** The local ledger is deterministic and fully
   covered by tests, but it only holds what you recorded by hand. The dashboard
   adapter is tested against the two captured
   fixtures and a stubbed transport — including `401`, non-JSON, transport failure,
   a missing session and a non-numeric number — but no test can prove that the live
   endpoint still behaves like the capture. That gap is real and permanent.

Because of all five, the design is honest rather than clever: the dashboard is
shown when it works, and the ledger (or "unknown") is what you see when it does
not. The widget never shows a dashboard figure it did not receive.

## Reproducing the discovery

`scripts/probe-dashboard.py` does the discovery without guessing a schema. It
fetches the app shell and its JavaScript chunks with your cookie, pulls every
`/api/...` string out of them, GETs each candidate, and writes one JSON report with
the status, content type and body of every response.

```bash
python3 scripts/probe-dashboard.py --cookie-file ~/.cache/sarvam-cookie.txt --out probe.json
```

If discovery finds nothing, open DevTools, click through Settings → Usage and
Settings → Billing, and note the request URLs the page makes; those can be added to
`CANDIDATE_ENDPOINTS` in the script and re-run. `--redact` masks every number in
the report if you would rather not share your actual figures.

The cookie is sent only to the host in `--base`, never printed, never written to the
report, and never logged. Every request is a paced GET; nothing is mutated.

## What is deliberately not done

- Storing a Sarvam password, or automating the login flow.
- Scraping the HTML of the usage page: the app is client-rendered, so the HTML
  carries no figures. The JSON the page fetches is the only usable target.
- Any write, purchase or settings call. The adapter is read-only by construction.
- Polling the `/api/billing/*` paths (billing status, subscriptions, payment
  instruments, quote). They exist, but they answer questions the widget does not
  draw, and every extra call is extra traffic for nothing.
