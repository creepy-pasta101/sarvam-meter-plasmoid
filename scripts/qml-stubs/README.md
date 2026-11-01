# QML stubs for the safety harness

Minimal stand-ins for the Plasma QML modules the applet uses
(`org.kde.plasma.extras`, `org.kde.plasma.components`, `org.kde.kirigami`), so
`scripts/qml-safety-harness.py` can load the real `main.qml`,
`CompactRepresentation.qml` and `FullRepresentation.qml` in a plain Qt engine on
a machine with no Plasma installed.

They are test doubles, not a theme: no layout fidelity, no styling, no icons.
They exist so a syntax error, a bad property name or a broken signal connection
fails in CI instead of on someone's desktop. Never install or ship them.
