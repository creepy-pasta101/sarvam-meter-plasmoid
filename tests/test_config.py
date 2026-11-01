# SPDX-License-Identifier: MIT
"""Tests for sarvam_meter.config (SPEC section 4)."""

import json
import os
import shutil
import stat
import tempfile
import unittest

from sarvam_meter.config import (
    DEFAULT_CONFIG_PATH,
    Config,
    ConfigError,
    default_config_dict,
    is_loopback_host,
    load_config,
    write_default_config,
)

EXPECTED_DEFAULT = {
    "version": 1,
    "db_path": "~/.local/share/sarvam-meter/usage.db",
    "daemon": {
        "host": "127.0.0.1",
        "port": 8787,
        "refresh_seconds": 60,
        "request_timeout_seconds": 10,
    },
    "proxy": {
        "enabled": False,
        "host": "127.0.0.1",
        "port": 8788,
        "upstream_base": "https://api.sarvam.ai",
        "max_body_bytes": 268435456,
    },
    "sarvam": {
        "enabled": True,
        "currency": "INR",
        "starting_balance": None,
        "rate_overrides": {},
    },
    "cliproxy": {
        "enabled": False,
        "base_url": "http://127.0.0.1:8317",
        "management_key": "",
        "api_version": "v8",
        "quota_fetch": True,
        "quota_fetch_interval_seconds": 300,
        "include_api_key_usage": False,
        "allow_remote": False,
    },
    "dashboard": {
        "enabled": False,
        "base_url": "https://indus.sarvam.ai",
        "session_file": "~/.config/sarvam-meter/session.json",
        "poll_seconds": 300,
        "product": "sarvam-api-platform",
    },
}

TOP_LEVEL_ORDER = [
    "version",
    "db_path",
    "daemon",
    "proxy",
    "sarvam",
    "cliproxy",
    "dashboard",
]


class ConfigTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sarvam-config-")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self._env_backup = {
            "SARVAM_METER_CONFIG": os.environ.pop("SARVAM_METER_CONFIG", None),
            "SARVAM_METER_CLIPROXY_KEY": os.environ.pop(
                "SARVAM_METER_CLIPROXY_KEY", None
            ),
        }

        def restore():
            for key, value in self._env_backup.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)

    def write_document(self, document, name="config.json"):
        path = os.path.join(self.tmpdir, name)
        with open(path, "w", encoding="utf-8") as handle:
            if isinstance(document, str):
                handle.write(document)
            else:
                json.dump(document, handle)
        return path

    def load_document(self, document):
        return load_config(self.write_document(document))

    def assert_config_error(self, document, needle):
        with self.assertRaises(ConfigError) as ctx:
            self.load_document(document)
        self.assertIn(needle, str(ctx.exception))
        return ctx.exception


class DefaultDocumentTests(ConfigTestBase):
    def test_default_config_dict_matches_spec_document(self):
        self.assertEqual(default_config_dict(), EXPECTED_DEFAULT)

    def test_default_config_dict_key_order(self):
        self.assertEqual(list(default_config_dict().keys()), TOP_LEVEL_ORDER)

    def test_default_config_dict_returns_independent_copies(self):
        first = default_config_dict()
        first["daemon"]["port"] = 1
        first["sarvam"]["rate_overrides"]["chat"] = {"default": {}}
        second = default_config_dict()
        self.assertEqual(second["daemon"]["port"], 8787)
        self.assertEqual(second["sarvam"]["rate_overrides"], {})

    def test_default_config_path_is_home_based(self):
        self.assertTrue(DEFAULT_CONFIG_PATH.endswith("sarvam-meter/config.json"))
        self.assertNotIn("~", DEFAULT_CONFIG_PATH)


class WriteDefaultConfigTests(ConfigTestBase):
    def test_writes_file_with_0600_and_private_directory(self):
        target = os.path.join(self.tmpdir, "nested", "deeper", "config.json")
        written = write_default_config(target)
        self.assertEqual(written, target)
        self.assertTrue(os.path.isfile(target))
        file_mode = stat.S_IMODE(os.stat(target).st_mode)
        self.assertEqual(file_mode, 0o600)
        dir_mode = stat.S_IMODE(os.stat(os.path.dirname(target)).st_mode)
        self.assertEqual(dir_mode, 0o700)

    def test_written_file_reproduces_default_document(self):
        target = write_default_config(os.path.join(self.tmpdir, "config.json"))
        with open(target, "r", encoding="utf-8") as handle:
            document = json.load(handle)
        self.assertEqual(document, EXPECTED_DEFAULT)
        # no temporary file left behind
        self.assertEqual(
            [name for name in os.listdir(self.tmpdir) if name.endswith(".tmp")], []
        )

    def test_written_file_loads(self):
        target = write_default_config(os.path.join(self.tmpdir, "config.json"))
        config = load_config(target)
        self.assertIsInstance(config, Config)
        self.assertEqual(config.version, 1)
        self.assertEqual(config.daemon.port, 8787)
        self.assertEqual(config.proxy.upstream_base, "https://api.sarvam.ai")
        self.assertEqual(config.sarvam.currency, "INR")
        self.assertIsNone(config.sarvam.starting_balance)
        self.assertEqual(config.cliproxy.api_version, "v8")
        self.assertFalse(config.dashboard.enabled)
        self.assertEqual(config.dashboard.base_url, "https://indus.sarvam.ai")
        self.assertEqual(config.dashboard.poll_seconds, 300)
        self.assertEqual(config.dashboard.product, "sarvam-api-platform")
        self.assertNotIn("~", config.dashboard.session_file)
        self.assertTrue(config.dashboard.session_file.endswith("session.json"))

    def test_overwrites_existing_file(self):
        target = os.path.join(self.tmpdir, "config.json")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("{}")
        write_default_config(target)
        self.assertEqual(load_config(target).daemon.port, 8787)


class LoadConfigTests(ConfigTestBase):
    def test_missing_file_raises(self):
        missing = os.path.join(self.tmpdir, "nope.json")
        with self.assertRaises(ConfigError) as ctx:
            load_config(missing)
        self.assertIn("nope.json", str(ctx.exception))

    def test_invalid_json_names_path_and_reason(self):
        path = self.write_document("{not json")
        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        message = str(ctx.exception)
        self.assertIn(path, message)
        self.assertIn("invalid JSON", message)

    def test_top_level_must_be_object(self):
        with self.assertRaises(ConfigError):
            self.load_document("[]")

    def test_empty_document_yields_defaults(self):
        config = self.load_document({})
        self.assertEqual(config.daemon.port, 8787)
        self.assertIsNone(config.sarvam.starting_balance)

    def test_deep_merge_keeps_sibling_defaults(self):
        config = self.load_document({"daemon": {"port": 9000}})
        self.assertEqual(config.daemon.port, 9000)
        self.assertEqual(config.daemon.host, "127.0.0.1")
        self.assertEqual(config.daemon.refresh_seconds, 60)

    def test_unknown_top_level_keys_are_ignored(self):
        config = self.load_document({"surprise": {"a": 1}, "version": 1})
        self.assertEqual(config.version, 1)

    def test_env_path_used_when_argument_missing(self):
        path = self.write_document({"daemon": {"port": 9100}})
        os.environ["SARVAM_METER_CONFIG"] = path
        self.assertEqual(load_config().daemon.port, 9100)

    def test_explicit_path_beats_env(self):
        env_path = self.write_document({"daemon": {"port": 9100}}, "env.json")
        arg_path = self.write_document({"daemon": {"port": 9200}}, "arg.json")
        os.environ["SARVAM_METER_CONFIG"] = env_path
        self.assertEqual(load_config(arg_path).daemon.port, 9200)

    def test_db_path_tilde_is_expanded(self):
        config = self.load_document({"db_path": "~/sarvam/usage.db"})
        self.assertNotIn("~", config.db_path)
        self.assertTrue(config.db_path.endswith("sarvam/usage.db"))

    def test_config_is_frozen(self):
        config = self.load_document({})
        with self.assertRaises(Exception):
            config.daemon.port = 1  # type: ignore[misc]


class VersionAndNumberValidationTests(ConfigTestBase):
    def test_version_must_be_one(self):
        self.assert_config_error({"version": 2}, "version")

    def test_version_must_be_integer(self):
        self.assert_config_error({"version": "1"}, "version")

    def test_daemon_port_range(self):
        self.assert_config_error({"daemon": {"port": 0}}, "daemon.port")
        self.assert_config_error({"daemon": {"port": 65536}}, "daemon.port")
        self.assertEqual(
            self.load_document({"daemon": {"port": 65535}}).daemon.port, 65535
        )
        self.assertEqual(self.load_document({"daemon": {"port": 1}}).daemon.port, 1)

    def test_proxy_port_range(self):
        self.assert_config_error({"proxy": {"port": -5}}, "proxy.port")

    def test_port_must_be_integer(self):
        self.assert_config_error({"daemon": {"port": "8787"}}, "daemon.port")

    def test_refresh_seconds_minimum(self):
        self.assert_config_error({"daemon": {"refresh_seconds": 4}}, "refresh_seconds")
        self.assertEqual(
            self.load_document({"daemon": {"refresh_seconds": 5}}).daemon.refresh_seconds,
            5,
        )

    def test_request_timeout_seconds_minimum(self):
        self.assert_config_error(
            {"daemon": {"request_timeout_seconds": 0}}, "request_timeout_seconds"
        )

    def test_api_version_allowed_values(self):
        self.assert_config_error({"cliproxy": {"api_version": "v9"}}, "api_version")
        self.assertEqual(
            self.load_document({"cliproxy": {"api_version": "v0"}}).cliproxy.api_version,
            "v0",
        )

    def test_currency_must_be_three_uppercase_letters(self):
        for bad in ("inr", "IN", "INRR", "I1R", ""):
            self.assert_config_error({"sarvam": {"currency": bad}}, "currency")
        self.assertEqual(
            self.load_document({"sarvam": {"currency": "USD"}}).sarvam.currency, "USD"
        )

    def test_starting_balance_null_or_non_negative(self):
        self.assertIsNone(self.load_document({}).sarvam.starting_balance)
        self.assertEqual(
            self.load_document({"sarvam": {"starting_balance": 0}}).sarvam.starting_balance,
            0.0,
        )
        self.assertEqual(
            self.load_document(
                {"sarvam": {"starting_balance": 500.5}}
            ).sarvam.starting_balance,
            500.5,
        )
        self.assert_config_error({"sarvam": {"starting_balance": -1}}, "starting_balance")
        self.assert_config_error(
            {"sarvam": {"starting_balance": "500"}}, "starting_balance"
        )

    def test_starting_balance_rejects_non_finite(self):
        self.assert_config_error(
            '{"sarvam": {"starting_balance": NaN}}', "starting_balance"
        )
        self.assert_config_error(
            '{"sarvam": {"starting_balance": Infinity}}', "starting_balance"
        )

    def test_rate_overrides_must_be_object(self):
        self.assert_config_error({"sarvam": {"rate_overrides": []}}, "rate_overrides")
        self.assert_config_error(
            {"sarvam": {"rate_overrides": {"chat": 5}}}, "rate_overrides"
        )
        config = self.load_document(
            {"sarvam": {"rate_overrides": {"chat": {"sarvam-105b": {"in_per_million": 1.0}}}}}
        )
        self.assertEqual(
            config.sarvam.rate_overrides["chat"]["sarvam-105b"]["in_per_million"], 1.0
        )


class UrlValidationTests(ConfigTestBase):
    def test_upstream_base_requires_http_scheme_and_host(self):
        for bad in ("ftp://api.sarvam.ai", "api.sarvam.ai", "https://", "http://"):
            self.assert_config_error({"proxy": {"upstream_base": bad}}, "upstream_base")

    def test_cliproxy_base_url_requires_http_scheme_and_host(self):
        self.assert_config_error(
            {"cliproxy": {"base_url": "127.0.0.1:8317"}}, "base_url"
        )

    def test_cliproxy_https_loopback_accepted(self):
        config = self.load_document({"cliproxy": {"base_url": "https://127.0.0.1:8317"}})
        self.assertEqual(config.cliproxy.base_url, "https://127.0.0.1:8317")

    def test_non_loopback_cliproxy_rejected_without_allow_remote(self):
        error = self.assert_config_error(
            {"cliproxy": {"base_url": "http://10.0.0.5:8317"}}, "base_url"
        )
        self.assertIn("allow_remote", str(error))

    def test_non_loopback_cliproxy_accepted_with_allow_remote(self):
        config = self.load_document(
            {"cliproxy": {"base_url": "http://10.0.0.5:8317", "allow_remote": True}}
        )
        self.assertEqual(config.cliproxy.base_url, "http://10.0.0.5:8317")
        self.assertTrue(config.cliproxy.allow_remote)

    def test_loopback_forms_accepted(self):
        for url in (
            "http://127.0.0.1:8317",
            "http://127.5.5.5:8317",
            "http://localhost:8317",
            "http://[::1]:8317",
        ):
            config = self.load_document({"cliproxy": {"base_url": url}})
            self.assertEqual(config.cliproxy.base_url, url)

    def test_public_host_rejected(self):
        self.assert_config_error(
            {"cliproxy": {"base_url": "https://cliproxy.example.com"}}, "base_url"
        )

    def test_is_loopback_host(self):
        for host in ("127.0.0.1", "127.255.0.9", "::1", "localhost", "LOCALHOST", "[::1]"):
            self.assertTrue(is_loopback_host(host), host)
        for host in ("api.sarvam.ai", "10.0.0.1", "8.8.8.8", "", None):
            self.assertFalse(is_loopback_host(host), host)


class DashboardSectionTests(ConfigTestBase):
    """SPEC 17.4: the dashboard section, its defaults and its validation."""

    def test_defaults(self):
        dashboard = self.load_document({}).dashboard
        self.assertFalse(dashboard.enabled)
        self.assertEqual(dashboard.base_url, "https://indus.sarvam.ai")
        self.assertEqual(dashboard.poll_seconds, 300)
        self.assertEqual(dashboard.product, "sarvam-api-platform")
        self.assertTrue(dashboard.session_file.endswith("sarvam-meter/session.json"))

    def test_base_url_must_be_https(self):
        for bad in (
            "http://indus.sarvam.ai",
            "ftp://indus.sarvam.ai",
            "indus.sarvam.ai",
            "https://",
        ):
            self.assert_config_error({"dashboard": {"base_url": bad}}, "dashboard.base_url")

    def test_base_url_rejects_userinfo(self):
        for bad in (
            "https://user:secret@indus.sarvam.ai",
            "https://user@indus.sarvam.ai",
        ):
            error = self.assert_config_error(
                {"dashboard": {"base_url": bad}}, "dashboard.base_url"
            )
            self.assertIn("credentials", str(error))
            self.assertNotIn("secret", str(error))

    def test_remote_host_is_accepted_without_allow_remote(self):
        # The dashboard is remote by design: the loopback guard must not apply.
        for url in ("https://indus.sarvam.ai", "https://dashboard.sarvam.ai"):
            self.assertEqual(
                self.load_document({"dashboard": {"base_url": url}}).dashboard.base_url,
                url,
            )

    def test_https_loopback_is_also_accepted(self):
        self.assertEqual(
            self.load_document(
                {"dashboard": {"base_url": "https://127.0.0.1:9443"}}
            ).dashboard.base_url,
            "https://127.0.0.1:9443",
        )

    def test_poll_seconds_minimum(self):
        self.assert_config_error({"dashboard": {"poll_seconds": 59}}, "dashboard.poll_seconds")
        self.assertEqual(
            self.load_document(
                {"dashboard": {"poll_seconds": 60}}
            ).dashboard.poll_seconds,
            60,
        )
        self.assert_config_error({"dashboard": {"poll_seconds": "300"}}, "dashboard.poll_seconds")

    def test_product_must_be_a_non_empty_string(self):
        self.assert_config_error({"dashboard": {"product": ""}}, "dashboard.product")
        self.assert_config_error({"dashboard": {"product": 7}}, "dashboard.product")
        self.assertEqual(
            self.load_document({"dashboard": {"product": "other-product"}}).dashboard.product,
            "other-product",
        )

    def test_session_file_is_tilde_expanded(self):
        config = self.load_document({"dashboard": {"session_file": "~/sarvam/session.json"}})
        self.assertNotIn("~", config.dashboard.session_file)
        self.assertTrue(config.dashboard.session_file.endswith("sarvam/session.json"))
        self.assert_config_error({"dashboard": {"session_file": ""}}, "dashboard.session_file")

    def test_enabled_must_be_boolean(self):
        self.assert_config_error({"dashboard": {"enabled": "yes"}}, "dashboard.enabled")
        self.assertTrue(
            self.load_document({"dashboard": {"enabled": True}}).dashboard.enabled
        )


class PackagedExampleTests(unittest.TestCase):
    """``packaging/config.example.json`` is shipped documentation of the defaults."""

    def setUp(self):
        here = os.path.dirname(os.path.abspath(__file__))
        self.path = os.path.join(os.path.dirname(here), "packaging", "config.example.json")

    def test_example_file_exists(self):
        self.assertTrue(os.path.isfile(self.path), self.path)

    def test_example_equals_the_default_document(self):
        with open(self.path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
        self.assertEqual(document, default_config_dict())

    def test_example_keeps_the_key_order(self):
        with open(self.path, "r", encoding="utf-8") as handle:
            text = handle.read()
        document = json.loads(text)
        self.assertEqual(list(document.keys()), list(default_config_dict().keys()))

    def test_example_loads_as_a_valid_config(self):
        config = load_config(self.path)
        self.assertFalse(config.dashboard.enabled)
        self.assertEqual(config.dashboard.base_url, "https://indus.sarvam.ai")


class SecretOverrideTests(ConfigTestBase):
    def test_env_management_key_overrides_file(self):
        path = self.write_document({"cliproxy": {"management_key": "from-file"}})
        os.environ["SARVAM_METER_CLIPROXY_KEY"] = "from-env"
        self.assertEqual(load_config(path).cliproxy.management_key, "from-env")

    def test_empty_env_management_key_keeps_file_value(self):
        path = self.write_document({"cliproxy": {"management_key": "from-file"}})
        os.environ["SARVAM_METER_CLIPROXY_KEY"] = ""
        self.assertEqual(load_config(path).cliproxy.management_key, "from-file")

    def test_default_management_key_is_empty(self):
        self.assertEqual(self.load_document({}).cliproxy.management_key, "")


if __name__ == "__main__":
    unittest.main()
