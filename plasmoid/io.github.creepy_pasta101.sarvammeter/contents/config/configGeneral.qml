// SPDX-License-Identifier: MIT
//
// Sarvam Meter - plasmoid configuration page "General" (unit U6, transport unit B2).
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
// command is a plain property so that an invalid string can never be written to
// the config file (see syncCommand()).
//
// HOW THIS PAGE TALKS TO THE DAEMON (transport change, unit B2)
// The daemon no longer listens on a TCP port: it serves newline-delimited JSON
// on a Unix socket, and `sarvam-meter query <op>` is its client, printing the
// reply's `result` as JSON on stdout. So there is no XMLHttpRequest anywhere in
// this page any more. Every call is one child process, started through Plasma's
// executable data source (org.kde.plasma.plasma5support, engine "executable"),
// and the answer arrives asynchronously through its newData signal as a map of
// "stdout", "stderr" and "exit code".
//
// The program that is run comes from the "Command" field: a bare program name
// (sarvam-meter, resolved on PATH) or an absolute path. Because it is executed,
// it is validated against a whitelist - no spaces, no quotes, no shell
// metacharacters, no leading dash - and an unacceptable value is never saved.
//
// A SECOND STORE
// The page also edits the daemon's own config, read with "<command> query
// config" and written with "<command> query set-config '<json>'" (the "Backend
// (sarvam-meter daemon)" group at the bottom). Those values belong to the daemon
// and live in its config file, not in KConfigXT, so they are deliberately not
// named cfg_*, are not declared in main.xml, and are written only by the "Save
// backend settings" button - never by this dialog's Apply, which copies cfg_*
// properties only.
//
// A THIRD THING: THE DASHBOARD SESSION (unit D)
// The dashboard source needs a signed-in Sarvam session, stored by the daemon in
// a 0600 file. The "Capture session..." button below opens SessionCapture.qml in
// a browser window; that sheet hands the captured headers to the daemon over
// D-Bus (org.kde.sarvam.Meter.SetSession), not over HTTP. This page only reads
// the session's *state* back, with "<command> query session".
//
// That sheet is the only file in this package that imports QtWebEngine, and
// QtWebEngine is not part of a standard KDE install - an unresolvable QML import
// is fatal to the component that holds it. So this page never names the module
// and never instantiates the sheet at load time: it is reached through a Loader
// that is activated on demand (sessionSheetLoader, source "SessionCapture.qml"),
// and a Loader.Error - the missing module - is turned into a message naming the
// package to install and the CLI fallback. A machine without QtWebEngine loses
// this one button and keeps the whole settings page.
//
// Session values are secret. They never enter a property of this page, a label,
// a tooltip, a log line or an error message. The status line shows counts and
// header names only.

import QtQuick
import QtQuick.Layouts

import org.kde.kirigami as Kirigami
import org.kde.kcmutils as KCM
import org.kde.plasma.components as PlasmaComponents
import org.kde.plasma.plasma5support as P5Support

KCM.SimpleKCM {
    id: page

    // ---- KConfigXT keys, group "General" (contents/config/main.xml) ----
    property string cfg_command: "sarvam-meter"
    property alias cfg_refreshSeconds: refreshSpin.value
    property string cfg_compactMode: "sarvam"
    property alias cfg_warnBelowPercent: warnBelowSpin.value
    property alias cfg_showApiBreakdown: breakdownSwitch.checked

    // Plasma sets *two* initial properties per key: cfg_<key> and
    // cfg_<key>Default. Without the Default one the dialog reports
    // "Setting initial properties failed: SimpleKCM does not have a property
    // called cfg_<key>Default" and the page never gets its values. They must
    // match the <default> in main.xml; a test pins that.
    property string cfg_commandDefault: "sarvam-meter"
    property int cfg_refreshSecondsDefault: 60
    property string cfg_compactModeDefault: "sarvam"
    property int cfg_warnBelowPercentDefault: 20
    property bool cfg_showApiBreakdownDefault: true

    // ---- "Test" button state: idle | pending | ok | fail ----
    property string testState: "idle"
    property string testMessage: ""

    // The settings window can be closed while a query is in flight; drop it so
    // no callback outlives the page (and so the dialog cannot be left waiting on
    // a child process).
    Component.onDestruction: {
        page.abortQuery();
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

    readonly property bool backendBusy: pendingQuery !== ""
    readonly property int backendTimeoutMs: 8000
    readonly property int maxBackendResponseChars: 256 * 1024

    // ---- dashboard session (unit D): state shown in the Backend group ----
    // idle | loading | ready | error
    property string sessionState: "idle"
    property bool sessionConfigured: false
    // Header NAMES the daemon reports holding ("cookie", "authorization").
    // Never a value: `query session` does not return one.
    property var sessionHeaderNames: []
    property string sessionMessage: ""

    // The sheet, its loader and the last result of a capture.
    property bool sessionSheetActive: false
    property bool sessionLoadFailed: false
    property string sessionFeedback: ""
    property string sessionFeedbackKind: "none"   // none | ok | fail | info

    // One `query session` when the page opens, then a `query config`.
    // Both go through runQuery(), which is single-flight, so they are sequenced
    // rather than fired together: the session read settles, and its completion
    // starts the config read. Read-only, so neither can disturb anything the
    // user has typed.
    Component.onCompleted: page.loadSessionStatus()

    // ---- the command, and why it is validated ------------------------
    readonly property string defaultCommand: "sarvam-meter"

    // The program every query runs. A configured value that would be unsafe on
    // a command line falls back to the default instead of being executed.
    readonly property string command: {
        var raw = page.cfg_command;
        if (typeof raw === "undefined" || raw === null) {
            return page.defaultCommand;
        }
        var text = page.trimmed(raw);
        if (page.validateCommand(text) !== "") {
            return page.defaultCommand;
        }
        return text;
    }

    // Validation of what is currently typed in the field, for the inline error.
    readonly property string commandErrorText: {
        if (typeof commandField === "undefined" || !commandField) {
            return "";
        }
        return page.validateCommand(commandField.text);
    }
    readonly property bool commandInvalid: page.commandErrorText !== ""

    // Value changed from outside the text field (Discard, Reset to defaults).
    // Comparing the field text keeps this from fighting the user while typing.
    // cfg_* may be assigned during instantiation, before the children below
    // exist, so the handler guards against an undefined child id.
    onCfg_commandChanged: {
        if (typeof commandField === "undefined" || !commandField) {
            return;
        }
        if (page.trimmed(commandField.text) !== page.cfg_command) {
            commandField.text = page.cfg_command;
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

    // ---- command helpers (plain JS, no Qt objects) ----

    function trimmed(raw) {
        if (raw === undefined || raw === null) {
            return "";
        }
        return String(raw).trim();
    }

    // "" when the string is safe to execute, otherwise a user-facing reason.
    // This string becomes the first word of a command line, so the check is a
    // whitelist - letters, digits, dot, underscore, dash, plus and slash - and
    // nothing else survives. In particular a space, a quote, a backtick, $, ;,
    // &, |, <, >, ( or ) can never reach the executed command.
    function validateCommand(raw) {
        var s = page.trimmed(raw);
        if (s === "") {
            return "the command is empty. Use sarvam-meter, or an absolute path to it.";
        }
        if (s.length > 512) {
            return "the command is too long.";
        }
        if (!/^[A-Za-z0-9._\/+-]+$/.test(s)) {
            return "the command must be a bare program name or an absolute path: no spaces, quotes or shell metacharacters.";
        }
        if (s.charAt(0) === "-") {
            return "the command must not start with a dash.";
        }
        if (s.indexOf("/") > 0 && s.charAt(0) !== "/") {
            return "the command must be a bare program name or an absolute path, not a relative path.";
        }
        return "";
    }

    // Push the field into cfg_command only when the text is valid, so the config
    // file can never receive a command that would be run as something else.
    function syncCommand() {
        var candidate = page.trimmed(commandField.text);
        if (page.validateCommand(candidate) !== "") {
            return;
        }
        if (candidate !== page.cfg_command) {
            page.cfg_command = candidate;
        }
    }

    // Trimmed URL without a trailing slash, for the CLIProxyAPI base URL.
    function normalisedUrl(raw) {
        var s = page.trimmed(raw);
        while (s.length > 0 && s.charAt(s.length - 1) === "/") {
            s = s.substring(0, s.length - 1);
        }
        return s;
    }

    // Host part of an http(s) URL, without userinfo, port or brackets.
    function hostOf(raw) {
        var s = page.trimmed(raw).toLowerCase();
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

    // ------------------------------------------------------------------
    // The query layer: one child process per call
    // ------------------------------------------------------------------

    // The command whose answer is outstanding ("" = idle). A non-empty value is
    // also the single-flight guard: runQuery() aborts it before starting another.
    property string pendingQuery: ""
    property var queryOnSuccess: null
    property var queryOnFailure: null

    // The only process this page spawns. `connectedSources` holds exactly one
    // entry per query; the entry is removed - and a running child killed - the
    // moment the query settles, times out or the page is destroyed.
    P5Support.DataSource {
        id: querySource
        engine: "executable"
        connectedSources: []
        onNewData: function (sourceName, data) {
            page.handleQueryData(sourceName, data);
        }
    }

    // The data source has no timeout of its own: a CLI waiting on a lock would
    // otherwise leave the settings window spinning for ever.
    Timer {
        id: queryWatchdog
        interval: page.backendTimeoutMs + 2000
        repeat: false
        running: false
        onTriggered: page.failPendingQuery("the daemon did not answer in time")
    }

    // POSIX single-quote quoting: a literal ' becomes '\''. The JSON argument of
    // `query set-config` is the only thing this page ever quotes, and it is
    // wrapped so the shell passes it through as one word.
    function shellQuote(text) {
        return "'" + String(text).replace(/'/g, "'\\''") + "'";
    }

    // Run "<command> query <op>" with optional already-quoted arguments.
    // Single flight: starting a query abandons the previous one, exactly like
    // the request this page used to send.
    function runQuery(op, args, onSuccess, onFailure) {
        page.abortQuery();
        var target = page.command + " query " + op;
        if (args !== null && args !== undefined && String(args).length > 0) {
            target = target + " " + String(args);
        }
        page.pendingQuery = target;
        page.queryOnSuccess = onSuccess;
        page.queryOnFailure = onFailure;
        queryWatchdog.restart();
        try {
            querySource.connectSource(target);
        } catch (e) {
            page.failPendingQuery("cannot run " + target);
        }
    }

    // Forget the outstanding query and disconnect its source (killing a child
    // that is still running). The callbacks are dropped: nothing may fire into a
    // page that has moved on.
    function abortQuery() {
        var target = page.pendingQuery;
        page.pendingQuery = "";
        page.queryOnSuccess = null;
        page.queryOnFailure = null;
        queryWatchdog.stop();
        if (target === "") {
            return;
        }
        try {
            querySource.disconnectSource(target);
        } catch (e) {
            // already disconnected
        }
    }

    function failPendingQuery(message) {
        var failure = page.queryOnFailure;
        page.abortQuery();
        if (failure) {
            failure(message);
        }
    }

    function succeedPendingQuery(document) {
        var success = page.queryOnSuccess;
        page.abortQuery();
        if (success) {
            success(document);
        }
    }

    // The data source answered. `data` is the map Plasma hands over:
    // {"stdout": ..., "stderr": ..., "exit code": ...}. An answer for a source
    // this page is no longer waiting on is ignored.
    function handleQueryData(sourceName, data) {
        if (page.pendingQuery === "" || String(sourceName) !== page.pendingQuery) {
            return;
        }
        var payload = data;
        if (!payload || typeof payload !== "object") {
            // Some engines deliver the map only through `data[source]`.
            try {
                payload = querySource.data[sourceName];
            } catch (e) {
                payload = null;
            }
        }
        if (!payload || typeof payload !== "object") {
            page.failPendingQuery("the daemon returned an unreadable answer");
            return;
        }

        var exitCode = Number(payload["exit code"]);
        if (!isFinite(exitCode)) {
            exitCode = 0;
        }
        var body = (typeof payload["stdout"] === "string") ? payload["stdout"] : "";
        var stderr = (typeof payload["stderr"] === "string") ? payload["stderr"] : "";

        if (exitCode !== 0) {
            var detail = page.firstLine(stderr);
            page.failPendingQuery(detail !== ""
                ? detail
                : ("sarvam-meter exited with status " + exitCode));
            return;
        }
        if (body.length === 0) {
            page.failPendingQuery("the daemon returned nothing - is sarvam-meter running?");
            return;
        }
        // Never hand a huge string to JSON.parse on the GUI thread: that is the
        // one way this page could stall plasmashell.
        if (body.length > page.maxBackendResponseChars) {
            page.failPendingQuery("the daemon's answer is too large to read safely");
            return;
        }

        var parsed = null;
        try {
            parsed = JSON.parse(body);
        } catch (e) {
            parsed = null;
        }
        if (!parsed || typeof parsed !== "object") {
            page.failPendingQuery("the daemon answered with something that is not JSON");
            return;
        }
        page.succeedPendingQuery(parsed);
    }

    // The first line of a child's stderr, bounded and stripped of control
    // characters. It is the CLI's own message ("sarvam-meter: ..."), which the
    // daemon builds without ever quoting a stored value.
    function firstLine(text) {
        var line = String(text).replace(/\r/g, "\n");
        var cut = line.indexOf("\n");
        if (cut >= 0) {
            line = line.substring(0, cut);
        }
        line = line.replace(/[\u0000-\u001f]/g, "").trim();
        if (line.length > 300) {
            line = line.substring(0, 300) + "...";
        }
        return line;
    }

    // Async query of the daemon's health. Never synchronous, never blocking.
    function testConnection() {
        var error = page.validateCommand(commandField.text);
        if (error !== "") {
            page.testState = "fail";
            page.testMessage = "FAIL: " + error;
            return;
        }

        page.testState = "pending";
        page.testMessage = "Running " + page.command + " query health ...";

        page.runQuery("health", "", function (document) {
            var version = "";
            if (document && document.version !== undefined && document.version !== null) {
                version = String(document.version);
            }
            page.testState = "ok";
            page.testMessage = version !== ""
                ? "OK: daemon " + version + " is answering."
                : "OK: the daemon answered (no version reported).";
        }, function (message) {
            page.testState = "fail";
            page.testMessage = "FAIL: " + message;
        });
    }

    // ------------------------------------------------------------------
    // Backend group: reading and writing the daemon's own config
    // ------------------------------------------------------------------

    function validateHttpUrl(raw, what) {
        var s = page.trimmed(raw);
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
        if (page.hostOf(s) === "") {
            return what + " has no host name.";
        }
        return "";
    }

    function cliproxyUrlError() {
        if (typeof cliproxyUrlField === "undefined" || !cliproxyUrlField) {
            return "";
        }
        return page.validateHttpUrl(cliproxyUrlField.text, "the CLIProxyAPI base URL");
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
            || page.normalisedUrl(cliproxyUrlField.text) !== page.loadedCliproxyBaseUrl;
    }

    // ------------------------------------------------------------------
    // Dashboard session (unit D)
    // ------------------------------------------------------------------

    // The header names the daemon says it holds, filtered and bounded: the
    // answer comes off a child process and nothing about its shape is
    // guaranteed.
    function sanitiseHeaderNames(raw) {
        if (!raw || typeof raw !== "object" || typeof raw.length !== "number") {
            return [];
        }
        var names = [];
        var limit = Math.min(raw.length, 4);
        for (var i = 0; i < limit; i++) {
            var name = raw[i];
            if (typeof name !== "string" || name.length === 0 || name.length > 40) {
                continue;
            }
            if (!/^[A-Za-z][A-Za-z0-9-]*$/.test(name)) {
                continue;
            }
            names.push(name.toLowerCase());
        }
        return names;
    }

    function applySessionDocument(doc) {
        if (!doc || typeof doc !== "object") {
            page.sessionState = "error";
            page.sessionMessage = "Cannot read the session - the daemon did not answer with a session document.";
            return;
        }
        page.sessionConfigured = doc.configured === true;
        page.sessionHeaderNames = page.sanitiseHeaderNames(doc.headers);
        page.sessionState = "ready";
        page.sessionMessage = "";
    }

    // "session captured (cookie)" / "no session captured yet". Counts and header
    // names only - the daemon never hands out a value and this page could not
    // show one if it did.
    function sessionStatusLabel() {
        if (page.sessionState === "loading") {
            return "reading the session ...";
        }
        if (page.sessionState === "error") {
            return page.sessionMessage;
        }
        if (page.sessionState === "ready") {
            if (!page.sessionConfigured) {
                return "no session captured yet";
            }
            var names = page.sessionHeaderNames.join(", ");
            return names === "" ? "session captured" : "session captured (" + names + ")";
        }
        return "not read yet.";
    }

    function sessionFeedbackColor() {
        if (page.sessionFeedbackKind === "ok") {
            return Kirigami.Theme.positiveTextColor;
        }
        if (page.sessionFeedbackKind === "fail") {
            return Kirigami.Theme.negativeTextColor;
        }
        return Kirigami.Theme.disabledTextColor;
    }

    // `query session`, then `query config`. The config read is chained here
    // because runQuery() is single-flight: starting the second query abandons
    // the first, so they must not overlap.
    function loadSessionStatus() {
        page.sessionState = "loading";
        page.sessionMessage = "";

        page.runQuery("session", "", function (doc) {
            page.applySessionDocument(doc);
            page.loadBackendConfig();
        }, function (message) {
            page.sessionState = "error";
            page.sessionMessage = "Cannot read the session - " + message;
            page.loadBackendConfig();
        });
    }

    // Shown in place of the sheet when the Loader cannot load it. QtWebEngine is
    // the overwhelmingly likely cause: it is an optional package on every
    // distribution and is absent from most KDE installs.
    function sessionUnavailableText() {
        return "The sign-in sheet could not be loaded. Either the QtWebEngine QML module is missing (it is not part of a standard KDE install) or the sheet itself failed to load. "
            + "On Fedora, install it with: sudo dnf install qt6-qtwebengine - then reopen this page. On Debian and Ubuntu the package is qml6-module-qtwebengine. "
            + "Nothing else here depends on it: the rest of this page and the widget keep working. "
            + "To capture the session without a web engine, sign in to indus.sarvam.ai in your browser, copy the Cookie header value out of the developer tools into a file only you can read, and run: sarvam-meter session set --cookie-file PATH (see docs/dashboard-scrape.md).";
    }

    function openSessionCapture() {
        page.sessionFeedback = "";
        page.sessionFeedbackKind = "none";
        page.sessionLoadFailed = false;
        page.sessionSheetActive = true;
        // Created here, on demand, and nowhere else. This is the only place the
        // page can fail on a missing QtWebEngine, and it fails inside the Loader.
        sessionSheetLoader.active = true;
    }

    function closeSessionCapture() {
        page.sessionSheetActive = false;
        page.sessionLoadFailed = false;
        // Destroying the item drops the WebEngine profile's in-memory cookies.
        sessionSheetLoader.active = false;
    }

    // Loader.Error: the module is missing or the sheet is broken. Do not retry -
    // a missing import is not transient - and keep the page usable.
    function noteSessionSheetError() {
        page.sessionLoadFailed = true;
        sessionSheetLoader.active = false;
    }

    // The sheet's stored(): it delivered the captured headers to the daemon over
    // D-Bus itself, so this page has nothing to send - only to re-read the state
    // the daemon now reports.
    function handleSessionStored() {
        page.sessionFeedback = "OK: the session was handed to the daemon over D-Bus.";
        page.sessionFeedbackKind = "ok";
        page.loadSessionStatus();
    }

    // The sheet's deliveryFailed(message): busctl and gdbus both failed, or the
    // daemon rejected the call. The message is the sheet's own and never carries
    // a cookie value.
    function handleSessionDeliveryFailed(message) {
        page.sessionFeedback = "FAIL: " + message;
        page.sessionFeedbackKind = "fail";
    }

    // `query config`: the one read that populates the group.
    function loadBackendConfig() {
        page.backendState = "loading";
        page.backendMessage = "Reading the backend settings with " + page.command + " query config ...";

        page.runQuery("config", "", function (doc) {
            page.applyBackendDocument(doc);
        }, function (message) {
            page.backendState = "error";
            page.backendMessage = "Cannot read the backend settings - " + message;
        });
    }

    // Copy a sanitised config document into the controls and into the diff
    // baseline. Every field is read defensively: the document comes out of a
    // child process and nothing about its shape is guaranteed here.
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
            ? page.normalisedUrl(cliproxy.base_url)
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
            var cliproxyUrl = page.normalisedUrl(cliproxyUrlField.text);
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
    // changed subset as one quoted JSON argument, then re-reads the document so
    // the form shows what the daemon actually stored (it sanitises, clamps and
    // redacts).
    function saveBackendConfig() {
        if (!page.backendLoaded) {
            page.backendFeedback = "FAIL: nothing has been read from the daemon yet.";
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

        var json = null;
        try {
            json = JSON.stringify(subset);
        } catch (e) {
            json = null;
        }
        if (json === null) {
            page.backendFeedback = "FAIL: cannot serialise the settings to send.";
            page.backendFeedbackKind = "fail";
            return;
        }

        page.backendState = "saving";
        page.backendMessage = "";
        page.backendFeedback = "Saving with " + page.command + " query set-config ...";
        page.backendFeedbackKind = "info";

        page.runQuery("set-config", page.shellQuote(json), function (doc) {
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
            id: commandField
            Kirigami.FormData.label: "Command:"
            placeholderText: "sarvam-meter"
            text: page.cfg_command
            Layout.minimumWidth: Kirigami.Units.gridUnit * 18
            Layout.maximumWidth: Kirigami.Units.gridUnit * 26
            onTextChanged: page.syncCommand()
            onEditingFinished: page.syncCommand()
        }

        PlasmaComponents.Label {
            text: "The program the widget and this page run to reach the daemon: \"<command> query status\", \"query refresh\", \"query config\", \"query set-config\" and \"query session\". Use the bare program name when sarvam-meter is on PATH, or an absolute path to it. It is executed, so spaces, quotes and shell metacharacters are refused."
            color: Kirigami.Theme.disabledTextColor
            wrapMode: Text.Wrap
            Layout.maximumWidth: Kirigami.Units.gridUnit * 26
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
            visible: page.commandInvalid && commandField.text.length > 0
            text: "Not saved - " + page.commandErrorText
            color: Kirigami.Theme.negativeTextColor
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
            text: "These values belong to the sarvam-meter daemon and live in its own config file. The page reads them by running the command above with \"query config\", and the button below writes them with \"query set-config\" and a JSON argument. They are not widget settings: the dialog's Apply button does not touch them."
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

        RowLayout {
            Kirigami.FormData.label: "Dashboard session:"
            spacing: Kirigami.Units.smallSpacing

            PlasmaComponents.Button {
                text: "Capture session..."
                icon.name: "dialog-password"
                enabled: !page.backendBusy
                onClicked: page.openSessionCapture()
            }

            PlasmaComponents.Label {
                text: page.sessionStatusLabel()
                color: page.sessionState === "error" ? Kirigami.Theme.negativeTextColor
                     : (page.sessionState === "ready" && page.sessionConfigured) ? Kirigami.Theme.positiveTextColor
                     : Kirigami.Theme.disabledTextColor
                wrapMode: Text.Wrap
                Layout.fillWidth: true
                Layout.maximumWidth: Kirigami.Units.gridUnit * 22
            }
        }

        PlasmaComponents.Label {
            text: "The dashboard needs a signed-in session. Capture it once here, or from a terminal with sarvam-meter session set --cookie-file PATH. The daemon keeps it in a file only your user can read and never shows the value again; this page only ever reports whether a session is stored and which headers it holds."
            color: Kirigami.Theme.disabledTextColor
            wrapMode: Text.Wrap
            Layout.maximumWidth: Kirigami.Units.gridUnit * 26
        }

        // The sheet lives behind this Loader and nowhere else. It stays inactive
        // - and therefore unloaded, and therefore harmless on a system without
        // QtWebEngine - until the button above asks for it.
        ColumnLayout {
            id: sessionSheetHost
            Kirigami.FormData.label: ""
            visible: page.sessionSheetActive
            Layout.fillWidth: true
            Layout.maximumWidth: Kirigami.Units.gridUnit * 30
            spacing: Kirigami.Units.smallSpacing

            Loader {
                id: sessionSheetLoader
                Layout.fillWidth: true
                Layout.preferredHeight: (page.sessionSheetActive && status === Loader.Ready)
                    ? Kirigami.Units.gridUnit * 30 : 0
                active: false
                // Resolved against this page's own URL, i.e. contents/config/,
                // next to configGeneral.qml.
                source: "SessionCapture.qml"

                onLoaded: {
                    if (!item) {
                        page.noteSessionSheetError();
                        return;
                    }
                    item.width = Qt.binding(function () { return sessionSheetLoader.width; });
                    item.height = Qt.binding(function () { return sessionSheetLoader.height; });
                    try {
                        item.stored.connect(page.handleSessionStored);
                        item.deliveryFailed.connect(page.handleSessionDeliveryFailed);
                        item.closeRequested.connect(page.closeSessionCapture);
                    } catch (e) {
                        // The file loaded but is not the sheet we expect: treat it
                        // exactly like a missing module rather than half-using it.
                        page.noteSessionSheetError();
                    }
                }

                onStatusChanged: {
                    if (status === Loader.Error) {
                        page.noteSessionSheetError();
                    }
                }
            }

            PlasmaComponents.Label {
                visible: page.sessionLoadFailed
                text: page.sessionUnavailableText()
                color: Kirigami.Theme.negativeTextColor
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }

            PlasmaComponents.Label {
                visible: page.sessionSheetActive && !page.sessionLoadFailed
                    && sessionSheetLoader.status === Loader.Loading
                text: "Opening the sign-in window ..."
                color: Kirigami.Theme.disabledTextColor
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }

            RowLayout {
                PlasmaComponents.Button {
                    text: "Close"
                    icon.name: "dialog-close"
                    onClicked: page.closeSessionCapture()
                }

                Item {
                    Layout.fillWidth: true
                }
            }
        }

        PlasmaComponents.Label {
            visible: page.sessionFeedback !== ""
            text: page.sessionFeedback
            color: page.sessionFeedbackColor()
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
