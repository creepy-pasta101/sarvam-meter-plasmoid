pragma Singleton
import QtQuick

QtObject {
    readonly property int gridUnit: 18
    readonly property int smallSpacing: 4
    readonly property int largeSpacing: 8
    readonly property int fontMetricsM: 12
    readonly property QtObject iconSizes: QtObject {
        readonly property int small: 16
        readonly property int smallMedium: 22
        readonly property int medium: 32
        readonly property int large: 48
    }
}
