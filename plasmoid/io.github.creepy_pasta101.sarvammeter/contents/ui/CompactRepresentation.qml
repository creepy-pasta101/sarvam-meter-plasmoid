// SPDX-License-Identifier: MIT
//
// Sarvam Meter - panel (compact) representation.
//
// Shows `compactValue` from the applet root (Sarvam remaining rupees, or the
// worst CLIProxyAPI remaining percentage) in a warning colour when it is below
// the configured threshold, plus a small "!" badge when the daemon is offline,
// the payload is stale, or Sarvam reported quota_exhausted.

import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.components as PlasmaComponents3
import org.kde.kirigami as Kirigami

Item {
    id: compactRoot

    // Assigned by main.qml; never assumed non-null.
    property var meter: null

    readonly property bool hasMeter: meter !== null && typeof meter !== "undefined"
    readonly property string valueText: hasMeter ? meter.compactValue : "\u2014"
    readonly property bool warning: hasMeter ? meter.compactWarning : false
    readonly property bool showBadge: hasMeter ? meter.showBadge : false

    // ---- panel sizing ---------------------------------------------------
    // The item has to ask for the width its content needs. A three-digit
    // balance plus the badge is wider than the default slot, and an item that is
    // handed less than it needs does not shrink: its children overflow its own
    // bounds and paint over the neighbouring applet. So the width is computed
    // from the *natural* text width, the label is allowed to elide when the
    // panel clamps anyway, and the whole thing is capped so a silly value cannot
    // eat the panel.
    readonly property int compactPadding: Math.max(1, Math.round(Kirigami.Units.smallSpacing * 0.5))
    readonly property int badgeWidth: Math.max(8, Math.round(Kirigami.Units.gridUnit * 0.55))
    readonly property int badgeSlot: compactRoot.showBadge
        ? (compactRoot.badgeWidth + Kirigami.Units.smallSpacing)
        : 0

    // Hidden measuring text, declared *before* the width that reads it so there
    // is no forward reference (`implicitWidth` is evaluated while the component
    // is still being built).  It renders nothing; `implicitWidth` on a Text is
    // its natural width, which is what the panel needs to be asked for.
    Text {
        id: valueMetrics
        visible: false
        text: compactRoot.valueText
        font.weight: compactRoot.warning ? Font.DemiBold : Font.Normal
    }

    readonly property int naturalWidth: Math.ceil(valueMetrics.implicitWidth)
        + compactRoot.badgeSlot + 2 * compactRoot.compactPadding
    readonly property int maximumCompactWidth: Kirigami.Units.gridUnit * 14
    readonly property int minimumCompactWidth: Kirigami.Units.gridUnit * 2

    implicitWidth: Math.max(compactRoot.minimumCompactWidth,
                            Math.min(compactRoot.maximumCompactWidth,
                                     compactRoot.naturalWidth))
    implicitHeight: Math.max(Kirigami.Units.iconSizes.small, row.implicitHeight)

    // A panel that honours these hands over the right slot; one that clamps
    // still leaves the label eliding rather than overlapping its neighbour.
    Layout.preferredWidth: implicitWidth
    Layout.minimumWidth: Math.min(implicitWidth, compactRoot.minimumCompactWidth)

    // Opening the popup is the compact representation's job, not Plasma's: the
    // panel item only forwards the gesture.  Every stock applet wires this up
    // itself, and without it a click does nothing at all.
    function toggleExpanded() {
        if (typeof Plasmoid !== "undefined" && Plasmoid)
            Plasmoid.expanded = !Plasmoid.expanded;
    }

    MouseArea {
        objectName: "compactClickArea"
        anchors.fill: parent
        // Left button only: the context menu and middle-click still reach the
        // panel, which is what a user expects from any other applet.
        acceptedButtons: Qt.LeftButton
        onClicked: compactRoot.toggleExpanded()
    }

    RowLayout {
        id: row
        anchors.fill: parent
        spacing: Kirigami.Units.smallSpacing

        PlasmaComponents3.Label {
            id: valueLabel
            Layout.alignment: Qt.AlignVCenter
            // Fill and allow shrinking to nothing: a squeezed panel must elide
            // the number, never push the badge out over the next applet.
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            Layout.preferredWidth: Math.ceil(valueMetrics.implicitWidth)
            text: compactRoot.valueText
            color: compactRoot.warning ? Kirigami.Theme.negativeTextColor : Kirigami.Theme.textColor
            font.weight: compactRoot.warning ? Font.DemiBold : Font.Normal
            elide: Text.ElideRight
            verticalAlignment: Text.AlignVCenter
        }

        Rectangle {
            id: badge
            visible: compactRoot.showBadge
            Layout.alignment: Qt.AlignVCenter
            // Fixed size: a Layout may shrink an item below its implicit size
            // unless it is pinned, and a squashed badge is unreadable.
            Layout.minimumWidth: compactRoot.badgeWidth
            Layout.maximumWidth: compactRoot.badgeWidth
            Layout.preferredWidth: compactRoot.badgeWidth
            implicitWidth: compactRoot.badgeWidth
            implicitHeight: compactRoot.badgeWidth
            radius: implicitWidth / 2
            color: Kirigami.Theme.neutralTextColor

            PlasmaComponents3.Label {
                anchors.centerIn: parent
                text: "!"
                color: Kirigami.Theme.backgroundColor
                font.bold: true
                font.pixelSize: Math.max(7, Math.round(parent.height * 0.8))
            }
        }
    }
}
