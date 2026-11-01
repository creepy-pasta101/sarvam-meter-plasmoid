# SPDX-License-Identifier: MIT
"""The plasmashell-safety contract, checked statically.

The applet runs *inside* plasmashell, on the same GUI thread as every panel, so a
blocking or unbounded operation freezes the whole desktop rather than one widget.
`scripts/qml-safety-harness.py` proves the behaviour in a real Qt engine; this
file is the cheap guard that runs in CI, where no Qt is installed, and fails if
any of the guards is removed from the QML.

The widget no longer speaks HTTP: it runs `<command> query <op>` through Plasma's
executable data source and reads the JSON on the child's stdout.  These
assertions therefore check the data source, the single-flight guard, the
watchdog, the size cap and the command whitelist -- and that no XMLHttpRequest
survives anywhere in the package.

These are structural assertions, not a substitute for the harness: they catch a
deleted timeout or a re-introduced synchronous request, not a subtle lifecycle
bug.
"""

from __future__ import annotations

import os
import re
import unittest
import xml.etree.ElementTree as ET

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = os.path.join(REPO_ROOT, "plasmoid", "io.github.creepy_pasta101.sarvammeter")
PLASMOID = os.path.join(PACKAGE, "contents")
MAIN_QML = os.path.join(PLASMOID, "ui", "main.qml")
CONFIG_QML = os.path.join(PLASMOID, "config", "configGeneral.qml")
SESSION_QML = os.path.join(PLASMOID, "config", "SessionCapture.qml")
MAIN_XML = os.path.join(PLASMOID, "config", "main.xml")
STUB_DIR = os.path.join(REPO_ROOT, "scripts", "qml-stubs")


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _local(tag):
    """Element tag without its XML namespace (KConfigXT files carry one)."""
    return tag.rsplit("}", 1)[-1]


def code_of(source):
    """*source* with `//` comments stripped, so prose cannot satisfy a test."""
    return re.sub(r"//[^\n]*", "", source)


def package_files(suffixes=(".qml", ".js", ".xml", ".json")):
    """Every file of the applet package worth scanning."""
    for root, _dirs, names in os.walk(PACKAGE):
        for name in sorted(names):
            if name.endswith(suffixes):
                yield os.path.join(root, name)


class MainQmlSafetyTest(unittest.TestCase):
    def setUp(self):
        self.source = read(MAIN_QML)
        self.code = code_of(self.source)

    def test_the_widget_talks_through_the_executable_data_source(self):
        self.assertIn("import org.kde.plasma.plasma5support as P5Support", self.code)
        self.assertRegex(self.code, r"P5Support\.DataSource\s*\{")
        self.assertRegex(self.code, r'engine:\s*"executable"')
        self.assertRegex(self.code, r"connectedSources:\s*\[")
        # Exactly one data source: a second one would be a second child process
        # per tick, which is the opposite of the single-flight rule.
        self.assertEqual(self.code.count("P5Support.DataSource {"), 1)

    def test_the_poll_runs_the_query_cli(self):
        # Both entry points go through startRequest(), which owns the guard.
        self.assertRegex(
            self.code,
            r'function refresh\(\)\s*\{\s*root\.startRequest\("status", false\);\s*\}')
        self.assertRegex(
            self.code,
            r'function forceRefresh\(\)\s*\{\s*root\.startRequest\("refresh", true\);\s*\}')
        # The command line is built from the whitelisted program plus a literal.
        self.assertRegex(
            self.code, r'function queryCommand\(op\)\s*\{\s*return root\.command \+ " query " \+ op;')

    def test_the_command_is_a_bare_program_name_or_an_absolute_path(self):
        # A whitelist, so a space, a quote or a metacharacter cannot survive.
        self.assertRegex(self.code, r"/\^\[A-Za-z0-9\._\\/\+-\]\+\\?\$/")
        self.assertIn("function commandError(", self.code)
        self.assertIn("function safeCommand(", self.code)
        # An unusable value falls back to the default instead of being run.
        self.assertRegex(
            self.code,
            r"function safeCommand\(raw\)\s*\{\s*if \(root\.commandError\(raw\) !== \"\"\)\s*"
            r"return root\.defaultCommand;")
        self.assertIn('defaultCommand: "sarvam-meter"', self.code)

    def test_every_request_has_a_bounded_watchdog(self):
        self.assertIn("requestTimeoutMs", self.code)
        # A bounded, small timeout: the daemon is local.
        match = re.search(r"requestTimeoutMs:\s*(\d+)", self.code)
        self.assertIsNotNone(match, "requestTimeoutMs must be a literal")
        self.assertLessEqual(int(match.group(1)), 5000)
        # The data source has no timeout of its own, so the watchdog is the only
        # thing that can end a wedged child.
        self.assertIn("id: requestWatchdog", self.code)
        self.assertRegex(
            self.code, r"id: requestWatchdog[\s\S]{0,300}interval:\s*root\.requestTimeoutMs")
        self.assertIn("abortRequest", self.code)
        self.assertIn("requestWatchdog.restart()", self.code)
        self.assertIn("requestWatchdog.stop()", self.code)

    def test_the_watchdog_kills_the_child_it_gave_up_on(self):
        abort = self.code[self.code.index("function abortRequest("):]
        abort = abort[:abort.index("\n    }\n") + 6]
        self.assertIn("execSource.disconnectSource(target)", abort)
        self.assertIn("requestWatchdog.stop()", abort)

    def test_requests_are_single_flight(self):
        self.assertRegex(
            self.code,
            r"function startRequest\([^)]*\)\s*\{[\s\S]{0,400}if \(root\.loading\)\s*return",
            "startRequest() must not start a second process while one is in flight",
        )
        self.assertIn("pendingCommand", self.code)

    def test_the_response_size_is_capped_before_parsing(self):
        self.assertIn("maxResponseChars", self.code)
        cap = re.search(r"maxResponseChars:\s*(\d+)\s*\*\s*(\d+)", self.code)
        self.assertIsNotNone(cap, "maxResponseChars must be a literal expression")
        self.assertLessEqual(int(cap.group(1)) * int(cap.group(2)), 4 * 1024 * 1024)
        # The length check must come before JSON.parse in the same handler.
        body = self.code.index("body.length > root.maxResponseChars")
        parse = self.code.index("JSON.parse(body)")
        self.assertLess(body, parse, "the size cap must be checked before JSON.parse")

    def test_json_parse_is_guarded(self):
        self.assertRegex(
            self.code, r"try\s*\{[^}]*JSON\.parse\(body\)",
            "JSON.parse must sit inside a try block",
        )

    def test_a_non_zero_exit_is_a_failure(self):
        # A CLI that failed prints its reason on stderr and exits non-zero; the
        # widget must not try to parse its (empty) stdout.
        self.assertIn('payload["exit code"]', self.code)
        self.assertIn("exited with status", self.code)
        self.assertIn("the daemon returned nothing", self.code)

    def test_delegate_lists_are_sliced(self):
        self.assertRegex(self.code, r"accountsList:[^\n]*\.slice\(")
        self.assertRegex(self.code, r"errorList[\s\S]{0,200}\.slice\(")
        self.assertIn("maxAccountsShown", self.code)
        self.assertIn("maxErrorsShown", self.code)

    def test_the_child_is_dropped_when_the_widget_goes_away(self):
        self.assertIn("Component.onDestruction", self.code)
        destruction = self.code[self.code.index("Component.onDestruction"):]
        self.assertIn("abortRequest", destruction)
        # ... and abortRequest is what disconnects the source.
        self.assertIn("execSource.disconnectSource", self.code)

    def test_the_clock_timer_is_slow_and_gated(self):
        match = re.search(r"id: clockTimer\s*\n\s*interval:\s*(\d+)", self.code)
        self.assertIsNotNone(match, "clockTimer must have a literal interval")
        self.assertGreaterEqual(int(match.group(1)), 10000)
        self.assertRegex(self.code, r"id: clockTimer[\s\S]{0,200}running:\s*root\.balanceAsOf")

    def test_no_unbounded_loops_over_payload_data(self):
        self.assertNotIn("while (true)", self.code)
        self.assertNotIn("while(true)", self.code)
        for match in re.finditer(r"for\s*\(([^;]*;[^;]*;[^)]*)\)", self.code):
            condition = match.group(1).split(";")[1]
            self.assertTrue(
                re.search(r"<\s*[A-Za-z0-9_.]+", condition),
                "unbounded for-loop: %s" % match.group(0),
            )

    def test_the_applet_never_calls_a_remote_host(self):
        # The widget runs a local program and nothing else: there is no URL left
        # in this file at all.
        urls = set(re.findall(r"https?://[A-Za-z0-9_.:/-]+", self.source))
        self.assertEqual(urls, set(), "the applet must not reference a URL: %r" % (urls,))


class NoXmlHttpRequestTest(unittest.TestCase):
    """The transport change is complete only if no XHR is left anywhere."""

    def test_no_file_of_the_package_uses_xmlhttprequest(self):
        offenders = []
        for path in package_files():
            text = read(path)
            if "XMLHttpRequest" in code_of(text):
                offenders.append(os.path.relpath(path, REPO_ROOT))
        self.assertEqual(offenders, [], "XMLHttpRequest survives in %r" % (offenders,))

    def test_no_file_of_the_package_uses_an_http_endpoint(self):
        # The daemon serves a Unix socket; /v1/... is dead code.
        offenders = []
        for path in package_files():
            text = read(path)
            if "/v1/" in code_of(text):
                offenders.append(os.path.relpath(path, REPO_ROOT))
        self.assertEqual(offenders, [], "/v1/ endpoints survive in %r" % (offenders,))


class PlasmoidUiTest(unittest.TestCase):
    """The popup follows the same structure as Plasma's own applets."""

    def setUp(self):
        self.full = read(MAIN_QML.replace("main.qml", "FullRepresentation.qml"))
        self.main = read(MAIN_QML)

    def test_the_popup_is_a_plasma_representation_with_a_heading(self):
        self.assertIn("import org.kde.plasma.extras as PlasmaExtras", self.full)
        self.assertRegex(self.full, r"PlasmaExtras\.Representation\s*\{")
        self.assertRegex(self.full, r"header:\s*PlasmaExtras\.PlasmoidHeading\s*\{")
        self.assertIn("collapseMargins: true", self.full)
        self.assertNotRegex(self.full, r"(?m)^Item\s*\{")

    def test_the_empty_state_uses_the_standard_placeholder(self):
        self.assertRegex(self.full, r"PlasmaExtras\.PlaceholderMessage\s*\{")
        self.assertIn('icon.name:', self.full)

    def test_the_header_carries_a_refresh_button(self):
        self.assertRegex(self.full, r"id:\s*refreshButton")
        self.assertIn('icon.name: "view-refresh"', self.full)
        # It must call forceRefresh, not refresh: only `query refresh` re-polls.
        self.assertRegex(self.full, r"onClicked:[\s\S]{0,200}forceRefresh\(\)")
        self.assertRegex(self.full, r"id:\s*refreshButton[\s\S]{0,300}enabled:")

    def test_a_busy_indicator_is_shown_while_loading(self):
        self.assertRegex(self.full, r"BusyIndicator\s*\{")
        self.assertRegex(self.full, r"BusyIndicator\s*\{[\s\S]{0,200}running:\s*fullRoot\.busy")


class CompactRepresentationTest(unittest.TestCase):
    """Clicking the panel item must open the popup."""

    def setUp(self):
        self.compact = read(os.path.join(PLASMOID, "ui", "CompactRepresentation.qml"))

    def test_the_panel_item_handles_the_click_itself(self):
        self.assertRegex(self.compact, r"MouseArea\s*\{")
        self.assertRegex(self.compact, r"onClicked:\s*compactRoot\.toggleExpanded\(\)")
        self.assertRegex(
            self.compact,
            r"function toggleExpanded\(\)\s*\{[\s\S]{0,200}Plasmoid\.expanded = !Plasmoid\.expanded",
        )
        self.assertIn("import org.kde.plasma.plasmoid", self.compact)

    def test_the_item_asks_for_the_width_its_content_needs(self):
        self.assertRegex(
            self.compact, r"naturalWidth:\s*Math\.ceil\(valueMetrics\.implicitWidth\)")
        self.assertRegex(
            self.compact, r"implicitWidth:\s*Math\.max\([\s\S]{0,160}naturalWidth")
        self.assertRegex(self.compact, r"maximumCompactWidth")
        self.assertRegex(self.compact, r"Math\.min\(compactRoot\.maximumCompactWidth")

    def test_the_label_elides_instead_of_overflowing(self):
        self.assertRegex(self.compact, r"Layout\.fillWidth:\s*true")
        self.assertRegex(self.compact, r"Layout\.minimumWidth:\s*0")
        self.assertRegex(self.compact, r"elide:\s*Text\.ElideRight")

    def test_the_badge_cannot_be_squashed(self):
        self.assertRegex(self.compact, r"Layout\.minimumWidth:\s*compactRoot\.badgeWidth")
        self.assertRegex(self.compact, r"Layout\.maximumWidth:\s*compactRoot\.badgeWidth")

    def test_the_measurement_has_no_forward_reference(self):
        self.assertIn("id: valueMetrics", self.compact)
        measure = self.compact.index("id: valueMetrics")
        first_use = self.compact.index("valueMetrics.implicitWidth")
        self.assertLess(measure, first_use)

    def test_only_the_left_button_is_taken(self):
        self.assertRegex(self.compact, r"acceptedButtons:\s*Qt\.LeftButton")


class BackendSettingsPageTest(unittest.TestCase):
    """The settings page can configure the daemon, not just the widget."""

    def setUp(self):
        self.page = read(CONFIG_QML)
        self.code = code_of(self.page)

    def test_the_poll_interval_is_bounded_to_two_minutes_and_six_hours(self):
        self.assertRegex(self.code, r"from:\s*120")
        self.assertRegex(self.code, r"to:\s*21600")
        self.assertRegex(self.code, r"from:\s*120")

    def test_the_backend_is_read_and_written_with_queries(self):
        # One helper owns the process lifecycle; both directions go through it.
        self.assertRegex(self.code, r'page\.runQuery\("config"')
        self.assertRegex(self.code, r'page\.runQuery\("set-config"')
        self.assertRegex(self.code, r'page\.runQuery\("session"')
        self.assertRegex(self.code, r'page\.runQuery\("health"')
        # The JSON argument is single-quoted for the shell.
        self.assertIn("function shellQuote(", self.code)
        self.assertRegex(self.code, r'page\.runQuery\("set-config", page\.shellQuote\(json\)')

    def test_the_page_talks_through_the_executable_data_source(self):
        self.assertIn("import org.kde.plasma.plasma5support as P5Support", self.code)
        self.assertRegex(self.code, r"P5Support\.DataSource\s*\{")
        self.assertRegex(self.code, r'engine:\s*"executable"')
        self.assertIn("function handleQueryData(", self.code)

    def test_the_command_field_is_validated_before_it_is_saved(self):
        self.assertIn('property string cfg_command: "sarvam-meter"', self.code)
        self.assertIn("function validateCommand(", self.code)
        self.assertIn("function syncCommand(", self.code)
        self.assertRegex(self.code, r"/\^\[A-Za-z0-9\._\\/\+-\]\+\\?\$/")
        # Only a valid value is ever written into the KConfigXT key.
        self.assertRegex(
            self.code,
            r"function syncCommand\(\)\s*\{[\s\S]{0,300}validateCommand\(candidate\) !== \"\"")
        self.assertNotIn("cfg_daemonUrl", self.code)

    def test_backend_values_are_not_widget_config_keys(self):
        for name in ("cfg_pollSeconds", "cfg_dashboardEnabled", "cfg_cliproxyUrl"):
            self.assertNotIn(name, self.code)


class ConfigPageSafetyTest(unittest.TestCase):
    def setUp(self):
        self.code = code_of(read(CONFIG_QML))

    def test_the_query_cannot_wedge_the_settings_window(self):
        # A bounded watchdog, an explicit disconnect, and an abort on destruction.
        self.assertRegex(self.code, r"id: queryWatchdog")
        self.assertRegex(
            self.code, r"id: queryWatchdog[\s\S]{0,300}interval:\s*page\.backendTimeoutMs")
        self.assertIn("Component.onDestruction", self.code)
        destruction = self.code[self.code.index("Component.onDestruction"):]
        self.assertIn("page.abortQuery()", destruction)
        self.assertIn("querySource.disconnectSource", self.code)

    def test_the_backend_response_is_capped_before_parsing(self):
        self.assertIn("maxBackendResponseChars", self.code)
        cap = re.search(r"maxBackendResponseChars:\s*(\d+)\s*\*\s*(\d+)", self.code)
        self.assertIsNotNone(cap)
        self.assertLessEqual(int(cap.group(1)) * int(cap.group(2)), 1024 * 1024)
        body = self.code.index("body.length > page.maxBackendResponseChars")
        parse = self.code.index("JSON.parse(body)")
        self.assertLess(body, parse)

    def test_the_queries_are_single_flight(self):
        self.assertRegex(
            self.code,
            r"function runQuery\([\s\S]{0,200}page\.abortQuery\(\);",
            "runQuery() must abandon the previous query before starting one",
        )


class SessionCaptureTest(unittest.TestCase):
    """The captured cookie goes to the daemon over D-Bus, never over HTTP."""

    def setUp(self):
        self.source = read(SESSION_QML)
        self.code = code_of(self.source)

    def test_the_cookie_is_delivered_over_dbus(self):
        # The command is assembled from the three bus constants, so check the
        # parts: busctl --user call <bus> <path> <interface> SetSession ss ...
        self.assertIn('"busctl --user call "', self.code)
        self.assertIn('" SetSession ss "', self.code)
        self.assertIn('busName: "org.kde.sarvam.Meter"', self.code)
        self.assertIn('busPath: "/org/kde/sarvam/Meter"', self.code)
        self.assertIn('busInterface: "org.kde.sarvam.Meter"', self.code)
        # ... with a gdbus fallback for machines without busctl.
        self.assertIn('"gdbus call --session --dest "', self.code)
        self.assertIn('".SetSession "', self.code)

    def test_the_arguments_are_single_quoted(self):
        self.assertIn("function shellQuote(", self.code)
        self.assertRegex(self.code, r"sheet\.shellQuote\(cookie\)")
        self.assertRegex(self.code, r"sheet\.shellQuote\(authorization\)")

    def test_the_delivery_is_bounded_and_killed(self):
        self.assertIn("id: deliveryWatchdog", self.code)
        self.assertIn("execSource.disconnectSource", self.code)
        self.assertIn("Component.onDestruction", self.code)
        destruction = self.code[self.code.index("Component.onDestruction"):]
        self.assertIn("sheet.stopDelivery()", destruction)

    def test_the_values_are_wiped_once_delivered(self):
        self.assertIn("function clearSecret(", self.code)
        complete = self.code[self.code.index("function completeDelivery("):]
        self.assertLess(
            complete.index("sheet.clearSecret()"), complete.index("sheet.stored()"),
            "the secret must be wiped before the signal is emitted",
        )

    def test_no_value_reaches_a_message(self):
        # The failure text is built from words and an exit status only.
        self.assertNotIn("stderr", self.code)
        failure = self.code[self.code.index("function failDelivery("):]
        failure = failure[:failure.index("\n    }\n") + 6]
        self.assertNotIn("payload", failure)
        self.assertNotIn("cookie", failure.lower())

    def test_the_argv_trade_off_is_documented(self):
        # The comment is the only place a reviewer learns the cookie is in the
        # child's argv for the duration of the call.
        self.assertIn("argv", self.source)
        self.assertIn("sarvam-meter session set --cookie-file", self.source)

    def test_the_webengine_isolation_is_unchanged(self):
        self.assertIn("import QtWebEngine", self.code)
        self.assertEqual(self.code.count("import QtWebEngine"), 1)
        self.assertIn("WebEngineProfile.defaultProfile", self.code)
        # The manual fallback is still there, still password-echoed.
        self.assertIn("manualCookieField", self.code)
        self.assertIn("echoMode: TextInput.Password", self.code)


class ConfigSchemaTest(unittest.TestCase):
    def setUp(self):
        self.xml = read(MAIN_XML)
        self.page = read(CONFIG_QML)
        self.code = code_of(self.page)

    def test_the_command_key_replaced_the_daemon_url(self):
        self.assertIn('<entry name="command" type="String">', self.xml)
        self.assertIn("<default>sarvam-meter</default>", self.xml)
        self.assertNotIn("daemonUrl", self.xml)
        self.assertNotIn("127.0.0.1", self.xml)

    def test_the_other_keys_are_untouched(self):
        for key in ("refreshSeconds", "compactMode", "warnBelowPercent", "showApiBreakdown"):
            self.assertIn('name="%s"' % key, self.xml)


    # -- the cfg_<key> / cfg_<key>Default contract -----------------------

    def config_entries(self):
        """{name: default} from main.xml, namespaces stripped."""
        tree = ET.parse(MAIN_XML)
        entries = {}
        for group in [c for c in tree.getroot() if _local(c.tag) == "group"]:
            for entry in [e for e in group if _local(e.tag) == "entry"]:
                default = next(
                    (d for d in entry if _local(d.tag) == "default"), None)
                entries[entry.get("name")] = (default.text or "").strip()
        return entries

    def declared_default(self, name):
        """The literal on `property <type> cfg_<name>Default: <literal>`."""
        match = re.search(
            r"(?m)^\s*property\s+\w+\s+cfg_%sDefault\s*:\s*([^\n]+?)\s*$"
            % re.escape(name), self.page)
        if not match:
            return None
        literal = match.group(1).strip()
        if len(literal) >= 2 and literal[0] == literal[-1] and literal[0] in "\"'":
            literal = literal[1:-1]
        return literal

    def test_every_config_key_has_a_cfg_property_and_a_default(self):
        """Plasma sets cfg_<key> *and* cfg_<key>Default as initial properties.

        Missing either one is not a warning: the dialog prints "Setting initial
        properties failed: SimpleKCM does not have a property called
        cfg_<key>Default" and the page silently never receives its values, so
        every control shows its hard-coded fallback.
        """
        entries = self.config_entries()
        self.assertTrue(entries, "main.xml declares no entries")

        for name, default in sorted(entries.items()):
            self.assertIn("cfg_" + name, self.code,
                          "main.xml declares %r but configGeneral.qml has no cfg_%s" % (name, name))
            self.assertIn("cfg_" + name + "Default", self.code,
                          "configGeneral.qml has no cfg_%sDefault" % (name,))
            declared = self.declared_default(name)
            self.assertIsNotNone(declared, "cfg_%sDefault is not a literal" % (name,))
            self.assertEqual(
                str(declared).lower(), default.lower(),
                "cfg_%sDefault (%r) does not match main.xml <default> (%r)"
                % (name, declared, default))

    def test_the_settings_page_root_is_a_kcm(self):
        # Plasma 6 loads widget config pages as KCMs; a plain Item root never
        # gets its cfg_* properties set.
        self.assertRegex(self.code, r"(?m)^KCM\.SimpleKCM\s*\{")
        self.assertIn("import org.kde.kcmutils", self.page)

class QmlStubTest(unittest.TestCase):
    """The harness needs a plasma5support stand-in to load the widget at all."""

    def setUp(self):
        self.directory = os.path.join(STUB_DIR, "org", "kde", "plasma", "plasma5support")
        self.qmldir = read(os.path.join(self.directory, "qmldir"))
        self.stub = read(os.path.join(self.directory, "DataSource.qml"))
        self.code = code_of(self.stub)

    def test_the_module_declares_the_data_source(self):
        self.assertIn("module org.kde.plasma.plasma5support", self.qmldir)
        self.assertIn("DataSource 1.0 DataSource.qml", self.qmldir)

    def test_the_stub_has_the_api_the_widget_uses(self):
        for member in (
            "property string engine",
            "property var connectedSources",
            "property var data",
            "signal newData(string sourceName, var data)",
            "function connectSource(sourceName)",
            "function disconnectSource(sourceName)",
        ):
            self.assertIn(member, self.code)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
