# SPDX-License-Identifier: MIT
"""The packaged systemd user unit, and what makes it work on a fresh install.

`systemctl --user enable --now sarvam-meter` has to succeed on a machine where
nothing has been configured yet, and has to respect the config once it exists.
These tests pin both halves: the unit file itself, and the CLI behaviour it
depends on (`init --if-missing`, and `serve` reading `proxy.enabled`).
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

from sarvam_meter.cli import should_start_proxy  # noqa: E402
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

    def test_the_proxy_is_left_to_the_config(self):
        # Hardcoding --with-proxy here would override `proxy.enabled: false`,
        # which is the default and what the settings page toggles.  Only the
        # directives count: the comment above explains the flag on purpose.
        directives = [
            line for line in self.unit.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertNotIn("--with-proxy", "\n".join(directives))

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


class ProxyDecisionTest(unittest.TestCase):
    """`serve` must honour `proxy.enabled`, since the unit cannot pass a flag."""

    def config(self, enabled):
        document = config_mod.default_config_dict()
        document["proxy"]["enabled"] = enabled
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "config.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        return config_mod.load_config(path)

    class Args(object):
        def __init__(self, with_proxy=False):
            self.with_proxy = with_proxy

    def test_the_config_alone_turns_the_proxy_on(self):
        self.assertTrue(should_start_proxy(self.Args(), self.config(True)))

    def test_the_config_alone_keeps_it_off(self):
        self.assertFalse(should_start_proxy(self.Args(), self.config(False)))

    def test_the_flag_still_forces_it_on(self):
        self.assertTrue(should_start_proxy(self.Args(True), self.config(False)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
