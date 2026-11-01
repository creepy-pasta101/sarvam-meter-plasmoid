// SPDX-License-Identifier: MIT
//
// Sarvam Meter - Plasma 6 applet root (SPEC.md sections 10 and 13).
//
// The applet polls GET <daemonUrl>/v1/status and keeps the parsed model here;
// CompactRepresentation.qml and FullRepresentation.qml read it through the
// `meter` property that this file assigns.  Nothing in this file blocks: the
// only network call is an asynchronous XMLHttpRequest.
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

PlasmoidItem {
    id: root

    switchWidth: Kirigami.Units.gridUnit * 10
    switchHeight: Kirigami.Units.gridUnit * 10

    // ------------------------------------------------------------------
    // Configuration.  These are the only five keys the widget may read.
    // ------------------------------------------------------------------

    readonly property string daemonUrl: {
        var raw = Plasmoid.configuration.daemonUrl;
        if (typeof raw === "undefined" || raw === null)
            return "http://127.0.0.1:8787";
        var url = String(raw);
        if (url.length === 0)
            return "http://127.0.0.1:8787";
        // Tolerate a trailing slash in the configured base URL.
        while (url.length > 1 && url.charAt(url.length - 1) === "/")
            url = url.substring(0, url.length - 1);
        return url;
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

    Timer {
        id: refreshTimer
        interval: root.pollInterval
        repeat: true
        running: true
        triggeredOnStart: false
        onTriggered: root.refresh()
    }

    Component.onCompleted: root.refresh()

    // Re-poll straight away if the user repoints the widget at another daemon.
    onDaemonUrlChanged: root.refresh()

    function handleFailure(message) {
        root.offline = true;
        root.lastError = message;
        root.backoffMultiplier = Math.min(10, root.backoffMultiplier * 2);
    }

    function refresh() {
        if (root.loading)
            return;

        var url = root.daemonUrl + "/v1/status";
        var xhr = new XMLHttpRequest();
        var finished = false;

        function fail(message) {
            if (finished)
                return;
            finished = true;
            root.loading = false;
            root.handleFailure(message);
        }

        try {
            // Third argument `true` = asynchronous.  Synchronous XHR is banned.
            xhr.open("GET", url, true);
            xhr.setRequestHeader("Accept", "application/json");
        } catch (e) {
            fail("cannot reach " + url);
            return;
        }

        root.loading = true;
        xhr.timeout = Math.max(1000, Math.min(15000, root.baseIntervalMs));

        xhr.onreadystatechange = function () {
            if (xhr.readyState !== 4)
                return;
            if (finished)
                return;
            finished = true;
            root.loading = false;

            if (xhr.status !== 200) {
                root.handleFailure("HTTP " + xhr.status);
                return;
            }

            var parsed = null;
            try {
                parsed = JSON.parse(xhr.responseText);
            } catch (e) {
                parsed = null;
            }

            if (typeof parsed !== "object" || parsed === null) {
                root.handleFailure("daemon returned malformed JSON");
                return;
            }

            root.payload = parsed;
            root.offline = false;
            root.lastError = "";
            root.backoffMultiplier = 1;
        };

        xhr.onerror = function () {
            fail("daemon unreachable");
        };

        xhr.ontimeout = function () {
            fail("daemon timed out");
        };

        xhr.send();
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

    readonly property var accountsList: root.asArray(root.cliproxyObject.accounts)

    readonly property var errorList: {
        if (!root.hasPayload)
            return [];
        return root.asArray(root.payload.errors);
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
    // numbers, so the fallback is "ledger" — never an empty or undefined hint.
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
