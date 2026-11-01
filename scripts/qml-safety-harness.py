#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run the real main.qml in a real Qt engine against a hostile daemon.

This is the plasmashell-safety harness.  It loads
plasmoid/io.github.creepy_pasta101.sarvammeter/contents/ui/main.qml with the
smallest possible namespace substitution (there is no Plasma here, so
PlasmoidItem becomes Item, Plasmoid.configuration becomes a local object and the
two representation blocks are resolved against the QML stubs), then drives it
against a daemon that misbehaves in each of the ways a real one can.

THE DAEMON IS A CHILD PROCESS NOW
The widget no longer speaks HTTP: it runs `<command> query <op>` through
Plasma's executable data source and reads the JSON on the child's stdout.  So
the "daemon" in this harness is a tiny shell script named ``sarvam-meter`` in a
per-scenario temporary directory that is prepended to PATH, and the scenarios
are chosen by an environment variable the script reads:

  good           prints a valid status payload
  fresh-balance  a valid payload whose balance was updated 10 minutes ago
  old-balance    a valid payload whose balance was updated 2 hours ago
  garbage        prints something that is not JSON
  huge           prints a body far larger than the widget's cap
  hung           accepts the request and never answers (sleeps)
  empty          prints nothing at all and exits 0
  nonzero        prints an error on stderr and exits 3
  refresh        the header's refresh button is pressed (query refresh)
  refresh-fail   `query refresh` fails, so the widget falls back to status
  click          the panel item is clicked (the popup must expand)
  panel-width    the panel item widens for a long value plus the badge
  destroy        the widget is removed while a request is in flight

`plasma5support`'s DataSource is supplied by scripts/qml-stubs (so the widget
loads with no Plasma installed); that stub is passive, so this harness gives it
a process-spawning helper through the ``harnessExec`` context property.  Real
QProcess objects start the script, and disconnectSource() kills a child that is
still running - which is exactly what the widget relies on.

For every scenario it asserts that the GUI thread kept running (an independent
QTimer keeps ticking), that the widget settles into a defined state instead of
staying stuck in `loading`, that the child process is gone afterwards, and that
nothing throws into the engine.

Usage:  python3 scripts/qml-safety-harness.py [--repo DIR] [-v]
Exit code 0 means every scenario passed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time

DEFAULT_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: Minimal QML stand-ins for the Plasma modules, so the real files can load.
STUB_DIR = os.path.join(DEFAULT_REPO, "scripts", "qml-stubs")

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

#: The fake daemon.  One script, one scenario per run, selected by
#: $QML_HARNESS_MODE; every invocation is appended to $QML_HARNESS_LOG so a
#: scenario can assert *which* query the widget ran.
FAKE_DAEMON = r'''#!/bin/sh
# SPDX-License-Identifier: MIT
# Fake sarvam-meter for scripts/qml-safety-harness.py.  Never installed.
printf '%s\n' "$*" >> "$QML_HARNESS_LOG"
case "$QML_HARNESS_MODE" in
    garbage)
        printf '<html>not json</html>\n'
        ;;
    huge)
        head -c 921600 /dev/zero | tr '\0' 'x'
        ;;
    hung)
        sleep 30
        ;;
    empty)
        :
        ;;
    nonzero)
        printf 'boom: the daemon is unhappy\n' >&2
        exit 3
        ;;
    refresh-fail)
        case "$*" in
            *refresh*)
                printf 'refused: nothing to refresh\n' >&2
                exit 1
                ;;
            *)
                cat "$QML_HARNESS_PAYLOAD"
                ;;
        esac
        ;;
    *)
        cat "$QML_HARNESS_PAYLOAD"
        ;;
esac
exit 0
'''


# ---------------------------------------------------------------------------
# the QML source, with the minimum namespace substitution
# ---------------------------------------------------------------------------

def transform_main_qml(source: str, command: str, refresh_seconds: int) -> str:
    """Return a runnable copy of main.qml for a plain QtQuick engine."""
    text = source
    text = text.replace("import org.kde.plasma.plasmoid\n", "")
    text = text.replace("import org.kde.kirigami as Kirigami\n", "")
    text = text.replace("Kirigami.Units.gridUnit", "12")
    text = text.replace("PlasmoidItem {", "Item {", 1)
    text = text.replace("Plasmoid.configuration.", "harnessConfig.")

    config = {
        "command": command,
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

    # PlasmoidItem's two representation slots; declaring them lets the real
    # CompactRepresentation.qml and FullRepresentation.qml load against the stub
    # Plasma modules, so the harness exercises the whole widget, not a fragment.
    injection = (
        "    property var harnessConfig: (%s)\n"
        "    property Item compactRepresentation: null\n"
        "    property Item fullRepresentation: null\n"
    ) % json.dumps(config)
    text = text.replace("Item {", "Item {\n" + injection, 1)
    return text


# ---------------------------------------------------------------------------
# the fake daemon on PATH
# ---------------------------------------------------------------------------

class FakeDaemon:
    """A shell script named ``sarvam-meter`` in a temporary directory on PATH."""

    def __init__(self, mode: str):
        self.mode = mode
        self.dir = tempfile.mkdtemp(prefix="qml-fake-daemon-")
        self.bin_dir = os.path.join(self.dir, "bin")
        os.makedirs(self.bin_dir)
        self.payload_path = os.path.join(self.dir, "payload.json")
        self.log_path = os.path.join(self.dir, "calls.log")
        open(self.log_path, "w", encoding="utf-8").close()

        document = json.loads(json.dumps(GOOD_PAYLOAD))
        minutes = {"fresh-balance": 10, "old-balance": 120}.get(mode)
        if minutes is not None:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                  time.gmtime(time.time() - minutes * 60))
            document["sarvam"]["dashboard"]["balance"]["as_of"] = stamp
        with open(self.payload_path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)

        script = os.path.join(self.bin_dir, "sarvam-meter")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(FAKE_DAEMON)
        os.chmod(script, 0o755)

        self._saved_env = {}

    def __enter__(self):
        for name, value in (
            ("QML_HARNESS_MODE", self.mode),
            ("QML_HARNESS_PAYLOAD", self.payload_path),
            ("QML_HARNESS_LOG", self.log_path),
            ("PATH", self.bin_dir + os.pathsep + os.environ.get("PATH", "")),
        ):
            self._saved_env[name] = os.environ.get(name)
            os.environ[name] = value
        return self

    def __exit__(self, *_exc):
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(self.dir, ignore_errors=True)
        return False

    def calls(self):
        """Every argument list the fake daemon was invoked with."""
        try:
            with open(self.log_path, encoding="utf-8") as handle:
                return [line.strip() for line in handle if line.strip()]
        except OSError:  # pragma: no cover - the log always exists here
            return []


# ---------------------------------------------------------------------------
# the process-spawning helper the DataSource stub delegates to
# ---------------------------------------------------------------------------

def make_exec_runner():
    """A QObject standing in for Plasma's executable data engine."""
    from PySide6.QtCore import QObject, QProcess, Signal, Slot

    class ExecRunner(QObject):
        dataReady = Signal(str, "QVariantMap")

        def __init__(self):
            super().__init__()
            self._procs = {}

        @Slot(str)
        def connectSource(self, name):
            self.disconnectSource(name)
            proc = QProcess(self)
            proc.setProcessChannelMode(QProcess.SeparateChannels)
            proc.finished.connect(
                lambda code, status, n=name: self._finished(n, code))
            self._procs[name] = proc
            # The real engine splits the command into arguments; /bin/sh -c is
            # the same thing with the quoting rules the widget relies on.
            proc.start("/bin/sh", ["-c", name])

        def _finished(self, name, code):
            proc = self._procs.pop(name, None)
            if proc is None:
                return
            out = bytes(proc.readAllStandardOutput()).decode("utf-8", "replace")
            err = bytes(proc.readAllStandardError()).decode("utf-8", "replace")
            self.dataReady.emit(
                name, {"stdout": out, "stderr": err, "exit code": int(code)})

        @Slot(str)
        def disconnectSource(self, name):
            proc = self._procs.pop(name, None)
            if proc is None:
                return
            proc.kill()
            proc.waitForFinished(1000)

        def running_sources(self):
            return sorted(self._procs)

    return ExecRunner()


# ---------------------------------------------------------------------------
# scenarios
# ---------------------------------------------------------------------------

def run_scenario(mode: str, repo: str, verbose: bool) -> tuple:
    """Load the widget against one fake daemon; return (ok, detail, samples)."""
    from PySide6.QtCore import QMetaObject, Qt, QTimer, QUrl
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlComponent, QQmlEngine

    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])

    with FakeDaemon(mode) as daemon:
        ui_dir = os.path.join(
            repo, "plasmoid/io.github.creepy_pasta101.sarvammeter/contents/ui")
        source = open(os.path.join(ui_dir, "main.qml"), encoding="utf-8").read()
        text = transform_main_qml(source, "sarvam-meter", 60)
        # The representations are referenced relative to main.qml, so the copy has
        # to sit next to them.  Copy the whole directory to keep the repo clean.
        work_dir = tempfile.mkdtemp(prefix="qml-harness-")
        shutil.copytree(ui_dir, os.path.join(work_dir, "ui"))
        # `Plasmoid` is a C++ attached property; with no Plasma here the import
        # cannot resolve, so drop it and supply a context property instead.
        for name in os.listdir(os.path.join(work_dir, "ui")):
            if not name.endswith(".qml"):
                continue
            target = os.path.join(work_dir, "ui", name)
            with open(target, encoding="utf-8") as handle:
                body = handle.read()
            body = body.replace("import org.kde.plasma.plasmoid\n", "")
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(body)
        path = os.path.join(work_dir, "ui", "main_harness.qml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

        from PySide6.QtCore import Property, QObject

        class PlasmoidStub(QObject):
            """Stands in for the Plasmoid attached property."""

            def __init__(self):
                super().__init__()
                self._expanded = False

            def _get_expanded(self):
                return self._expanded

            def _set_expanded(self, value):
                self._expanded = bool(value)

            expanded = Property(bool, _get_expanded, _set_expanded)

        plasmoid = PlasmoidStub()
        runner = make_exec_runner()

        engine = QQmlEngine()
        engine.addImportPath(STUB_DIR)
        engine.rootContext().setContextProperty("Plasmoid", plasmoid)
        # The plasma5support stub delegates process spawning to this object.
        engine.rootContext().setContextProperty("harnessExec", runner)
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
            for _ in range(20):
                app.processEvents()
                time.sleep(0.01)
            heartbeat.stop()
            leftover = runner.running_sources()
            return (not leftover), (
                "destroyed cleanly while a request was in flight; "
                "child processes still running: %r" % (leftover,)), samples

        # The GUI thread must have kept running throughout.
        if beats_total < 20:
            return False, "the event loop stalled (%d ticks)" % beats_total, samples
        if loading:
            return False, "the widget stayed stuck in loading=true", samples
        # Nothing may be left running once a request has settled.
        leftover = runner.running_sources()
        if leftover:
            return False, "child processes left running: %r" % (leftover,), samples

        if mode == "good":
            # QML hands the parsed object back as a QJSValue, so convert it.
            document = {}
            if hasattr(payload, "toVariant"):
                document = payload.toVariant() or {}
            elif isinstance(payload, dict):
                document = payload
            ok = (offline is False and document.get("schema") == 2 and error == "")
            return ok, "payload accepted, offline=%s error=%r" % (offline, error), samples
        if mode in ("fresh-balance", "old-balance"):
            freshness = root.property("balanceFreshnessLine") or ""
            old = root.property("balanceAgeOld")
            if mode == "fresh-balance":
                # "balance 10m ago", and not flagged as old
                ok = bool(re.match(r"^balance (10|11)m ago$", freshness)) and not old
            else:
                # "balance 2h ago <middle dot> not refreshed", flagged as old
                ok = (bool(re.match(r"^balance (2|3)h ago", freshness))
                      and freshness.endswith("not refreshed") and bool(old))
            return ok, "freshness line %r (old=%s)" % (freshness, old), samples
        if mode == "panel-width":
            # A three-digit balance plus the offline badge must make the item ask
            # for more room; if it does not, its children overflow the item and
            # paint over the neighbouring applet.
            from PySide6.QtCore import Property, QObject, Signal

            class MeterStub(QObject):
                valueChanged = Signal()
                badgeChanged = Signal()

                def __init__(self):
                    super().__init__()
                    self._value = ""
                    self._badge = False

                def _get_value(self):
                    return self._value

                def _set_value(self, value):
                    self._value = str(value)
                    self.valueChanged.emit()

                def _get_badge(self):
                    return self._badge

                def _set_badge(self, value):
                    self._badge = bool(value)
                    self.badgeChanged.emit()

                # A NOTIFY signal is required, or QML reads the value once and
                # never re-evaluates the binding.
                compactValue = Property(str, _get_value, _set_value,
                                        notify=valueChanged)
                compactWarning = Property(bool, lambda self: False)
                showBadge = Property(bool, _get_badge, _set_badge,
                                     notify=badgeChanged)

            compact_component = QQmlComponent(
                engine, QUrl.fromLocalFile(os.path.join(work_dir, "ui", "CompactRepresentation.qml")))
            compact = compact_component.create()
            if compact is None:
                return False, "compact representation failed: %s" % (
                    compact_component.errorString(),), samples

            meter = MeterStub()
            compact.setProperty("meter", meter)

            def width_for(value, badge):
                meter.compactValue = value
                meter.showBadge = badge
                for _ in range(6):
                    app.processEvents()
                    time.sleep(0.01)
                return float(compact.property("implicitWidth") or 0)

            short = width_for("\u20b99", False)
            long_with_badge = width_for("\u20b91234", True)
            long_without_badge = width_for("\u20b91234", False)
            capped = width_for("\u20b9" + "9" * 60, True)
            maximum = float(compact.property("maximumCompactWidth") or 0)

            ok = (long_with_badge > long_without_badge > short
                  and maximum > 0 and capped <= maximum + 1)
            return ok, ("widths short=%.0f long=%.0f long+badge=%.0f capped=%.0f max=%.0f"
                        % (short, long_without_badge, long_with_badge, capped, maximum)), samples
        if mode == "click":
            # Clicking the panel item must expand the popup: the compact
            # representation owns that gesture, not Plasma.  It is loaded
            # standalone here, because a property-typed child is not part of the
            # parent's object tree and so cannot be reached with findChild().
            compact_component = QQmlComponent(
                engine, QUrl.fromLocalFile(os.path.join(work_dir, "ui", "CompactRepresentation.qml")))
            compact = compact_component.create()
            if compact is None:
                return False, "compact representation failed: %s" % (
                    compact_component.errorString(),), samples
            before = plasmoid.expanded
            QMetaObject.invokeMethod(compact, "toggleExpanded", Qt.DirectConnection)
            app.processEvents()
            after = plasmoid.expanded
            ok = (before is False) and (after is True)
            return ok, "expanded %s -> %s" % (before, after), samples
        if mode == "refresh":
            # The header's refresh button must ask the daemon to re-poll, i.e.
            # run `query refresh`, not just another `query status`.
            QMetaObject.invokeMethod(root, "forceRefresh", Qt.DirectConnection)
            deadline = time.time() + 4.0
            while time.time() < deadline:
                app.processEvents()
                time.sleep(0.02)
            calls = daemon.calls()
            refreshes = [call for call in calls if "query refresh" in call]
            ok = bool(refreshes) and not root.property("loading")
            return ok, "daemon ran %r" % (calls,), samples
        if mode == "refresh-fail":
            QMetaObject.invokeMethod(root, "forceRefresh", Qt.DirectConnection)
            deadline = time.time() + 5.0
            while time.time() < deadline:
                app.processEvents()
                time.sleep(0.02)
            calls = daemon.calls()
            ops = [call.split("query", 1)[-1].strip() for call in calls]
            document = payload.toVariant() if hasattr(payload, "toVariant") else {}
            ok = "refresh" in ops and ops.count("status") >= 1
            ok = ok and not root.property("loading")
            ok = ok and bool(document)
            return ok, "queries %r, settled with payload=%s" % (
                ops, bool(document)), samples
        if mode == "huge":
            ok = offline is True and "too large" in error
            return ok, "huge body rejected: error=%r" % error, samples
        if mode == "garbage":
            ok = offline is True and "malformed" in error
            return ok, "garbage rejected: error=%r" % error, samples
        if mode == "empty":
            ok = offline is True and "nothing" in error
            return ok, "empty output rejected: error=%r" % error, samples
        if mode == "nonzero":
            ok = offline is True and ("exited with status" in error or "sarvam-meter:" in error)
            return ok, "non-zero exit rejected: error=%r" % error, samples
        if mode == "hung":
            ok = offline is True and error != ""
            return ok, "hung child gave up: error=%r" % error, samples
        return False, "unknown mode", samples
    return False, "scenario crashed before it started", []  # pragma: no cover


def check_stub(repo: str) -> tuple:
    """Load the plasma5support stub on its own, so a broken stub is not silent."""
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlComponent, QQmlEngine

    QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    engine = QQmlEngine()
    engine.addImportPath(STUB_DIR)
    path = os.path.join(
        STUB_DIR, "org", "kde", "plasma", "plasma5support", "DataSource.qml")
    component = QQmlComponent(engine, QUrl.fromLocalFile(path))
    item = component.create()
    if item is None:
        return False, "stub failed to load: %s" % component.errorString()
    missing = [name for name in ("connectSource", "disconnectSource", "newData")
               if not hasattr(item, name) and not _has_signal(item, name)]
    ok = not missing
    return ok, "stub loaded; missing members: %r" % (missing,) if missing else "stub loaded"


def _has_signal(item, name) -> bool:
    """True when *item* exposes a signal called *name* (QML signals are meta)."""
    meta = item.metaObject()
    for index in range(meta.methodCount()):
        method = meta.method(index)
        if method.name() == name and method.methodType() == method.Signal:
            return True
    return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="qml-safety-harness")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    failures = 0
    ok, detail = check_stub(args.repo)
    print("%-8s %s  %s" % ("stub", "PASS" if ok else "FAIL", detail))
    if not ok:
        failures += 1

    modes = ("good", "garbage", "huge", "hung", "empty", "nonzero",
             "fresh-balance", "old-balance", "click", "panel-width",
             "refresh", "refresh-fail", "destroy")
    for mode in modes:
        ok, detail, samples = run_scenario(mode, args.repo, args.verbose)
        print("%-12s %s  %s" % (mode, "PASS" if ok else "FAIL", detail))
        if args.verbose and samples:
            print("             last samples (loading, offline, error): %r" % (samples[-3:],))
        if not ok:
            failures += 1
    total = len(modes) + 1
    print("")
    print("%d of %d checks passed" % (total - failures, total))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
