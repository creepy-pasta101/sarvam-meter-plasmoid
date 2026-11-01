// SPDX-License-Identifier: MIT
//
// Sarvam Meter - session capture sheet (unit D).
//
// WHAT THIS IS
// A self-contained sheet that signs the user in to the Sarvam dashboard in an
// embedded browser and hands the resulting session headers to the daemon. It
// replaces the "copy the Cookie header out of DevTools by hand" step described
// in docs/dashboard-scrape.md.
//
// HOW THE COOKIE REACHES THE DAEMON (unit B2): D-BUS, NOT HTTP
// The daemon no longer speaks HTTP, and it does not want the cookie written to
// its Unix socket either, so the sheet calls the daemon's D-Bus service:
//
//   busctl --user call org.kde.sarvam.Meter /org/kde/sarvam/Meter \
//          org.kde.sarvam.Meter SetSession ss '<cookie>' '<authorization>'
//
// with `gdbus call --session --dest ... --method ...SetSession` as the fallback
// when busctl is not installed. The service (sarvam_meter/dbus_service.py)
// publishes those two arguments to the same op handler the socket uses, which
// validates and writes them to the daemon's 0600 session file.
//
// BE HONEST ABOUT THE TRADE-OFF
// busctl/gdbus take the cookie as a *command-line argument*, so for the duration
// of that one call the value is visible in that child's argv (/proc/<pid>/cmdline)
// to the same user and to root - and only for that call, since the process exits
// as soon as the daemon answers. That is why the D-Bus path is used instead of
// `sarvam-meter query set-session --cookie ...` over the socket: that would put
// the same value in a *second* process's argv (the CLI's) for just as long, plus
// a copy in this sheet's own string, and it would make the CLI a second writer
// of the session file. The D-Bus call goes straight from this sheet to the
// daemon that already owns the file. A user who wants the cookie never to appear
// in any process's argv at all can bypass this sheet entirely and run
// `sarvam-meter session set --cookie-file PATH`, which reads it from a file that
// only they can read.
//
// The value is never put into a label, a tooltip, a status line, a console
// message or an error string: the sheet reports counts and words only, and the
// D-Bus failure message carries the exit status, never the child's output.
//
// THIS IS THE ONLY FILE IN THE PACKAGE THAT IMPORTS QtWebEngine.
// QtWebEngine is not part of a standard KDE install, and an unresolvable QML
// import is fatal for the component that contains it. configGeneral.qml
// therefore loads this file through a Loader that is only activated when the
// user asks for it, so a system without QtWebEngine loses the sheet and nothing
// else. Never import QtWebEngine anywhere else, and never reference this file
// from ui/ - the widget must keep working on a machine that has no web engine.
//
// THE COOKIE STORE, NOT document.cookie
// The dashboard's session cookie is HttpOnly, which by definition is invisible
// to page JavaScript: a `document.cookie` read returns the non-HttpOnly
// leftovers and silently produces a session that cannot authenticate. The only
// complete source is the engine's own cookie store,
// WebEngineProfile.defaultProfile.cookieStore, which is why this sheet connects
// `cookieAdded`, calls `loadAllCookies()` and accumulates what arrives. That is
// the whole reason this file exists rather than a four-line JavaScript snippet.
//
// NOTHING HERE IS EVER DISPLAYED OR LOGGED
// No cookie value and no authorization value is put into a label, a tooltip, a
// status line, a console message or an error string. The sheet reports counts
// and words only ("3 cookies collected from sarvam.ai"). The values live in a
// private JS object and are cleared the moment they are handed over through the
// `captured` signal.
//
// Plasma 6.7 / 6.8: Kirigami.Units and Plasma 6 components only, no
// PlasmaCore.Units, no PlasmaComponents2, no synchronous work on the GUI thread.

import QtQuick
import QtQuick.Layouts

// The one and only QtWebEngine import in this package. See the header.
import QtWebEngine

import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents
// Plasma's data source, used to run the D-Bus client (see the header).
import org.kde.plasma.plasma5support as P5Support

Item {
    id: sheet

    // The daemon accepted the session over D-Bus. The settings page only has to
    // re-read the state the daemon now reports; it never sees a value.
    signal stored()

    // The D-Bus delivery failed (no service, no bus, a rejected call). The
    // message is built here and carries the exit status only - never a cookie,
    // an authorization value or a child's output.
    signal deliveryFailed(string message)

    // The user dismissed the sheet. The page destroys the Loader, which drops
    // the WebEngine profile's in-memory cookies with it.
    signal closeRequested()

    // ---- read-only state the page may display (counts and words only) ----
    property int cookieCount: 0
    property bool signedIn: false
    property bool collecting: false
    property bool busy: false
    property string statusText: "Sign in to the Sarvam dashboard below."
    property string statusKind: "info"          // info | ok | fail
    property bool cookieStoreAttached: false

    readonly property string loginUrl: "https://indus.sarvam.ai/login"
    readonly property int settleDelayMs: 700        // quiet period after the last cookie
    readonly property int collectDeadlineMs: 5000   // hard stop for loadAllCookies()
    readonly property int autoCollectDelayMs: 1200  // let the redirect chain settle first
    readonly property int authProbeDeadlineMs: 2500 // never leave the user waiting on a probe
    readonly property int maxHeaderChars: 8192
    readonly property int maxCookieCount: 64

    // name -> value, held only between collection and hand-over. Never bound to
    // anything, never shown.
    property var cookieMap: ({})
    property string pendingCookieHeader: ""
    property bool captureSettled: true

    // ------------------------------------------------------------------
    // D-Bus delivery (unit B2)
    //
    // The captured headers go to the daemon's org.kde.sarvam.Meter service, not
    // to an HTTP endpoint and not down the daemon's socket. See the header for
    // why, and for the argv trade-off that comes with it.
    // ------------------------------------------------------------------

    readonly property string busName: "org.kde.sarvam.Meter"
    readonly property string busPath: "/org/kde/sarvam/Meter"
    readonly property string busInterface: "org.kde.sarvam.Meter"
    readonly property int deliveryTimeoutMs: 8000

    // The values live here only between capture and the moment the D-Bus call
    // settles, and are cleared immediately afterwards. They are never bound to
    // anything that is displayed.
    property string pendingCookie: ""
    property string pendingAuthorization: ""
    property string pendingDelivery: ""
    property bool delivering: false
    // 0 = busctl, 1 = gdbus (the fallback).
    property int deliveryAttempt: 0

    P5Support.DataSource {
        id: execSource
        engine: "executable"
        connectedSources: []
        onNewData: function (sourceName, data) {
            sheet.handleDeliveryData(sourceName, data);
        }
    }

    // A busctl that hangs (a wedged daemon, a bus that never answers) must not
    // leave the sheet spinning: give up, kill the child, report a failure.
    Timer {
        id: deliveryWatchdog
        interval: sheet.deliveryTimeoutMs
        repeat: false
        running: false
        onTriggered: sheet.failDelivery("the D-Bus call did not finish in time")
    }

    // POSIX single-quote quoting: a literal ' becomes '\''. The cookie and the
    // authorization are the only user-supplied strings this sheet ever quotes,
    // and they are always wrapped, so neither can end the argument or start a
    // new command.
    function shellQuote(text) {
        return "'" + String(text).replace(/'/g, "'\\''") + "'";
    }

    function busctlCommand(cookie, authorization) {
        return "busctl --user call " + sheet.busName + " " + sheet.busPath
            + " " + sheet.busInterface + " SetSession ss "
            + sheet.shellQuote(cookie) + " " + sheet.shellQuote(authorization);
    }

    function gdbusCommand(cookie, authorization) {
        return "gdbus call --session --dest " + sheet.busName
            + " --object-path " + sheet.busPath
            + " --method " + sheet.busInterface + ".SetSession "
            + sheet.shellQuote(cookie) + " " + sheet.shellQuote(authorization);
    }

    // Hand the two headers to the daemon. Both are secret from here on: they are
    // used to build one command line and are wiped the moment it settles.
    function startDelivery(cookieHeader, authorizationHeader) {
        sheet.pendingCookie = cookieHeader;
        sheet.pendingAuthorization = authorizationHeader;
        sheet.deliveryAttempt = 0;
        sheet.delivering = true;
        sheet.busy = true;
        sheet.statusKind = "info";
        sheet.statusText = "Handing the session to the daemon over D-Bus ...";
        sheet.runDelivery();
    }

    function runDelivery() {
        var target = (sheet.deliveryAttempt === 0)
            ? sheet.busctlCommand(sheet.pendingCookie, sheet.pendingAuthorization)
            : sheet.gdbusCommand(sheet.pendingCookie, sheet.pendingAuthorization);
        sheet.pendingDelivery = target;
        deliveryWatchdog.restart();
        try {
            execSource.connectSource(target);
        } catch (e) {
            sheet.failDelivery(sheet.deliveryAttempt === 0
                ? "cannot run busctl" : "cannot run gdbus");
        }
    }

    function handleDeliveryData(sourceName, data) {
        if (!sheet.delivering || String(sourceName) !== sheet.pendingDelivery) {
            return;
        }
        var payload = data;
        if (!payload || typeof payload !== "object") {
            try {
                payload = execSource.data[sourceName];
            } catch (e) {
                payload = null;
            }
        }
        var exitCode = payload ? Number(payload["exit code"]) : 0;
        if (!isFinite(exitCode)) {
            exitCode = 0;
        }
        sheet.stopDelivery();
        if (exitCode === 0) {
            sheet.completeDelivery();
            return;
        }
        if (sheet.deliveryAttempt === 0) {
            // busctl missing, or the daemon refused: try gdbus once before
            // reporting a failure.
            sheet.deliveryAttempt = 1;
            sheet.runDelivery();
            return;
        }
        sheet.failDelivery("the daemon did not accept the session over D-Bus "
            + "(busctl and gdbus both failed). Is sarvam-meter running?");
    }

    // Disconnect the source (killing a child that is still running) without
    // touching the pending values: the gdbus fallback still needs them.
    function stopDelivery() {
        var target = sheet.pendingDelivery;
        sheet.pendingDelivery = "";
        deliveryWatchdog.stop();
        if (target === "") {
            return;
        }
        try {
            execSource.disconnectSource(target);
        } catch (e) {
            // already disconnected
        }
    }

    function clearSecret() {
        sheet.pendingCookie = "";
        sheet.pendingAuthorization = "";
    }

    function completeDelivery() {
        sheet.delivering = false;
        sheet.clearSecret();
        sheet.busy = false;
        sheet.statusKind = "ok";
        sheet.statusText = "The session was handed to the daemon over D-Bus.";
        sheet.stored();
    }

    function failDelivery(message) {
        sheet.stopDelivery();
        sheet.delivering = false;
        sheet.clearSecret();
        sheet.busy = false;
        sheet.statusKind = "fail";
        sheet.statusText = message;
        sheet.deliveryFailed(message);
    }

    // ------------------------------------------------------------------
    // Cookie collection
    // ------------------------------------------------------------------

    Component.onCompleted: sheet.attachCookieStore()

    // The profile outlives this sheet, so the connection must not. Any D-Bus
    // call still in flight is dropped - and its child killed - here too.
    Component.onDestruction: {
        sheet.stopDelivery();
        sheet.delivering = false;
        sheet.clearSecret();
        sheet.detachCookieStore();
    }

    // One connection, made once. cookieAdded fires for every cookie the engine
    // adds from then on; loadAllCookies() replays the ones already stored.
    function attachCookieStore() {
        if (sheet.cookieStoreAttached) {
            return;
        }
        try {
            var profile = WebEngineProfile.defaultProfile;
            if (!profile || !profile.cookieStore) {
                return;
            }
            profile.cookieStore.cookieAdded.connect(sheet.onCookieAdded);
            sheet.cookieStoreAttached = true;
        } catch (e) {
            sheet.cookieStoreAttached = false;
        }
    }

    function detachCookieStore() {
        if (!sheet.cookieStoreAttached) {
            return;
        }
        try {
            WebEngineProfile.defaultProfile.cookieStore.cookieAdded.disconnect(
                sheet.onCookieAdded);
        } catch (e) {
            // already gone
        }
        sheet.cookieStoreAttached = false;
    }

    function countKeys(map) {
        var total = 0;
        for (var key in map) {
            if (map.hasOwnProperty(key)) {
                total = total + 1;
            }
        }
        return total;
    }

    // A cookie belongs to this sign-in when its domain is sarvam.ai or a real
    // subdomain of it. The dot check keeps a look-alike such as
    // "evil-sarvam.ai" out.
    function domainAccepted(rawDomain) {
        var domain = String(rawDomain).toLowerCase();
        while (domain.length > 0 && domain.charAt(0) === ".") {
            domain = domain.substring(1);
        }
        if (domain === "sarvam.ai") {
            return true;
        }
        if (domain.length > 9 && domain.substring(domain.length - 9) === "sarvam.ai") {
            return domain.charAt(domain.length - 10) === ".";
        }
        return false;
    }

    // Session cookies carry no usable expiry and must be kept; a cookie with a
    // real expiry in the past is dead weight and must not be sent.
    function cookieExpired(cookie) {
        var when = null;
        try {
            when = cookie.expirationDate;
        } catch (e) {
            when = null;
        }
        if (!when) {
            return false;
        }
        var ms = NaN;
        try {
            ms = (typeof when.getTime === "function") ? when.getTime() : Number(when);
        } catch (e) {
            ms = NaN;
        }
        if (!isFinite(ms) || ms <= 0) {
            return false;
        }
        return ms < Date.now();
    }

    function onCookieAdded(cookie) {
        if (!sheet.collecting || !cookie) {
            return;
        }
        var name = "";
        var value = "";
        var domain = "";
        try {
            name = String(cookie.name);
            value = String(cookie.value);
            domain = String(cookie.domain);
        } catch (e) {
            return;
        }
        if (name === "" || name.length > 128) {
            return;
        }
        if (!sheet.domainAccepted(domain)) {
            return;
        }
        // Cloudflare bot-management noise, not part of the session: sending it
        // would only bind the stored session to this machine's challenge.
        if (name === "__cf_bm") {
            return;
        }
        if (sheet.cookieExpired(cookie)) {
            return;
        }
        var map = sheet.cookieMap;
        if (!map.hasOwnProperty(name) && sheet.countKeys(map) >= sheet.maxCookieCount) {
            return;
        }
        map[name] = value;
        sheet.cookieMap = map;
        sheet.cookieCount = sheet.countKeys(map);
        // Debounce: wait for the store to go quiet rather than guessing a delay.
        cookieSettleTimer.restart();
    }

    function collectCookies() {
        sheet.attachCookieStore();
        if (!sheet.cookieStoreAttached) {
            sheet.collecting = false;
            sheet.busy = false;
            sheet.statusKind = "fail";
            sheet.statusText = "The browser cookie store is not reachable, so nothing can be collected automatically. Use the manual entry below.";
            return;
        }

        sheet.cookieMap = ({});
        sheet.cookieCount = 0;
        sheet.collecting = true;
        sheet.busy = true;
        sheet.statusKind = "info";
        sheet.statusText = "Collecting cookies ...";

        cookieSettleTimer.restart();
        cookieDeadlineTimer.restart();

        try {
            WebEngineProfile.defaultProfile.cookieStore.loadAllCookies();
        } catch (e) {
            cookieSettleTimer.stop();
            cookieDeadlineTimer.stop();
            sheet.collecting = false;
            sheet.busy = false;
            sheet.statusKind = "fail";
            sheet.statusText = "Reading the cookie store failed. Use the manual entry below.";
        }
    }

    // Stable order (sorted by name) so the same sign-in always produces the same
    // header, which makes a stored session diffable without ever printing it.
    function buildCookieHeader() {
        var names = [];
        for (var key in sheet.cookieMap) {
            if (sheet.cookieMap.hasOwnProperty(key)) {
                names.push(key);
            }
        }
        names.sort();
        var parts = [];
        for (var i = 0; i < names.length; i++) {
            parts.push(names[i] + "=" + sheet.cookieMap[names[i]]);
        }
        return parts.join("; ");
    }

    function finalizeCookies() {
        if (!sheet.collecting) {
            return;
        }
        sheet.collecting = false;
        cookieSettleTimer.stop();
        cookieDeadlineTimer.stop();

        var count = sheet.countKeys(sheet.cookieMap);
        sheet.cookieCount = count;
        if (count === 0) {
            sheet.busy = false;
            sheet.statusKind = "fail";
            sheet.statusText = "No sarvam.ai cookies were found in the sign-in window. Finish signing in, or use the manual entry below.";
            sheet.cookieMap = ({});
            return;
        }

        var header = sheet.buildCookieHeader();
        if (header.length > sheet.maxHeaderChars) {
            sheet.cookieMap = ({});
            sheet.busy = false;
            sheet.statusKind = "fail";
            sheet.statusText = "The collected cookies are too large to send. Use the manual entry below.";
            return;
        }

        sheet.statusKind = "ok";
        sheet.statusText = count + (count === 1
            ? " cookie collected from sarvam.ai"
            : " cookies collected from sarvam.ai");
        sheet.signedIn = true;
        sheet.finishCapture(header);
    }

    // ------------------------------------------------------------------
    // The authorization header
    //
    // The dashboard keeps its bearer token in the page's storage, not in a
    // cookie, and there is no engine-level hook for a request header the way
    // there is cookieAdded. Two honest sources: a value the user pasted into the
    // manual field, and a best-effort read of the well-known storage keys below.
    // A miss is not an error - the daemon accepts a cookie-only session.
    // ------------------------------------------------------------------

    function tokenProbeScript() {
        return [
            "(function(){",
            "  try {",
            "    var keys = ['access_token','accessToken','auth_token','authToken','id_token','token'];",
            "    var stores = [window.localStorage, window.sessionStorage];",
            "    for (var s = 0; s < stores.length; s++) {",
            "      var store = stores[s];",
            "      if (!store) { continue; }",
            "      for (var i = 0; i < keys.length; i++) {",
            "        var value = null;",
            "        try { value = store.getItem(keys[i]); } catch (e) { value = null; }",
            "        if (typeof value === 'string' && value.length > 0 && value.length < 4096) {",
            "          return value;",
            "        }",
            "      }",
            "    }",
            "  } catch (e) {}",
            "  return '';",
            "})()"
        ].join("\n");
    }

    function finishCapture(cookieHeader) {
        sheet.pendingCookieHeader = cookieHeader;
        sheet.captureSettled = false;

        var manual = "";
        try {
            manual = String(manualAuthField.text).replace(/^\s+/, "").replace(/\s+$/, "");
        } catch (e) {
            manual = "";
        }
        if (manual !== "") {
            sheet.emitCapture(manual);
            return;
        }

        authProbeTimer.restart();
        try {
            webView.runJavaScript(sheet.tokenProbeScript(), function (result) {
                sheet.onTokenProbed(result);
            });
        } catch (e) {
            sheet.onTokenProbed("");
        }
    }

    function onTokenProbed(result) {
        if (sheet.captureSettled) {
            return;
        }
        var token = "";
        try {
            if (typeof result === "string") {
                token = result;
            } else if (result !== null && result !== undefined) {
                token = String(result);
            }
        } catch (e) {
            token = "";
        }
        token = token.replace(/^\s+/, "").replace(/\s+$/, "");
        if (token.length === 0 || token.length > 4096) {
            token = "";
        } else if (!/^Bearer\s/i.test(token)) {
            token = "Bearer " + token;
        }
        sheet.emitCapture(token);
    }

    function emitCapture(authorizationHeader) {
        if (sheet.captureSettled) {
            return;
        }
        sheet.captureSettled = true;
        authProbeTimer.stop();
        cookieSettleTimer.stop();
        cookieDeadlineTimer.stop();
        sheet.collecting = false;
        sheet.busy = false;

        var header = sheet.pendingCookieHeader;
        // Clear before handing over: the sheet keeps the values for exactly as
        // long as it needs them and not a moment longer.
        sheet.pendingCookieHeader = "";
        sheet.cookieMap = ({});

        if (header === "") {
            sheet.statusKind = "fail";
            sheet.statusText = "Nothing was collected. Use the manual entry below.";
            return;
        }
        // The values do not go to the settings page: they go straight to the
        // daemon's D-Bus service, and this sheet reports only the outcome.
        sheet.startDelivery(header, authorizationHeader);
    }

    // ---- manual fallback, usable when cookie extraction fails ----

    function useManualHeaders() {
        var cookie = "";
        var auth = "";
        try {
            cookie = String(manualCookieField.text).replace(/^\s+/, "").replace(/\s+$/, "");
            auth = String(manualAuthField.text).replace(/^\s+/, "").replace(/\s+$/, "");
        } catch (e) {
            cookie = "";
            auth = "";
        }
        if (cookie === "") {
            sheet.statusKind = "fail";
            sheet.statusText = "Paste the Cookie header value first.";
            return;
        }
        if (cookie.indexOf("=") < 0) {
            sheet.statusKind = "fail";
            sheet.statusText = "That does not look like a Cookie header: it has no name=value pair.";
            return;
        }
        if (cookie.length > sheet.maxHeaderChars || auth.length > sheet.maxHeaderChars) {
            sheet.statusKind = "fail";
            sheet.statusText = "That header is too long to send.";
            return;
        }

        cookieSettleTimer.stop();
        cookieDeadlineTimer.stop();
        sheet.collecting = false;
        sheet.cookieMap = ({});
        sheet.cookieCount = 0;

        // Wipe the fields immediately: the value now lives in one place only.
        manualCookieField.text = "";
        manualAuthField.text = "";

        sheet.statusKind = "ok";
        sheet.statusText = "Handing the pasted headers to the settings page ...";
        sheet.pendingCookieHeader = cookie;
        sheet.captureSettled = false;
        sheet.emitCapture(auth);
    }

    function clearCookies() {
        try {
            WebEngineProfile.defaultProfile.cookieStore.deleteAllCookies();
        } catch (e) {
            // nothing to clear
        }
        sheet.cookieMap = ({});
        sheet.cookieCount = 0;
        sheet.collecting = false;
        sheet.busy = false;
        cookieSettleTimer.stop();
        cookieDeadlineTimer.stop();
        sheet.statusKind = "info";
        sheet.statusText = "Cookies cleared from this window. Sign in again to capture a fresh session.";
    }

    // ------------------------------------------------------------------
    // Sign-in detection
    // ------------------------------------------------------------------

    function isLoginUrl(raw) {
        var url = String(raw).toLowerCase();
        var marker = url.indexOf("sarvam.ai");
        if (marker < 0) {
            return false;
        }
        var rest = url.substring(marker + 9);
        var cut = rest.indexOf("?");
        if (cut >= 0) {
            rest = rest.substring(0, cut);
        }
        cut = rest.indexOf("#");
        if (cut >= 0) {
            rest = rest.substring(0, cut);
        }
        return rest.indexOf("/login") === 0;
    }

    // The URL leaving /login means the app accepted the sign-in. Wait a moment
    // for the redirect chain to finish, then harvest - the cookies are usually
    // written by the response that follows the redirect.
    function noteLocation(raw) {
        var url = String(raw);
        if (url.indexOf("sarvam.ai") < 0) {
            return;
        }
        if (sheet.isLoginUrl(url)) {
            sheet.signedIn = false;
            return;
        }
        if (sheet.collecting || sheet.busy) {
            return;
        }
        if (sheet.signedIn && sheet.cookieCount > 0) {
            return;
        }
        sheet.signedIn = true;
        sheet.statusKind = "info";
        sheet.statusText = "Signed in. Collecting cookies ...";
        autoCollectTimer.restart();
    }

    function statusColor() {
        if (sheet.statusKind === "ok") {
            return Kirigami.Theme.positiveTextColor;
        }
        if (sheet.statusKind === "fail") {
            return Kirigami.Theme.negativeTextColor;
        }
        return Kirigami.Theme.disabledTextColor;
    }

    // ------------------------------------------------------------------
    // Timers. Every wait in this sheet is bounded: nothing here may hold the
    // GUI thread or leave the user staring at "Collecting cookies ...".
    // ------------------------------------------------------------------

    // The store has no "all cookies loaded" signal, so collection ends when the
    // additions go quiet ...
    Timer {
        id: cookieSettleTimer
        interval: sheet.settleDelayMs
        repeat: false
        onTriggered: sheet.finalizeCookies()
    }

    // ... and this is the hard stop for a store that never goes quiet.
    Timer {
        id: cookieDeadlineTimer
        interval: sheet.collectDeadlineMs
        repeat: false
        onTriggered: sheet.finalizeCookies()
    }

    Timer {
        id: autoCollectTimer
        interval: sheet.autoCollectDelayMs
        repeat: false
        onTriggered: sheet.collectCookies()
    }

    Timer {
        id: authProbeTimer
        interval: sheet.authProbeDeadlineMs
        repeat: false
        onTriggered: sheet.emitCapture("")
    }

    // ------------------------------------------------------------------
    // The sheet itself
    // ------------------------------------------------------------------

    ColumnLayout {
        anchors.fill: parent
        spacing: Kirigami.Units.smallSpacing

        PlasmaComponents.Label {
            text: "Sign in to the Sarvam dashboard in the window below. When the sign-in succeeds the sheet collects the session cookies and hands them to the daemon. Nothing is typed here except your Sarvam password, which goes to Sarvam and never to this widget."
            color: Kirigami.Theme.disabledTextColor
            wrapMode: Text.Wrap
            Layout.fillWidth: true
        }

        PlasmaComponents.Label {
            text: sheet.statusText
            color: sheet.statusColor()
            wrapMode: Text.Wrap
            Layout.fillWidth: true
        }

        RowLayout {
            spacing: Kirigami.Units.smallSpacing

            PlasmaComponents.Button {
                text: "I'm signed in"
                icon.name: "dialog-ok-apply"
                enabled: !sheet.collecting && !sheet.busy
                onClicked: sheet.collectCookies()
            }

            PlasmaComponents.Button {
                text: "Clear cookies"
                icon.name: "edit-clear"
                enabled: !sheet.collecting && !sheet.busy
                onClicked: sheet.clearCookies()
            }

            Item {
                Layout.fillWidth: true
            }

            PlasmaComponents.Button {
                text: "Close"
                icon.name: "dialog-close"
                onClicked: sheet.closeRequested()
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: Kirigami.Units.gridUnit * 16
            color: Kirigami.Theme.backgroundColor

            // The default profile is the engine's off-the-record profile, so the
            // sign-in cookies live in memory for the lifetime of this sheet and
            // are gone when the Loader is destroyed. Nothing is written to disk
            // by this window; the only stored copy is the daemon's 0600 file.
            WebEngineView {
                id: webView
                anchors.fill: parent
                url: sheet.loginUrl

                onUrlChanged: sheet.noteLocation(url)

                onLoadingChanged: function (loadRequest) {
                    if (loadRequest.status === WebEngineView.LoadSucceededStatus) {
                        sheet.noteLocation(loadRequest.url);
                    }
                }
            }
        }

        PlasmaComponents.Label {
            text: "If the cookies cannot be collected, paste the headers yourself. In the browser you normally use, sign in to indus.sarvam.ai, open the developer tools, take any request to that site and copy its Cookie header value. An Authorization header is optional."
            color: Kirigami.Theme.disabledTextColor
            wrapMode: Text.Wrap
            Layout.fillWidth: true
        }

        RowLayout {
            spacing: Kirigami.Units.smallSpacing

            PlasmaComponents.TextField {
                id: manualCookieField
                placeholderText: "Cookie header value"
                // A session cookie is a credential: it is never rendered back,
                // not even to the person who pasted it.
                echoMode: TextInput.Password
                Layout.fillWidth: true
            }

            PlasmaComponents.TextField {
                id: manualAuthField
                placeholderText: "Authorization header (optional)"
                echoMode: TextInput.Password
                Layout.fillWidth: true
            }

            PlasmaComponents.Button {
                text: "Use these"
                icon.name: "document-send"
                enabled: !sheet.busy
                onClicked: sheet.useManualHeaders()
            }
        }
    }
}
