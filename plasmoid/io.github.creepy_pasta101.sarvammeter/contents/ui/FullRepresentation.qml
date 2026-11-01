// SPDX-License-Identifier: MIT
//
// Sarvam Meter - full (popup) representation.
//
// ScrollView > ColumnLayout with three parts (SPEC.md section 13):
//   (a) Sarvam section  - remaining balance, or a "spend only" presentation when
//                         balance.known is false, today/month spend, calls, and a
//                         Bar per entry of the top 3 by_api entries.
//   (b) CLIProxyAPI     - one block per account, one Bar per bucket with the
//                         window label, remaining fraction and reset time;
//                         the literal text "unknown" when remaining_fraction is
//                         null (never rendered as 0%).
//   (c) footer          - generated_at plus the offline/stale notice.
//
// Everything is read through the `meter` model property and every access is
// guarded, so a partial payload renders instead of throwing.

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.kde.plasma.components as PlasmaComponents3
import org.kde.kirigami as Kirigami

Item {
    id: fullRoot

    // Assigned by main.qml; never assumed non-null.
    property var meter: null

    readonly property bool hasMeter: meter !== null && typeof meter !== "undefined"

    readonly property var spend: hasMeter ? meter.spendObject : null
    readonly property var topApis: hasMeter ? meter.topApis : []
    readonly property var accounts: hasMeter ? meter.accountsList : []

    readonly property string currency: hasMeter ? meter.currencyCode : "INR"
    readonly property string headline: hasMeter ? meter.balanceHeadline : ""
    readonly property string caption: hasMeter ? meter.balanceCaption : ""
    readonly property string spendLine: hasMeter ? meter.spendLine : ""
    readonly property string callsLine: hasMeter ? meter.callsLine : ""
    readonly property string footerText: hasMeter ? meter.footerText : ""
    readonly property string errorText: hasMeter ? meter.errorSummary : ""

    readonly property bool hasData: hasMeter ? meter.hasPayload : false
    readonly property bool offline: hasMeter ? meter.offline : false
    readonly property bool stale: hasMeter ? meter.stale : false
    readonly property bool quotaExhausted: hasMeter ? meter.quotaExhausted : false
    readonly property bool warning: hasMeter ? meter.compactWarning : false
    readonly property bool showBreakdown: hasMeter ? meter.showApiBreakdown : false
    readonly property bool sarvamConfigured: hasMeter ? meter.sarvamConfigured : false
    readonly property bool cliproxyConfigured: hasMeter ? meter.cliproxyConfigured : false
    readonly property bool cliproxyReachable: hasMeter ? meter.cliproxyReachable : false
    readonly property int warnPercent: hasMeter ? meter.warnBelowPercent : 20

    // The root of a representation is placed by Plasma, not by a QtQuick
    // Layout, so Layout.* attached properties would be ignored: express the
    // popup size with implicitWidth/implicitHeight only.
    implicitWidth: Kirigami.Units.gridUnit * 24
    implicitHeight: Kirigami.Units.gridUnit * 22

    // ---- guarded helpers ---------------------------------------------

    function money(value) {
        return hasMeter ? meter.formatMoney(value, currency) : "";
    }

    function pct(value) {
        return hasMeter ? meter.formatPercent(value) : "unknown";
    }

    function field(object, key) {
        if (typeof object === "undefined" || object === null)
            return undefined;
        return object[key];
    }

    function textOf(value, fallback) {
        if (typeof value === "undefined" || value === null)
            return fallback;
        var text = String(value);
        return text.length > 0 ? text : fallback;
    }

    function asArray(value) {
        return hasMeter ? meter.asArray(value) : [];
    }

    function numberOr(value, fallback) {
        if (typeof value === "undefined" || value === null)
            return fallback;
        var n = Number(value);
        return isFinite(n) ? n : fallback;
    }

    function colourFor(fraction) {
        if (typeof fraction !== "number" || !isFinite(fraction) || fraction < 0)
            return Kirigami.Theme.neutralTextColor;
        return (fraction * 100) < fullRoot.warnPercent
            ? Kirigami.Theme.negativeTextColor
            : Kirigami.Theme.positiveTextColor;
    }

    ScrollView {
        id: scrollView
        anchors.fill: parent
        clip: true
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

        ColumnLayout {
            id: contentColumn
            width: scrollView.availableWidth
            spacing: Kirigami.Units.smallSpacing

            // ==============================================================
            // (a) Sarvam
            // ==============================================================
            PlasmaComponents3.Label {
                Layout.fillWidth: true
                Layout.topMargin: Kirigami.Units.smallSpacing
                text: "Sarvam AI"
                font.weight: Font.DemiBold
                opacity: fullRoot.sarvamConfigured ? 1.0 : 0.6
            }

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                visible: !fullRoot.hasData
                text: "Waiting for the sarvam-meter daemon at the configured URL\u2026"
                opacity: 0.7
                wrapMode: Text.WordWrap
            }

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                visible: fullRoot.hasData
                text: fullRoot.headline
                font.pointSize: Math.round(Kirigami.Theme.defaultFont.pointSize * 1.6)
                font.weight: Font.DemiBold
                color: fullRoot.warning ? Kirigami.Theme.negativeTextColor : Kirigami.Theme.textColor
                elide: Text.ElideRight
            }

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                visible: fullRoot.hasData
                text: fullRoot.caption
                font: Kirigami.Theme.smallFont
                opacity: 0.7
                elide: Text.ElideRight
            }

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                visible: fullRoot.hasData
                text: fullRoot.spendLine
                elide: Text.ElideRight
            }

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                visible: fullRoot.hasData
                text: fullRoot.callsLine
                font: Kirigami.Theme.smallFont
                opacity: 0.7
                elide: Text.ElideRight
            }

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                visible: fullRoot.quotaExhausted
                text: "Sarvam reported 402 \u2014 the credit balance is exhausted"
                color: Kirigami.Theme.negativeTextColor
                wrapMode: Text.WordWrap
            }

            // ---- top-3 by_api breakdown ------------------------------
            ColumnLayout {
                id: apiBreakdown
                Layout.fillWidth: true
                Layout.topMargin: Kirigami.Units.smallSpacing
                spacing: Math.round(Kirigami.Units.smallSpacing / 2)
                visible: fullRoot.showBreakdown && fullRoot.topApis.length > 0

                Repeater {
                    model: fullRoot.topApis

                    delegate: ColumnLayout {
                        id: apiRow

                        readonly property string apiName: fullRoot.textOf(fullRoot.field(modelData, "api"), "other")
                        readonly property real apiSpend: fullRoot.numberOr(fullRoot.field(modelData, "spend"), 0)
                        readonly property int apiCalls: {
                            var n = fullRoot.numberOr(fullRoot.field(modelData, "calls"), 0);
                            return Math.round(n);
                        }
                        // Share of this month's measured spend; falls back to
                        // scaling against the largest of the shown entries.
                        readonly property real share: {
                            var month = fullRoot.numberOr(fullRoot.field(fullRoot.spend, "month"), 0);
                            if (month > 0)
                                return Math.max(0, Math.min(1, apiSpend / month));
                            var largest = 0;
                            for (var i = 0; i < fullRoot.topApis.length; i++) {
                                var value = fullRoot.numberOr(fullRoot.field(fullRoot.topApis[i], "spend"), 0);
                                if (value > largest)
                                    largest = value;
                            }
                            if (largest <= 0)
                                return 0;
                            return Math.max(0, Math.min(1, apiSpend / largest));
                        }

                        Layout.fillWidth: true
                        spacing: Math.round(Kirigami.Units.smallSpacing / 2)

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Kirigami.Units.smallSpacing

                            PlasmaComponents3.Label {
                                Layout.fillWidth: true
                                text: apiRow.apiName
                                font: Kirigami.Theme.smallFont
                                elide: Text.ElideRight
                            }

                            PlasmaComponents3.Label {
                                text: apiRow.apiCalls + (apiRow.apiCalls === 1 ? " call" : " calls")
                                font: Kirigami.Theme.smallFont
                                opacity: 0.7
                            }

                            PlasmaComponents3.Label {
                                text: fullRoot.money(apiRow.apiSpend)
                                font: Kirigami.Theme.smallFont
                            }
                        }

                        Bar {
                            Layout.fillWidth: true
                            fraction: apiRow.share
                            fillColor: Kirigami.Theme.highlightColor
                        }
                    }
                }
            }

            // ==============================================================
            // (b) CLIProxyAPI
            // ==============================================================
            Rectangle {
                Layout.fillWidth: true
                Layout.topMargin: Kirigami.Units.smallSpacing
                implicitHeight: 1
                color: Qt.rgba(Kirigami.Theme.textColor.r,
                               Kirigami.Theme.textColor.g,
                               Kirigami.Theme.textColor.b,
                               0.2)
            }

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                text: "CLIProxyAPI"
                font.weight: Font.DemiBold
                opacity: fullRoot.cliproxyConfigured ? 1.0 : 0.6
            }

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                visible: !fullRoot.cliproxyConfigured
                text: "Not configured"
                font: Kirigami.Theme.smallFont
                opacity: 0.7
            }

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                visible: fullRoot.cliproxyConfigured && !fullRoot.cliproxyReachable
                text: "Unreachable \u2014 quota data unavailable"
                color: Kirigami.Theme.neutralTextColor
                font: Kirigami.Theme.smallFont
                wrapMode: Text.WordWrap
            }

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                visible: fullRoot.cliproxyConfigured && fullRoot.cliproxyReachable
                         && fullRoot.accounts.length === 0
                text: "No accounts reported"
                font: Kirigami.Theme.smallFont
                opacity: 0.7
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: Kirigami.Units.largeSpacing
                visible: fullRoot.accounts.length > 0

                Repeater {
                    model: fullRoot.accounts

                    delegate: ColumnLayout {
                        id: accountBlock

                        readonly property string accountName: fullRoot.textOf(
                            fullRoot.field(modelData, "name"),
                            fullRoot.textOf(fullRoot.field(modelData, "label"), "account"))
                        readonly property string providerName: fullRoot.textOf(
                            fullRoot.field(modelData, "provider"), "")
                        readonly property string planName: fullRoot.textOf(
                            fullRoot.field(modelData, "plan"), "")
                        readonly property string statusName: fullRoot.textOf(
                            fullRoot.field(modelData, "status"), "")
                        readonly property string accountError: fullRoot.textOf(
                            fullRoot.field(modelData, "error"), "")
                        readonly property var buckets: fullRoot.asArray(
                            fullRoot.field(modelData, "buckets"))

                        Layout.fillWidth: true
                        spacing: Math.round(Kirigami.Units.smallSpacing / 2)

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Kirigami.Units.smallSpacing

                            PlasmaComponents3.Label {
                                Layout.fillWidth: true
                                text: accountBlock.accountName
                                font.weight: Font.DemiBold
                                elide: Text.ElideRight
                            }

                            PlasmaComponents3.Label {
                                visible: accountBlock.providerName.length > 0
                                text: accountBlock.providerName
                                font: Kirigami.Theme.smallFont
                                opacity: 0.7
                                elide: Text.ElideRight
                            }

                            PlasmaComponents3.Label {
                                visible: accountBlock.planName.length > 0
                                text: accountBlock.planName
                                font: Kirigami.Theme.smallFont
                                opacity: 0.85
                                elide: Text.ElideRight
                            }

                            PlasmaComponents3.Label {
                                visible: accountBlock.statusName.length > 0
                                         && accountBlock.statusName !== "active"
                                text: accountBlock.statusName
                                font: Kirigami.Theme.smallFont
                                color: Kirigami.Theme.neutralTextColor
                            }
                        }

                        PlasmaComponents3.Label {
                            Layout.fillWidth: true
                            visible: accountBlock.accountError.length > 0
                            text: accountBlock.accountError
                            font: Kirigami.Theme.smallFont
                            color: Kirigami.Theme.neutralTextColor
                            wrapMode: Text.WordWrap
                        }

                        PlasmaComponents3.Label {
                            Layout.fillWidth: true
                            visible: accountBlock.buckets.length === 0
                                     && accountBlock.accountError.length === 0
                            text: "No quota windows reported"
                            font: Kirigami.Theme.smallFont
                            opacity: 0.7
                        }

                        Repeater {
                            model: accountBlock.buckets

                            delegate: ColumnLayout {
                                id: bucketBlock

                                readonly property var rawFraction: fullRoot.field(modelData, "remaining_fraction")
                                readonly property bool unknown: {
                                    if (typeof rawFraction === "undefined" || rawFraction === null)
                                        return true;
                                    return !isFinite(Number(rawFraction));
                                }
                                readonly property real fractionValue: {
                                    if (unknown)
                                        return -1;
                                    return Math.max(0, Math.min(1, Number(rawFraction)));
                                }
                                readonly property string windowLabel: fullRoot.textOf(
                                    fullRoot.field(modelData, "window"), "quota")
                                readonly property string remainingLabel: unknown
                                    ? "unknown"
                                    : fullRoot.pct(fractionValue)
                                readonly property string descriptionLabel: fullRoot.textOf(
                                    fullRoot.field(modelData, "description"), "")
                                readonly property string resetLabel: {
                                    var reset = fullRoot.field(modelData, "reset_at");
                                    if (typeof reset === "undefined" || reset === null)
                                        return "";
                                    var text = String(reset);
                                    return text.length > 0 ? ("resets " + text) : "";
                                }

                                Layout.fillWidth: true
                                spacing: Math.round(Kirigami.Units.smallSpacing / 2)

                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: Kirigami.Units.smallSpacing

                                    PlasmaComponents3.Label {
                                        Layout.fillWidth: true
                                        text: bucketBlock.windowLabel
                                        font: Kirigami.Theme.smallFont
                                        elide: Text.ElideRight
                                    }

                                    PlasmaComponents3.Label {
                                        text: bucketBlock.remainingLabel
                                        font: Kirigami.Theme.smallFont
                                        color: bucketBlock.unknown
                                            ? Kirigami.Theme.neutralTextColor
                                            : Kirigami.Theme.textColor
                                    }
                                }

                                Bar {
                                    Layout.fillWidth: true
                                    fraction: bucketBlock.fractionValue
                                    fillColor: fullRoot.colourFor(bucketBlock.fractionValue)
                                }

                                PlasmaComponents3.Label {
                                    Layout.fillWidth: true
                                    visible: bucketBlock.resetLabel.length > 0
                                    text: bucketBlock.resetLabel
                                    font: Kirigami.Theme.smallFont
                                    opacity: 0.7
                                    elide: Text.ElideRight
                                }

                                PlasmaComponents3.Label {
                                    Layout.fillWidth: true
                                    visible: bucketBlock.descriptionLabel.length > 0
                                    text: bucketBlock.descriptionLabel
                                    font: Kirigami.Theme.smallFont
                                    opacity: 0.6
                                    wrapMode: Text.WordWrap
                                }
                            }
                        }
                    }
                }
            }

            // ==============================================================
            // (c) Footer
            // ==============================================================
            Rectangle {
                Layout.fillWidth: true
                Layout.topMargin: Kirigami.Units.smallSpacing
                implicitHeight: 1
                color: Qt.rgba(Kirigami.Theme.textColor.r,
                               Kirigami.Theme.textColor.g,
                               Kirigami.Theme.textColor.b,
                               0.2)
            }

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                text: fullRoot.footerText
                font: Kirigami.Theme.smallFont
                opacity: 0.7
                wrapMode: Text.WordWrap
                color: (fullRoot.offline || fullRoot.stale)
                    ? Kirigami.Theme.neutralTextColor
                    : Kirigami.Theme.textColor
            }

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                visible: fullRoot.errorText.length > 0
                text: fullRoot.errorText
                font: Kirigami.Theme.smallFont
                color: Kirigami.Theme.neutralTextColor
                wrapMode: Text.WordWrap
            }

            Item {
                Layout.fillWidth: true
                Layout.preferredHeight: Kirigami.Units.smallSpacing
            }
        }
    }
}
