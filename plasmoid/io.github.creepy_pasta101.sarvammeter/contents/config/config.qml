// SPDX-License-Identifier: MIT
//
// Sarvam Meter - plasmoid configuration dialog entry point (unit U6).
//
// Plasma 6 (6.7 / 6.8): the widget configuration dialog reads the page list
// from this ConfigModel and pushes each category's page into a Kirigami page
// stack (see AppletConfiguration.qml in plasma-desktop). ConfigModel and
// ConfigCategory still live in the org.kde.plasma.configuration module; the
// removed Plasma 5 types (KCategorization, org.kde.kcm imports, configDialog
// .configuration bindings) are not used anywhere in this package.
//
// The keys themselves are declared in main.xml (KConfigXT). The cfg_<key>
// properties that actually carry the values live on the page root in
// configGeneral.qml, which is where the dialog looks for them.

import QtQuick

import org.kde.plasma.configuration

ConfigModel {
    ConfigCategory {
        name: "General"
        icon: "preferences-system-network"

        // Plasma 6 resolves a *relative* ConfigCategory.source through the
        // applet's fileUrl(), i.e. against the package's QML root
        // (contents/ui/), while this page lives next to this file in
        // contents/config/ (SPEC section 3). Handing over an absolute local
        // path makes the lookup independent of that base directory:
        // ConfigModel uses a source that is an absolute .qml path verbatim.
        source: Qt.resolvedUrl("configGeneral.qml").toString().replace("file://", "")
    }
}
