# Scraping the dashboard (planned 0.2 source)

Version 0.1.0 meters usage locally because Sarvam publishes no usage or
credit-balance REST endpoint. The alternative — and in some ways the better one —
is to read the numbers the dashboard itself shows, from the user's own signed-in
session. This document records what is known about that path, so the work can be
finished against facts rather than guesses.

## What the dashboard is

- `https://dashboard.sarvam.ai` answers `308` and redirects to
  `https://indus.sarvam.ai`.
- Unauthenticated requests are redirected to `/login?return_to=%2F`.
- The app is a Next.js single-page application (it serves `/_next/image` and
  streams its payload inline), behind Cloudflare bot management
  (`__cf_bm` cookie, `SameSite=None; Secure; Domain=sarvam.ai`).
- Cloudflare answers a blocked client with its "you have been blocked" page,
  sometimes with HTTP `200` and sometimes `403`, so a status code alone cannot
  tell a real page from a challenge.
- The endpoint list is **not documented**, but it is partly discoverable: the
  JavaScript chunks name their own endpoints, and an unknown `/api/...` path falls
  through to the app router and returns the SPA shell with `404`, while a real
  endpoint answers `401 application/json` when unauthenticated. That difference
  is a reliable existence test.

## What has been confirmed so far

These paths answer `401 application/json` without a session, so they exist and
are the dashboard's own API:

| path | likely content |
| --- | --- |
| `/api/billing/billing-status` | credit balance, plan, low-balance state |
| `/api/billing/billing-profile` | organisation billing profile |
| `/api/billing/subscriptions` | plan and subscription details |
| `/api/billing/payment-instruments` | saved payment methods |
| `/api/billing/quote` | pricing quote |
| `/api/billing/registration-options` | signup options |

The usage/spend endpoints are **not** at the obvious `/api/usage`, `/api/usage/summary`
or `/api/analytics/usage` paths (all of those return the `404` shell). They are
most likely built from a template string inside the bundle, which is why an
authenticated run of the probe, or the Network tab, is needed to pin them down.

## What that means for the design

Reading the dashboard is better data than local metering: it is organisation-wide,
it includes credit purchases and workspace attribution, and it needs no proxy in
the request path. It is worse in three ways, and the design has to absorb them:

1. **It is unsupported.** These are internal endpoints behind bot management.
   They can change without notice, and a widget polling them is exactly the
   traffic bot management exists to notice. Poll slowly (5-10 minutes, not 60
   seconds) and treat a failure as normal.
2. **Authentication cannot be automated safely.** The login flow is a browser
   flow behind Cloudflare; storing a password to replay it is both fragile and a
   credential-handling problem. The workable route is a session cookie the user
   captures once from their own browser, stored `0600`, refreshed when it expires.
3. **It cannot be tested offline.** The local ledger is deterministic and fully
   covered by tests; a scraper can only be tested against fixtures captured from
   a real session.

That is why the plan is *additive*: a second source, preferred when it works,
falling back to the ledger (or to "unknown") when it does not. The widget already
renders an unknown/unavailable state, so nothing in the UI has to change shape.

## Step 1 — capture the endpoints (do this first)

`scripts/probe-dashboard.py` does the discovery without guessing a schema. It
fetches the app shell and its JavaScript chunks with your cookie, pulls every
`/api/...` string out of them, GETs each candidate, and writes one JSON report
with the status, content type and body of every response.

```bash
# Sign in to https://indus.sarvam.ai in a browser.
# DevTools -> Network -> any request -> copy the whole Cookie header value.
printf '%s' 'PASTE_THE_COOKIE_HEADER_HERE' > ~/.cache/sarvam-cookie.txt
chmod 600 ~/.cache/sarvam-cookie.txt

python3 scripts/probe-dashboard.py --cookie-file ~/.cache/sarvam-cookie.txt --out probe.json
```

If discovery finds nothing, open DevTools, click through Settings -> Usage and
Settings -> Billing, and note the request URLs the page makes; those can be added
to `CANDIDATE_ENDPOINTS` in the script and re-run. `--redact` masks every number
in the report if you would rather not share your actual figures.

The cookie is sent only to the host in `--base`, never printed, never written to
the report, and never logged. Every request is a paced GET; nothing is mutated.

## Step 2 — write the adapter against the captured shapes

Once the report exists, the mapping is mechanical:

- a `dashboard` section in the config (`enabled`, `base_url`, `cookie_file`,
  `poll_seconds`, `endpoints.credits`, `endpoints.usage`),
- a `sarvam_meter/dashboard.py` client shaped like `cliproxy.py`: injectable
  transport, cookie read from a `0600` file, never logged, `snapshot()` that
  never raises,
- a `balance.source` field in the status payload (`"ledger"` or `"dashboard"`)
  so the widget can label which number it is showing,
- fixture-based tests captured from the report, plus the existing rule that a
  source failure degrades to `unknown` rather than to a wrong number.

## What is deliberately not planned

- Storing a Sarvam password, or automating the login flow.
- Scraping the HTML of the usage page: the app is client-rendered, so the HTML
  carries no figures. The JSON the page fetches is the only usable target.
- Any write, purchase or settings call. The adapter is read-only by construction.
