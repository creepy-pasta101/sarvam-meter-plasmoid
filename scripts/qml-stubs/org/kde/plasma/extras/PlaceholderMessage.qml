// SPDX-License-Identifier: MIT
//
// Stand-in for PlasmaExtras.PlaceholderMessage.
//
// It declares ONLY `text` and `explanation`, and it is deliberately NOT a
// Button: a Button carries an `icon` group, which made this stub more permissive
// than the real type, so `icon.name: ...` on a real PlaceholderMessage passed the
// harness and then failed in Plasma with "Cannot assign to non-existent property
// icon". A stub that accepts more than the real type is worse than no stub.
import QtQuick

Item {
    id: root

    property string text: ""
    property string explanation: ""
    implicitWidth: 200
    implicitHeight: 80
}
