// SPDX-License-Identifier: MIT
//
// Test double for org.kde.plasma.plasma5support's DataSource.
//
// The applet talks to the daemon by running `sarvam-meter query <op>` through
// Plasma's *executable* data source.  There is no Plasma in CI, so this stands
// in for it: the same names (engine, connectedSources, connectSource,
// disconnectSource, data, newData) and the same payload shape
// ({"stdout", "stderr", "exit code"}).
//
// It is passive by itself - a QML file cannot start a process - so the safety
// harness supplies a process-spawning helper as the context property
// `harnessExec` (see scripts/qml-safety-harness.py).  With no helper present
// (a bare qmlscene run) the stub simply never answers, which is the honest
// behaviour of "nothing is running".
//
// QtObject, not Item: the real DataSource is not an item, and `Item` already
// owns a `data` property that a var-typed `data` would shadow.

import QtQuick

QtObject {
    id: source

    // "executable" is the only engine the widget uses; the value is passed
    // through to the runner untouched so a wrong one fails visibly.
    property string engine: ""

    // Sources this instance is subscribed to, exactly like the real thing: the
    // widget adds one with connectSource() and removes it with
    // disconnectSource().
    property var connectedSources: []

    // Last payload per source name, as delivered.
    property var data: ({})

    // Emitted once per finished source: newData(sourceName, payload).
    signal newData(string sourceName, var data)

    // True when the harness supplied a process-spawning helper.
    readonly property bool hasRunner: (typeof harnessExec !== "undefined" && harnessExec !== null)

    Component.onCompleted: {
        if (source.hasRunner) {
            try {
                harnessExec.dataReady.connect(source.__onRunnerData);
            } catch (e) {
                // No helper signal: stay passive rather than throw.
            }
        }
    }

    function __onRunnerData(sourceName, payload) {
        if (source.connectedSources.indexOf(sourceName) < 0) {
            // Disconnected while the child was still running: drop the answer,
            // exactly as the real data source would.
            return;
        }
        source.__deliver(sourceName, payload);
    }

    function __deliver(sourceName, payload) {
        var map = source.data;
        map[sourceName] = payload;
        source.data = map;
        source.newData(sourceName, payload);
    }

    function connectSource(sourceName) {
        var name = String(sourceName);
        if (source.connectedSources.indexOf(name) < 0) {
            var list = [];
            for (var i = 0; i < source.connectedSources.length; i++)
                list.push(source.connectedSources[i]);
            list.push(name);
            source.connectedSources = list;
        }
        if (source.hasRunner) {
            try {
                harnessExec.connectSource(name);
            } catch (e) {
                // The helper refused; the source stays silent.
            }
        }
    }

    function disconnectSource(sourceName) {
        var name = String(sourceName);
        var kept = [];
        for (var i = 0; i < source.connectedSources.length; i++) {
            if (source.connectedSources[i] !== name)
                kept.push(source.connectedSources[i]);
        }
        source.connectedSources = kept;
        if (source.hasRunner) {
            try {
                harnessExec.disconnectSource(name);
            } catch (e) {
                // Nothing was running.
            }
        }
    }

    function disconnectAllSources() {
        var names = [];
        for (var i = 0; i < source.connectedSources.length; i++)
            names.push(source.connectedSources[i]);
        for (var j = 0; j < names.length; j++)
            source.disconnectSource(names[j]);
    }
}
