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

    // Plasma places this item itself, so Layout.* hints would be ignored;
    // implicitWidth/implicitHeight are what the panel actually uses.
    implicitWidth: row.implicitWidth
    implicitHeight: Math.max(Kirigami.Units.iconSizes.small, row.implicitHeight)

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
            implicitWidth: Math.max(8, Math.round(Kirigami.Units.gridUnit * 0.55))
            implicitHeight: implicitWidth
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
