# sarvam-meter-plasmoid — SHARED CONTRACT (v1)

This document is the single source of truth for every unit of work on this project.
You are implementing one unit of it. Nothing else in this repo exists yet: assume
every file listed here that is not yours is being written in parallel by another
developer against this same document. **Do not invent interfaces beyond this
document.** If something is genuinely unspecified, choose the obvious option,
keep it consistent with this document, and note the decision in your summary.

---

## 1. What the project is

A KDE Plasma 6 widget (plasmoid) that shows API usage/quota for:

1. **Sarvam AI** — Sarvam publishes **no** usage/credits REST endpoint. Balance and
   usage exist only in their web dashboard, so the project **scrapes that dashboard**:
   with a session the user captures once from their own browser, the daemon reads
   the authoritative org-wide spend, the per-model split for the window, and the
   real credit balance, and serves them as status JSON.
   A local SQLite ledger still exists for **manual** entries (`sarvam-meter record`
   prices one event against a rate card), and `status` reports what it holds.
2. **CLIProxyAPI** (`router-for-me/CLIProxyAPI`) — an optional second source. Its
   management API *does* expose real quota: OAuth-account quotas (Claude / Codex /
   Gemini-CLI style subscriptions) via a normalized quota endpoint. The widget shows
   remaining fraction per quota window per account.

The widget itself does **no** network work beyond `GET` on the local daemon.

**The project relays no API traffic and listens on no TCP port.** It is not a
proxy, it is not a local meter, and no client of the user's is pointed at it. There
is exactly one process — the **daemon**, started by `sarvam-meter serve` — which
serves the status JSON the widget reads over its own local socket. The daemon's
socket transport is not specified here.

---

## 2. Hard rules for every unit

- **Python: standard library only.** No third-party imports at runtime (no requests,
  no pydantic, no tomli). `sqlite3`, `urllib.request`, `http.server`, `json`,
  `zoneinfo`, `threading`, `dataclasses`, `argparse`, `unittest` are all fine.
  Target **Python 3.9+**. No `match` statements, no `X | Y` type unions at runtime
  in annotations that are evaluated (use `Optional[...]`/`from __future__ import annotations`).
- **Tests: `unittest.TestCase` classes only**, discoverable by
  `python3 -m unittest discover -s tests -v` from the repo root. No pytest-specific
  fixtures, no third-party test helpers. Tests must pass with no network access to
  the internet; loopback sockets on `127.0.0.1` with ephemeral ports are allowed.
- **Never hardcode credentials.** API keys / management keys come from config or env.
- **Never log secrets.** Any header named `api-subscription-key`, `authorization`,
  `x-management-key` must be redacted as `***` before it can reach a log line.
- **ASCII only** in source files (the ₹ symbol may appear only inside QML/user-facing
  strings and docs, never in Python source; Python must emit it as `"\u20b9"`).
- Every file starts with the SPDX line `# SPDX-License-Identifier: MIT` (or the QML
  comment form `// SPDX-License-Identifier: MIT`).
- **Write your files to `/scratch/output/` preserving the exact relative paths** given
  in the repo layout (§9). Your `/scratch/output` is the only thing returned.
- Do not modify files owned by other units. Do not create files not in your unit.
- Work the unit to completion. Long-running is fine. A placeholder, stub, `TODO`,
  or "first draft" is a failed unit. If you truly cannot finish, say exactly what is
  missing in your summary — do not silently shrink scope.

---

## 3. Repo layout

```
sarvam-meter-plasmoid/
├── README.md
├── LICENSE                              (MIT, full text)
├── SPEC.md                              (this file, already written by the lead)
├── pyproject.toml
├── .gitignore
├── sarvam_meter/
│   ├── __init__.py        __version__ = "0.1.0"
│   ├── __main__.py        from .cli import main; sys.exit(main())
│   ├── config.py
│   ├── rates.py
│   ├── ledger.py
│   ├── cliproxy.py
│   ├── dashboard.py
│   ├── status.py
│   ├── daemon.py
│   └── cli.py
├── plasmoid/io.github.creepy_pasta101.sarvammeter/
│   ├── metadata.json
│   └── contents/
│       ├── config/{main.xml,config.qml,configGeneral.qml}
│       └── ui/{main.qml,CompactRepresentation.qml,FullRepresentation.qml,Bar.qml}
├── packaging/
│   ├── plasma-applet-sarvam-meter.spec
│   ├── sarvam-meter.service
│   └── config.example.json
├── scripts/{install-dev.sh,build-plasmoid.sh,smoke.sh}
├── tests/{test_config.py,test_rates.py,test_ledger.py,test_cliproxy.py,test_dashboard.py,
│          test_status.py,test_daemon.py,test_integration.py,test_regressions.py,
│          test_service.py,test_config_endpoint.py,test_session_endpoint.py,
│          test_dashboard_status.py,test_qml_safety.py}
├── docs/{architecture.md,configuration.md,cliproxyapi.md,edge-cases.md}
└── .github/workflows/{ci.yml,release.yml}
```

---

## 4. Configuration (`sarvam_meter/config.py`)

Path: `$SARVAM_METER_CONFIG` else `~/.config/sarvam-meter/config.json`.
Data dir: `~/.local/share/sarvam-meter/`.

```python
DEFAULT_CONFIG_PATH: str
class ConfigError(Exception): ...

@dataclass(frozen=True)
class DaemonConfig:  host: str; port: int; refresh_seconds: int; request_timeout_seconds: int
@dataclass(frozen=True)
class SarvamConfig:  enabled: bool; currency: str; starting_balance: Optional[float]; rate_overrides: dict
@dataclass(frozen=True)
class CliproxyConfig: enabled: bool; base_url: str; management_key: str; api_version: str
                      quota_fetch: bool; quota_fetch_interval_seconds: int
                      include_api_key_usage: bool; allow_remote: bool
@dataclass(frozen=True)
class Config:
    version: int; db_path: str; daemon: DaemonConfig
    sarvam: SarvamConfig; cliproxy: CliproxyConfig; dashboard: DashboardConfig

def default_config_dict() -> dict
def load_config(path: Optional[str] = None) -> Config      # raises ConfigError
def write_default_config(path: Optional[str] = None) -> str  # creates dirs, chmod 0600 file / 0700 dir
```

Default JSON document (`packaging/config.example.json` must match it exactly):

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

Validation (each failure raises `ConfigError` with a message naming the offending key):

- unknown top-level keys are ignored; missing keys take defaults (deep merge).
- `version` must be `1`.
- ports in `1..65535`; `refresh_seconds >= 5`; `request_timeout_seconds >= 1`.
- `api_version` in `{"v8","v0"}`.
- `currency` is a 3-letter uppercase code; default `INR`.
- `starting_balance` is `null` or a finite number `>= 0`.
- `cliproxy.base_url` must be an `http://` or `https://` URL with a host.
- **SSRF guard:** a non-loopback `cliproxy.base_url` is rejected unless
  `cliproxy.allow_remote` is `true`. Loopback = `127.0.0.0/8`, `::1`, `localhost`.
- There is no `proxy` section. It was removed with the recording proxy; a document
  that still carries one is not an error (unknown keys are ignored).
- env override: `SARVAM_METER_CLIPROXY_KEY` (if non-empty) replaces
  `cliproxy.management_key`.
- `db_path` supports `~` expansion.
- JSON parse errors raise `ConfigError("... <path>: <reason>")`; the file path must
  never be echoed with file contents.

---

## 5. Rate card (`sarvam_meter/rates.py`)

Units used throughout: `"tokens"`, `"chars"`, `"seconds"`, `"pages"`, `"requests"`.
All money is INR, floats, rounded to 6 decimals on write.

```python
DEFAULT_RATE_CARD: dict
def rate_card(overrides: Optional[dict] = None) -> dict
def cost_for(api: str, model: Optional[str], quantity: float, unit: str,
             card: Optional[dict] = None, cached_quantity: float = 0.0,
             out_quantity: Optional[float] = None) -> float
def rate_known(api: str, model: Optional[str], card: Optional[dict] = None) -> bool
```

Default card (pay-as-you-go list prices; these are the only numbers to hardcode):

```python
DEFAULT_RATE_CARD = {
  "chat": {
    "default":            {"in_per_million": 15.0, "cached_in_per_million": 5.0, "out_per_million": 60.0},
    "sarvam-105b":        {"in_per_million": 15.0, "cached_in_per_million": 5.0, "out_per_million": 60.0},
    "sarvam-105b-conversations": {"in_per_million": 15.0, "cached_in_per_million": 5.0, "out_per_million": 60.0},
    "glm5.3":             {"in_per_million": 126.0, "cached_in_per_million": 20.0, "out_per_million": 390.0},
    "glm5.3-flash":       {"in_per_million": 13.5,  "cached_in_per_million": 2.5,  "out_per_million": 45.0},
    "gemma4":             {"in_per_million": 35.0,  "cached_in_per_million": 10.0, "out_per_million": 90.0},
    "deepseekv4.1-flash": {"in_per_million": 25.0,  "cached_in_per_million": 0.5,  "out_per_million": 105.0}
  },
  "stt":       {"default": {"per_minute": 0.49}},
  "tts":       {"default": {"per_1k_chars": 3.00}},
  "translate": {"default": {"per_1k_chars": None}},
  "transliterate": {"default": {"per_1k_chars": None}},
  "lid":       {"default": {"per_request": None}},
  "other":     {"default": {}}
}
```

`None` means *price not published*: `cost_for` returns `0.0` and `rate_known`
returns `False`. An unknown `api` also returns `0.0` / `False`. Model lookup is
exact-match then `"default"`. `cached_quantity` and `out_quantity` only apply to
`chat`: output tokens cost four times input tokens on the Sarvam models, so the
caller passes the generated count separately. With `out_quantity` given,
`quantity` is the *non-cached input* count, `cached_quantity` the cached input
count and `out_quantity` the generated count, each priced at its own rate. With
`out_quantity` omitted the legacy blended path applies (every non-cached token at
the input rate), which understates a chat call by up to 4x. A price that
overflows to infinity raises `ValueError`.
Negative or non-finite quantity → `ValueError`.

---

## 6. Ledger (`sarvam_meter/ledger.py`)

SQLite, WAL, `busy_timeout=5000`, thread-safe (per-thread connection via
`threading.local`; every method opens/reuses its thread's connection).

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

```python
def utc_now_iso() -> str
def local_day_start_iso(now: Optional[datetime] = None) -> str   # local tz (zoneinfo), returns UTC ISO
def local_month_start_iso(now: Optional[datetime] = None) -> str
class Ledger:
    def __init__(self, db_path: str)                 # creates parent dir, applies schema
    def record_event(self, *, source: str, api: str, model: Optional[str] = None,
                     quantity: Optional[float] = None, unit: Optional[str] = None,
                     cost: float = 0.0, status: Optional[int] = None,
                     estimated: bool = False, meta: Optional[dict] = None,
                     ts: Optional[str] = None) -> int
    def set_balance(self, starting: float, currency: str = "INR") -> None
    def get_balance(self) -> Optional[dict]           # {"starting","currency","set_at"}
    def spend_since(self, since_iso: str) -> float
    def calls_since(self, since_iso: str) -> int
    def spend_by_api_since(self, since_iso: str) -> list  # [{"api","spend","calls"}], spend desc
    def count_status_since(self, status: int, since_iso: str) -> int
    def last_status_ts(self, status: int) -> Optional[str]
    def set_kv(self, key: str, value: Optional[str]) -> None
    def get_kv(self, key: str) -> Optional[str]
    def purge_before(self, before_iso: str) -> int
    def close(self) -> None
```

Requirements: `record_event` with a `meta` dict serialises with `json.dumps`,
`sort_keys=True`, `ensure_ascii=False`. `spend_since` sums `cost`. Times compared as
strings (all stored as fixed-width UTC ISO8601 with `Z`) — that is a valid ordering.
`set_balance` upserts row id=1. `get_balance` returns `None` when unset.
`purge_before` returns the number of rows deleted.

---

## 7. Audio duration -- removed

`sarvam_meter/audio.py` (and `tests/test_audio.py`) are deleted. The module only
existed to derive STT duration from an uploaded audio file that the recording proxy
had intercepted. Nothing relays an upload any more, so a duration is now supplied by
the caller: `sarvam-meter record --api stt --quantity <seconds> --unit seconds`.

The section number is kept so cross-references elsewhere stay stable.

---

## 8. The recording proxy -- removed

`sarvam_meter/proxy.py` (and `tests/test_proxy.py`) are deleted, together with the
`proxy` config section, the `proxy` subcommand, `serve --with-proxy`,
`should_start_proxy`, and the status fields that only the proxy could populate
(`rate_limits`, `quota_exhausted`, `quota_exhausted_at`, `untracked_calls`).

**This is deliberate and permanent: the project relays no API traffic and listens
on no TCP port.** There is no upstream to forward to, no `max_body_bytes` to
enforce, no SSE relay to parse and no per-call quantity to measure. Anything that
used to be "measured" is now either scraped from the dashboard (§17) or recorded by
hand with `sarvam-meter record`.

The section number is kept so cross-references elsewhere stay stable.

---

## 9. CLIProxyAPI adapter (`sarvam_meter/cliproxy.py`)

Facts taken from the CLIProxyAPI source (`router-for-me/CLIProxyAPI`, v8):

- Auth headers: `Authorization: Bearer <management-key>` **and**
  `X-Management-Key: <management-key>` (send both).
- v8 base path `/v8/management`, v0 base path `/v0/management`.
- Account list: v8 `GET /v8/management/credentials`, v0 `GET /v0/management/auth-files`.
  Response: `{"files":[{"id","auth_index","name","type","provider","label","status",
  "status_message","disabled","unavailable","source","size",...}], ...}` (v8 may use
  `"accounts"` instead of `"files"` — accept either key, and a bare JSON array).
- Quota providers: v8 `GET /v8/management/credentials/quota/providers`,
  v0 `GET /v0/management/quota/providers` → `{"providers":[...]}`.
- Quota fetch: v8 `POST /v8/management/credentials/quota/fetch`,
  v0 `POST /v0/management/quota/fetch`, body `{"auth_index":"<idx>","provider":"<opt>"}`
  → normalized quota:
  ```json
  {"subscription":{"plan":"...","tierName":"...","tierId":"..."},
   "summary":[{"key":"...","label":"...","value":1.0,"unit":"...","format":"number|currency","currency":"USD"}],
   "serverTimeOffsetMs":0,
   "groups":[{"displayName":"...","buckets":[{"window":"5h","remainingFraction":0.62,
              "resetTime":"2026-11-01T10:00:00Z","description":"..."}]}]}
  ```
  Field names may arrive camelCase **or** snake_case — accept both
  (`remainingFraction`/`remaining_fraction`, `resetTime`/`reset_time`,
  `displayName`/`display_name`, `tierName`/`tier_name`, `serverTimeOffsetMs`/`server_time_offset_ms`).
- API-key usage (optional): v8 `GET /v8/management/observability/usage/api-keys`,
  v0 `GET /v0/management/api-key-usage` → `{provider: {"base_url|key": {"success":N,
  "failed":N,"recent_requests":[...]}}}`.
- **Never call `/usage-queue`** (v0) or `/observability/usage/queue` (v8): the handler
  *pops* records off a queue, so polling it destroys data. A unit test must assert the
  client never requests those paths.

```python
class CliproxyError(Exception): ...   # .status: Optional[int]

class CliproxyClient:
    def __init__(self, base_url: str, management_key: str, api_version: str = "v8",
                 timeout: float = 10.0, allow_remote: bool = False, transport=None)
    def health(self) -> dict
    def list_accounts(self) -> list           # normalized list, see below
    def quota_providers(self) -> list
    def fetch_quota(self, auth_index: str, provider: Optional[str] = None) -> Optional[dict]
    def api_key_usage(self) -> Optional[dict]
    def snapshot(self) -> dict                # {"configured","reachable","api_version",
                                              #  "accounts":[...],"errors":[...]}

def normalize_account(raw: dict) -> dict
def normalize_quota(raw: dict) -> dict
def worst_fraction(accounts: list) -> Optional[float]
```

`transport` is an injectable callable `transport(method, url, headers, body_bytes, timeout) -> (status, headers, body_bytes)`;
default implementation uses `urllib.request` with `ssl` verification on and follows no
redirects. Tests inject a fake transport — **no network in tests**.

Normalized account:
```json
{"auth_index":"...","id":"...","name":"...","provider":"claude","label":"...",
 "status":"active|disabled|unavailable|cooldown|error","plan":"Max",
 "buckets":[{"window":"5h","remaining_fraction":0.62,"reset_at":"...","description":"","unknown":false}],
 "summary":[{"key":"...","label":"...","value":1.0,"unit":"...","format":"currency","currency":"USD"}],
 "error":null}
```
Rules: `remaining_fraction` clamped to `0.0..1.0`; a missing/null/non-numeric fraction
sets `"unknown": true` and `remaining_fraction: null` (never fake it as 0 or 1);
`resetTime` normalised to UTC `...Z` when parseable, else passed through verbatim.
`status` derived from `status` string if present, else from `disabled`/`unavailable`
flags. Accounts that fail quota fetch keep `"buckets": []` and an `error` string; one
failing account must not fail the snapshot.

Error mapping in `snapshot()`: connection refused/timeout → `reachable:false` +
`errors:[{"source":"cliproxy","code":"unreachable","message":"..."}]`; `401`/`403` →
`code:"unauthorized"`; `404` → `code:"endpoint_missing"`; `501` from quota fetch →
per-account `error: "no quota provider"`; non-JSON body → `code:"bad_response"`.
Never raise out of `snapshot()`.

---

## 10. Status payload (`sarvam_meter/status.py`)

**This is the widget's contract. The JSON shape below is exact**; add fields only
inside objects explicitly marked extensible. Schema version is `2` (§17.5 lists the
schema-2 additions; this block is the whole payload, and the key sets are exact).

```python
def build_status(config: Config, ledger: Ledger, cliproxy_snapshot: Optional[dict] = None,
                 now: Optional[datetime] = None,
                 dashboard_snapshot: Optional[dict] = None) -> dict
```

```json
{
  "schema": 2,
  "generated_at": "2026-11-01T10:00:00+05:30",
  "stale": false,
  "errors": [{"source": "cliproxy", "code": "unreachable", "message": "..."}],
  "sarvam": {
    "configured": true,
    "currency": "INR",
    "balance": {"known": true, "remaining": 412.6, "starting": 500.0,
                "spent_total": 87.4, "set_at": "2026-10-20T09:00:00Z",
                "source": "dashboard"},
    "spend": {"today": 12.3, "month": 87.4, "source": "dashboard"},
    "calls": {"today": 42},
    "by_api": [{"api": "chat", "spend": 8.0, "calls": 20}],
    "dashboard": {
      "configured": true, "reachable": true, "as_of": "2026-11-01T09:58:00+05:30",
      "org_id": "019f37be-...", "currency": "INR",
      "window": {"start": "2026-10-20", "end": "2026-11-01"},
      "spend": {"window": 134.23, "today": 12.3},
      "balance": {"known": true, "amount": 412.6, "currency": "INR", "as_of": "...",
                  "state": "provisioned", "billing_model": "prepaid", "unit": "rupees"},
      "by_model": [{"model": "saaras:v4", "cost": 74.05}],
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

Rules: `balance.remaining` is only ever the figure the dashboard's balance endpoint
reported; when none was reported `balance.known` is `false`, `remaining`, `starting`
and `set_at` are `null`, and `spent_total` carries the ledger's all-time total (the
manual `sarvam-meter record` entries). `spend.today`/`spend.month` come from the
dashboard when it is reachable and from the ledger otherwise, and `spend.source` says
which. `calls.today` and `by_api` count ledger entries — the manual ones, because
nothing else writes to the ledger. `stale` is `true` when `cliproxy_snapshot` is
`None` while `cliproxy.enabled` is true. `errors` is always present (possibly `[]`),
and carries the cliproxy snapshot's `errors` when present. `worst_fraction` = min over
all known account bucket fractions, `null` if none known. All money rounded to 2 dp
for display; `quantity`/fractions 4 dp. Never raise: a ledger failure must be caught
and reported as `errors:[{"source":"ledger","code":"read_failed","message":...}]` with
zeroed numbers.

**There are no proxy-derived fields.** `rate_limits`, `quota_exhausted`,
`quota_exhausted_at` and `untracked_calls` were removed with the recording proxy
(§8): nothing in the project observes an upstream status or an unmeasured call any
more, so a permanently-empty stub would be a lie. `by_api` stays because the ledger
still fills it from manual entries.

---

## 11. Daemon (`sarvam_meter/daemon.py`)

`ThreadingHTTPServer`, bound to `daemon.host:daemon.port`.

| method | path | behaviour |
| --- | --- | --- |
| GET | `/v1/status` | `build_status(...)`, `200`, `application/json; charset=utf-8`, `Cache-Control: no-store` |
| GET | `/v1/health` | `{"ok":true,"version":"0.1.0","uptime_seconds":N}`, `200` |
| POST | `/v1/refresh` | forces a cliproxy re-poll, returns the new status |
| GET | `/v1/config` | sanitised config (keys redacted to `""`/`"***"`), read-only |
| anything else | — | `404` `{"error":{"message":"not found","code":"not_found"}}` |

```python
class Daemon:
    def __init__(self, config: Config, ledger: Optional[Ledger] = None, logger=None)
    def start(self) -> int          # binds, starts background refresh thread, returns port
    def serve_forever(self) -> None
    def shutdown(self) -> None
    def refresh_cliproxy(self) -> dict
def make_server(config, ledger=None) -> tuple            # (ThreadingHTTPServer, Daemon)
```

Requirements: background thread re-polls cliproxy every
`cliproxy.quota_fetch_interval_seconds` when enabled, guarded by a lock; `/v1/status`
serves the cached snapshot and never blocks on a slow cliproxy. Requests must not be
able to mutate anything (no PUT/PATCH/DELETE → `405`). `SO_REUSEADDR` on, `daemon_threads`
on. Log to stderr, one line per request, no secrets.

---

## 12. CLI (`sarvam_meter/cli.py`)

`argparse`, prog `sarvam-meter`, exit `0` ok / `1` runtime error / `2` config or usage error.

```
sarvam-meter serve [--config PATH] [--port N] [--print-status]   # starts the daemon, nothing else
sarvam-meter status [--config PATH] [--json]
sarvam-meter balance set <amount> [--config PATH]
sarvam-meter balance show [--config PATH]
sarvam-meter record --api <api> --quantity <n> --unit <unit> [--model M] [--config PATH]
sarvam-meter session set|show [--config PATH]
sarvam-meter dashboard [--config PATH] [--json]
sarvam-meter doctor [--config PATH]
sarvam-meter init [--config PATH]        # writes default config, prints the path
```

`status` human output: one line per source, e.g.
`Sarvam  INR 412.60 left  (spent 12.30 today, 87.40 this month, 42 calls)`
`Claude  max 5h window 62% left, resets 2026-11-01T10:00Z`
`record` prices the event with `cost_for` and stores it with `source="manual"`; it is
the only writer of the ledger, because the project relays no traffic of its own.
`doctor` checks: config loads, data dir writable, db opens, daemon socket free,
cliproxy reachable (if enabled), dashboard session, dashboard reachable (if
enabled), and prints one `OK`/`FAIL` line each; exit `1` if any FAIL.
There is no `proxy` subcommand and no `--with-proxy` flag: `serve` starts the daemon
and nothing else.

---

## 13. Plasmoid (QML, Plasma 6)

`metadata.json`:

```json
{
  "KPlugin": {
    "Id": "io.github.creepy_pasta101.sarvammeter",
    "Name": "Sarvam Meter",
    "Description": "Sarvam AI and CLIProxyAPI usage and quota",
    "Icon": "office-chart-bar",
    "Authors": [{"Name": "Suhan G Paradkar", "Email": "suhangp2002@gmail.com"}],
    "License": "MIT",
    "Version": "0.1.0",
    "Website": "https://github.com/creepy-pasta101/sarvam-meter-plasmoid"
  },
  "KPackageStructure": "Plasma/Applet",
  "X-Plasma-API-Minimum-Version": "6.0"
}
```

QML rules (Plasma **6.7 and 6.8 compatible**; 6.8 removes nothing we use, but do
**not** use anything already deprecated in 6.x):

- imports: `org.kde.plasma.plasmoid`, `org.kde.plasma.core`, `org.kde.plasma.components`
  (Plasma 6 components, i.e. `PlasmaComponents3` alias), `org.kde.kirigami`,
  `QtQuick`, `QtQuick.Layouts`, `QtQuick.Controls` only if needed.
- **Never** use `PlasmaCore.Units` (deprecated) — use `Kirigami.Units`.
- **Never** use `PlasmaComponents2`, `plasmoid.*` lowercase, `Item { }` as root.
- Root is `PlasmoidItem` with `compactRepresentation` and `fullRepresentation`.
- **No custom contextual actions in v1** (they are the most migration-fragile part).
- Data: `XMLHttpRequest` (GET) against `Plasmoid.configuration.daemonUrl` +
  `/v1/status`, driven by a `Timer` with `interval = Plasmoid.configuration.refreshSeconds * 1000`,
  `repeat: true`, `running: true`, plus one immediate `refresh()` on `Component.onCompleted`.
  On error keep the previous payload, set `offline = true`, and back off (double the
  interval up to 10x, reset on success).
- Never block: no synchronous XHR (`open(..., false)` is banned).
- Format money as `₹412.60` using the payload's `currency`; use a small
  `formatMoney(v, currency)` JS helper (₹ = `\u20b9`, $ = `$`).
- Compact representation: icon or short value — shows `₹412` (Sarvam) or the worst
  cliproxy percentage, whichever the config asks for; red text when below the warning
  threshold; a small `!` badge when `offline`.
- Full representation: a `ColumnLayout` with
  (a) Sarvam section: remaining balance or "spend only" when `balance.known` is false,
  today/month spend, calls, a `Bar` per top-3 `by_api` entries;
  (b) CLIProxyAPI section: one row per account with a `Bar` per bucket showing
  remaining fraction, `window` label and reset time; "unknown" text when
  `remaining_fraction` is null;
  (c) a footer line with `generated_at` and an offline/stale notice.
  Use a `ScrollView` so many accounts do not overflow.
- Tooltip: `Plasmoid.toolTipMainText` = "Sarvam Meter", `toolTipSubText` = the one-line
  summary (same string as the compact value plus spend today).
- `Bar.qml`: an inline `Rectangle`-based horizontal bar, `property real fraction`
  (0..1), `property color fillColor`, indeterminate look when `fraction < 0`,
  `Layout.fillWidth: true`, height `Math.max(4, Kirigami.Units.smallSpacing)`.
  No `org.kde.plasma.core` gauge, no QtQuick.Shapes.
- Config keys read in QML: `daemonUrl` (string), `refreshSeconds` (int),
  `compactMode` (string: `sarvam` | `cliproxy`), `warnBelowPercent` (int),
  `showApiBreakdown` (bool). All must exist in `main.xml` with defaults
  `http://127.0.0.1:8787`, `60`, `sarvam`, `20`, `true`.

`config/main.xml` is KConfigXT: `<group name="General">` with those five entries and
`<default>` values, plus a second group `Advanced` if needed. `config.qml` is a
`Kirigami.ScrollablePage`/`ColumnLayout` with `ConfigCategory` (KCM.SimpleKCM) wiring,
and `configGeneral.qml` holds the controls: `TextField` for the daemon URL with a
"Test" button that does an async XHR to `<url>/v1/health` and shows OK/FAIL,
`SpinBox` for refresh, `ComboBox` for compact mode, `Slider`/`SpinBox` for the warning
threshold, `Switch` for the API breakdown. Configuration pages must validate: reject a
URL that is not `http://`/`https://` and show an inline error rather than saving.

---

## 14. Packaging and CI

`pyproject.toml`: `[project] name="sarvam-meter"`, version 0.1.0, `requires-python=">=3.9"`,
`license={text="MIT"}`, `[project.scripts] sarvam-meter="sarvam_meter.cli:main"`,
`[tool.setuptools] packages=["sarvam_meter"]`. No runtime dependencies. No build backend
beyond `setuptools`; do not add `wheel`/`build` as requirements.

`packaging/plasma-applet-sarvam-meter.spec` (Fedora, `BuildArch: noarch`):

- `Name: plasma-applet-sarvam-meter`, `Version: 0.1.0`, `Release: 1%{?dist}`,
  `License: MIT`, `URL`, `Source0: %{name}-%{version}.tar.gz` (CI creates it with
  `git archive --prefix=%{name}-%{version}/`).
- `Requires: python3 >= 3.9`, `plasma-workspace >= 6.0`.
- `%build` does nothing (pure data + python source). `%install` installs:
  `%{python3_sitelib}/sarvam_meter/*` (use `%{python3_sitelib}` from `%{py3_shbang_opts}`?
  — no: use `install -d %{buildroot}%{python3_sitelib}/sarvam_meter` and copy), a
  `%{_bindir}/sarvam-meter` wrapper script (`#!/usr/bin/python3` + `from sarvam_meter.cli import main`),
  the plasmoid under `%{_datadir}/plasma/plasmoids/io.github.creepy_pasta101.sarvammeter/`,
  the user unit to `%{_userunitdir}/sarvam-meter.service`, example config to
  `%{_docdir}/%{name}/config.example.json`, docs to `%{_docdir}/%{name}/`.
- `%check` runs `python3 -m unittest discover -s tests -v`.
- `%post`/`%postun` use `%systemd_user_post sarvam-meter.service` /
  `%systemd_user_postun sarvam-meter.service` (guarded with `%{?systemd_requires}`).
- `%files` must list every installed path, with `%config(noreplace)` only for the
  example config (never for a user's real config — it lives in `$HOME`).

`.github/workflows/ci.yml` — on `push`/`pull_request`:

1. `test` job on `ubuntu-latest`: `actions/checkout@v4`, `actions/setup-python@v5`
   (`3.12` and `3.9` matrix), `python -m unittest discover -s tests -v`,
   `python -m compileall -q sarvam_meter tests`, `python -m json.tool` over
   `plasmoid/**/metadata.json` and `packaging/config.example.json`, and a
   `qmllint`-free structural check (assert each `.qml` file parses as balanced
   braces is not required — instead just assert the files exist and metadata has
   `KPackageStructure`). Do not install Qt in CI.
2. `rpm` job in `container: fedora:latest`: `dnf install -y rpm-build git tar`,
   build the tarball with `git archive`, `rpmbuild -ba` with `_topdir`,
   `actions/upload-artifact@v4` for the `.rpm` files. Fail loudly if no rpm is produced.
3. `plasmoid` job: zip the plasmoid dir into `sarvam-meter-<version>.plasmoid`,
   upload as an artifact.

`.github/workflows/release.yml` — on `push: tags: ['v*']`: run the same rpm build and
attach artifacts to a GitHub Release with `softprops/action-gh-release@v2`. Pin every
action to a major tag (`@v4`, `@v5`, `@v2`). No secrets required.

`packaging/sarvam-meter.service` — systemd **user** unit:
`ExecStart=/usr/bin/sarvam-meter serve`, `Restart=on-failure`,
`RestartSec=5`, `WantedBy=default.target`, `After=network-online.target` is wrong for a
user unit — use `After=default.target` or omit. Add `Environment=SARVAM_METER_CONFIG=%h/.config/sarvam-meter/config.json`.

---

## 15. Edge cases the whole project must handle

These are contractual. Whoever owns the relevant file must cover them, and the
integration-test unit must test as many as it can without network:

1. Daemon down / connection refused → widget offline state, previous data retained.
2. Daemon returns non-JSON or a 500 → widget keeps last good payload, shows stale.
3. Config missing → `init` writes defaults; daemon refuses to start with a clear
   message rather than crashing with a traceback.
4. Config unreadable JSON → exit code 2, message names the file and the reason.
5. DB path unwritable → daemon still serves `/v1/status` with a `ledger` error entry.
6. Port already in use → `OSError`/clear message, exit 1.
7. `starting_balance` unset → `balance.known == false`, spend still shown.
8. 402/429 from upstream → **retired** with the recording proxy (§8): the payload
   carries no `rate_limits`/`quota_exhausted*`; an exhausted wallet shows up as a
   reported balance of `0.00`.
9. Chat response without `usage` → **retired** with the proxy; the surviving analogue
   is an unpriced manual entry (`cost 0`, `estimated = 1`).
10. STT duration unparseable → **retired** with `audio.py`; `record` takes the
    duration from the user.
11. TTS text with non-ASCII/emoji → **retired** with the proxy; `record` takes the
    character count from the user.
12. `remainingFraction` null/missing → `unknown: true`, never rendered as 0%.
13. `resetTime` in the past → still shown, with the raw timestamp.
14. CLIProxyAPI 401/404/501/HTML-body/empty-providers → distinct, non-fatal errors.
15. Never poll CLIProxyAPI's destructive usage-queue endpoint (assert in tests).
16. Concurrent writers on one database → WAL, no `database is locked` under load
    (a test with 20 parallel `record_event` calls must pass).
17. Secrets never appear in logs, `meta`, `/v1/config`, or `status`.
18. Non-loopback cliproxy `base_url` refused unless `allow_remote`.
19. Widget with two instances → both poll; daemon is threaded and idempotent.
20. Large upload streamed, `max_body_bytes` enforced → **retired** with the proxy:
    no request body passes through this project at all.
21. Timezone: "today" is the local calendar day; "month" is the local calendar month.
22. Plasma 6.8: no deprecated QML APIs, no `PlasmaCore.Units`, no custom
    contextual actions.

---

## 16. What your summary must contain

- The exact list of files you wrote (paths relative to the repo root).
- Any deviation from this document, and why (one line each).
- What you verified yourself (e.g. "ran `python3 -m unittest tests.test_rates -v`, 14 pass").
- Anything a reviewer must know (interface assumption, tricky part, remaining risk).
- If you could not finish: exactly which files are missing and what is unimplemented.

---

# 17. The dashboard source (added after 0.1.0, schema 2)

Sarvam's dashboard exposes the authoritative numbers. The captured responses in
`tests/fixtures/` are the contract; everything below is derived from them and must
not be guessed beyond them.

## 17.1 Confirmed endpoints

Base: `https://indus.sarvam.ai` (the dashboard; `dashboard.sarvam.ai` 308-redirects here).

```
GET /api/dhan/org/usage/summary?start=YYYY-MM-DD&end=YYYY-MM-DD&group_by=model&product=sarvam-api-platform
GET /api/dhan/org/usage/timeseries?start=YYYY-MM-DD&end=YYYY-MM-DD&group_by=product&product=sarvam-api-platform&granularity=auto
GET /api/dhan/org/balance
```

Balance payload (fixture `dhan_org_balance.json`) — the authoritative credits left:

```json
{"org_id": "019f37be-...", "balance": "80.03533", "real_time_credit_balance": "80.03533",
 "balance_updated_at": "2026-11-01T05:55:39.713767Z", "state": "provisioned",
 "billing_model": "prepaid", "currency": "INR", "unit": "rupees"}
```

Here `currency` is already uppercase and the numbers are still strings.

Both need the signed-in session. Unauthenticated they answer `401 application/json`;
an unknown `/api/...` path answers `404` with the SPA shell instead.

Summary payload (fixture `dhan_usage_summary_by_model.json`):

```json
{
  "org_id": "019f37be-...", "range": {"start": "2026-10-02T18:30:00Z", "end": "2026-11-01T18:29:59Z"},
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
    {"model": "glm5.3-flash", "subtotal": "53.388129", "total_cost": "53.388129", "currency": "inr", "usage": [...]},
    {"model": "saaras:v3-realtime", "total_cost": "0.1551666667", ...},
    {"model": "saaras:v4", "total_cost": "74.0498055238", ...},
    {"model": "sarvam-105b", "total_cost": "6.63813", ...}
  ]
}
```

Timeseries payload (fixture `dhan_usage_timeseries_by_product.json`) adds
`granularity`, `available_granularities`, `groups[].product` and
`points: [{"timestamp","subtotal","total_discount","total_cost","event_count"}]`
(28 hourly points in the fixture), and has no per-model groups.

**Every number arrives as a JSON string.** They must be parsed to float; a
non-numeric string is an error, never a silent 0. `currency` arrives lowercase
(`"inr"`) and is normalised to uppercase (`"INR"`).

## 17.2 Session file

`~/.config/sarvam-meter/session.json`, mode `0600`, written by
`sarvam-meter session set`:

```json
{"Cookie": "...", "Authorization": "Bearer ..."}
```

Only these two keys are accepted (case-insensitive match). Values are never
logged, never returned by `/v1/config` (rendered `"***"`), never written into the
status payload or an error message. A missing or unreadable file is
`DashboardError(code="no_session")`.

## 17.3 `sarvam_meter/dashboard.py`

```python
class DashboardError(Exception): ...        # .code: str, .status: Optional[int]
def load_session(path: str) -> dict          # {lower-name: value}; raises DashboardError
class DashboardClient:
    def __init__(self, base_url: str, session: dict, *, timeout: float = 15.0,
                 product: str = "sarvam-api-platform", transport=None)
    def usage_summary(self, start_date: str, end_date: str, group_by: str = "model") -> dict
    def usage_timeseries(self, start_date: str, end_date: str, group_by: str = "product",
                         granularity: str = "auto") -> dict
    def balance(self) -> dict
    def snapshot(self, *, since_date: str, today: str) -> dict   # NEVER raises
def parse_summary(payload: bytes | dict) -> dict
def parse_timeseries(payload: bytes | dict) -> dict
def parse_balance(payload: bytes | dict) -> dict
```

`transport(method, url, headers, timeout) -> (status, headers, body_bytes)`,
injectable, default urllib with `ProxyHandler({})`, certificate verification on,
no redirects (same rules as `cliproxy.py`). Dates are `YYYY-MM-DD`.

`parse_summary` returns:

```python
{"org_id": str, "currency": "INR", "group_by": "model",
 "range": {"start": str, "end": str},
 "total_cost": float, "subtotal": float,
 "usage": [{"unit": str, "quantity": float,
            "breakdown": [{"unit": str, "quantity": float}]}],
 "by_model": [{"model": str, "cost": float, "usage": [ ... ]}]}
```

`parse_timeseries` returns the same minus `by_model`, plus
`"points": [{"timestamp": str, "cost": float, "event_count": int}]` and
`"granularity": str`.

`snapshot(since_date, today)` returns, and never raises:

```python
{"configured": bool, "reachable": bool, "as_of": str, "org_id": str|None,
 "currency": "INR", "window": {"start": str, "end": str},
 "spend": {"window": float, "today": float, "month": float},
 "balance": {"known": bool, "amount": float|None, "currency": "INR",
             "as_of": str|None, "state": str, "billing_model": str, "unit": str},
 "by_model": [{"model": str, "cost": float}],
 "usage": [ ... ],
 "error": None | {"source": "dashboard", "code": str, "message": str}}
```

`parse_balance` returns `{"org_id","balance","currency","as_of","state","billing_model","unit"}`
(`as_of` from `balance_updated_at`; `real_time_credit_balance` wins over `balance`
when both are present).

Error codes: `no_session`, `unauthorized` (401/403), `bad_response` (non-JSON or
unparsable), `unreachable` (transport failure), `http_error`. Four calls per
snapshot, in this order: the **balance** (authoritative, needs no arithmetic, so
it is fetched first and a later failure must not cost it), the window summary,
today's summary, and the month-to-date summary (first of the current month ..
today, because the ledger's own month total is not comparable once the dashboard
is the source). A failure of either short call leaves that field at `0.0` and
keeps the window result. The **first** error encountered is the one recorded, and
`reachable` stays `false` unless the window call produced numbers — so a reported
balance can be known while `reachable` is `false`. Dates are validated before any
request is made (`bad_request`, no transport call).

## 17.4 Config section

```json
"dashboard": {
  "enabled": false,
  "base_url": "https://indus.sarvam.ai",
  "session_file": "~/.config/sarvam-meter/session.json",
  "poll_seconds": 300,
  "product": "sarvam-api-platform"
}
```

`enabled` defaults to `true`: scraping is the default source, because it is the
only one that reports the real credit balance. `poll_seconds` is bounded to
`120..21600` (every 2 minutes to every 6 hours).

Validation: `base_url` must be `https://` with a host and **no** userinfo (this is
a remote host by design, so the loopback guard does **not** apply). A plain
`http://` URL is accepted for a **loopback** host only, so the source can be
exercised against a local fixture server; anything reachable remotely must be
`https://`. `poll_seconds` >= 60 (bot-management friendly); `product` a non-empty
string; `session_file` `~`-expanded. `packaging/config.example.json` must gain
this section.

## 17.5 Status payload — schema 2

`schema` becomes `2`. `sarvam` gains:

```json
"spend": {"today": 12.3, "month": 87.4, "source": "dashboard"},
"balance": {"known": true, "remaining": 365.77, "starting": 500.0,
            "spent_total": 134.23, "set_at": "...", "source": "dashboard"},
"dashboard": {
  "configured": true, "reachable": true, "as_of": "...",
  "org_id": "...", "currency": "INR",
  "window": {"start": "2026-10-20", "end": "2026-11-01"},
  "spend": {"window": 134.23, "today": 12.3},
  "by_model": [{"model": "saaras:v4", "cost": 74.05}],
  "error": null
}
```

Rules:

- `spend.source` is `"dashboard"` when the dashboard snapshot is reachable, else
  `"ledger"`.
- `balance.source` is `"dashboard"` when the balance endpoint reported a number,
  otherwise it describes where the *spend* came from (`"dashboard"` or `"ledger"`).
  The value `"dashboard-derived"` no longer exists.
- `balance.remaining` is **only ever** the figure Sarvam reports. It is never
  derived from local entries: a local estimate silently disagreed with the real
  wallet (top-ups, other machines, calls made elsewhere), and a wrong "left" number
  is worse than none. `sarvam-meter balance set` still records a starting balance and
  `sarvam-meter record` still writes ledger entries, but neither affects `remaining`.
- Without a reported balance: `known: false`, `remaining`/`starting`/`set_at` are
  `null`, and `spent_total` carries the ledger's all-time spend.
- `spend.today` and `spend.month` come from the dashboard when reachable, else
  from the ledger.
- `sarvam.dashboard` is always present; `configured: false` when disabled.
- `build_status` gains the keyword argument `dashboard_snapshot=None`.

## 17.6a Writing the backend settings from the widget

`POST /v1/config` lets the plasmoid's settings page change the daemon's own
configuration without hand-editing a file. The body is a JSON object holding any
subset of:

```
dashboard.{enabled, base_url, poll_seconds, session_file}
cliproxy.{enabled, base_url, api_version}
daemon.{refresh_seconds}
```

Anything else — an unknown section, an unknown key, `cliproxy.management_key`,
`sarvam.starting_balance` — is rejected whole with `400` and
`{"error": {"message": "<section>.<key> is not writable", "code": "invalid_config"}}`.
Values are validated by the same rules `load_config` applies; a rejected request
leaves the file byte-identical. On success the file is written atomically at
`0600`, the daemon reloads its in-memory config, drops the cached dashboard
snapshot, and returns the same sanitised document `GET /v1/config` returns.
`PUT`/`PATCH`/`DELETE` stay `405`.

The settings page keeps two stores apart: the five widget settings stay in
KConfigXT (`cfg_*`), the backend settings go only through this endpoint. Applying
the dialog never writes daemon settings, and the Save button never writes widget
settings.

## 17.6 CLI and daemon

- `sarvam-meter session set (--cookie VALUE | --cookie-file PATH) [--authorization VALUE]`
  writes the session file `0600`; `session show` prints the path, the mode and
  which headers are present — never a value.
- `sarvam-meter dashboard [--json]` prints one snapshot.
- `doctor` gains `dashboard session` (file exists, mode `0600`, only allowed keys)
  and, when enabled, `dashboard reachable`.
- `Daemon` keeps a dashboard snapshot with a TTL of `dashboard.poll_seconds`,
  refreshed by the same background thread pattern as cliproxy; `/v1/status` never
  blocks on it and `/v1/refresh` refreshes both sources.

## 17.7 Widget

- When `spend.source == "dashboard"`, the full representation labels the spend line
  `spend today` and shows a small "dashboard" hint; when `"ledger"` it shows the
  existing `spend today` with a "local meter" hint.
- When `sarvam.dashboard.reachable`, the full representation adds up to three
  `by_model` rows (model name and cost) under the Sarvam section, using the same
  `Bar` component scaled against the largest cost.
- When `sarvam.dashboard.error` is present, one muted line: `dashboard: <code>`.
- Compact representation is unchanged.

## 17.8 Tests

`tests/test_dashboard.py` must parse the two committed fixtures and assert the
exact figures: `total_cost` 134.2312311905, four models at 53.388129 /
0.1551666667 / 74.0498055238 / 6.63813, `usage` token 13046343 with breakdown
11296128 / 1513846 / 236369, seconds 9086.323125, `currency` normalised from
`"inr"` to `"INR"`, and 28 timeseries points totalling 134.2312311907. Also:
string-number parsing, a non-numeric string is an error, 401 → `unauthorized`,
non-JSON → `bad_response`, transport failure → `unreachable`, missing session →
`no_session`, and that no session value ever appears in a snapshot or an error
message.
