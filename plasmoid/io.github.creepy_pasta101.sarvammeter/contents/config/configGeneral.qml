// SPDX-License-Identifier: MIT
//
// Sarvam Meter - plasmoid configuration page "General" (unit U6).
//
// Plasma 6 (6.7 / 6.8). The root is a KCM component (KCM.SimpleKCM, a
// Kirigami.ScrollablePage) because Plasma 6 loads widget configuration pages
// as KCMs and no longer wraps them in its own ScrollView.
//
// HOW PERSISTENCE WORKS
// main.xml (KConfigXT) declares the five keys. On open, Plasma's
// AppletConfiguration.qml instantiates this page with one initial property per
// key, named cfg_<key>, taken from Plasmoid.configuration (a
// KConfigPropertyMap). On Apply it copies every cfg_<key> property of the page
// back into Plasmoid.configuration and calls writeConfig(), which persists to
// ~/.config/plasma-org.kde.plasma.desktop-appletsrc under
// [Containments][N][Applets][M][Configuration][General].
// Declaring these cfg_ properties is therefore the whole contract. Controls
// whose value is always valid are exposed as two-way property aliases; the
// daemon URL is a plain property so that an invalid string can never be
// written to the config file (see syncDaemonUrl()).

import QtQuick
import QtQuick.Layouts

import org.kde.kirigami as Kirigami
import org.kde.kcmutils as KCM
import org.kde.plasma.components as PlasmaComponents

KCM.SimpleKCM {
    id: page

    // ---- KConfigXT keys, group "General" (contents/config/main.xml) ----
    property string cfg_daemonUrl: "http://127.0.0.1:8787"
    property alias cfg_refreshSeconds: refreshSpin.value
    property string cfg_compactMode: "sarvam"
    property alias cfg_warnBelowPercent: warnBelowSpin.value
    property alias cfg_showApiBreakdown: breakdownSwitch.checked

    // ---- "Test" button state: idle | pending | ok | fail ----
    property string testState: "idle"
    property string testMessage: ""
    property var testRequest: null

    readonly property string daemonUrlError: validateDaemonUrl(daemonUrlField.text)
    readonly property bool daemonUrlInvalid: daemonUrlError !== ""
    readonly property bool daemonUrlNonLocal: !daemonUrlInvalid && !isLoopbackHost(hostOf(daemonUrlField.text))

    // Value changed from outside the text field (Discard, Reset to defaults).
    // Comparing the normalised field text keeps this from fighting the user
    // while typing.
    // cfg_* may be assigned during instantiation, before the children below
    // exist, so every handler guards against an undefined child id.
    onCfg_daemonUrlChanged: {
        if (typeof daemonUrlField === "undefined" || !daemonUrlField) {
            return;
        }
        if (normalisedBaseUrl(daemonUrlField.text) !== cfg_daemonUrl) {
            daemonUrlField.text = cfg_daemonUrl;
        }
    }

    onCfg_compactModeChanged: {
        if (typeof compactModeCombo === "undefined" || !compactModeCombo) {
            return;
        }
        var index = compactModeCombo.indexOfValue(cfg_compactMode);
        if (index >= 0 && index !== compactModeCombo.currentIndex) {
            compactModeCombo.currentIndex = index;
        }
    }

    // ---- URL helpers (plain JS, no Qt objects) ----

    function trimmedUrl(raw) {
        if (raw === undefined || raw === null) {
            return "";
        }
        return String(raw).trim();
    }

    // Host part of an http(s) URL, without userinfo, port or brackets.
    function hostOf(raw) {
        var s = trimmedUrl(raw).toLowerCase();
        var marker = s.indexOf("//");
        if (marker < 0) {
            return "";
        }
        var rest = s.substring(marker + 2);
        var cut = rest.indexOf("/");
        if (cut >= 0) {
            rest = rest.substring(0, cut);
        }
        cut = rest.indexOf("#");
        if (cut >= 0) {
            rest = rest.substring(0, cut);
        }
        cut = rest.indexOf("?");
        if (cut >= 0) {
            rest = rest.substring(0, cut);
        }
        var at = rest.lastIndexOf("@");
        if (at >= 0) {
            rest = rest.substring(at + 1);
        }
        if (rest.charAt(0) === "[") {
            var close = rest.indexOf("]");
            return close > 0 ? rest.substring(1, close) : "";
        }
        var colon = rest.indexOf(":");
        if (colon >= 0) {
            rest = rest.substring(0, colon);
        }
        return rest;
    }

    // Returns "" when the URL is acceptable, otherwise a user-facing reason.
    function validateDaemonUrl(raw) {
        var s = trimmedUrl(raw);
        if (s === "") {
            return "the daemon URL is empty. Use http://127.0.0.1:8787.";
        }
        if (/\s/.test(s)) {
            return "the daemon URL must not contain spaces.";
        }
        var lower = s.toLowerCase();
        if (lower.indexOf("http://") !== 0 && lower.indexOf("https://") !== 0) {
            return "the daemon URL must start with http:// or https://.";
        }
        if (hostOf(s) === "") {
            return "the daemon URL has no host name.";
        }
        return "";
    }

    function isLoopbackHost(host) {
        var h = String(host).toLowerCase();
        if (h === "localhost" || h === "::1" || h === "0:0:0:0:0:0:0:1") {
            return true;
        }
        if (/^127\.\d+\.\d+\.\d+$/.test(h)) {
            return true;
        }
        return false;
    }

    // Trimmed base URL without a trailing slash, so "<base>/v1/health" is well formed.
    function normalisedBaseUrl(raw) {
        var s = trimmedUrl(raw);
        while (s.length > 0 && s.charAt(s.length - 1) === "/") {
            s = s.substring(0, s.length - 1);
        }
        return s;
    }

    // Push the field into cfg_daemonUrl only when the text is valid, so the
    // config file can never receive a bad URL.
    function syncDaemonUrl() {
        var candidate = trimmedUrl(daemonUrlField.text);
        if (validateDaemonUrl(candidate) !== "") {
            return;
        }
        var normalised = normalisedBaseUrl(candidate);
        if (normalised !== cfg_daemonUrl) {
            cfg_daemonUrl = normalised;
        }
    }

    // Async XHR against <base>/v1/health. Never synchronous, never blocking.
    function testConnection() {
        var error = validateDaemonUrl(daemonUrlField.text);
        if (error !== "") {
            testState = "fail";
            testMessage = "FAIL: " + error;
            return;
        }

        var target = normalisedBaseUrl(daemonUrlField.text) + "/v1/health";
        testState = "pending";
        testMessage = "Contacting " + target + " ...";

        var request = new XMLHttpRequest();
        testRequest = request;

        request.ontimeout = function() {
            page.testState = "fail";
            page.testMessage = "FAIL: " + target + " did not answer within 5 seconds.";
            page.testRequest = null;
        };
        request.onerror = function() {
            page.testState = "fail";
            page.testMessage = "FAIL: could not reach " + target + " - is sarvam-meter running?";
            page.testRequest = null;
        };
        request.onreadystatechange = function() {
            if (request.readyState !== XMLHttpRequest.DONE) {
                return;
            }
            if (request.status === 200) {
                var version = "";
                try {
                    var payload = JSON.parse(request.responseText);
                    if (payload && payload.version !== undefined && payload.version !== null) {
                        version = String(payload.version);
                    }
                } catch (parseError) {
                    version = "";
                }
                page.testState = "ok";
                page.testMessage = version !== ""
                    ? "OK: daemon " + version + " is answering at " + target
                    : "OK: the daemon answered at " + target + " (no version reported).";
            } else if (request.status === 0) {
                page.testState = "fail";
                page.testMessage = "FAIL: no answer from " + target + " - is sarvam-meter running?";
            } else {
                var detail = request.statusText ? " " + request.statusText : "";
                page.testState = "fail";
                page.testMessage = "FAIL: HTTP " + request.status + detail + " from " + target + ".";
            }
            page.testRequest = null;
        };

        request.timeout = 5000;
        // Third argument true: asynchronous. A synchronous open() is banned.
        request.open("GET", target, true);
        request.send();
    }

    Kirigami.FormLayout {
        id: form

        Kirigami.Separator {
            Kirigami.FormData.isSection: true
            Kirigami.FormData.label: "Daemon"
        }

        PlasmaComponents.TextField {
            id: daemonUrlField
            Kirigami.FormData.label: "Daemon URL:"
            placeholderText: "http://127.0.0.1:8787"
            text: page.cfg_daemonUrl
            Layout.minimumWidth: Kirigami.Units.gridUnit * 18
            Layout.maximumWidth: Kirigami.Units.gridUnit * 26
            onTextChanged: page.syncDaemonUrl()
            onEditingFinished: page.syncDaemonUrl()
        }

        RowLayout {
            Kirigami.FormData.label: ""
            spacing: Kirigami.Units.smallSpacing

            PlasmaComponents.Button {
                text: "Test"
                enabled: page.testState !== "pending"
                onClicked: page.testConnection()
            }

            PlasmaComponents.Label {
                text: page.testState === "idle" ? "Not tested yet." : page.testMessage
                color: page.testState === "ok" ? Kirigami.Theme.positiveTextColor
                     : page.testState === "fail" ? Kirigami.Theme.negativeTextColor
                     : Kirigami.Theme.disabledTextColor
                wrapMode: Text.Wrap
                Layout.fillWidth: true
                Layout.maximumWidth: Kirigami.Units.gridUnit * 22
            }
        }

        PlasmaComponents.Label {
            visible: page.daemonUrlInvalid && daemonUrlField.text.length > 0
            text: "Not saved - " + page.daemonUrlError
            color: Kirigami.Theme.negativeTextColor
            wrapMode: Text.Wrap
            Layout.maximumWidth: Kirigami.Units.gridUnit * 26
        }

        PlasmaComponents.Label {
            visible: page.daemonUrlNonLocal
            text: "Note: this host is not a loopback address. sarvam-meter is meant to run on this machine, so the daemon will probably not be reachable there."
            color: Kirigami.Theme.neutralTextColor
            wrapMode: Text.Wrap
            Layout.maximumWidth: Kirigami.Units.gridUnit * 26
        }

        Kirigami.Separator {
            Kirigami.FormData.isSection: true
            Kirigami.FormData.label: "Display and refresh"
        }

        PlasmaComponents.SpinBox {
            id: refreshSpin
            Kirigami.FormData.label: "Refresh every (seconds):"
            from: 5
            to: 3600
            stepSize: 5
            editable: true
        }

        PlasmaComponents.ComboBox {
            id: compactModeCombo
            Kirigami.FormData.label: "Compact view shows:"
            textRole: "label"
            valueRole: "value"
            model: ListModel {
                ListElement {
                    label: "Sarvam balance"
                    value: "sarvam"
                }
                ListElement {
                    label: "CLIProxyAPI quota"
                    value: "cliproxy"
                }
            }
            currentIndex: Math.max(0, indexOfValue(page.cfg_compactMode))
            onActivated: function(index) {
                page.cfg_compactMode = valueAt(index);
            }
            onCurrentIndexChanged: {
                var selected = valueAt(currentIndex);
                if (selected !== undefined && selected !== null && selected !== ""
                        && page.cfg_compactMode !== selected) {
                    page.cfg_compactMode = selected;
                }
            }
        }

        PlasmaComponents.SpinBox {
            id: warnBelowSpin
            Kirigami.FormData.label: "Warn below (%):"
            from: 0
            to: 100
            stepSize: 5
            editable: true
        }

        PlasmaComponents.Switch {
            id: breakdownSwitch
            Kirigami.FormData.label: "Full view:"
            text: "Show the per-API spend breakdown"
        }
    }
}
