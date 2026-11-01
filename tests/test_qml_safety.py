# SPDX-License-Identifier: MIT
"""The plasmashell-safety contract, checked statically.

The applet runs *inside* plasmashell, on the same GUI thread as every panel, so a
blocking or unbounded operation freezes the whole desktop rather than one widget.
`scripts/qml-safety-harness.py` proves the behaviour in a real Qt engine; this
file is the cheap guard that runs in CI, where no Qt is installed, and fails if
any of the guards is removed from the QML.

These are structural assertions, not a substitute for the harness: they catch a
deleted timeout or a re-introduced synchronous request, not a subtle lifecycle
bug.
"""

from __future__ import annotations

import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLASMOID = os.path.join(
    REPO_ROOT, "plasmoid", "io.github.creepy_pasta101.sarvammeter", "contents"
)
MAIN_QML = os.path.join(PLASMOID, "ui", "main.qml")
CONFIG_QML = os.path.join(PLASMOID, "config", "configGeneral.qml")


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class MainQmlSafetyTest(unittest.TestCase):
    def setUp(self):
        self.source = read(MAIN_QML)
        # Strip comments so a guard mentioned only in prose cannot satisfy a test.
        self.code = re.sub(r"//[^\n]*", "", self.source)

    def test_requests_are_always_asynchronous(self):
        for match in re.finditer(r"\.open\(\s*[^)]*\)", self.code):
            call = match.group(0)
            self.assertRegex(
                call, r",\s*true\s*\)",
                "synchronous XHR is banned in a plasmoid: %s" % call,
            )

    def test_every_request_has_a_timeout(self):
        self.assertIn("requestTimeoutMs", self.code)
        self.assertRegex(self.code, r"\.timeout\s*=\s*root\.requestTimeoutMs")
        # A bounded, small timeout: the daemon is local.
        match = re.search(r"requestTimeoutMs:\s*(\d+)", self.code)
        self.assertIsNotNone(match, "requestTimeoutMs must be a literal")
        self.assertLessEqual(int(match.group(1)), 5000)

    def test_a_watchdog_aborts_a_wedged_request(self):
        self.assertIn("id: requestWatchdog", self.code)
        self.assertIn("abortRequest", self.code)
        self.assertRegex(self.code, r"request\.abort\(\)")
        # The watchdog must be restarted per request and stopped when it settles.
        self.assertIn("requestWatchdog.restart()", self.code)
        self.assertIn("requestWatchdog.stop()", self.code)

    def test_requests_are_single_flight(self):
        # Both entry points (the timer's refresh() and the header's
        # forceRefresh()) delegate to startRequest(), which owns the guard.
        self.assertRegex(
            self.code,
            r"function startRequest\([^)]*\)\s*\{[\s\S]{0,400}if \(root\.loading\)\s*return",
            "startRequest() must not start a second request while one is in flight",
        )

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

    def test_delegate_lists_are_sliced(self):
        self.assertRegex(self.code, r"accountsList:[^\n]*\.slice\(")
        self.assertRegex(self.code, r"errorList[\s\S]{0,200}\.slice\(")
        self.assertIn("maxAccountsShown", self.code)
        self.assertIn("maxErrorsShown", self.code)

    def test_a_request_is_dropped_when_the_widget_goes_away(self):
        self.assertIn("Component.onDestruction", self.code)
        destruction = self.code[self.code.index("Component.onDestruction"):]
        self.assertIn("abortRequest", destruction)

    def test_the_clock_timer_is_slow_and_gated(self):
        # A timer that repaints text must not be cheap enough to burn the GUI
        # thread, and it must stop when there is nothing to age.
        match = re.search(r"id: clockTimer\s*\n\s*interval:\s*(\d+)", self.code)
        self.assertIsNotNone(match, "clockTimer must have a literal interval")
        self.assertGreaterEqual(int(match.group(1)), 10000)
        self.assertRegex(self.code, r"id: clockTimer[\s\S]{0,200}running:\s*root\.balanceAsOf")

    def test_no_unbounded_loops_over_payload_data(self):
        self.assertNotIn("while (true)", self.code)
        self.assertNotIn("while(true)", self.code)
        # Every for-loop must carry an upper bound in its condition.
        for match in re.finditer(r"for\s*\(([^;]*;[^;]*;[^)]*)\)", self.code):
            condition = match.group(1).split(";")[1]
            self.assertTrue(
                re.search(r"<\s*[A-Za-z0-9_.]+", condition),
                "unbounded for-loop: %s" % match.group(0),
            )

    def test_the_applet_never_calls_a_remote_host(self):
        # The widget talks to the local daemon and nothing else.  Any http(s) URL
        # in the file must therefore be loopback (the default daemon address).
        # The raw source is used here, because stripping `//` comments would also
        # eat the `//` inside a URL literal.
        urls = set(re.findall(r"https?://[A-Za-z0-9_.:/-]+", self.source))
        remote = {url for url in urls if not url.startswith("http://127.0.0.1")}
        self.assertEqual(remote, set(),
                         "the applet must not reference a remote host: %r" % (remote,))


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
        # The root must not be a bare Item any more: that is what makes it look
        # like the rest of the shell.
        self.assertNotRegex(self.full, r"(?m)^Item\s*\{")

    def test_the_empty_state_uses_the_standard_placeholder(self):
        self.assertRegex(self.full, r"PlasmaExtras\.PlaceholderMessage\s*\{")
        self.assertIn('icon.name:', self.full)

    def test_the_header_carries_a_refresh_button(self):
        self.assertRegex(self.full, r"id:\s*refreshButton")
        self.assertIn('icon.name: "view-refresh"', self.full)
        # It must call forceRefresh, not refresh: only the POST re-polls.
        self.assertRegex(self.full, r"onClicked:[\s\S]{0,200}forceRefresh\(\)")
        # And it must not be clickable while a request is in flight.
        self.assertRegex(self.full, r"id:\s*refreshButton[\s\S]{0,300}enabled:")

    def test_a_busy_indicator_is_shown_while_loading(self):
        self.assertRegex(self.full, r"BusyIndicator\s*\{")
        self.assertRegex(self.full, r"BusyIndicator\s*\{[\s\S]{0,200}running:\s*fullRoot\.busy")

    def test_the_refresh_goes_through_the_same_safety_path(self):
        # forceRefresh must not open its own XHR: it goes through startRequest,
        # which owns the timeout, the watchdog and the single-flight guard.
        self.assertRegex(
            self.main,
            r"function forceRefresh\(\)\s*\{\s*root\.startRequest\(\"POST\", \"/v1/refresh\", true\);\s*\}")
        self.assertRegex(
            self.main,
            r"function refresh\(\)\s*\{\s*root\.startRequest\(\"GET\", \"/v1/status\", false\);\s*\}")
        # Exactly one XMLHttpRequest constructor in the whole file.
        self.assertEqual(self.main.count("new XMLHttpRequest()"), 1)


class CompactRepresentationTest(unittest.TestCase):
    """Clicking the panel item must open the popup."""

    def setUp(self):
        self.compact = read(os.path.join(PLASMOID, "ui", "CompactRepresentation.qml"))

    def test_the_panel_item_handles_the_click_itself(self):
        # Plasma does not expand the popup for us: without this the widget is
        # completely inert in the panel, which is exactly what happened.
        self.assertRegex(self.compact, r"MouseArea\s*\{")
        self.assertRegex(self.compact, r"onClicked:\s*compactRoot\.toggleExpanded\(\)")
        self.assertRegex(
            self.compact,
            r"function toggleExpanded\(\)\s*\{[\s\S]{0,200}Plasmoid\.expanded = !Plasmoid\.expanded",
        )
        self.assertIn("import org.kde.plasma.plasmoid", self.compact)

    def test_only_the_left_button_is_taken(self):
        # Right-click must still reach the panel for the context menu.
        self.assertRegex(self.compact, r"acceptedButtons:\s*Qt\.LeftButton")


class BackendSettingsPageTest(unittest.TestCase):
    """The settings page can configure the daemon, not just the widget."""

    def setUp(self):
        self.page = read(CONFIG_QML)

    def test_the_poll_interval_is_bounded_to_two_minutes_and_six_hours(self):
        self.assertRegex(self.page, r"from:\s*120")
        self.assertRegex(self.page, r"to:\s*21600")
        self.assertIn("/v1/config", self.page)

    def test_the_backend_is_written_with_a_post_and_read_with_a_get(self):
        # One helper owns the request lifecycle; both verbs go through it, and
        # the open() call itself is asynchronous.
        self.assertIn('sendBackendRequest("GET"', self.page)
        self.assertIn('sendBackendRequest("POST"', self.page)
        self.assertRegex(self.page, r"request\.open\(\s*method\s*,\s*url\s*,\s*true\s*\)")

    def test_backend_values_are_not_widget_config_keys(self):
        # Backend settings must never be persisted into plasma's appletsrc: no
        # cfg_ property may carry one.
        for name in ("cfg_pollSeconds", "cfg_dashboardEnabled", "cfg_cliproxyUrl"):
            self.assertNotIn(name, self.page)


class ConfigPageSafetyTest(unittest.TestCase):
    def setUp(self):
        self.code = re.sub(r"//[^\n]*", "", read(CONFIG_QML))

    def test_the_test_button_cannot_wedge_the_settings_window(self):
        self.assertRegex(self.code, r"request\.timeout\s*=\s*\d+")
        self.assertRegex(self.code, r"\.open\(\s*[^)]*,\s*true\s*\)")
        self.assertIn("Component.onDestruction", self.code)
        self.assertRegex(self.code, r"testRequest\.abort\(\)")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
