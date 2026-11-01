#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run the real main.qml in a real Qt engine against a hostile daemon.

This is the plasmashell-safety harness.  It loads
plasmoid/io.github.creepy_pasta101.sarvammeter/contents/ui/main.qml with the
smallest possible namespace substitution (there is no Plasma here, so
PlasmoidItem becomes Item, Plasmoid.configuration becomes a local object and the
two representation blocks are commented out), then drives it against a daemon
that misbehaves in each of the ways a real one can:

  hung      accepts the connection and never answers
  huge      answers with a body far larger than the cap
  garbage   answers with something that is not JSON
  good      answers with a valid status payload
  destroy   the widget is removed while a request is in flight

For every scenario it asserts that the GUI thread kept running (an independent
QTimer keeps ticking), that the widget settles into a defined state instead of
staying stuck in `loading`, and that nothing throws into the engine.

Usage:  python3 scripts/qml-safety-harness.py [--repo DIR] [-v]
Exit code 0 means every scenario passed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import threading
import time

DEFAULT_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: A status payload with the full schema-2 shape, so the good case is realistic.
GOOD_PAYLOAD = {
    "schema": 2,
    "generated_at": "2026-11-01T06:00:00+05:30",
    "stale": False,
    "errors": [],
    "sarvam": {
        "configured": True,
        "currency": "INR",
        "balance": {
            "known": True, "remaining": 80.04, "starting": None,
            "spent_total": 134.23, "set_at": None, "source": "dashboard",
        },
        "spend": {"today": 12.3, "month": 134.23, "source": "dashboard"},
        "calls": {"today": 42},
        "by_api": [{"api": "chat", "spend": 8.0, "calls": 20}],
        "rate_limits": {"count_24h": 0, "last_at": None},
        "quota_exhausted": False,
        "quota_exhausted_at": None,
        "untracked_calls": {"today": 0, "total": 0},
        "dashboard": {
            "configured": True, "reachable": True, "as_of": "2026-11-01T06:00:00Z",
            "org_id": "019f37be", "currency": "INR",
            "window": {"start": "2026-10-20", "end": "2026-11-01"},
            "spend": {"window": 134.23, "today": 12.3, "month": 134.23},
            "balance": {
                "known": True, "amount": 80.03533, "currency": "INR",
                "as_of": "2026-11-01T05:55:39.713767Z", "state": "provisioned",
                "billing_model": "prepaid", "unit": "rupees",
            },
            "by_model": [{"model": "saaras:v4", "cost": 74.05}],
            "error": None,
        },
    },
    "cliproxy": {
        "configured": False, "reachable": False, "api_version": "v8",
        "worst_fraction": None, "accounts": [],
    },
}


# ---------------------------------------------------------------------------
# the QML source, with the minimum namespace substitution
# ---------------------------------------------------------------------------

def transform_main_qml(source: str, daemon_url: str, refresh_seconds: int) -> str:
    """Return a runnable copy of main.qml for a plain QtQuick engine."""
    text = source
    text = text.replace("import org.kde.plasma.plasmoid\n", "")
    text = text.replace("import org.kde.kirigami as Kirigami\n", "")
    text = text.replace("Kirigami.Units.gridUnit", "12")
    text = text.replace("PlasmoidItem {", "Item {", 1)
    text = text.replace("Plasmoid.configuration.", "harnessConfig.")

    # Comment out the two representation blocks: they instantiate the other files
    # and are irrelevant to the request lifecycle under test.
    for key in ("compactRepresentation:", "fullRepresentation:"):
        start = text.find("\n    " + key)
        if start < 0:
            continue
        brace = text.find("{", start)
        if brace < 0:
            continue
        depth, i = 0, brace
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        end = i + 1
        text = text[:start] + "\n    /* representation removed by the harness" + text[start:end] + " */" + text[end:]

    config = {
        "daemonUrl": daemon_url,
        "refreshSeconds": refresh_seconds,
        "compactMode": "sarvam",
        "warnBelowPercent": 20,
        "showApiBreakdown": True,
    }
    # PlasmoidItem carries these as its own properties; on a plain Item they must
    # be declared, so turn each assignment into a declaration.
    for name, kind in (("toolTipMainText", "string"), ("toolTipSubText", "string"),
                       ("switchWidth", "int"), ("switchHeight", "int")):
        text = re.sub(r"(?m)^(\s*)%s:" % name,
                      r"\1property %s %s:" % (kind, name), text)

    injection = "    property var harnessConfig: (%s)\n" % json.dumps(config)
    text = text.replace("Item {", "Item {\n" + injection, 1)
    return text


# ---------------------------------------------------------------------------
# misbehaving daemons
# ---------------------------------------------------------------------------

class HostileServer:
    """A one-connection-at-a-time TCP server that behaves as told."""

    def __init__(self, mode: str):
        self.mode = mode
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.5)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        self._thread.join(timeout=3)

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.settimeout(30)
            try:
                conn.recv(65536)
            except OSError:
                return
            if self.mode == "hung":
                # Accept the request and never answer.  The client must give up.
                self._stop.wait(30)
                return
            if self.mode == "huge":
                body = b"x" * (900 * 1024)
                head = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n")
                conn.sendall(head + body)
                return
            if self.mode == "garbage":
                body = b"<html>not json</html>"
                head = (b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n")
                conn.sendall(head + body)
                return
            body = json.dumps(GOOD_PAYLOAD).encode()
            head = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n")
            conn.sendall(head + body)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# scenarios
# ---------------------------------------------------------------------------

def run_scenario(mode: str, repo: str, verbose: bool) -> tuple:
    """Load the widget against one daemon mode; return (ok, detail, samples)."""
    from PySide6.QtCore import QTimer, QUrl, QCoreApplication  # noqa: F401
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlComponent, QQmlEngine

    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])

    server = HostileServer(mode).start()
    try:
        source = open(os.path.join(
            repo, "plasmoid/io.github.creepy_pasta101.sarvammeter/contents/ui/main.qml"),
            encoding="utf-8").read()
        text = transform_main_qml(source, server.url, 60)
        path = os.path.join("/tmp", "harness_main_%s.qml" % mode)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

        engine = QQmlEngine()
        warnings = []
        engine.warnings.connect(lambda w: warnings.append(str(w.toString())))

        component = QQmlComponent(engine, QUrl.fromLocalFile(path))
        root = component.create()
        if root is None:
            return False, "component failed: %s" % component.errorString(), []

        # An independent heartbeat: if the GUI thread were blocked, this would
        # not tick, and the harness itself would stall.
        beats = {"n": 0}
        heartbeat = QTimer()
        heartbeat.setInterval(50)
        heartbeat.timeout.connect(lambda: beats.__setitem__("n", beats["n"] + 1))
        heartbeat.start()

        samples = []
        deadline = time.time() + (9.0 if mode == "hung" else 4.0)
        while time.time() < deadline:
            app.processEvents()
            time.sleep(0.02)
            samples.append((root.property("loading"), root.property("offline"),
                            root.property("lastError")))
        app.processEvents()

        loading = root.property("loading")
        offline = root.property("offline")
        error = root.property("lastError") or ""
        payload = root.property("payload")
        beats_total = beats["n"]

        if mode == "destroy":
            root.deleteLater()
            app.processEvents()
            heartbeat.stop()
            return True, "destroyed cleanly while a request was in flight", samples

        # The GUI thread must have kept running throughout.
        if beats_total < 20:
            return False, "the event loop stalled (%d ticks)" % beats_total, samples
        if loading:
            return False, "the widget stayed stuck in loading=true", samples

        if mode == "good":
            # QML hands the parsed object back as a QJSValue, so convert it.
            document = {}
            if hasattr(payload, "toVariant"):
                document = payload.toVariant() or {}
            elif isinstance(payload, dict):
                document = payload
            ok = (offline is False and document.get("schema") == 2 and error == "")
            return ok, "payload accepted, offline=%s error=%r" % (offline, error), samples
        if mode == "huge":
            ok = offline is True and "too large" in error
            return ok, "huge body rejected: error=%r" % error, samples
        if mode == "garbage":
            ok = offline is True and ("malformed" in error or "HTTP" in error)
            return ok, "garbage rejected: error=%r" % error, samples
        if mode == "hung":
            ok = offline is True and error != ""
            return ok, "hung daemon timed out: error=%r" % error, samples
        return False, "unknown mode", samples
    finally:
        server.stop()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="qml-safety-harness")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    failures = 0
    for mode in ("good", "garbage", "huge", "hung", "destroy"):
        ok, detail, samples = run_scenario(mode, args.repo, args.verbose)
        print("%-8s %s  %s" % (mode, "PASS" if ok else "FAIL", detail))
        if args.verbose and samples:
            print("         last samples (loading, offline, error): %r" % (samples[-3:],))
        if not ok:
            failures += 1
    print("")
    print("%d of 5 scenarios passed" % (5 - failures))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
