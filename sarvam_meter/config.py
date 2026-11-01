# SPDX-License-Identifier: MIT
"""Configuration loading, validation and default-config writing.

Implements SPEC section 4.  Standard library only, Python 3.9 compatible.

Config file path resolution order:

1. the ``path`` argument of :func:`load_config` / :func:`write_default_config`
2. ``$SARVAM_METER_CONFIG``
3. ``~/.config/sarvam-meter/config.json``

A missing config file is a :class:`ConfigError` (the CLI/daemon turn that into a
clear message and tell the user to run ``sarvam-meter init``).
"""

from __future__ import annotations

import copy
import ipaddress
import json
import math
import os
import socket
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

ENV_CONFIG_PATH = "SARVAM_METER_CONFIG"
ENV_CLIPROXY_KEY = "SARVAM_METER_CLIPROXY_KEY"

#: Default document value of ``db_path`` (kept with ``~`` for the JSON file).
DEFAULT_DB_PATH = "~/.local/share/sarvam-meter/usage.db"

#: ``~/.config/sarvam-meter/config.json``, already ``~``-expanded.
DEFAULT_CONFIG_PATH = os.path.expanduser(
    os.path.join("~", ".config", "sarvam-meter", "config.json")
)

#: ``~/.local/share/sarvam-meter/``, already ``~``-expanded.
DATA_DIR = os.path.expanduser(os.path.join("~", ".local", "share", "sarvam-meter"))

#: The only supported config document version.
SUPPORTED_VERSION = 1

#: Default dashboard base URL (``dashboard.sarvam.ai`` 308-redirects here).
DEFAULT_DASHBOARD_BASE_URL = "https://indus.sarvam.ai"

#: Default session-file document value (kept with ``~`` for the JSON file).
DEFAULT_SESSION_FILE = "~/.config/sarvam-meter/session.json"

#: Dashboard API product scope (SPEC 17.1).
DEFAULT_DASHBOARD_PRODUCT = "sarvam-api-platform"

#: Minimum dashboard poll interval: bot-management friendly (SPEC 17.4).
#: The dashboard is re-polled between every 2 minutes and every 6 hours.  The
#: floor is deliberate (the dashboard sits behind bot management); the ceiling
#: keeps a slow setting from looking broken.
MIN_DASHBOARD_POLL_SECONDS = 120
MAX_DASHBOARD_POLL_SECONDS = 21600

#: Accepted CLIProxyAPI management API versions.
API_VERSIONS = ("v8", "v0")

#: Host names that always count as loopback.
LOOPBACK_NAMES = frozenset(
    {"localhost", "localhost.localdomain", "ip6-localhost", "::1", "0:0:0:0:0:0:0:1"}
)


class ConfigError(Exception):
    """Raised when the configuration is missing, unreadable or invalid."""


@dataclass(frozen=True)
class DaemonConfig:
    #: Where the daemon listens.  ``None`` means "resolve it": ``$XDG_RUNTIME_DIR/
    #: sarvam-meter/daemon.sock``, or ``~/.cache/sarvam-meter/daemon.sock`` when
    #: there is no runtime dir.  ``$SARVAM_METER_SOCKET`` overrides both, which is
    #: how the tests and a second instance point at their own socket.
    socket_path: Optional[str] = None
    refresh_seconds: int = 60
    request_timeout_seconds: int = 10


@dataclass(frozen=True)
class SarvamConfig:
    enabled: bool = True
    currency: str = "INR"
    starting_balance: Optional[float] = None
    rate_overrides: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CliproxyConfig:
    enabled: bool = False
    base_url: str = "http://127.0.0.1:8317"
    management_key: str = ""
    api_version: str = "v8"
    quota_fetch: bool = True
    quota_fetch_interval_seconds: int = 300
    include_api_key_usage: bool = False
    allow_remote: bool = False


@dataclass(frozen=True)
class DashboardConfig:
    """The Sarvam dashboard source (SPEC 17.4)."""

    #: Scraping is the default source: it reports the real credit balance.
    enabled: bool = True
    base_url: str = DEFAULT_DASHBOARD_BASE_URL
    session_file: str = DEFAULT_SESSION_FILE
    poll_seconds: int = 300
    product: str = DEFAULT_DASHBOARD_PRODUCT


@dataclass(frozen=True)
class Config:
    version: int = SUPPORTED_VERSION
    db_path: str = DEFAULT_DB_PATH
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    sarvam: SarvamConfig = field(default_factory=SarvamConfig)
    cliproxy: CliproxyConfig = field(default_factory=CliproxyConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)


def default_config_dict() -> dict:
    """Return a fresh copy of the default config document.

    The returned dict is exactly the document that
    ``packaging/config.example.json`` must reproduce (same keys, same values,
    same order).
    """
    return {
        "version": 1,
        "db_path": "~/.local/share/sarvam-meter/usage.db",
        "daemon": {
            "socket_path": None,
            "refresh_seconds": 60,
            "request_timeout_seconds": 10,
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
            "enabled": True,
            "base_url": "https://indus.sarvam.ai",
            "session_file": "~/.config/sarvam-meter/session.json",
            "poll_seconds": 300,
            "product": "sarvam-api-platform",
        },
    }


# --------------------------------------------------------------------------
# path resolution
# --------------------------------------------------------------------------


def resolve_config_path(path: Optional[str] = None) -> str:
    """The config file that would actually be used for *path*.

    Explicit argument, then ``$SARVAM_METER_CONFIG``, then the default.  Callers
    that need to *look at* the file (rather than load or write it) must use this:
    passing a bare ``--config`` value around gives ``None`` when the flag was not
    given, and ``os.path.isfile(None)`` raises TypeError.
    """
    return _resolve_path(path)


def _resolve_path(path: Optional[str]) -> str:
    if path is not None and str(path).strip():
        return os.path.expanduser(str(path))
    env_path = os.environ.get(ENV_CONFIG_PATH, "")
    if env_path.strip():
        return os.path.expanduser(env_path.strip())
    return DEFAULT_CONFIG_PATH


# --------------------------------------------------------------------------
# validation helpers -- every message names the offending key
# --------------------------------------------------------------------------


def _require_dict(value: Any, key: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError("%s: must be a JSON object" % key)
    return value


def _require_str(value: Any, key: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ConfigError("%s: must be a string" % key)
    if not allow_empty and not value.strip():
        raise ConfigError("%s: must not be empty" % key)
    return value


def _optional_str(value: Any, key: str) -> Optional[str]:
    """``None`` or a non-empty string, ``~`` expanded.  For keys whose absence is
    meaningful (an unset socket path means "resolve it")."""
    if value is None:
        return None
    text = _require_str(value, key)
    return os.path.expanduser(text.strip())


def _require_bool(value: Any, key: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError("%s: must be true or false" % key)
    return value


def _require_int(
    value: Any, key: str, minimum: Optional[int] = None, maximum: Optional[int] = None
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError("%s: must be an integer" % key)
    if minimum is not None and value < minimum:
        raise ConfigError("%s: must be >= %d" % (key, minimum))
    if maximum is not None and value > maximum:
        raise ConfigError("%s: must be <= %d" % (key, maximum))
    return value


def _require_currency(value: Any, key: str) -> str:
    if not isinstance(value, str):
        raise ConfigError("%s: must be a 3-letter uppercase currency code" % key)
    code = value.strip()
    if len(code) != 3 or not code.isalpha() or not code.isupper() or not code.isascii():
        raise ConfigError(
            "%s: must be a 3-letter uppercase currency code, got %r" % (key, value)
        )
    return code


def _require_balance(value: Any, key: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError("%s: must be null or a number" % key)
    number = float(value)
    if not math.isfinite(number):
        raise ConfigError("%s: must be a finite number" % key)
    if number < 0:
        raise ConfigError("%s: must be >= 0" % key)
    return number


def _require_url(value: Any, key: str) -> str:
    text = _require_str(value, key).strip()
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https"):
        raise ConfigError("%s: must be an http:// or https:// URL" % key)
    if not parts.hostname:
        raise ConfigError("%s: must include a host" % key)
    return text


def _require_https_url(value: Any, key: str) -> str:
    """A ``https://`` URL with a host and **no** userinfo (SPEC 17.4).

    The dashboard is a remote host by design, so the loopback guard used for
    ``cliproxy.base_url`` deliberately does not apply here.  Plain ``http://`` is
    accepted for a loopback host only, so the source can be exercised against a
    local fixture server without weakening anything that is reachable remotely.
    """
    text = _require_str(value, key).strip()
    parts = urlsplit(text)
    if parts.username is not None or parts.password is not None:
        raise ConfigError("%s: credentials in the URL are not supported" % (key,))
    if not parts.hostname:
        raise ConfigError("%s: must include a host" % (key,))
    if parts.scheme == "https":
        return text
    if parts.scheme == "http" and is_loopback_host(parts.hostname):
        return text
    raise ConfigError(
        "%s: must be an https:// URL (http:// is allowed for a loopback host only)"
        % (key,)
    )


def _require_rate_overrides(value: Any, key: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError("%s: must be a JSON object" % key)
    for api, models in value.items():
        if not isinstance(models, dict):
            raise ConfigError("%s.%s: must be a JSON object" % (key, api))
        for model, prices in models.items():
            if prices is not None and not isinstance(prices, dict):
                raise ConfigError("%s.%s.%s: must be a JSON object" % (key, api, model))
    return value


#: Memo for resolved single-label host names (bounded; see is_loopback_host).
_LOOPBACK_CACHE: Dict[str, bool] = {}


def is_loopback_host(host: Optional[str]) -> bool:
    """True for ``127.0.0.0/8``, ``::1`` and names that resolve to loopback.

    The literal check runs first (fast, and it never touches DNS).  A name that
    is not a literal is resolved and *every* returned address must be loopback,
    so ``ip6-loopback`` and ``localhost.`` are accepted while a name that
    resolves to a public address is not.
    """
    if not host:
        return False
    name = host.strip().lower()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    name = name.rstrip(".")
    if name in LOOPBACK_NAMES:
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        address = None
    if address is not None:
        return address.is_loopback
    if "." in name:
        # A dotted name is a remote host: treat it as such instead of paying a
        # DNS round trip (and a possible multi-second timeout) on every config
        # load.  Only single-label names are resolved, which covers the /etc/hosts
        # style local aliases such as ``ip6-loopback`` or ``my-machine``.
        return False
    cached = _LOOPBACK_CACHE.get(name)
    if cached is not None:
        return cached
    try:
        infos = socket.getaddrinfo(name, None)
    except (socket.gaierror, UnicodeError, OSError):
        _LOOPBACK_CACHE[name] = False
        return False
    verdict = bool(infos)
    for info in infos:
        try:
            resolved = ipaddress.ip_address(info[4][0])
        except (ValueError, IndexError, TypeError):
            verdict = False
            break
        if not resolved.is_loopback:
            verdict = False
            break
    if len(_LOOPBACK_CACHE) < 256:
        _LOOPBACK_CACHE[name] = verdict
    return verdict


def _guard_loopback(url: str, key: str) -> None:
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None:
        # Credentials in a URL end up in error messages and in /v1/status.
        raise ConfigError("%s: credentials in the URL are not supported" % (key,))
    host = parts.hostname
    if not is_loopback_host(host):
        raise ConfigError(
            "%s: non-loopback host %r is only allowed when allow_remote is true"
            % (key, host)
        )


# --------------------------------------------------------------------------
# merge + build
# --------------------------------------------------------------------------


def _deep_merge(base: dict, override: dict) -> dict:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _build_config(document: dict) -> Config:
    """Validate a merged config document and turn it into a :class:`Config`."""

    version = document.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise ConfigError("version: must be 1, got %r" % (version,))

    db_path = _require_str(document.get("db_path"), "db_path")
    db_path = os.path.expanduser(db_path.strip())

    daemon_raw = _require_dict(document.get("daemon"), "daemon")
    daemon = DaemonConfig(
        socket_path=_optional_str(daemon_raw.get("socket_path"), "daemon.socket_path"),
        refresh_seconds=_require_int(
            daemon_raw.get("refresh_seconds"), "daemon.refresh_seconds", 5
        ),
        request_timeout_seconds=_require_int(
            daemon_raw.get("request_timeout_seconds"),
            "daemon.request_timeout_seconds",
            1,
        ),
    )

    sarvam_raw = _require_dict(document.get("sarvam"), "sarvam")
    sarvam = SarvamConfig(
        enabled=_require_bool(sarvam_raw.get("enabled"), "sarvam.enabled"),
        currency=_require_currency(sarvam_raw.get("currency"), "sarvam.currency"),
        starting_balance=_require_balance(
            sarvam_raw.get("starting_balance"), "sarvam.starting_balance"
        ),
        rate_overrides=_require_rate_overrides(
            sarvam_raw.get("rate_overrides"), "sarvam.rate_overrides"
        ),
    )

    cliproxy_raw = _require_dict(document.get("cliproxy"), "cliproxy")
    allow_remote = _require_bool(cliproxy_raw.get("allow_remote"), "cliproxy.allow_remote")
    base_url = _require_url(cliproxy_raw.get("base_url"), "cliproxy.base_url")
    if not allow_remote:
        _guard_loopback(base_url, "cliproxy.base_url")

    management_key = _require_str(
        cliproxy_raw.get("management_key"), "cliproxy.management_key", allow_empty=True
    )
    env_key = os.environ.get(ENV_CLIPROXY_KEY, "")
    if env_key.strip():
        management_key = env_key.strip()

    api_version = _require_str(cliproxy_raw.get("api_version"), "cliproxy.api_version")
    if api_version not in API_VERSIONS:
        raise ConfigError(
            'cliproxy.api_version: must be one of "v8" or "v0", got %r' % (api_version,)
        )

    cliproxy = CliproxyConfig(
        enabled=_require_bool(cliproxy_raw.get("enabled"), "cliproxy.enabled"),
        base_url=base_url,
        management_key=management_key,
        api_version=api_version,
        quota_fetch=_require_bool(cliproxy_raw.get("quota_fetch"), "cliproxy.quota_fetch"),
        quota_fetch_interval_seconds=_require_int(
            cliproxy_raw.get("quota_fetch_interval_seconds"),
            "cliproxy.quota_fetch_interval_seconds",
            1,
        ),
        include_api_key_usage=_require_bool(
            cliproxy_raw.get("include_api_key_usage"), "cliproxy.include_api_key_usage"
        ),
        allow_remote=allow_remote,
    )

    dashboard_raw = _require_dict(document.get("dashboard"), "dashboard")
    session_file = _require_str(
        dashboard_raw.get("session_file"), "dashboard.session_file"
    )
    session_file = os.path.expanduser(session_file.strip())
    if not session_file:
        raise ConfigError("dashboard.session_file: must not be empty")
    dashboard = DashboardConfig(
        enabled=_require_bool(dashboard_raw.get("enabled"), "dashboard.enabled"),
        base_url=_require_https_url(dashboard_raw.get("base_url"), "dashboard.base_url"),
        session_file=session_file,
        poll_seconds=_require_int(
            dashboard_raw.get("poll_seconds"),
            "dashboard.poll_seconds",
            MIN_DASHBOARD_POLL_SECONDS,
            MAX_DASHBOARD_POLL_SECONDS,
        ),
        product=_require_str(dashboard_raw.get("product"), "dashboard.product"),
    )

    return Config(
        version=version,
        db_path=db_path,
        daemon=daemon,
        sarvam=sarvam,
        cliproxy=cliproxy,
        dashboard=dashboard,
    )


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------


def load_config(path: Optional[str] = None) -> Config:
    """Load, deep-merge and validate the config document.

    Raises :class:`ConfigError` for a missing file, unreadable file, invalid
    JSON or any validation failure.  Error messages name the config file and the
    reason, never the file contents.
    """
    resolved = _resolve_path(path)
    if not os.path.isfile(resolved):
        raise ConfigError("config file not found: %s" % resolved)
    try:
        with open(resolved, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except OSError as exc:
        raise ConfigError(
            "cannot read config %s: %s" % (resolved, exc.strerror or exc)
        ) from None
    except ValueError as exc:  # json.JSONDecodeError and UnicodeDecodeError
        raise ConfigError("invalid JSON in %s: %s" % (resolved, exc)) from None

    if not isinstance(document, dict):
        raise ConfigError("config %s: top level must be a JSON object" % resolved)

    merged = _deep_merge(default_config_dict(), document)
    return _build_config(merged)


def write_default_config(path: Optional[str] = None) -> str:
    """Write the default config document and return the path written.

    Missing parent directories are created with mode ``0700``; the file is
    written atomically with mode ``0600``.
    """
    resolved = _resolve_path(path)
    directory = os.path.dirname(os.path.abspath(resolved))
    created = False
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, mode=0o700, exist_ok=True)
        created = True
    if created:
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass

    payload = json.dumps(default_config_dict(), indent=2) + "\n"
    tmp_path = resolved + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        handle.write(payload)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, resolved)
    return resolved


__all__ = [
    "API_VERSIONS",
    "CliproxyConfig",
    "Config",
    "ConfigError",
    "DATA_DIR",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_DASHBOARD_BASE_URL",
    "DEFAULT_DASHBOARD_PRODUCT",
    "DEFAULT_DB_PATH",
    "DEFAULT_SESSION_FILE",
    "DaemonConfig",
    "DashboardConfig",
    "resolve_config_path",
    "MIN_DASHBOARD_POLL_SECONDS",
    "MAX_DASHBOARD_POLL_SECONDS",
    "ENV_CLIPROXY_KEY",
    "ENV_CONFIG_PATH",
    "SarvamConfig",
    "SUPPORTED_VERSION",
    "default_config_dict",
    "is_loopback_host",
    "load_config",
    "write_default_config",
]
