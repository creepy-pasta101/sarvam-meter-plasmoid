#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Discover which endpoints the Sarvam dashboard uses for usage and credits.

The dashboard is a signed-in single-page app, so there is no documented API to
call: the numbers live behind a session cookie, and the endpoints are whatever
the front-end bundle happens to request.  This script does not guess a schema.
It uses *your* signed-in session to

  1. fetch the app shell and its JavaScript bundles,
  2. pull every ``/api/...`` string out of those bundles,
  3. GET each candidate with your cookie, and
  4. write one JSON report with the status, content type and body of each.

Run it on your own machine, signed in, then hand the report back so the field
mapping can be written against the real shapes.

    python3 scripts/probe-dashboard.py --cookie-file ~/.cache/sarvam-cookie.txt
    python3 scripts/probe-dashboard.py --cookie-file cookie.txt --redact --out probe.json

The cookie is sent only to the host you pass in ``--base`` and is never printed,
written to the report, or logged.  Nothing is posted or mutated: every request
is a GET, paced, and capped.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlsplit

DEFAULT_BASE = "https://indus.sarvam.ai"

#: Pages worth fetching for their bundles (and, for an SSR app, their payload).
CANDIDATE_PAGES = (
    "/",
    "/usage",
    "/settings/usage",
    "/settings/billing",
    "/billing",
    "/organisations",
)

#: Endpoints to try even when the bundles mention none of them.
CANDIDATE_ENDPOINTS = (
    "/api/usage",
    "/api/v1/usage",
    "/api/credits",
    "/api/billing",
    "/api/billing/credits",
    "/api/me",
    "/api/account",
    "/api/organisations",
    "/api/organizations",
    "/api/workspaces",
    "/api/subscription",
)

#: Endpoint-looking strings inside JavaScript bundles.
API_PATTERN = re.compile(r"""["'`](/api/[A-Za-z0-9/_.\-{}$:]*)["'`]""")
ABS_PATTERN = re.compile(
    r"""["'`](https://[A-Za-z0-9.\-]*sarvam[A-Za-z0-9.\-]*/api/[A-Za-z0-9/_.\-{}$:]*)["'`]"""
)
SCRIPT_PATTERN = re.compile(r"""<script[^>]+src=["']([^"']+)["']""", re.I)
#: Next.js also references its chunks from the inline RSC payload, not only from
#: <script src> tags, so the whole page text is scanned for chunk URLs.
CHUNK_PATTERN = re.compile(r"""(/_next/static/[A-Za-z0-9/_.~\-]+\.js)""")

#: Markers of a Cloudflare bot challenge. The dashboard serves these with HTTP
#: 200 as well as 403, so the status code alone is not enough to detect one.
CHALLENGE_MARKERS = (
    "attention required",
    "cf-error-details",
    "you have been blocked",
    "__cf_chl",
    "just a moment",
)

MAX_BUNDLES = 12
#: How much of a response is kept for parsing (a Next.js shell is ~16 KB).
MAX_FETCH = 256 * 1024
#: How much of a response is copied into the report.
MAX_BODY = 4096
MAX_ENDPOINTS = 40
PACING_SECONDS = 0.4


def _redact_number(value: float) -> str:
    return "<number>"


def is_challenge(result: dict) -> bool:
    """True when a response is a Cloudflare interstitial rather than the app."""
    body = (result.get("body") or "").lower()
    return any(marker in body for marker in CHALLENGE_MARKERS)


def redact_report(report: dict) -> dict:
    """Replace every numeric leaf with a placeholder, for sharing a report."""
    def walk(node):
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, bool) or node is None:
            return node
        if isinstance(node, (int, float)):
            return _redact_number(float(node))
        if isinstance(node, str):
            return re.sub(r"\b\d[\d,.]*\b", "<n>", node)
        return node
    return walk(report)


class Fetcher:
    """A tiny GET-only client that carries the session cookie."""

    def __init__(self, cookie: str, timeout: float):
        self._cookie = cookie
        self._timeout = timeout
        self.requests = 0

    def get(self, url: str):
        self.requests += 1
        request = urllib.request.Request(url, method="GET")
        if self._cookie:
            request.add_header("Cookie", self._cookie)
        request.add_header(
            "User-Agent",
            "Mozilla/5.0 (X11; Linux x86_64) sarvam-meter-probe/0.1",
        )
        request.add_header("Accept", "application/json, text/html;q=0.9, */*;q=0.8")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read(MAX_FETCH)
                text = raw.decode("utf-8", "replace")
                return {
                    "status": int(getattr(response, "status", 200)),
                    "final_url": response.geturl(),
                    "content_type": response.headers.get("Content-Type", ""),
                    "body": text[:MAX_BODY],
                    "text": text,
                    "body_bytes": len(raw),
                }
        except urllib.error.HTTPError as exc:
            payload = b""
            try:
                payload = exc.read(MAX_FETCH)
            except Exception:  # noqa: BLE001 - defensive
                pass
            text = payload.decode("utf-8", "replace")
            return {
                "status": int(exc.code),
                "final_url": url,
                "content_type": exc.headers.get("Content-Type", "") if exc.headers else "",
                "body": text[:MAX_BODY],
                "text": text,
                "body_bytes": len(payload),
            }
        except Exception as exc:  # noqa: BLE001 - transport failures are data
            message = "%s: %s" % (type(exc).__name__, exc)
            return {
                "status": 0,
                "final_url": url,
                "content_type": "",
                "body": message,
                "text": message,
                "body_bytes": 0,
            }


def _same_origin(base: str, url: str) -> bool:
    try:
        a, b = urlsplit(base), urlsplit(url)
    except ValueError:
        return False
    return (a.scheme, a.netloc) == (b.scheme, b.netloc)


def collect_endpoints(base: str, bundles: list) -> list:
    """Endpoint paths found in the bundles, in first-seen order."""
    seen, out = set(), []
    for bundle in bundles:
        for pattern in (API_PATTERN, ABS_PATTERN):
            for match in pattern.finditer(bundle or ""):
                path = match.group(1)
                if path in seen:
                    continue
                seen.add(path)
                out.append(path)
                if len(out) >= MAX_ENDPOINTS:
                    return out
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="probe-dashboard",
        description="Discover the Sarvam dashboard's usage/credit endpoints with your session.",
    )
    parser.add_argument("--base", default=DEFAULT_BASE,
                        help="dashboard origin (default: %(default)s)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--cookie", help="raw Cookie header value (kept in memory only)")
    group.add_argument("--cookie-file", help="file holding the raw Cookie header value")
    parser.add_argument("--out", default="dashboard-probe.json",
                        help="report path (default: %(default)s)")
    parser.add_argument("--redact", action="store_true",
                        help="replace numbers in the report before writing it")
    parser.add_argument("--timeout", type=float, default=20.0,
                        help="per-request timeout in seconds (default: %(default)s)")
    parser.add_argument("--json", action="store_true", help="print the report to stdout")
    args = parser.parse_args(argv)

    cookie = (args.cookie or "").strip()
    if args.cookie_file:
        try:
            with open(args.cookie_file, encoding="utf-8") as handle:
                cookie = handle.read().strip()
        except OSError as exc:
            print("probe-dashboard: cannot read the cookie file: %s" % exc, file=sys.stderr)
            return 2
    if not cookie:
        print(
            "probe-dashboard: no cookie given. Sign in to the dashboard in a browser, "
            "copy the request's Cookie header, and pass it with --cookie-file.",
            file=sys.stderr,
        )
        return 2
    if "\n" in cookie:
        print("probe-dashboard: the cookie must be a single line", file=sys.stderr)
        return 2

    base = args.base.rstrip("/")
    fetcher = Fetcher(cookie, args.timeout)
    report = {
        "tool": "probe-dashboard",
        "version": 1,
        "base": base,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "pages": {},
        "bundles": [],
        "endpoints": {},
        "notes": [],
    }

    print("probe-dashboard: fetching %d candidate pages from %s" % (len(CANDIDATE_PAGES), base),
          file=sys.stderr)
    bundle_urls = []
    for path in CANDIDATE_PAGES:
        result = fetcher.get(urljoin(base + "/", path.lstrip("/")))
        report["pages"][path] = result
        print("  %-20s -> %s %s" % (path, result["status"], result["content_type"][:40]),
              file=sys.stderr)
        sources = SCRIPT_PATTERN.findall(result["text"]) + CHUNK_PATTERN.findall(result["text"])
        for src in sources:
            absolute = urljoin(result["final_url"] or base + "/", src)
            if _same_origin(base, absolute) and absolute not in bundle_urls:
                bundle_urls.append(absolute)
        time.sleep(PACING_SECONDS)

    challenged = [path for path, page in report["pages"].items() if is_challenge(page)]
    if challenged:
        report["notes"].append(
            "Cloudflare served a bot challenge for: %s. A plain HTTP client cannot "
            "pass it; run this from a normal desktop network (a datacenter IP is "
            "usually blocked) or use a real browser session." % ", ".join(challenged)
        )
        for entry in report["pages"].values():
            entry.pop("text", None)
        try:
            with open(args.out, "w", encoding="utf-8") as handle:
                json.dump(report, handle, indent=2, sort_keys=True)
                handle.write("\n")
        except OSError:
            pass
        print(
            "probe-dashboard: Cloudflare is blocking this client (%s), so there is "
            "nothing to discover from here. The evidence was written to %s. Re-run "
            "this from your desktop, where you are signed in in a normal browser."
            % (", ".join(challenged), args.out),
            file=sys.stderr,
        )
        return 3

    print("probe-dashboard: reading up to %d JavaScript bundles" % MAX_BUNDLES, file=sys.stderr)
    bundle_bodies = []
    for url in bundle_urls[:MAX_BUNDLES]:
        result = fetcher.get(url)
        report["bundles"].append({"url": url, "status": result["status"],
                                  "bytes": result["body_bytes"]})
        bundle_bodies.append(result["text"])
        time.sleep(PACING_SECONDS)

    # Discovery runs over both the page payloads and the bundle bodies.
    discovered = collect_endpoints(
        base, [r["text"] for r in report["pages"].values()] + bundle_bodies
    )
    candidates = list(discovered)
    for path in CANDIDATE_ENDPOINTS:
        if path not in candidates:
            candidates.append(path)

    print("probe-dashboard: trying %d candidate endpoints" % len(candidates), file=sys.stderr)
    for path in candidates[:MAX_ENDPOINTS]:
        url = path if path.startswith("http") else urljoin(base + "/", path.lstrip("/"))
        result = fetcher.get(url)
        report["endpoints"][path] = result
        print("  %-40s -> %s %s" % (path, result["status"], result["content_type"][:30]),
              file=sys.stderr)
        time.sleep(PACING_SECONDS)

    # The parsing copies are large and are never part of the report.
    for bucket in ("pages", "endpoints"):
        for entry in report[bucket].values():
            entry.pop("text", None)

    if args.redact:
        report = redact_report(report)

    try:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except OSError as exc:
        print("probe-dashboard: cannot write %s: %s" % (args.out, exc), file=sys.stderr)
        return 1

    interesting = [
        path for path, result in report["endpoints"].items()
        if result["status"] == 200 and "json" in result["content_type"].lower()
    ]
    print("", file=sys.stderr)
    print("probe-dashboard: %d requests, report written to %s" % (fetcher.requests, args.out),
          file=sys.stderr)
    if interesting:
        print("probe-dashboard: JSON endpoints that answered 200:", file=sys.stderr)
        for path in interesting:
            print("  %s" % path, file=sys.stderr)
    else:
        print(
            "probe-dashboard: no candidate endpoint returned JSON. Open the dashboard's "
            "Network tab, click through Settings -> Usage and Settings -> Billing, and "
            "copy the request URLs you see there; they can be added to the report by "
            "hand or to CANDIDATE_ENDPOINTS in this script.",
            file=sys.stderr,
        )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
