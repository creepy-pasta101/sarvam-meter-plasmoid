// Button is the lightest QQC2 type that carries the grouped `icon.name`
// property the real PlaceholderMessage exposes; the stub draws nothing.
import QtQuick.Controls

Button {
    id: root

    background: null
    property string explanation: ""
    implicitWidth: 200
    implicitHeight: 80
}
