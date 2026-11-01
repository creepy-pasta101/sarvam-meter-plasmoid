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

// A SECOND STORE
// The page also edits the daemon's own config, read with GET /v1/config and
// written with POST /v1/config (the "Backend (sarvam-meter daemon)" group at
// the bottom). Those values belong to the daemon and live in its config file,
// not in KConfigXT, so they are deliberately not named cfg_*, are not declared
// in main.xml, and are written only by the "Save backend settings" button -
// never by this dialog's Apply, which copies cfg_* properties only.

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

    // The settings window can be closed while the probe is in flight; drop it so
    // no callback outlives the page (and so the config dialog cannot be left
    // waiting on a socket).
    Component.onDestruction: {
        if (testRequest) {
            try {
                testRequest.abort();
            } catch (e) {
                // already finished
            }
            testRequest = null;
        }
        // Same discipline for the backend requests: nothing may outlive the
        // page, and no callback may fire into a half-destroyed dialog.
        page.abortBackendRequest();
    }

    // ------------------------------------------------------------------
    // Backend group state. These are NOT widget settings: they describe the
    // sarvam-meter daemon's own config document and never reach KConfigXT.
    // ------------------------------------------------------------------

    // idle | loading | ready | saving | error
    property string backendState: "idle"
    property string backendMessage: ""
    // Inline result of the last Save: "" | "OK: ..." | "FAIL: ..." | info.
    property string backendFeedback: ""
    property string backendFeedbackKind: "none"   // none | ok | fail | info

    // The in-flight request and a mutable "already handled" flag shared with
    // its callbacks, so abort() and a late event can never both settle it.
    property var backendRequest: null
    property var backendRequestState: null

    // The last document accepted from the daemon: the baseline the diff is
    // taken against, so Save only ever sends what the user actually changed.
    property bool backendLoaded: false
    property bool backendHasDashboard: false
    property bool backendHasCliproxy: false
    property bool loadedDashboardEnabled: false
    property int loadedDashboardPollSeconds: 300
    property bool loadedCliproxyEnabled: false
    property string loadedCliproxyBaseUrl: ""
    property bool backendDirty: false

    readonly property bool backendBusy: backendRequest !== null
    readonly property int backendTimeoutMs: 8000
    readonly property int maxBackendResponseChars: 256 * 1024

    // One GET /v1/config when the page opens. Read-only, so it cannot disturb
    // anything the user has typed.
    Component.onCompleted: page.loadBackendConfig()

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

        // A second click must not leave the first request running: its callbacks
        // would clobber the state of the one being shown.
        if (testRequest) {
            try {
                testRequest.abort();
            } catch (e) {
                // already finished
            }
            testRequest = null;
        }

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

    // ------------------------------------------------------------------
    // Backend group: reading and writing the daemon's own config
    // ------------------------------------------------------------------

    function validateHttpUrl(raw, what) {
        var s = trimmedUrl(raw);
        if (s === "") {
            return what + " is empty.";
        }
        if (/\s/.test(s)) {
            return what + " must not contain spaces.";
        }
        var lower = s.toLowerCase();
        if (lower.indexOf("http://") !== 0 && lower.indexOf("https://") !== 0) {
            return what + " must start with http:// or https://.";
        }
        if (hostOf(s) === "") {
            return what + " has no host name.";
        }
        return "";
    }

    function cliproxyUrlError() {
        if (typeof cliproxyUrlField === "undefined" || !cliproxyUrlField) {
            return "";
        }
        return validateHttpUrl(cliproxyUrlField.text, "the CLIProxyAPI base URL");
    }

    // The daemon the backend group talks to: whatever the field says, falling
    // back to the widget setting when the field has not been filled yet.
    function backendBaseUrl() {
        var fromField = normalisedBaseUrl(daemonUrlField.text);
        if (fromField !== "") {
            return fromField;
        }
        return normalisedBaseUrl(page.cfg_daemonUrl);
    }

    // 120 s (2 minutes) .. 21600 s (6 hours). The SpinBox already enforces the
    // range; clamping here means neither a stale document nor a stray value can
    // put an out-of-range interval on the wire.
    function clampPollSeconds(raw) {
        var value = Number(raw);
        if (!isFinite(value)) {
            return 120;
        }
        value = Math.round(value);
        if (value < 120) {
            return 120;
        }
        if (value > 21600) {
            return 21600;
        }
        return value;
    }

    // "= 2 minutes" / "= 1.5 hours", for the readout beside the SpinBox.
    function describeInterval(raw) {
        var value = Number(raw);
        if (!isFinite(value) || value <= 0) {
            return "";
        }
        if (value >= 3600) {
            var hours = Math.round((value / 3600) * 100) / 100;
            return "= " + hours + (hours === 1 ? " hour" : " hours");
        }
        var minutes = Math.round((value / 60) * 100) / 100;
        return "= " + minutes + (minutes === 1 ? " minute" : " minutes");
    }

    function backendFeedbackColor() {
        if (page.backendFeedbackKind === "ok") {
            return Kirigami.Theme.positiveTextColor;
        }
        if (page.backendFeedbackKind === "fail") {
            return Kirigami.Theme.negativeTextColor;
        }
        return Kirigami.Theme.disabledTextColor;
    }

    // Recomputes "the form no longer matches the daemon". Called from the
    // controls' handlers rather than bound, because the controls are declared
    // further down and a binding would read them before they exist.
    function refreshBackendDirty() {
        if (typeof dashboardEnabledSwitch === "undefined" || !dashboardEnabledSwitch
                || typeof pollSecondsSpin === "undefined" || !pollSecondsSpin
                || typeof cliproxyEnabledSwitch === "undefined" || !cliproxyEnabledSwitch
                || typeof cliproxyUrlField === "undefined" || !cliproxyUrlField) {
            page.backendDirty = false;
            return;
        }
        if (!page.backendLoaded) {
            page.backendDirty = false;
            return;
        }
        page.backendDirty = dashboardEnabledSwitch.checked !== page.loadedDashboardEnabled
            || pollSecondsSpin.value !== page.loadedDashboardPollSeconds
            || cliproxyEnabledSwitch.checked !== page.loadedCliproxyEnabled
            || normalisedBaseUrl(cliproxyUrlField.text) !== page.loadedCliproxyBaseUrl;
    }

    // Drop the in-flight backend request, if any, and make its callbacks inert.
    function abortBackendRequest() {
        var state = page.backendRequestState;
        var request = page.backendRequest;
        page.backendRequestState = null;
        page.backendRequest = null;
        if (state) {
            state.done = true;
        }
        if (request) {
            try {
                request.abort();
            } catch (e) {
                // aborting an already-finished request is harmless
            }
        }
    }

    // One bounded XHR, following the same rules as testConnection() above:
    // asynchronous only, an explicit timeout, the response size capped before
    // JSON.parse, a single flight (starting a request aborts the previous one)
    // and an abort on destruction. Nothing here can block plasmashell.
    function sendBackendRequest(method, url, payload, onSuccess, onFailure) {
        page.abortBackendRequest();

        var request = null;
        try {
            request = new XMLHttpRequest();
        } catch (e) {
            request = null;
        }
        if (!request) {
            onFailure("cannot create a request");
            return;
        }

        var body = null;
        if (payload !== null && payload !== undefined) {
            try {
                body = JSON.stringify(payload);
            } catch (e) {
                body = null;
            }
            if (body === null) {
                onFailure("cannot serialise the settings to send");
                return;
            }
        }

        var state = { "done": false };

        function settle(message, doc) {
            if (state.done) {
                return;
            }
            state.done = true;
            if (page.backendRequestState === state) {
                page.backendRequestState = null;
                page.backendRequest = null;
            }
            if (message !== "") {
                onFailure(message);
                return;
            }
            onSuccess(doc);
        }

        request.onreadystatechange = function () {
            // 4 is DONE; the numeric form does not rely on the engine exposing
            // XMLHttpRequest.DONE.
            if (request.readyState !== 4 || state.done) {
                return;
            }

            if (request.status === 200) {
                var bodyText = "";
                try {
                    bodyText = request.responseText || "";
                } catch (e) {
                    bodyText = "";
                }
                // Never hand a huge string to JSON.parse on the GUI thread:
                // that is the one way this page could stall plasmashell.
                if (bodyText.length > page.maxBackendResponseChars) {
                    settle("the daemon's answer from " + url + " is too large to read safely");
                    return;
                }
                var parsed = null;
                try {
                    parsed = JSON.parse(bodyText);
                } catch (e) {
                    parsed = null;
                }
                if (!parsed || typeof parsed !== "object") {
                    settle("the daemon answered " + url + " with something that is not JSON");
                    return;
                }
                settle("", parsed);
                return;
            }

            if (request.status === 0) {
                settle("no answer from " + url + " - is sarvam-meter running?");
                return;
            }

            var detail = backendErrorDetail(request);
            if (detail !== "") {
                settle("HTTP " + request.status + ": " + detail);
            } else {
                settle("HTTP " + request.status + " from " + url);
            }
        };

        request.onerror = function () {
            settle("could not reach " + url + " - is sarvam-meter running?");
        };
        request.ontimeout = function () {
            settle(url + " did not answer within " + (page.backendTimeoutMs / 1000) + " seconds");
        };
        request.onabort = function () {
            settle("the request to " + url + " was aborted");
        };

        try {
            // Third argument true: asynchronous. A synchronous open() is banned.
            request.open(method, url, true);
            request.setRequestHeader("Accept", "application/json");
            if (body !== null) {
                request.setRequestHeader("Content-Type", "application/json");
            }
        } catch (e) {
            settle("cannot open a request to " + url);
            return;
        }

        request.timeout = page.backendTimeoutMs;

        page.backendRequest = request;
        page.backendRequestState = state;

        try {
            if (body === null) {
                request.send();
            } else {
                request.send(body);
            }
        } catch (e) {
            settle("cannot send the request to " + url);
        }
    }

    // The daemon's own reason for a 400, from
    // {"error": {"message": "...", "code": "invalid_config"}}. Size-capped and
    // fully guarded: an error body is still untrusted input.
    function backendErrorDetail(request) {
        var text = "";
        try {
            text = request.responseText || "";
        } catch (e) {
            return "";
        }
        if (text.length === 0 || text.length > page.maxBackendResponseChars) {
            return "";
        }
        var parsed = null;
        try {
            parsed = JSON.parse(text);
        } catch (e) {
            return "";
        }
        if (!parsed || typeof parsed !== "object") {
            return "";
        }
        var error = parsed.error;
        if (!error || typeof error !== "object") {
            return "";
        }
        var message = error.message;
        if (typeof message !== "string" || message.length === 0) {
            return "";
        }
        // A daemon message is one line, not a stack trace.
        if (message.length > 300) {
            message = message.substring(0, 300) + "...";
        }
        return message;
    }

    // GET /v1/config: the one read that populates the group.
    function loadBackendConfig() {
        var base = backendBaseUrl();
        var urlError = validateDaemonUrl(base);
        if (urlError !== "") {
            page.backendState = "error";
            page.backendMessage = "Cannot read the backend settings - " + urlError;
            return;
        }

        var target = base + "/v1/config";
        page.backendState = "loading";
        page.backendMessage = "Reading the backend settings from " + target + " ...";

        sendBackendRequest("GET", target, null, function (doc) {
            page.applyBackendDocument(doc);
        }, function (message) {
            page.backendState = "error";
            page.backendMessage = "Cannot read the backend settings - " + message;
        });
    }

    // Copy a sanitised config document into the controls and into the diff
    // baseline. Every field is read defensively: the document comes off the
    // wire and nothing about its shape is guaranteed here.
    function applyBackendDocument(doc) {
        if (!doc || typeof doc !== "object") {
            page.backendState = "error";
            page.backendMessage = "The daemon answered, but not with a config document.";
            return;
        }

        var dashboard = (doc.dashboard && typeof doc.dashboard === "object") ? doc.dashboard : null;
        var cliproxy = (doc.cliproxy && typeof doc.cliproxy === "object") ? doc.cliproxy : null;

        if (!dashboard && !cliproxy) {
            page.backendState = "error";
            page.backendMessage = "The daemon's config document has no dashboard or cliproxy section, so there is nothing here to edit.";
            return;
        }

        page.backendHasDashboard = dashboard !== null;
        page.backendHasCliproxy = cliproxy !== null;

        page.loadedDashboardEnabled = dashboard ? dashboard.enabled === true : false;
        page.loadedDashboardPollSeconds = page.clampPollSeconds(
            (dashboard && typeof dashboard.poll_seconds === "number") ? dashboard.poll_seconds : 300);
        page.loadedCliproxyEnabled = cliproxy ? cliproxy.enabled === true : false;
        page.loadedCliproxyBaseUrl = (cliproxy && typeof cliproxy.base_url === "string")
            ? normalisedBaseUrl(cliproxy.base_url)
            : "";

        dashboardEnabledSwitch.checked = page.loadedDashboardEnabled;
        pollSecondsSpin.value = page.loadedDashboardPollSeconds;
        cliproxyEnabledSwitch.checked = page.loadedCliproxyEnabled;
        cliproxyUrlField.text = page.loadedCliproxyBaseUrl;

        page.backendLoaded = true;
        page.refreshBackendDirty();
        page.backendState = "ready";
        page.backendMessage = "";
    }

    // The changed subset, or null when nothing changed. Only the keys the user
    // actually touched are included, so an untouched section is never sent.
    function buildBackendSubset(pollSeconds) {
        var subset = null;

        if (page.backendHasDashboard) {
            var dashboardPart = null;
            if (dashboardEnabledSwitch.checked !== page.loadedDashboardEnabled) {
                dashboardPart = dashboardPart || {};
                dashboardPart.enabled = dashboardEnabledSwitch.checked;
            }
            if (pollSeconds !== page.loadedDashboardPollSeconds) {
                dashboardPart = dashboardPart || {};
                dashboardPart.poll_seconds = pollSeconds;
            }
            if (dashboardPart) {
                subset = subset || {};
                subset.dashboard = dashboardPart;
            }
        }

        if (page.backendHasCliproxy) {
            var cliproxyPart = null;
            if (cliproxyEnabledSwitch.checked !== page.loadedCliproxyEnabled) {
                cliproxyPart = cliproxyPart || {};
                cliproxyPart.enabled = cliproxyEnabledSwitch.checked;
            }
            var cliproxyUrl = normalisedBaseUrl(cliproxyUrlField.text);
            if (cliproxyUrl !== page.loadedCliproxyBaseUrl) {
                cliproxyPart = cliproxyPart || {};
                cliproxyPart.base_url = cliproxyUrl;
            }
            if (cliproxyPart) {
                subset = subset || {};
                subset.cliproxy = cliproxyPart;
            }
        }

        return subset;
    }

    // The "Save backend settings" button. Validates first, sends only the
    // changed subset, then re-reads the document so the form shows what the
    // daemon actually stored (it sanitises, clamps and redacts).
    function saveBackendConfig() {
        if (!page.backendLoaded) {
            page.backendFeedback = "FAIL: nothing has been read from the daemon yet.";
            page.backendFeedbackKind = "fail";
            return;
        }

        var base = backendBaseUrl();
        var urlError = validateDaemonUrl(base);
        if (urlError !== "") {
            page.backendFeedback = "FAIL: " + urlError;
            page.backendFeedbackKind = "fail";
            return;
        }

        // Rejected here rather than at the daemon: an invalid value must not
        // leave this page at all.
        if (page.backendHasCliproxy) {
            var cliproxyError = page.cliproxyUrlError();
            if (cliproxyError !== "") {
                page.backendFeedback = "FAIL: " + cliproxyError;
                page.backendFeedbackKind = "fail";
                return;
            }
        }

        var pollSeconds = page.clampPollSeconds(pollSecondsSpin.value);
        if (pollSecondsSpin.value !== pollSeconds) {
            pollSecondsSpin.value = pollSeconds;
        }

        var subset = page.buildBackendSubset(pollSeconds);
        if (subset === null) {
            page.backendFeedback = "Nothing to save - the backend settings already match the daemon.";
            page.backendFeedbackKind = "info";
            return;
        }

        var target = base + "/v1/config";
        page.backendState = "saving";
        page.backendMessage = "";
        page.backendFeedback = "Saving to " + target + " ...";
        page.backendFeedbackKind = "info";

        sendBackendRequest("POST", target, subset, function (doc) {
            page.applyBackendDocument(doc);
            page.backendFeedback = "OK: the daemon accepted the new backend settings.";
            page.backendFeedbackKind = "ok";
            page.loadBackendConfig();
        }, function (message) {
            page.backendState = "error";
            page.backendMessage = "";
            page.backendFeedback = "FAIL: " + message;
            page.backendFeedbackKind = "fail";
        });
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

        Kirigami.Separator {
            Kirigami.FormData.isSection: true
            Kirigami.FormData.label: "Backend (sarvam-meter daemon)"
        }

        PlasmaComponents.Label {
            text: "These values belong to the sarvam-meter daemon and live in its own config file. The page reads them with GET /v1/config and the button below writes them with POST /v1/config. They are not widget settings: the dialog's Apply button does not touch them."
            color: Kirigami.Theme.disabledTextColor
            wrapMode: Text.Wrap
            Layout.maximumWidth: Kirigami.Units.gridUnit * 26
        }

        PlasmaComponents.Label {
            visible: page.backendState === "loading"
            text: page.backendMessage
            color: Kirigami.Theme.disabledTextColor
            wrapMode: Text.Wrap
            Layout.maximumWidth: Kirigami.Units.gridUnit * 26
        }

        PlasmaComponents.Label {
            visible: page.backendState === "error"
            text: page.backendMessage
            color: Kirigami.Theme.negativeTextColor
            wrapMode: Text.Wrap
            Layout.maximumWidth: Kirigami.Units.gridUnit * 26
        }

        PlasmaComponents.Switch {
            id: dashboardEnabledSwitch
            Kirigami.FormData.label: "Dashboard source:"
            text: "Read spend from the Sarvam dashboard"
            enabled: page.backendLoaded && page.backendHasDashboard && page.backendState !== "saving"
            onCheckedChanged: page.refreshBackendDirty()
        }

        RowLayout {
            Kirigami.FormData.label: "Dashboard poll interval (seconds):"
            spacing: Kirigami.Units.smallSpacing

            PlasmaComponents.SpinBox {
                id: pollSecondsSpin
                from: 120
                to: 21600
                stepSize: 60
                value: 300
                editable: true
                enabled: page.backendLoaded && page.backendHasDashboard && page.backendState !== "saving"
                onValueChanged: page.refreshBackendDirty()
            }

            PlasmaComponents.Label {
                text: page.describeInterval(pollSecondsSpin.value)
                color: Kirigami.Theme.disabledTextColor
                Layout.leftMargin: Kirigami.Units.smallSpacing
            }
        }

        PlasmaComponents.Label {
            visible: page.backendHasDashboard
            text: "Allowed range: 120 seconds (2 minutes) to 21600 seconds (6 hours), in steps of 60 seconds. The dashboard sits behind bot management, so it must not be polled minute by minute."
            color: Kirigami.Theme.disabledTextColor
            wrapMode: Text.Wrap
            Layout.maximumWidth: Kirigami.Units.gridUnit * 26
        }

        PlasmaComponents.Switch {
            id: cliproxyEnabledSwitch
            Kirigami.FormData.label: "CLIProxyAPI source:"
            text: "Read quota from CLIProxyAPI"
            enabled: page.backendLoaded && page.backendHasCliproxy && page.backendState !== "saving"
            onCheckedChanged: page.refreshBackendDirty()
        }

        PlasmaComponents.TextField {
            id: cliproxyUrlField
            Kirigami.FormData.label: "CLIProxyAPI base URL:"
            placeholderText: "http://127.0.0.1:8317"
            Layout.minimumWidth: Kirigami.Units.gridUnit * 18
            Layout.maximumWidth: Kirigami.Units.gridUnit * 26
            enabled: page.backendLoaded && page.backendHasCliproxy && page.backendState !== "saving"
            onTextChanged: page.refreshBackendDirty()
        }

        PlasmaComponents.Label {
            visible: cliproxyUrlField.text.length > 0 && page.cliproxyUrlError() !== ""
            text: "Not saved - " + page.cliproxyUrlError()
            color: Kirigami.Theme.negativeTextColor
            wrapMode: Text.Wrap
            Layout.maximumWidth: Kirigami.Units.gridUnit * 26
        }

        RowLayout {
            Kirigami.FormData.label: ""
            spacing: Kirigami.Units.smallSpacing

            PlasmaComponents.Button {
                text: "Save backend settings"
                icon.name: "dialog-ok-apply"
                enabled: page.backendLoaded && !page.backendBusy
                onClicked: page.saveBackendConfig()
            }

            PlasmaComponents.Button {
                text: "Reload"
                icon.name: "view-refresh"
                enabled: !page.backendBusy
                onClicked: page.loadBackendConfig()
            }
        }

        PlasmaComponents.Label {
            visible: page.backendFeedback !== ""
            text: page.backendFeedback
            color: page.backendFeedbackColor()
            wrapMode: Text.Wrap
            Layout.maximumWidth: Kirigami.Units.gridUnit * 26
        }

        PlasmaComponents.Label {
            visible: page.backendDirty && !page.backendBusy && page.backendState !== "error"
            text: "Unsaved backend changes."
            color: Kirigami.Theme.neutralTextColor
            wrapMode: Text.Wrap
        }
    }
}
