// SPDX-License-Identifier: MIT
//
// Sarvam Meter - Plasma 6 applet root (SPEC.md sections 10 and 13).
//
// The applet runs `<command> query status` and keeps the parsed model here;
// CompactRepresentation.qml and FullRepresentation.qml read it through the
// `meter` property that this file assigns.
//
// HOW IT TALKS TO THE DAEMON (transport change, unit B2)
// The daemon no longer listens on a TCP port: it serves newline-delimited JSON
// on a Unix socket under $XDG_RUNTIME_DIR, and `sarvam-meter query <op>` is the
// client that prints the reply's `result` as JSON on stdout.  So the widget does
// not speak HTTP at all any more - there is no XMLHttpRequest anywhere in this
// package.  Instead it runs the CLI through Plasma's executable data source
// (org.kde.plasma.plasma5support, `engine: "executable"`), which starts the
// child process off the GUI thread and hands the answer back through the
// `newData` signal as a map of "stdout", "stderr" and "exit code".
//
// `command` is the program that is executed, and it is *executed*, so it is
// restricted to a bare program name or an absolute path - no spaces, no quotes,
// no shell metacharacters - and an unacceptable value falls back to the default
// instead of being run.
//
// Defensive-parsing contract: the payload may be missing keys, contain nulls or
// arrive malformed.  Every access below goes through a typeof/null guard, so a
// bad payload can never throw a TypeError into the QML engine.  That includes the
// schema-2 dashboard additions (SPEC.md sections 17.5 and 17.7): a schema-1
// payload from an older daemon carries no `sarvam.dashboard` and no
// `spend.source`, and both degrade silently to the ledger presentation instead of
// rendering "undefined".

import QtQuick
import org.kde.plasma.plasmoid
import org.kde.kirigami as Kirigami
import org.kde.plasma.plasma5support as P5Support

PlasmoidItem {
    id: root

    switchWidth: Kirigami.Units.gridUnit * 10
    switchHeight: Kirigami.Units.gridUnit * 10

    // ------------------------------------------------------------------
    // Configuration.  These are the only five keys the widget may read.
    // ------------------------------------------------------------------

    // The program this widget executes, resolved on PATH ("sarvam-meter") or an
    // absolute path to it.  This string ends up in a command line, so anything
    // that is not a bare program name or an absolute path is refused and the
    // default is used instead: no spaces, no quotes, no metacharacters, and no
    // leading dash (which would be read as an option).
    readonly property string defaultCommand: "sarvam-meter"

    readonly property string command: {
        var raw = Plasmoid.configuration.command;
        if (typeof raw === "undefined" || raw === null)
            return root.defaultCommand;
        return root.safeCommand(String(raw));
    }

    // "" when the string is safe to execute, otherwise the reason it is not.
    function commandError(raw) {
        var text = String(raw).trim();
        if (text.length === 0)
            return "the command is empty. Use sarvam-meter, or an absolute path to it.";
        if (text.length > 512)
            return "the command is too long.";
        // A whitelist, not a blacklist: letters, digits, dot, underscore,
        // dash, plus and slash.  Nothing else survives - in particular no
        // space, quote, backtick, $, ;, &, |, <, >, ( or ).
        if (!/^[A-Za-z0-9._\/+-]+$/.test(text))
            return "the command must be a bare program name or an absolute path: "
                + "no spaces, quotes or shell metacharacters.";
        if (text.charAt(0) === "-")
            return "the command must not start with a dash.";
        if (text.indexOf("/") > 0 && text.charAt(0) !== "/")
            return "the command must be a bare program name or an absolute path, "
                + "not a relative path.";
        return "";
    }

    // The value that is actually executed: the configured command when it is
    // acceptable, the default when it is not.  Never returns a broken string.
    function safeCommand(raw) {
        if (root.commandError(raw) !== "")
            return root.defaultCommand;
        return String(raw).trim();
    }

    readonly property int refreshSeconds: {
        var raw = Plasmoid.configuration.refreshSeconds;
        if (typeof raw === "undefined" || raw === null)
            return 60;
        var value = Number(raw);
        if (!isFinite(value) || value <= 0)
            return 60;
        return Math.max(5, Math.round(value));
    }

    readonly property string compactMode: {
        var raw = Plasmoid.configuration.compactMode;
        if (typeof raw === "undefined" || raw === null)
            return "sarvam";
        var mode = String(raw);
        return mode === "cliproxy" ? "cliproxy" : "sarvam";
    }

    readonly property int warnBelowPercent: {
        var raw = Plasmoid.configuration.warnBelowPercent;
        if (typeof raw === "undefined" || raw === null)
            return 20;
        var value = Number(raw);
        if (!isFinite(value))
            return 20;
        return Math.max(0, Math.min(100, Math.round(value)));
    }

    readonly property bool showApiBreakdown: {
        var raw = Plasmoid.configuration.showApiBreakdown;
        if (typeof raw === "undefined" || raw === null)
            return true;
        return raw === true || raw === "true" || raw === 1;
    }

    // ------------------------------------------------------------------
    // Polling state
    // ------------------------------------------------------------------

    // Last good payload.  Never cleared on failure (edge case 1 and 2).
    property var payload: null
    property bool offline: false
    property bool loading: false
    property string lastError: ""
    // 1 = normal cadence, doubled on every failure up to 10x, reset on success.
    property int backoffMultiplier: 1

    readonly property bool hasPayload: typeof payload === "object" && payload !== null
    readonly property bool stale: hasPayload ? payload.stale === true : false

    readonly property int baseIntervalMs: refreshSeconds * 1000
    readonly property int pollInterval: Math.round(baseIntervalMs * backoffMultiplier)

    // ---- plasmashell safety -------------------------------------------
    // This applet runs *inside* plasmashell, on the same GUI thread as every
    // panel and every other widget.  Anything that blocks or runs unbounded
    // there freezes the whole desktop, so the one call this widget makes is
    // bounded four ways: it goes through Plasma's executable data source (which
    // starts the child asynchronously and answers through a signal, never a
    // blocking read), a hard watchdog that gives up on a wedged child and kills
    // it, a single-flight guard so a slow daemon cannot stack processes up, and
    // an explicit disconnectSource() when the request settles or the widget goes
    // away.  The answer is size-capped before JSON.parse, and every list the
    // delegate model sees is sliced.  Nothing below loops unboundedly.
    readonly property int requestTimeoutMs: 4000
    readonly property int maxResponseChars: 512 * 1024
    readonly property int maxAccountsShown: 12
    readonly property int maxErrorsShown: 5

    // The command whose answer is outstanding ("" = idle).  A non-empty value
    // *is* the single-flight guard: no second request starts while it is set.
    property string pendingCommand: ""
    property bool pendingFallback: false

    // The only process this widget ever spawns.  `connectedSources` starts empty
    // and holds exactly one entry per request; the entry is removed - and the
    // child killed - the moment the request settles, the watchdog fires or the
    // widget is destroyed.
    P5Support.DataSource {
        id: execSource
        engine: "executable"
        connectedSources: []
        onNewData: function (sourceName, data) {
            root.handleNewData(sourceName, data);
        }
    }

    Timer {
        id: refreshTimer
        interval: root.pollInterval
        repeat: true
        running: true
        triggeredOnStart: false
        onTriggered: root.refresh()
    }

    // Re-evaluates the balance age text.  No process, no allocation worth
    // mentioning: one Date.now() and a string format every 30 seconds, and only
    // while there is an as_of timestamp to age.
    Timer {
        id: clockTimer
        interval: 30000
        repeat: true
        running: root.balanceAsOf.length > 0
        onTriggered: root.clockTick = Date.now()
    }

    // The data source has no timeout of its own: a child that hangs (a wedged
    // daemon, a CLI waiting on a lock) would leave the widget in `loading` for
    // ever.  This is the independent watchdog that gives up, kills the child and
    // records a failure.
    Timer {
        id: requestWatchdog
        interval: root.requestTimeoutMs + 2000
        repeat: false
        running: false
        onTriggered: root.abortRequest("daemon did not answer in time")
    }

    Component.onCompleted: root.refresh()

    // Never leave a child process or a timer behind.
    Component.onDestruction: root.abortRequest("")

    // Re-poll straight away if the user repoints the widget at another program.
    onCommandChanged: root.refresh()

    function handleFailure(message) {
        root.offline = true;
        root.lastError = message;
        root.backoffMultiplier = Math.min(10, root.backoffMultiplier * 2);
    }

    // The command line for one op: "<command> query <op>".  Both halves are
    // safe - `command` is whitelisted above and `op` is a literal below - so no
    // quoting is needed and no user text ever reaches the shell.
    function queryCommand(op) {
        return root.command + " query " + op;
    }

    // Drop the outstanding request, if there is one: stop the watchdog, forget
    // the command and disconnect the source, which kills a child that is still
    // running.  `message` empty means "do not count this as a failure" (a widget
    // being removed is not an error).
    function abortRequest(message) {
        var target = root.pendingCommand;
        root.pendingCommand = "";
        root.pendingFallback = false;
        requestWatchdog.stop();
        root.loading = false;
        if (target !== "") {
            try {
                execSource.disconnectSource(target);
            } catch (e) {
                // already disconnected
            }
        }
        if (message !== "")
            root.handleFailure(message);
    }

    // The timer's entry point: re-read the daemon's cached status.
    function refresh() {
        root.startRequest("status", false);
    }

    // The header's refresh button.  A plain status read only returns the
    // daemon's cache (the dashboard is re-polled on its own TTL), so the button
    // asks the daemon to poll its sources now.  That can take a while when the
    // dashboard is slow, so a failure falls back to the cached status: the
    // spinner always stops and the user always ends up with numbers on screen.
    function forceRefresh() {
        root.startRequest("refresh", true);
    }

    function startRequest(op, allowFallback) {
        // Single flight: while a request is outstanding, do nothing.  A daemon
        // that stops answering therefore cannot pile processes up.
        if (root.loading)
            return;

        var target = root.queryCommand(op);
        root.loading = true;
        root.pendingCommand = target;
        root.pendingFallback = allowFallback;
        requestWatchdog.restart();
        try {
            execSource.connectSource(target);
        } catch (e) {
            // Nothing was started, so nothing has to be stopped.
            root.settleFailure("cannot run " + target, allowFallback);
        }
    }

    // The data source answered.  `data` is the map Plasma hands over:
    // {"stdout": ..., "stderr": ..., "exit code": ...}.  Anything for a source
    // we are no longer waiting on (a late answer from a request that already
    // timed out) is ignored.
    function handleNewData(sourceName, data) {
        if (!root.loading)
            return;
        if (String(sourceName) !== root.pendingCommand)
            return;

        var fallback = root.pendingFallback;
        var payload = data;
        if (typeof payload !== "object" || payload === null) {
            // Some engines deliver the map only through `data[source]`.
            try {
                payload = execSource.data[sourceName];
            } catch (e) {
                payload = null;
            }
        }
        if (typeof payload !== "object" || payload === null) {
            root.settleFailure("the daemon returned an unreadable answer", fallback);
            return;
        }

        var exitCode = root.numberOr(payload["exit code"], 0);
        var body = "";
        var detail = "";
        if (typeof payload["stdout"] === "string")
            body = payload["stdout"];
        if (typeof payload["stderr"] === "string")
            detail = root.firstLine(payload["stderr"]);

        if (exitCode !== 0) {
            root.settleFailure(
                detail !== "" ? ("sarvam-meter: " + detail)
                              : ("sarvam-meter exited with status " + exitCode),
                fallback);
            return;
        }
        if (body.length === 0) {
            root.settleFailure("the daemon returned nothing", fallback);
            return;
        }

        // Never hand a huge string to JSON.parse on the GUI thread: that is the
        // one way this widget could stall plasmashell.
        if (body.length > root.maxResponseChars) {
            root.settleFailure("daemon response too large", fallback);
            return;
        }

        var parsed = null;
        try {
            parsed = JSON.parse(body);
        } catch (e) {
            parsed = null;
        }

        if (typeof parsed !== "object" || parsed === null) {
            root.settleFailure("daemon returned malformed JSON", fallback);
            return;
        }

        root.settleSuccess(parsed);
    }

    // The first line of a child's stderr, bounded and stripped of control
    // characters.  It is the CLI's own message ("sarvam-meter: ..."), which the
    // daemon builds without ever quoting a stored value.
    function firstLine(text) {
        var line = String(text).replace(/\r/g, "\n");
        var cut = line.indexOf("\n");
        if (cut >= 0)
            line = line.substring(0, cut);
        line = line.replace(/[\u0000-\u001f]/g, "").trim();
        if (line.length > 200)
            line = line.substring(0, 200) + "...";
        return line;
    }

    function settleSuccess(parsed) {
        root.clearPending();
        root.payload = parsed;
        root.offline = false;
        root.lastError = "";
        root.backoffMultiplier = 1;
    }

    function settleFailure(message, allowFallback) {
        root.clearPending();
        root.handleFailure(message);
        if (allowFallback)
            root.startRequest("status", false);
    }

    // Forget the outstanding request and disconnect its source (killing the
    // child if it is still running).  Shared by both settlement paths.
    function clearPending() {
        var target = root.pendingCommand;
        root.pendingCommand = "";
        root.pendingFallback = false;
        requestWatchdog.stop();
        root.loading = false;
        if (target === "")
            return;
        try {
            execSource.disconnectSource(target);
        } catch (e) {
            // already disconnected
        }
    }

    // ------------------------------------------------------------------
    // Small JS helpers
    // ------------------------------------------------------------------

    // QML can hand a list back as an array-like sequence object rather than a
    // real JS array (in particular for lists nested inside model data), so
    // Array.isArray() alone is not enough.  Normalise to a plain JS array.
    function asArray(value) {
        if (typeof value === "undefined" || value === null)
            return [];
        if (Array.isArray(value))
            return value;
        if (typeof value === "object" && typeof value.length === "number") {
            var copy = [];
            for (var i = 0; i < value.length; i++)
                copy.push(value[i]);
            return copy;
        }
        return [];
    }

    function numberOr(value, fallback) {
        if (typeof value === "undefined" || value === null)
            return fallback;
        var n = Number(value);
        return isFinite(n) ? n : fallback;
    }

    function currencySymbol(currency) {
        var code = (typeof currency === "string" && currency.length > 0) ? currency : "INR";
        if (code === "INR")
            return "\u20b9";
        if (code === "USD")
            return "$";
        return "";
    }

    // e.g. rupee-symbol + 412.60, "$12.30", "12.30 EUR".  Never throws, never prints NaN.
    function formatMoney(value, currency) {
        var n = root.numberOr(value, NaN);
        if (!isFinite(n))
            return "\u2014";
        var code = (typeof currency === "string" && currency.length > 0) ? currency : "INR";
        var symbol = root.currencySymbol(code);
        var sign = n < 0 ? "-" : "";
        var body = Math.abs(n).toFixed(2);
        if (symbol.length > 0)
            return sign + symbol + body;
        return sign + body + " " + code;
    }

    // Short form for the panel: rupee-symbol + 412 (truncated, per SPEC.md section 13).
    function formatMoneyCompact(value, currency) {
        var n = root.numberOr(value, NaN);
        if (!isFinite(n))
            return "\u2014";
        var code = (typeof currency === "string" && currency.length > 0) ? currency : "INR";
        var symbol = root.currencySymbol(code);
        var magnitude = Math.abs(n);
        var whole = Math.floor(magnitude);
        var sign = n < 0 ? "-" : "";
        if (symbol.length > 0)
            return sign + symbol + whole;
        return sign + whole + " " + code;
    }

    // Fractions are shown as whole percentages.
    function formatPercent(fraction) {
        var n = root.numberOr(fraction, NaN);
        if (!isFinite(n))
            return "unknown";
        return Math.round(Math.max(0, Math.min(1, n)) * 100) + "%";
    }

    // ------------------------------------------------------------------
    // Parsed model (every nested object falls back to {})
    // ------------------------------------------------------------------

    readonly property var sarvamObject: {
        if (!root.hasPayload)
            return {};
        var s = root.payload.sarvam;
        return (typeof s === "object" && s !== null) ? s : {};
    }

    readonly property var balanceObject: {
        var b = root.sarvamObject.balance;
        return (typeof b === "object" && b !== null) ? b : {};
    }

    readonly property var spendObject: {
        var s = root.sarvamObject.spend;
        return (typeof s === "object" && s !== null) ? s : {};
    }

    readonly property var callsObject: {
        var c = root.sarvamObject.calls;
        return (typeof c === "object" && c !== null) ? c : {};
    }

    // Schema 2: the dashboard snapshot.  Always present from a schema-2 daemon
    // (`configured: false` when disabled); absent entirely from a schema-1
    // payload, where this falls back to {} and every read below stays quiet.
    readonly property var dashboardObject: {
        var d = root.sarvamObject.dashboard;
        return (typeof d === "object" && d !== null) ? d : {};
    }

    readonly property var cliproxyObject: {
        if (!root.hasPayload)
            return {};
        var c = root.payload.cliproxy;
        return (typeof c === "object" && c !== null) ? c : {};
    }

    readonly property var byApiList: root.asArray(root.sarvamObject.by_api)

    // Top three by_api entries, as given (the daemon sorts spend descending).
    readonly property var topApis: {
        var list = root.byApiList;
        var out = [];
        for (var i = 0; i < list.length && out.length < 3; i++) {
            var entry = list[i];
            if (typeof entry === "object" && entry !== null)
                out.push(entry);
        }
        return out;
    }

    // Dashboard per-model spend (schema 2).  Empty for a schema-1 payload, for a
    // disabled or unreachable dashboard, and for a missing/odd by_model value.
    readonly property var byModelList: root.asArray(root.dashboardObject.by_model)

    // Top three by_model entries, as given (the daemon sorts cost descending).
    readonly property var topModels: {
        var list = root.byModelList;
        var out = [];
        for (var i = 0; i < list.length && out.length < 3; i++) {
            var entry = list[i];
            if (typeof entry === "object" && entry !== null)
                out.push(entry);
        }
        return out;
    }

    // The Bar scale for those rows: the largest cost among the rows actually
    // drawn.  0 means "nothing to scale against", which the delegate renders as a
    // zero-width bar instead of dividing by zero.
    readonly property real topModelMaxCost: {
        var list = root.topModels;
        var largest = 0;
        for (var i = 0; i < list.length; i++) {
            var value = root.numberOr(list[i].cost, 0);
            if (value > largest)
                largest = value;
        }
        return largest;
    }

    // Sliced: a pathological payload must not build thousands of delegates on
    // the GUI thread.  Twelve accounts is more than anyone watches.
    readonly property var accountsList: root.asArray(root.cliproxyObject.accounts).slice(0, root.maxAccountsShown)

    readonly property var errorList: {
        if (!root.hasPayload)
            return [];
        return root.asArray(root.payload.errors).slice(0, root.maxErrorsShown);
    }

    readonly property string currencyCode: {
        var c = root.sarvamObject.currency;
        return (typeof c === "string" && c.length > 0) ? c : "INR";
    }

    readonly property bool sarvamConfigured: {
        var c = root.sarvamObject.configured;
        if (typeof c === "undefined" || c === null)
            return true;
        return c === true;
    }

    readonly property bool cliproxyConfigured: root.cliproxyObject.configured === true
    readonly property bool cliproxyReachable: root.cliproxyObject.reachable === true
    readonly property bool quotaExhausted: root.sarvamObject.quota_exhausted === true

    readonly property bool sarvamBalanceKnown: root.balanceObject.known === true
    readonly property real remaining: root.numberOr(root.balanceObject.remaining, NaN)
    readonly property real todaySpend: root.numberOr(root.spendObject.today, NaN)
    readonly property real monthSpend: root.numberOr(root.spendObject.month, NaN)

    // Schema 2: which source the balance and spend figures come from.  A schema-1
    // payload has no `spend.source`, and an older daemon's numbers are ledger
    // numbers, so the fallback is "ledger" - never an empty or undefined hint.
    readonly property string spendSource: {
        var s = root.spendObject.source;
        if (typeof s === "undefined" || s === null)
            return "ledger";
        return String(s) === "dashboard" ? "dashboard" : "ledger";
    }

    // The small muted hint next to the spend line (SPEC.md section 17.7).
    readonly property string spendSourceHint: root.spendSource === "dashboard" ? "dashboard" : "local meter"

    readonly property bool dashboardConfigured: root.dashboardObject.configured === true
    readonly property bool dashboardReachable: root.dashboardObject.reachable === true

    // True only for a non-empty error object.  `error` is null when the last
    // snapshot was clean, and absent altogether on a schema-1 payload.
    readonly property bool dashboardHasError: {
        var e = root.dashboardObject.error;
        if (typeof e !== "object" || e === null)
            return false;
        if (Array.isArray(e))
            return false;
        try {
            return Object.keys(e).length > 0;
        } catch (err) {
            return false;
        }
    }

    // Only the code is ever shown: the message may quote the upstream response,
    // so it stays out of the UI entirely.
    readonly property string dashboardErrorCode: {
        if (!root.dashboardHasError)
            return "";
        var code = root.dashboardObject.error.code;
        if (typeof code === "string" && code.length > 0)
            return code;
        return "error";
    }

    readonly property string dashboardErrorLine: {
        var code = root.dashboardErrorCode;
        return code.length > 0 ? ("dashboard: " + code) : "";
    }

    readonly property int todayCalls: {
        var n = root.numberOr(root.callsObject.today, NaN);
        if (!isFinite(n))
            return -1;
        return Math.round(n);
    }

    // -1 means "no known fraction" (null worst_fraction, or nothing configured).
    readonly property real worstFraction: root.numberOr(root.cliproxyObject.worst_fraction, -1)

    readonly property string generatedAt: {
        if (!root.hasPayload)
            return "";
        var g = root.payload.generated_at;
        if (typeof g === "undefined" || g === null)
            return "";
        return String(g);
    }

    // ------------------------------------------------------------------
    // Presentation strings shared by both representations
    // ------------------------------------------------------------------

    readonly property string balanceHeadline: {
        if (!root.hasPayload)
            return "\u2014";
        if (!root.sarvamBalanceKnown) {
            var spent = root.numberOr(root.balanceObject.spent_total, NaN);
            if (!isFinite(spent))
                return "spend only";
            return root.formatMoney(spent, root.currencyCode) + " spent in total";
        }
        if (!isFinite(root.remaining))
            return "balance not reported";
        return root.formatMoney(root.remaining, root.currencyCode) + " left";
    }

    readonly property string balanceCaption: {
        if (!root.sarvamBalanceKnown)
            return "spend only \u2014 no starting balance set";
        var starting = root.numberOr(root.balanceObject.starting, NaN);
        var spentTotal = root.numberOr(root.balanceObject.spent_total, NaN);
        if (!isFinite(starting))
            return "starting balance not reported";
        var text = "of " + root.formatMoney(starting, root.currencyCode) + " starting";
        if (isFinite(spentTotal))
            text += " \u00b7 " + root.formatMoney(spentTotal, root.currencyCode) + " spent";
        return text;
    }

    readonly property string spendLine: {
        var today = root.formatMoney(root.todaySpend, root.currencyCode);
        var month = root.formatMoney(root.monthSpend, root.currencyCode);
        return today + " today \u00b7 " + month + " this month";
    }

    readonly property string callsLine: {
        if (root.todayCalls < 0)
            return "calls not reported";
        return root.todayCalls + (root.todayCalls === 1 ? " call today" : " calls today");
    }

    readonly property string errorSummary: {
        var list = root.errorList;
        var parts = [];
        for (var i = 0; i < list.length && i < 3; i++) {
            var entry = list[i];
            if (typeof entry !== "object" || entry === null)
                continue;
            var source = (typeof entry.source === "string") ? entry.source : "error";
            var code = (typeof entry.code === "string") ? entry.code : "";
            var message = (typeof entry.message === "string") ? entry.message : "";
            var line = source + (code.length > 0 ? (": " + code) : "");
            if (message.length > 0)
                line += " (" + message + ")";
            parts.push(line);
        }
        if (parts.length === 0)
            return "";
        return "Errors: " + parts.join("; ");
    }

    // ---- balance freshness --------------------------------------------
    // `balance_updated_at` from /api/dhan/org/balance is when the *balance value*
    // last changed, not when we last asked.  Sarvam updates it lazily, so a
    // reading can legitimately be an hour old while the widget itself polled a
    // minute ago - and a stale "80.04 left" looks exactly like a live one.  The
    // age is shown next to the balance so the two cannot be confused.
    readonly property string balanceAsOf: {
        var block = root.dashboardObject.balance;
        if (typeof block !== "object" || block === null)
            return "";
        var stamp = block.as_of;
        return (typeof stamp === "string") ? stamp : "";
    }

    // Bumped by clockTimer so the age text re-evaluates without a network call.
    property double clockTick: 0

    readonly property double balanceAgeMs: {
        var stamp = root.balanceAsOf;
        var tick = root.clockTick;
        if (stamp.length === 0)
            return -1;
        var parsed = Date.parse(stamp);
        if (isNaN(parsed))
            return -1;
        var now = tick > 0 ? tick : Date.now();
        return now - parsed;
    }

    readonly property bool balanceAgeOld: root.balanceAgeMs > 30 * 60 * 1000

    // "just now", "7m ago", "3h ago", "2d ago".
    function ageText(ms) {
        if (!isFinite(ms) || ms < 0)
            return "";
        var minutes = Math.floor(ms / 60000);
        if (minutes < 1)
            return "just now";
        if (minutes < 60)
            return minutes + "m ago";
        var hours = Math.floor(minutes / 60);
        if (hours < 24)
            return hours + "h ago";
        return Math.floor(hours / 24) + "d ago";
    }

    readonly property string balanceFreshnessLine: {
        if (root.balanceAgeMs < 0)
            return "";
        var text = root.ageText(root.balanceAgeMs);
        if (text.length === 0)
            return "";
        return root.balanceAgeOld
            ? ("balance " + text + " \u00b7 not refreshed")
            : ("balance " + text);
    }

    readonly property string footerText: {
        var parts = [];
        parts.push(root.generatedAt.length > 0 ? ("Updated " + root.generatedAt) : "No status received yet");
        if (root.offline)
            parts.push("daemon offline \u2014 showing last known data");
        else if (root.stale)
            parts.push("data may be stale");
        if (root.offline && root.lastError.length > 0)
            parts.push(root.lastError);
        return parts.join(" \u00b7 ");
    }

    // Panel text: remaining rupees, or the worst CLIProxyAPI percentage.
    readonly property string compactValue: {
        if (root.compactMode === "cliproxy") {
            if (root.worstFraction < 0)
                return "\u2014";
            return root.formatPercent(root.worstFraction);
        }
        if (!root.sarvamBalanceKnown) {
            if (!isFinite(root.todaySpend))
                return "\u2014";
            return root.formatMoneyCompact(root.todaySpend, root.currencyCode);
        }
        if (!isFinite(root.remaining))
            return "\u2014";
        return root.formatMoneyCompact(root.remaining, root.currencyCode);
    }

    readonly property bool compactWarning: {
        if (root.quotaExhausted)
            return true;
        if (root.compactMode === "cliproxy") {
            if (root.worstFraction < 0)
                return false;
            return (root.worstFraction * 100) < root.warnBelowPercent;
        }
        if (!root.sarvamBalanceKnown)
            return false;
        var starting = root.numberOr(root.balanceObject.starting, NaN);
        if (!isFinite(starting) || starting <= 0)
            return false;
        if (!isFinite(root.remaining))
            return false;
        return ((root.remaining / starting) * 100) < root.warnBelowPercent;
    }

    readonly property bool showBadge: root.offline || root.stale || root.quotaExhausted

    readonly property string summaryLine: {
        var parts = [];
        parts.push(root.compactValue);
        if (root.compactMode === "cliproxy") {
            parts.push(root.formatMoney(root.todaySpend, root.currencyCode) + " spent today");
        } else if (root.sarvamBalanceKnown) {
            parts.push(root.formatMoney(root.todaySpend, root.currencyCode) + " today");
        } else {
            parts.push("spend only");
        }
        if (root.offline)
            parts.push("daemon offline");
        else if (root.stale)
            parts.push("stale");
        if (root.quotaExhausted)
            parts.push("quota exhausted");
        return parts.join(" \u00b7 ");
    }

    // ------------------------------------------------------------------
    // Tooltip and representations
    // ------------------------------------------------------------------

    toolTipMainText: "Sarvam Meter"
    toolTipSubText: root.summaryLine

    compactRepresentation: CompactRepresentation {
        meter: root
    }

    fullRepresentation: FullRepresentation {
        meter: root
    }
}
