# SPDX-License-Identifier: MIT
"""The packaged systemd user unit, and what makes it work on a fresh install.

`systemctl --user enable --now sarvam-meter` has to succeed on a machine where
nothing has been configured yet, and has to respect the config once it exists.
These tests pin the unit file itself and the CLI behaviour it depends on
(`init --if-missing`).  The recording proxy is gone, so `serve` has nothing to
decide any more: it only binds the daemon's socket.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UNIT = os.path.join(REPO_ROOT, "packaging", "sarvam-meter.service")
SPEC = os.path.join(REPO_ROOT, "packaging", "plasma-applet-sarvam-meter.spec")

sys.path.insert(0, REPO_ROOT)

from sarvam_meter import config as config_mod  # noqa: E402


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class UnitFileTest(unittest.TestCase):
    def setUp(self):
        self.unit = read(UNIT)

    def test_it_is_a_user_unit(self):
        self.assertIn("[Unit]", self.unit)
        self.assertIn("[Service]", self.unit)
        self.assertIn("[Install]", self.unit)
        self.assertRegex(self.unit, r"(?m)^WantedBy=default\.target$")
        # A user unit must not carry a system-level WantedBy.
        self.assertNotIn("multi-user.target", self.unit)

    def test_it_runs_the_installed_binary(self):
        match = re.search(r"(?m)^ExecStart=(.*)$", self.unit)
        self.assertIsNotNone(match, "the unit has no ExecStart")
        self.assertEqual(match.group(1).strip(), "/usr/bin/sarvam-meter serve")

    def test_it_creates_a_config_but_never_overwrites_one(self):
        match = re.search(r"(?m)^ExecStartPre=(.*)$", self.unit)
        self.assertIsNotNone(match, "the unit has no ExecStartPre")
        line = match.group(1).strip()
        self.assertIn("/usr/bin/sarvam-meter init", line)
        self.assertIn("--if-missing", line)

    def test_it_points_the_daemon_at_the_users_own_config(self):
        self.assertRegex(self.unit, r"(?m)^Environment=SARVAM_METER_CONFIG=%h/")
        self.assertRegex(self.unit, r"(?m)^Type=simple$")
        self.assertRegex(self.unit, r"(?m)^Restart=on-failure$")

    def test_the_exec_lines_name_paths_that_exist_in_the_package(self):
        # The RPM installs the entry point to %{_bindir}; /usr/bin is that path
        # on Fedora. A typo here is the difference between "works" and
        # "status=203/EXEC".
        for line in re.findall(r"(?m)^Exec\w+=(.*)$", self.unit):
            binary = line.split()[0]
            if binary.startswith("-"):
                binary = binary[1:]
            self.assertTrue(
                binary.startswith("/usr/bin/") or binary.startswith("/bin/"),
                "Exec path %r is not an installed absolute path" % (binary,),
            )


class SpecTest(unittest.TestCase):
    def setUp(self):
        self.spec = read(SPEC)

    def test_the_unit_is_installed_and_owned(self):
        self.assertIn("%{_userunitdir}/sarvam-meter.service", self.spec)
        self.assertIn("packaging/sarvam-meter.service", self.spec)

    def test_the_systemd_macros_run_for_the_user(self):
        self.assertIn("%systemd_user_post", self.spec)
        self.assertIn("%systemd_user_postun", self.spec)

    def test_it_tells_the_user_how_to_enable_the_service(self):
        # A package must not enable a user unit for every account, so the
        # instructions have to be in %post.
        self.assertIn("systemctl --user enable --now sarvam-meter.service", self.spec)

    def test_qtwebengine_is_only_a_recommendation(self):
        self.assertIn("Recommends:", self.spec)
        self.assertIn("qt6-qtwebengine", self.spec)
        self.assertNotRegex(self.spec, r"(?m)^Requires:\s+qt6-qtwebengine")


class ReleaseMustChangeTest(unittest.TestCase):
    """A rebuilt RPM must be installable over the previous one.

    With a fixed Version-Release every CI build produces the same NEVRA, so
    `dnf install ./new.rpm` reports "already installed" and replaces nothing --
    the user reinstalls and sees the old files, which is exactly how a fixed
    settings page kept failing to appear.
    """

    def setUp(self):
        self.spec = read(SPEC)
        self.ci = read(os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml"))

    def test_the_release_is_a_parameter_with_a_default(self):
        self.assertRegex(self.spec, r"(?m)^%\{!\?sarvam_release:%global sarvam_release ")
        self.assertRegex(self.spec, r"(?m)^Release:\s+%\{sarvam_release\}%\{\?dist\}$")
        # No hardcoded release that would defeat the parameter.
        self.assertNotRegex(self.spec, r"(?m)^Release:\s+\d")

    def test_ci_passes_a_release_that_changes_per_build(self):
        self.assertIn('--define "sarvam_release ${RELEASE}"', self.ci)
        self.assertRegex(self.ci, r'RELEASE="1\.\$\(date -u \+%Y%m%d%H%M%S\)')
        # ... and it must sort above the installed 1.fc<N>, or dnf treats it as a
        # downgrade and refuses.
        self.assertIn('RELEASE="1.', self.ci)


class QmlDependenciesAreDeclaredTest(unittest.TestCase):
    """Every QML module the applet imports must be a declared dependency.

    A missing module is not a warning: the applet fails to load with "module
    <name> is not installed". Relying on plasma-workspace to pull the rest in
    transitively works until it does not -- plasma5support is a recent split-out.
    """

    #: import -> the Fedora package that provides it.  QtQuick* ships with the Qt
    #: runtime that plasma-workspace already requires, so it needs no line here.
    PROVIDED = {
        "QtQuick": None,
        "QtQuick.Controls": None,
        "QtQuick.Layouts": None,
        "org.kde.plasma.plasmoid": "plasma-workspace",
        "org.kde.plasma.components": "plasma-workspace",
        "org.kde.plasma.extras": "plasma-workspace",
        "org.kde.plasma.configuration": "plasma-workspace",
        "org.kde.plasma.plasma5support": "plasma5support",
        "org.kde.kirigami": "kf6-kirigami",
        "org.kde.kcmutils": "kf6-kcmutils",
        "QtWebEngine": "qt6-qtwebengine",
    }

    def imports(self):
        root = os.path.join(REPO_ROOT, "plasmoid", "io.github.creepy_pasta101.sarvammeter", "contents")
        found = set()
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if not name.endswith(".qml"):
                    continue
                with open(os.path.join(dirpath, name), encoding="utf-8") as handle:
                    for line in handle:
                        match = re.match(r"\s*import\s+([\w.]+)", line)
                        if match:
                            found.add(match.group(1))
        return found

    def test_every_import_maps_to_a_package(self):
        unknown = sorted(self.imports() - set(self.PROVIDED))
        self.assertEqual(
            unknown, [],
            "these imports are not mapped to a package; add them here and to the "
            "spec's Requires: %r" % (unknown,))

    def test_each_package_is_declared(self):
        spec = read(SPEC)
        declared = set(re.findall(r"(?m)^(?:Requires|Recommends):\s*(\S+)", spec))
        for module, package in sorted(self.PROVIDED.items()):
            if package is None:
                continue
            self.assertIn(
                package, declared,
                "%r is imported but %r is not in Requires/Recommends" % (module, package))


class SpecMacroSafetyTest(unittest.TestCase):
    """Comments in a spec are still macro-expanded.

    A comment containing `%Y%m%d` (as a shell date snippet does) makes rpmbuild
    fail with "%S: argument expected", so keep the timestamp out of the prose.
    """

    def test_no_stray_percent_in_comments(self):
        spec = read(SPEC)
        for number, line in enumerate(spec.splitlines(), 1):
            if line.lstrip().startswith("#"):
                # %% is the escape; %{...}/%name are intentional macros.
                stripped = re.sub(r"%%|%\{[^}]*\}|%[a-zA-Z_]+", "", line)
                self.assertNotIn("%", stripped,
                                 "line %d: a comment carries an rpm macro "
                                 "rpmbuild will try to expand: %r" % (number, line))


class ConfigPageCheckTest(unittest.TestCase):
    """`doctor` must be able to say which config page Plasma will load.

    A stale page is invisible: the dialog opens, every control shows its fallback
    and the only clue is console noise naming properties. This check turns that
    into one line of output naming the directory that won.
    """

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.env = dict(os.environ)
        self.env["XDG_DATA_HOME"] = self.home
        self.env["XDG_DATA_DIRS"] = "/nonexistent"

    def install(self, source_name, contents):
        from sarvam_meter.cli import PLASMOID_ID

        target = os.path.join(self.home, "plasma", "plasmoids", PLASMOID_ID,
                              "contents", "config")
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, source_name), "w", encoding="utf-8") as handle:
            handle.write(contents)
        return target

    def run_check(self):
        code = (
            "import os, sys;"
            "from sarvam_meter.cli import _config_page_check;"
            "ok, detail = _config_page_check();"
            "print(int(ok), detail)"
        )
        return subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                              capture_output=True, text=True, env=self.env)

    def test_a_missing_install_is_not_a_failure(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("1 "), result.stdout)
        self.assertIn("not installed", result.stdout)

    def test_a_current_page_passes_and_names_the_directory(self):
        self.install("main.xml", '<entry name="command"/><entry name="refreshSeconds"/>')
        self.install("configGeneral.qml",
                     "property string cfg_command: \"sarvam-meter\"\n"
                     "property string cfg_commandDefault: \"sarvam-meter\"\n"
                     "property alias cfg_refreshSeconds: spin.value\n"
                     "property int cfg_refreshSecondsDefault: 60\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("1 "), result.stdout)
        self.assertIn("plasmoids", result.stdout)

    def test_a_page_without_the_defaults_fails_and_names_the_property(self):
        self.install("main.xml", '<entry name="command"/>')
        self.install("configGeneral.qml", 'property string cfg_command: "sarvam-meter"\n')
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("0 "), result.stdout)
        self.assertIn("cfg_commandDefault", result.stdout)
        self.assertIn("kpackagetool6", result.stdout)


class InitIfMissingTest(unittest.TestCase):
    """`init --if-missing` is what ExecStartPre runs on every start."""

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.path = os.path.join(self.directory, "config.json")

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "sarvam_meter.cli", "init", "--config", self.path]
            + list(args),
            cwd=REPO_ROOT, capture_output=True, text=True,
        )

    def test_it_creates_a_config_when_there_is_none(self):
        result = self.run_cli("--if-missing")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.isfile(self.path))
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), "0o600")

    def test_it_works_without_a_config_argument(self):
        """The systemd unit passes no --config: this is the exact invocation.

        `_config_path()` returns None when the flag is absent, and an
        `os.path.isfile(None)` in the --if-missing branch made
        `systemctl --user enable --now sarvam-meter` fail with a TypeError before
        the daemon ever started.  Every other test here passes --config, which is
        why CI missed it.
        """
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        env = dict(os.environ)
        env["HOME"] = home
        env.pop("SARVAM_METER_CONFIG", None)

        result = subprocess.run(
            [sys.executable, "-m", "sarvam_meter.cli", "init", "--if-missing"],
            cwd=REPO_ROOT, capture_output=True, text=True, env=env,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        expected = os.path.join(home, ".config", "sarvam-meter", "config.json")
        self.assertTrue(os.path.isfile(expected), result.stdout)
        self.assertEqual(oct(os.stat(expected).st_mode & 0o777), "0o600")

        # And a second start (every service restart) is a quiet no-op.
        again = subprocess.run(
            [sys.executable, "-m", "sarvam_meter.cli", "init", "--if-missing"],
            cwd=REPO_ROOT, capture_output=True, text=True, env=env,
        )
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("already present", again.stderr)

    def test_it_honours_the_env_config_path(self):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        env = dict(os.environ)
        env["HOME"] = home
        alt = os.path.join(home, "alt.json")
        env["SARVAM_METER_CONFIG"] = alt

        result = subprocess.run(
            [sys.executable, "-m", "sarvam_meter.cli", "init", "--if-missing"],
            cwd=REPO_ROOT, capture_output=True, text=True, env=env,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.isfile(alt))

    def test_it_leaves_an_edited_config_alone(self):
        edited = config_mod.default_config_dict()
        edited["dashboard"]["poll_seconds"] = 900
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(edited, handle)
        before = read(self.path)

        result = self.run_cli("--if-missing")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(read(self.path), before)
        self.assertEqual(json.loads(read(self.path))["dashboard"]["poll_seconds"], 900)

    def test_plain_init_still_resets(self):
        edited = config_mod.default_config_dict()
        edited["dashboard"]["poll_seconds"] = 900
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(edited, handle)

        result = self.run_cli()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(read(self.path))["dashboard"]["poll_seconds"], 300
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
