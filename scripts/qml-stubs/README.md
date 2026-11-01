# QML stubs for the safety harness

Minimal stand-ins for the Plasma QML modules the applet uses
(`org.kde.plasma.extras`, `org.kde.plasma.components`, `org.kde.kirigami`,
`org.kde.plasma.plasma5support`), so `scripts/qml-safety-harness.py` can load the
real `main.qml`, `CompactRepresentation.qml` and `FullRepresentation.qml` in a
plain Qt engine on a machine with no Plasma installed.

They are test doubles, not a theme: no layout fidelity, no styling, no icons.
They exist so a syntax error, a bad property name or a broken signal connection
fails in CI instead of on someone's desktop. Never install or ship them.

## plasma5support/DataSource

The applet no longer speaks HTTP: it runs `<command> query <op>` through Plasma's
*executable* data source and reads the JSON on the child's stdout. The stub
mirrors the real API the widget uses - `engine`, `connectedSources`,
`connectSource()`, `disconnectSource()`, `data`, and the `newData(sourceName,
data)` signal - and delivers the same payload shape
(`{"stdout", "stderr", "exit code"}`).

A QML file cannot start a process, so the stub is passive on its own: the
harness supplies a QProcess-backed helper as the context property
`harnessExec`, and the stub forwards `connectSource`/`disconnectSource` to it
and relays what comes back through `newData`. With no helper present (a bare
`qmlscene` run) nothing ever answers, which is the honest behaviour of "no
process is running". `harnessExec.disconnectSource()` kills a child that is
still running, which is exactly what the widget's watchdog relies on, so the
harness can prove the widget gives up on a wedged child instead of leaking it.
