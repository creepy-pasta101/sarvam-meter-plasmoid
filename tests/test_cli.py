# SPDX-License-Identifier: MIT
"""End-to-end tests for the ``sarvam-meter`` command line (SPEC section 12).

Every test drives :func:`sarvam_meter.cli.main` exactly as a shell would, with
stdout/stderr captured through ``contextlib.redirect_*``.  Nothing here touches
the real ``$HOME``: ``SARVAM_METER_CONFIG`` and every ``--config`` path point
into a throwaway temporary directory, and the ledger lives beside it.

The CLI is documented to *return* its exit code (``argparse``'s ``SystemExit``
is swallowed by ``main``), so every assertion checks both halves of that
contract: no ``SystemExit`` escapes, and the returned code is the documented
one (``0`` ok / ``1`` runtime error / ``2`` config or usage error).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import socket
import sqlite3
import tempfile
import unittest
from unittest import mock

from sarvam_meter import config as config_mod
from sarvam_meter.cli import main, render_status

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_CONFIG = 2

#: The six checks ``doctor`` performs with the default config
#: (``cliproxy.enabled`` is false, ``dashboard.enabled`` is true, so the
#: dashboard contributes its session *and* reachability checks).
DOCTOR_CHECKS = (
    "config loads",
    "data dir writable",
    "db opens",
    "daemon socket free",
    "dashboard session",
    "dashboard reachable",
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def run_cli(argv):
    """Run ``main(argv)``; return ``(code, stdout, stderr, escaped_system_exit)``.

    ``escaped_system_exit`` is the ``SystemExit`` argparse would have raised had
    ``main`` not handled it.  It must always be ``None``.
    """
    out = io.StringIO()
    err = io.StringIO()
    escaped = None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(list(argv))
        except SystemExit as exc:  # pragma: no cover - only on a regression
            escaped = exc
            code = exc.code if isinstance(exc.code, int) else EXIT_CONFIG
    return code, out.getvalue(), err.getvalue(), escaped


def read_events(db_path):
    """Every ledger row, oldest first, as plain dicts (``[]`` when never created)."""
    if not os.path.isfile(db_path):
        return []
    connection = sqlite3.connect(db_path)
    try:
        connection.row_factory = sqlite3.Row
        try:
            return [
                dict(row)
                for row in connection.execute("SELECT * FROM events ORDER BY id")
            ]
        except sqlite3.OperationalError:
            return []
    finally:
        connection.close()


def read_balance_row(db_path):
    connection = sqlite3.connect(db_path)
    try:
        connection.row_factory = sqlite3.Row
        rows = [dict(row) for row in connection.execute("SELECT * FROM balance")]
        return rows[0] if rows else None
    finally:
        connection.close()


class CliTestCase(unittest.TestCase):
    """Shared temporary config/ledger setup with a fully isolated environment."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sarvam-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self.config_path = os.path.join(self.tmp, "config.json")
        self.db_path = os.path.join(self.tmp, "usage.db")
        # Never read or write the real ~/.config/sarvam-meter/session.json.
        self.session_path = os.path.join(self.tmp, "session.json")
        # Where the daemon's socket lives for this test.  `doctor` resolves the
        # path exactly the way the daemon and the client do, so pinning it here
        # keeps every check inside the throwaway directory.
        self.socket_path = os.path.join(self.tmp, "run", "daemon.sock")

        saved = {}
        for name in (
            "SARVAM_METER_CONFIG",
            "SARVAM_METER_CLIPROXY_KEY",
            "SARVAM_METER_SOCKET",
        ):
            saved[name] = os.environ.get(name)
        self.addCleanup(self._restore_env, saved)
        os.environ["SARVAM_METER_CONFIG"] = self.config_path
        os.environ["SARVAM_METER_SOCKET"] = self.socket_path
        os.environ.pop("SARVAM_METER_CLIPROXY_KEY", None)

    @staticmethod
    def _restore_env(saved):
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def write_config(self, path=None, **sections):
        """Write a valid config document; returns its path."""
        path = path or self.config_path
        document = config_mod.default_config_dict()
        document["db_path"] = self.db_path
        document["dashboard"]["session_file"] = self.session_path
        for section, values in sections.items():
            document[section].update(values)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2)
        return path

    def assert_exit(self, argv, expected):
        """Run *argv* and assert the documented exit code, no escaping SystemExit."""
        code, out, err, escaped = run_cli(argv)
        self.assertIsNone(
            escaped, "main() must not let argparse's SystemExit escape: %r" % (argv,)
        )
        self.assertEqual(
            code, expected, "argv=%r stdout=%r stderr=%r" % (argv, out, err)
        )
        return out, err


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


class InitTest(CliTestCase):
    def test_init_writes_default_config_0600_and_prints_path(self):
        target = os.path.join(self.tmp, "fresh", "config.json")
        code, out, err, escaped = run_cli(["init", "--config", target])

        self.assertIsNone(escaped)
        self.assertEqual(code, EXIT_OK, err)
        self.assertTrue(os.path.isfile(target))
        self.assertEqual(out.strip(), target)

        mode = os.stat(target).st_mode & 0o777
        self.assertEqual(mode, 0o600, "config file must be 0600, got %o" % mode)
        parent_mode = os.stat(os.path.dirname(target)).st_mode & 0o777
        self.assertEqual(
            parent_mode, 0o700, "created config dir must be 0700, got %o" % parent_mode
        )

        with open(target, "r", encoding="utf-8") as handle:
            written = json.load(handle)
        self.assertEqual(written, config_mod.default_config_dict())

    def test_init_defaults_to_the_env_config_path(self):
        code, out, err, escaped = run_cli(["init"])

        self.assertIsNone(escaped)
        self.assertEqual(code, EXIT_OK, err)
        self.assertTrue(os.path.isfile(self.config_path))
        self.assertEqual(out.strip(), self.config_path)

    def test_init_writes_exactly_the_packaged_example(self):
        target = os.path.join(self.tmp, "packaged", "config.json")
        self.assert_exit(["init", "--config", target], EXIT_OK)
        example = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "packaging",
            "config.example.json",
        )
        with open(target, "r", encoding="utf-8") as handle:
            written = json.load(handle)
        with open(example, "r", encoding="utf-8") as handle:
            packaged = json.load(handle)
        self.assertEqual(written, packaged)
        self.assertEqual(written, config_mod.default_config_dict())

    def test_init_then_load_round_trips(self):
        target = os.path.join(self.tmp, "round", "config.json")
        self.assert_exit(["init", "--config", target], EXIT_OK)

        loaded = config_mod.load_config(target)
        self.assertEqual(loaded.version, 1)
        self.assertIsNone(loaded.daemon.socket_path)
        self.assertEqual(loaded.dashboard.poll_seconds, 300)
        self.assertEqual(loaded.sarvam.currency, "INR")
        # The recording proxy is gone: a config must not carry a proxy section
        # and the loaded Config must not expose one.
        self.assertFalse(hasattr(loaded, "proxy"))


# ---------------------------------------------------------------------------
# balance
# ---------------------------------------------------------------------------


class BalanceTest(CliTestCase):
    def test_balance_set_then_show_round_trips(self):
        self.write_config()

        out, _err = self.assert_exit(["balance", "set", "500"], EXIT_OK)
        self.assertIn("INR", out)
        self.assertIn("500.00", out)

        row = read_balance_row(self.db_path)
        self.assertIsNotNone(row, "balance row must exist after `balance set`")
        self.assertEqual(row["starting"], 500.0)
        self.assertEqual(row["currency"], "INR")
        self.assertTrue(row["set_at"].endswith("Z"), row["set_at"])

        out, _err = self.assert_exit(["balance", "show"], EXIT_OK)
        self.assertIn("INR 500.00", out)
        self.assertIn(row["set_at"], out)

    def test_balance_show_without_a_balance(self):
        self.write_config()
        out, _err = self.assert_exit(["balance", "show"], EXIT_OK)
        self.assertIn("No starting balance set", out)

    def test_balance_set_rejects_a_non_numeric_amount(self):
        self.write_config()
        out, err = self.assert_exit(["balance", "set", "lots"], EXIT_CONFIG)
        self.assertEqual(out, "")
        self.assertIn("invalid amount", err)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


class StatusTest(CliTestCase):
    TOP_KEYS = {"schema", "generated_at", "stale", "errors", "sarvam", "cliproxy"}
    SARVAM_KEYS = {
        "configured",
        "currency",
        "balance",
        "spend",
        "calls",
        "by_api",
        "dashboard",
    }
    CLIPROXY_KEYS = {
        "configured",
        "reachable",
        "api_version",
        "worst_fraction",
        "accounts",
    }

    def test_status_json_has_the_schema_2_shape(self):
        self.write_config()
        self.assert_exit(["balance", "set", "500"], EXIT_OK)

        out, err = self.assert_exit(["status", "--json"], EXIT_OK)
        payload = json.loads(out)  # must be parseable

        self.assertEqual(set(payload), self.TOP_KEYS)
        self.assertEqual(payload["schema"], 2)
        self.assertIsInstance(payload["generated_at"], str)
        self.assertIn("T", payload["generated_at"])
        self.assertIs(payload["stale"], False)
        self.assertEqual(payload["errors"], [])

        sarvam = payload["sarvam"]
        self.assertEqual(set(sarvam), self.SARVAM_KEYS)
        self.assertEqual(sarvam["currency"], "INR")
        # A starting balance was set, but only Sarvam can report a balance: with
        # no dashboard figure there is no "left" number at all.
        self.assertIs(sarvam["balance"]["known"], False)
        self.assertIsNone(sarvam["balance"]["starting"])
        self.assertIsNone(sarvam["balance"]["remaining"])
        self.assertEqual(sarvam["balance"]["spent_total"], 0.0)
        self.assertEqual(
            sarvam["spend"], {"today": 0.0, "month": 0.0, "source": "ledger"}
        )
        self.assertEqual(sarvam["balance"]["source"], "ledger")
        # The dashboard source is on by default; `status` never polls it, so the
        # block is configured but not yet reachable.
        self.assertTrue(sarvam["dashboard"]["configured"])
        self.assertFalse(sarvam["dashboard"]["reachable"])
        self.assertIsNone(sarvam["dashboard"]["error"])
        self.assertEqual(sarvam["calls"], {"today": 0})
        self.assertEqual(sarvam["by_api"], [])

        self.assertEqual(set(payload["cliproxy"]), self.CLIPROXY_KEYS)
        self.assertIs(payload["cliproxy"]["configured"], False)
        self.assertIs(payload["cliproxy"]["reachable"], False)
        self.assertIsNone(payload["cliproxy"]["worst_fraction"])
        self.assertEqual(payload["cliproxy"]["accounts"], [])

    def test_status_human_output_with_a_starting_balance_still_says_spend_only(self):
        self.write_config()
        self.assert_exit(["balance", "set", "500"], EXIT_OK)

        out, _err = self.assert_exit(["status"], EXIT_OK)
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertTrue(lines, "status printed nothing")
        sarvam_lines = [line for line in lines if "Sarvam" in line]
        self.assertEqual(len(sarvam_lines), 1, out)
        self.assertIn("INR", sarvam_lines[0])
        # A starting balance is not a balance: no "left" figure is claimed.
        self.assertIn("spend only", sarvam_lines[0])
        self.assertNotIn("left", sarvam_lines[0])

    def test_status_human_output_says_left_only_for_a_reported_balance(self):
        # The "X left" wording still exists; it is reached only with a balance
        # the dashboard reported (the CLI itself never polls the dashboard).
        known = {
            "schema": 2,
            "generated_at": "2026-11-15T10:00:00+05:30",
            "stale": False,
            "errors": [],
            "sarvam": {
                "configured": True,
                "currency": "INR",
                "balance": {
                    "known": True,
                    "remaining": 412.6,
                    "starting": 500.0,
                    "spent_total": 87.4,
                    "set_at": "2026-10-20T09:00:00Z",
                    "source": "dashboard",
                },
                "spend": {"today": 12.3, "month": 87.4, "source": "dashboard"},
                "calls": {"today": 42},
                "by_api": [],
                "dashboard": {"configured": True, "reachable": True},
            },
            "cliproxy": {
                "configured": False,
                "reachable": False,
                "api_version": "v8",
                "worst_fraction": None,
                "accounts": [],
            },
        }
        line = render_status(known)[0]
        self.assertIn("INR 412.60 left", line)
        self.assertIn("12.30 today", line)
        self.assertIn("42 calls", line)

    def test_status_human_output_without_a_balance_says_spend_only(self):
        self.write_config()
        out, _err = self.assert_exit(["status"], EXIT_OK)
        self.assertIn("INR spend only", out)


# ---------------------------------------------------------------------------
# record
# ---------------------------------------------------------------------------


class RecordTest(CliTestCase):
    def test_record_prices_with_the_rate_card(self):
        self.write_config()

        out, err = self.assert_exit(
            [
                "record",
                "--api",
                "chat",
                "--quantity",
                "1000",
                "--unit",
                "tokens",
                "--model",
                "sarvam-105b",
            ],
            EXIT_OK,
        )
        # 1000 input tokens at INR 15 / million = 0.015
        self.assertIn("0.015000", out, err)

        rows = read_events(self.db_path)
        self.assertEqual(len(rows), 1, rows)
        event = rows[0]
        self.assertEqual(event["source"], "manual")
        self.assertEqual(event["api"], "chat")
        self.assertEqual(event["model"], "sarvam-105b")
        self.assertEqual(event["unit"], "tokens")
        self.assertEqual(event["quantity"], 1000.0)
        self.assertAlmostEqual(event["cost"], 0.015, places=9)
        self.assertEqual(event["estimated"], 0)
        self.assertIsNone(event["status"])

    def test_record_counts_towards_status_spend_and_calls(self):
        self.write_config()
        self.assert_exit(["balance", "set", "500"], EXIT_OK)
        self.assert_exit(
            [
                "record",
                "--api",
                "chat",
                "--quantity",
                "1000",
                "--unit",
                "tokens",
                "--model",
                "sarvam-105b",
            ],
            EXIT_OK,
        )

        out, _err = self.assert_exit(["status", "--json"], EXIT_OK)
        payload = json.loads(out)
        sarvam = payload["sarvam"]
        self.assertEqual(sarvam["calls"]["today"], 1)
        self.assertEqual([entry["api"] for entry in sarvam["by_api"]], ["chat"])
        # money() rounds to 2 dp for display; the ledger keeps 6.
        self.assertEqual(sarvam["balance"]["spent_total"], round(0.015, 2))
        # The spend is counted, but 500 - 0.02 is not turned into a balance.
        self.assertFalse(sarvam["balance"]["known"])
        self.assertIsNone(sarvam["balance"]["remaining"])
        self.assertIsNone(sarvam["balance"]["starting"])

    def test_record_on_an_unpublished_rate_is_free_and_flagged_estimated(self):
        self.write_config()

        out, err = self.assert_exit(
            ["record", "--api", "translate", "--quantity", "1000", "--unit", "chars"],
            EXIT_OK,
        )
        self.assertIn("0.000000", out, err)

        rows = read_events(self.db_path)
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["api"], "translate")
        self.assertEqual(rows[0]["cost"], 0.0)
        self.assertEqual(rows[0]["estimated"], 1, "an unpublished rate is untracked")

        out, _err = self.assert_exit(["status", "--json"], EXIT_OK)
        sarvam = json.loads(out)["sarvam"]
        # The event is counted and listed; it is just priced at 0.
        self.assertEqual(sarvam["calls"]["today"], 1)
        self.assertEqual(sarvam["spend"]["today"], 0.0)
        self.assertEqual([entry["api"] for entry in sarvam["by_api"]], ["translate"])

    def test_record_rejects_a_negative_quantity(self):
        self.write_config()
        _out, err = self.assert_exit(
            ["record", "--api", "chat", "--quantity", "-5", "--unit", "tokens"],
            EXIT_CONFIG,
        )
        self.assertIn("invalid quantity", err)
        self.assertEqual(read_events(self.db_path), [])


# ---------------------------------------------------------------------------
# exit codes
# ---------------------------------------------------------------------------


class ExitCodeTest(CliTestCase):
    def test_missing_config_file_is_a_config_error(self):
        missing = os.path.join(self.tmp, "does-not-exist.json")
        _out, err = self.assert_exit(["status", "--config", missing], EXIT_CONFIG)
        self.assertIn("config file not found", err)
        self.assertIn(missing, err)

    def test_malformed_json_config_is_a_config_error(self):
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write('{"version": 1, "daemon": {')
        _out, err = self.assert_exit(["status"], EXIT_CONFIG)
        self.assertIn("invalid JSON", err)
        self.assertIn(self.config_path, err)

    def test_invalid_config_value_is_a_config_error(self):
        self.write_config(daemon={"refresh_seconds": 0})
        _out, err = self.assert_exit(["status"], EXIT_CONFIG)
        self.assertIn("refresh_seconds", err)

    def test_unknown_option_is_a_usage_error(self):
        self.write_config()
        _out, err = self.assert_exit(["status", "--bogus"], EXIT_CONFIG)
        self.assertIn("usage:", err)

    def test_missing_required_argument_is_a_usage_error(self):
        self.write_config()
        _out, err = self.assert_exit(["record", "--api", "chat"], EXIT_CONFIG)
        self.assertIn("required", err)

    def test_no_subcommand_is_a_usage_error(self):
        _out, err = self.assert_exit([], EXIT_CONFIG)
        self.assertIn("a command is required", err)

    def test_help_is_not_an_error(self):
        self.assert_exit(["--help"], EXIT_OK)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


class DoctorTest(CliTestCase):
    def _write_session(self):
        """A valid 0600 session file: the dashboard is on by default."""
        with open(self.session_path, "w", encoding="utf-8") as handle:
            json.dump({"Cookie": "a=1"}, handle)
        os.chmod(self.session_path, 0o600)

    def test_doctor_reports_ok_for_a_healthy_install(self):
        self.write_config()
        self._write_session()

        with mock.patch(
            "sarvam_meter.daemon.fetch_dashboard_snapshot",
            return_value=_dashboard_snapshot(),
        ):
            out, err = self.assert_exit(["doctor"], EXIT_OK)
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), len(DOCTOR_CHECKS), out)
        for line in lines:
            self.assertTrue(
                line.startswith("OK "), "every check must print OK, got %r" % (line,)
            )
        for name in DOCTOR_CHECKS:
            self.assertIn(name, out)
        self.assertNotIn("FAIL", out)

    def test_doctor_fails_when_a_daemon_holds_the_socket(self):
        from sarvam_meter.daemon import Daemon

        self.write_config()
        self._write_session()
        # A live daemon on the very path `doctor` probes: the socket exists, it
        # is a socket, and something answers on it -- so it is not free.
        daemon = Daemon(
            config_mod.load_config(self.config_path),
            logger=lambda _line: None,
            socket_path=self.socket_path,
        )
        daemon.start()
        self.addCleanup(daemon.shutdown)
        with mock.patch(
            "sarvam_meter.daemon.fetch_dashboard_snapshot",
            return_value=_dashboard_snapshot(),
        ):
            out, _err = self.assert_exit(["doctor"], EXIT_RUNTIME)

        fail_lines = [line for line in out.splitlines() if line.startswith("FAIL ")]
        self.assertTrue(fail_lines, out)
        self.assertIn("FAIL daemon socket free", out)
        self.assertIn("OK   config loads", out)
        # The busy socket is the *only* failure on an otherwise healthy box:
        # the dashboard session is captured and the snapshot is mocked, so the
        # dashboard checks must not add a second FAIL.
        self.assertEqual(len(fail_lines), 1, out)
        self.assertTrue(fail_lines[0].startswith("FAIL daemon socket free"), out)
        self.assertIn("OK   dashboard session", out)
        self.assertIn("OK   dashboard reachable", out)
        # One OK/FAIL line per check, no more and no fewer.
        self.assertEqual(
            len([line for line in out.splitlines() if line.strip()]),
            len(DOCTOR_CHECKS),
            out,
        )

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                     "root ignores permission bits, so the failure cannot be reproduced")
    def test_doctor_fails_when_the_data_dir_is_unwritable(self):
        readonly = os.path.join(self.tmp, "readonly")
        os.makedirs(readonly)
        self.db_path = os.path.join(readonly, "usage.db")
        self.write_config()

        os.chmod(readonly, 0o500)
        # Cleanups are LIFO, so this runs before the tree is removed.
        self.addCleanup(os.chmod, readonly, 0o700)

        out, _err = self.assert_exit(["doctor"], EXIT_RUNTIME)
        self.assertIn("FAIL data dir writable", out)
        self.assertIn("OK   config loads", out)

    def test_doctor_without_a_config_fails_and_still_prints_every_check(self):
        missing = os.path.join(self.tmp, "nope.json")
        out, _err = self.assert_exit(["doctor", "--config", missing], EXIT_RUNTIME)

        lines = [line for line in out.splitlines() if line.strip()]
        # "config loads" plus the six checks it could not run.
        self.assertEqual(len(lines), 7, out)
        for line in lines:
            self.assertTrue(line.startswith("FAIL "), line)
        self.assertIn("FAIL config loads", out)


# ---------------------------------------------------------------------------
# session
# ---------------------------------------------------------------------------


def _dashboard_snapshot(**overrides):
    snapshot = {
        "configured": True,
        "reachable": True,
        "as_of": "2026-11-15T04:30:00Z",
        "org_id": "019f37be-0000-7000-8000-000000000000",
        "currency": "INR",
        "window": {"start": "2026-10-20", "end": "2026-11-15"},
        "spend": {"window": 134.23, "today": 12.3},
        "by_model": [{"model": "saaras:v4", "cost": 74.05}],
        "usage": [],
        "error": None,
    }
    snapshot.update(overrides)
    return snapshot


class SessionTest(CliTestCase):
    COOKIE = "session-cookie-VALUE-9f3a"
    BEARER = "Bearer bearer-VALUE-77c1"

    def test_session_set_writes_0600_inside_0700_and_never_prints_values(self):
        nested = os.path.join(self.tmp, "cfgdir", "session.json")
        self.write_config(dashboard={"enabled": True, "session_file": nested})

        out, err = self.assert_exit(
            ["session", "set", "--cookie", self.COOKIE, "--authorization", self.BEARER],
            EXIT_OK,
        )
        self.assertIn(nested, out)
        self.assertNotIn(self.COOKIE, out + err)
        self.assertNotIn(self.BEARER, out + err)

        self.assertTrue(os.path.isfile(nested))
        self.assertEqual(os.stat(nested).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(nested)).st_mode & 0o777, 0o700)
        with open(nested, "r", encoding="utf-8") as handle:
            document = json.load(handle)
        self.assertEqual(
            document, {"Cookie": self.COOKIE, "Authorization": self.BEARER}
        )
        # atomic write leaves no temporary file behind
        self.assertEqual(
            [n for n in os.listdir(os.path.dirname(nested)) if n.endswith(".tmp")
             or n.startswith(".session-")],
            [],
        )

    def test_session_set_reads_the_cookie_from_a_file(self):
        cookie_file = os.path.join(self.tmp, "cookie.txt")
        with open(cookie_file, "w", encoding="utf-8") as handle:
            handle.write(self.COOKIE + "\n")
        self.write_config(dashboard={"session_file": self.session_path})

        self.assert_exit(["session", "set", "--cookie-file", cookie_file], EXIT_OK)
        with open(self.session_path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
        self.assertEqual(document, {"Cookie": self.COOKIE})

    def test_session_set_refuses_a_newline_in_the_value(self):
        self.write_config(dashboard={"session_file": self.session_path})
        out, err = self.assert_exit(
            ["session", "set", "--cookie", "a=1\nX-Injected: 1"], EXIT_CONFIG
        )
        self.assertEqual(out, "")
        self.assertIn("newline", err)
        self.assertFalse(os.path.exists(self.session_path))

    def test_session_set_refuses_a_newline_in_the_authorization(self):
        self.write_config(dashboard={"session_file": self.session_path})
        _out, err = self.assert_exit(
            ["session", "set", "--cookie", "a=1", "--authorization", "Bearer x\ny"],
            EXIT_CONFIG,
        )
        self.assertIn("newline", err)
        self.assertFalse(os.path.exists(self.session_path))

    def test_session_set_requires_a_source(self):
        self.write_config()
        _out, err = self.assert_exit(["session", "set"], EXIT_CONFIG)
        self.assertIn("required", err)

    def test_session_set_refuses_an_empty_cookie(self):
        self.write_config(dashboard={"session_file": self.session_path})
        _out, err = self.assert_exit(["session", "set", "--cookie", "   "], EXIT_CONFIG)
        self.assertIn("must not be empty", err)

    def test_session_set_rejects_two_sources_at_once(self):
        self.write_config()
        _out, err = self.assert_exit(
            ["session", "set", "--cookie", "a", "--cookie-file", "/tmp/x"], EXIT_CONFIG
        )
        self.assertIn("usage:", err)

    def test_session_show_prints_path_mode_and_presence_but_no_value(self):
        self.write_config(dashboard={"session_file": self.session_path})
        self.assert_exit(["session", "set", "--cookie", self.COOKIE], EXIT_OK)

        out, err = self.assert_exit(["session", "show"], EXIT_OK)
        self.assertIn(self.session_path, out)
        self.assertIn("0600", out)
        self.assertIn("Cookie: present", out)
        self.assertIn("Authorization: absent", out)
        self.assertNotIn(self.COOKIE, out + err)

    def test_session_show_reports_a_missing_file(self):
        self.write_config(dashboard={"session_file": self.session_path})
        out, _err = self.assert_exit(["session", "show"], EXIT_OK)
        self.assertIn(self.session_path, out)
        self.assertIn("missing", out)
        self.assertIn("Cookie: absent", out)
        self.assertIn("Authorization: absent", out)

    def test_session_show_reports_a_corrupt_file_without_a_traceback(self):
        self.write_config(dashboard={"session_file": self.session_path})
        with open(self.session_path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        os.chmod(self.session_path, 0o600)
        out, err = self.assert_exit(["session", "show"], EXIT_RUNTIME)
        self.assertEqual(out, "")
        self.assertIn("invalid JSON", err)


# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------


class DashboardCommandTest(CliTestCase):
    def test_dashboard_reports_not_configured_when_disabled(self):
        self.write_config(dashboard={"enabled": False})
        out, err = self.assert_exit(["dashboard"], EXIT_OK)
        self.assertIn("not configured", out)
        self.assertEqual(err, "")

    def test_dashboard_json_is_the_status_block(self):
        # Enabled by default with no session file: configured but unreachable,
        # which is a runtime error (the command could not do what was asked).
        self.write_config()
        out, _err = self.assert_exit(["dashboard", "--json"], EXIT_RUNTIME)
        block = json.loads(out)
        self.assertTrue(block["configured"])
        self.assertFalse(block["reachable"])
        self.assertEqual(block["error"]["code"], "no_session")
        self.assertEqual(
            set(block),
            {
                "configured",
                "reachable",
                "as_of",
                "org_id",
                "currency",
                "window",
                "spend",
                "balance",
                "by_model",
                "error",
            },
        )

    def test_dashboard_prints_a_reachable_snapshot(self):
        self.write_config(dashboard={"enabled": True, "session_file": self.session_path})
        with mock.patch(
            "sarvam_meter.daemon.fetch_dashboard_snapshot",
            return_value=_dashboard_snapshot(),
        ):
            out, _err = self.assert_exit(["dashboard"], EXIT_OK)
        self.assertIn("reachable", out)
        self.assertIn("134.23", out)
        self.assertIn("saaras:v4", out)

    def test_dashboard_exits_runtime_when_the_snapshot_has_an_error(self):
        self.write_config(dashboard={"enabled": True, "session_file": self.session_path})
        failing = _dashboard_snapshot(
            reachable=False,
            spend={"window": 0.0, "today": 0.0},
            by_model=[],
            error={"source": "dashboard", "code": "no_session", "message": "no session"},
        )
        with mock.patch(
            "sarvam_meter.daemon.fetch_dashboard_snapshot", return_value=failing
        ):
            out, _err = self.assert_exit(["dashboard"], EXIT_RUNTIME)
        self.assertIn("unreachable", out)
        self.assertIn("dashboard: no_session", out)


# ---------------------------------------------------------------------------
# doctor: dashboard checks
# ---------------------------------------------------------------------------


class DoctorDashboardTest(CliTestCase):
    def _write_session(self, mode=0o600, document=None):
        directory = os.path.dirname(self.session_path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with open(self.session_path, "w", encoding="utf-8") as handle:
            json.dump(document or {"Cookie": "a=1"}, handle)
        os.chmod(self.session_path, mode)

    def test_enabled_without_a_session_file_is_not_configured_not_a_failure(self):
        # Scraping is the default source, so a fresh install has no session yet.
        # That is a setup step with a one-line fix, not something to fail on.
        self.write_config(dashboard={"enabled": True, "session_file": self.session_path})
        out, _err = self.assert_exit(["doctor"], EXIT_OK)
        self.assertIn("OK   dashboard session", out)
        self.assertIn("not configured", out)
        self.assertIn("OK   dashboard reachable", out)
        self.assertNotIn("FAIL", out)

    def test_a_session_file_with_loose_permissions_fails(self):
        self.write_config(dashboard={"enabled": True, "session_file": self.session_path})
        self._write_session(document={"Cookie": "a=1"})
        os.chmod(self.session_path, 0o644)
        out, _err = self.assert_exit(["doctor"], EXIT_RUNTIME)
        self.assertIn("FAIL dashboard session", out)
        self.assertIn("0600", out)

    def test_a_good_session_file_passes_the_session_check(self):
        self.write_config(dashboard={"enabled": True, "session_file": self.session_path})
        self._write_session(document={"Cookie": "a=1", "Authorization": "Bearer b"})
        with mock.patch(
            "sarvam_meter.daemon.fetch_dashboard_snapshot",
            return_value=_dashboard_snapshot(),
        ):
            out, _err = self.assert_exit(["doctor"], EXIT_OK)
        self.assertIn("OK   dashboard session", out)
        self.assertIn("OK   dashboard reachable", out)
        self.assertNotIn("FAIL", out)

    def test_a_world_readable_session_file_fails(self):
        self.write_config(dashboard={"enabled": True, "session_file": self.session_path})
        self._write_session(mode=0o644)
        with mock.patch(
            "sarvam_meter.daemon.fetch_dashboard_snapshot",
            return_value=_dashboard_snapshot(),
        ):
            out, _err = self.assert_exit(["doctor"], EXIT_RUNTIME)
        self.assertIn("FAIL dashboard session", out)
        self.assertIn("0644", out)

    def test_unexpected_keys_fail_without_leaking_a_value(self):
        self.write_config(dashboard={"enabled": True, "session_file": self.session_path})
        self._write_session(document={"Cookie": "a=1", "Password": "hunter2-SECRET"})
        with mock.patch(
            "sarvam_meter.daemon.fetch_dashboard_snapshot",
            return_value=_dashboard_snapshot(),
        ):
            out, err = self.assert_exit(["doctor"], EXIT_RUNTIME)
        self.assertIn("FAIL dashboard session", out)
        self.assertIn("password", out.lower())
        self.assertNotIn("hunter2-SECRET", out + err)

    def test_a_corrupt_session_file_degrades_to_a_fail_line(self):
        self.write_config(dashboard={"enabled": True, "session_file": self.session_path})
        with open(self.session_path, "w", encoding="utf-8") as handle:
            handle.write("{{{")
        os.chmod(self.session_path, 0o600)
        with mock.patch(
            "sarvam_meter.daemon.fetch_dashboard_snapshot",
            return_value=_dashboard_snapshot(),
        ):
            out, _err = self.assert_exit(["doctor"], EXIT_RUNTIME)
        self.assertIn("FAIL dashboard session", out)

    def test_a_raising_snapshot_degrades_to_a_fail_line(self):
        self.write_config(dashboard={"enabled": True, "session_file": self.session_path})
        self._write_session()
        with mock.patch(
            "sarvam_meter.daemon.fetch_dashboard_snapshot",
            side_effect=RuntimeError("kaboom"),
        ):
            out, err = self.assert_exit(["doctor"], EXIT_RUNTIME)
        self.assertIn("FAIL dashboard reachable", out)
        self.assertNotIn("Traceback", out + err)

    def test_disabled_dashboard_adds_one_check_and_stays_ok(self):
        # Explicitly disabled (the default is on): only the session check is
        # run, and a missing session file is not a failure for a disabled source.
        self.write_config(dashboard={"enabled": False})
        out, _err = self.assert_exit(["doctor"], EXIT_OK)
        self.assertIn("OK   dashboard session", out)
        self.assertNotIn("dashboard reachable", out)
        self.assertNotIn("FAIL", out)



# ---------------------------------------------------------------------------
# query (talk to a running daemon over its socket)
# ---------------------------------------------------------------------------


class QueryTest(CliTestCase):
    """``query <op>`` sends one request down the socket and prints the result.

    This is the CLI half of the socket transport: the daemon it talks to is a
    real one, bound to the path ``SARVAM_METER_SOCKET`` names in the fixture.
    """

    def start_daemon(self):
        from sarvam_meter.daemon import Daemon

        self.write_config(dashboard={"enabled": False})
        daemon = Daemon(
            config_mod.load_config(self.config_path),
            logger=lambda _line: None,
            socket_path=self.socket_path,
        )
        daemon.start()
        self.addCleanup(daemon.shutdown)
        return daemon

    def test_query_health_prints_the_result(self):
        self.start_daemon()
        out, err = self.assert_exit(
            ["query", "health", "--socket", self.socket_path], EXIT_OK
        )
        payload = json.loads(out)
        self.assertIs(payload["ok"], True)
        self.assertIn("uptime_seconds", payload)
        self.assertEqual(err, "")

    def test_query_resolves_the_socket_from_the_environment(self):
        self.start_daemon()
        out, _err = self.assert_exit(["query", "status"], EXIT_OK)
        payload = json.loads(out)
        self.assertEqual(payload["schema"], 2)
        self.assertIn("sarvam", payload)

    def test_query_config_and_session_report_without_a_value(self):
        self.start_daemon()
        out, _err = self.assert_exit(
            ["query", "config", "--socket", self.socket_path], EXIT_OK
        )
        self.assertEqual(json.loads(out)["version"], 1)
        out, _err = self.assert_exit(
            ["query", "session", "--socket", self.socket_path], EXIT_OK
        )
        self.assertEqual(
            sorted(json.loads(out)), ["configured", "headers", "mode", "path"]
        )

    def test_query_set_config_writes_the_file(self):
        self.start_daemon()
        out, _err = self.assert_exit(
            [
                "query",
                "set-config",
                '{"dashboard": {"poll_seconds": 600}}',
                "--socket",
                self.socket_path,
            ],
            EXIT_OK,
        )
        self.assertEqual(json.loads(out)["dashboard"]["poll_seconds"], 600)
        with open(self.config_path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["dashboard"]["poll_seconds"], 600)

    def test_query_without_a_daemon_exits_one(self):
        # Nothing is listening: the client's own message, exit 1, no traceback.
        out, err = self.assert_exit(
            ["query", "health", "--socket", self.socket_path], EXIT_RUNTIME
        )
        self.assertEqual(out, "")
        self.assertIn("sarvam-meter:", err)
        self.assertNotIn("Traceback", err)

    def test_query_rejects_a_bad_op_or_a_bad_document(self):
        self.assert_exit(["query", "nope", "--socket", self.socket_path], EXIT_CONFIG)
        self.assert_exit(
            ["query", "set-config", "[]", "--socket", self.socket_path], EXIT_CONFIG
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
