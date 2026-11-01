pragma Singleton
import QtQuick

QtObject {
    readonly property font defaultFont: Qt.font({ "pointSize": 10 })
    readonly property font smallFont: Qt.font({ "pointSize": 8 })
    readonly property color textColor: "#000000"
    readonly property color disabledTextColor: "#808080"
    readonly property color neutralTextColor: "#606060"
    readonly property color positiveTextColor: "#107c10"
    readonly property color negativeTextColor: "#c42b1c"
    readonly property color highlightColor: "#3daee9"
    readonly property color backgroundColor: "#eff0f1"
}
