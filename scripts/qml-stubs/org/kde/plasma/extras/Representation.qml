// Minimal stand-in for PlasmaExtras.Representation: a ColumnLayout with a
// default `content` list, plus `header`/`footer` slots.
import QtQuick
import QtQuick.Layouts

Item {
    id: root

    default property alias content: column.data
    property Item header: null
    property Item footer: null
    property bool collapseMargins: false

    ColumnLayout {
        id: column
        anchors.fill: parent
        spacing: 0
    }

    onHeaderChanged: {
        if (header) {
            header.parent = column;
            header.Layout.fillWidth = true;
        }
    }
}
