import QtQuick

Item {
    id: root

    property Item contentItem: null
    property int position: 0
    implicitHeight: contentItem ? contentItem.implicitHeight : 0
    implicitWidth: contentItem ? contentItem.implicitWidth : 0

    onContentItemChanged: {
        if (contentItem) {
            contentItem.parent = root;
            contentItem.anchors.fill = root;
        }
    }
}
