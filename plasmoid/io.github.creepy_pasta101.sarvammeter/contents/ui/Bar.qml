// SPDX-License-Identifier: MIT
//
// Sarvam Meter - horizontal progress bar.
//
// A plain Rectangle-based bar: no PlasmaCore gauge and no shape-drawing
// module, so nothing here can break on Plasma 6.8.
//
//   fraction   0..1 remaining fraction; negative (or NaN) means "unknown" and
//              renders the indeterminate look: a dimmed, full-width fill.
//   fillColor  colour of the filled part.

import QtQuick
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

Rectangle {
    id: bar

    property real fraction: 0
    property color fillColor: Kirigami.Theme.highlightColor
    property color trackColor: Qt.rgba(Kirigami.Theme.textColor.r,
                                       Kirigami.Theme.textColor.g,
                                       Kirigami.Theme.textColor.b,
                                       0.15)

    // Defensive clamp: never let a bad fraction produce a negative or
    // overflowing width.
    readonly property bool indeterminate: isNaN(fraction) || fraction < 0
    readonly property real clampedFraction: indeterminate ? 1.0 : Math.max(0, Math.min(1, fraction))
    // Never thinner than max(4, smallSpacing).  All three size hints come from
    // this one constant, so there is no binding loop between them.
    readonly property int barThickness: Math.max(4, Kirigami.Units.smallSpacing)

    Layout.fillWidth: true
    Layout.preferredHeight: barThickness
    implicitWidth: Kirigami.Units.gridUnit * 4
    implicitHeight: barThickness
    height: barThickness

    radius: height / 2
    color: trackColor
    clip: true

    Rectangle {
        id: fill
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        width: Math.round(bar.width * bar.clampedFraction)
        radius: bar.radius
        color: bar.fillColor
        opacity: bar.indeterminate ? 0.35 : 1.0
    }
}
