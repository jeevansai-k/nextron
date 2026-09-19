"""Typed configuration for NEXTRON, persisted as TOML.

The configuration is a nested Pydantic model so that every value loaded from
``~/.config/nextron/config.toml`` is validated and clamped before any engine
sees it. Rotation intervals in particular are hard-clamped to the 5s--5m range
mandated by the specification.
"""

from __future__ import annotations

import logging
import os
import tomllib
from enum import Enum
from pathlib import Path
from typing import Any

import tomli_w
from pydantic import BaseModel, Field, ValidationError, field_validator

from nextron.core import constants
from nextron.core.exceptions import ConfigError
from nextron.storage import paths

log = logging.getLogger(__name__)

__all__ = [
    "ConfigManager",
    "DNSSettings",
    "InterfaceSettings",
    "NextronConfig",
    "RoutingMode",
    "ShuffleAlgorithm",
    "TorSettings",
    "VPNSettings",
    "VerificationSettings",
]


class RoutingMode(str, Enum):
    """The four mutually exclusive routing modes."""

    TOR_ONLY = "tor_only"
    VPN_ONLY = "vpn_only"
    TOR_OVER_VPN = "tor_over_vpn"
    VPN_OVER_TOR = "vpn_over_tor"

    @property
    def label(self) -> str:
        return {
            RoutingMode.TOR_ONLY: "Tor Only",
            RoutingMode.VPN_ONLY: "VPN Only",
            RoutingMode.TOR_OVER_VPN: "Tor over VPN",
            RoutingMode.VPN_OVER_TOR: "VPN over Tor",
        }[self]

    @property
    def chain(self) -> str:
        return {
            RoutingMode.TOR_ONLY: "You -> Tor -> Internet",
            RoutingMode.VPN_ONLY: "You -> VPN -> Internet",
            RoutingMode.TOR_OVER_VPN: "You -> VPN -> Tor -> Internet",
            RoutingMode.VPN_OVER_TOR: "You -> Tor -> VPN -> Internet",
        }[self]

    @property
    def uses_tor(self) -> bool:
        return self is not RoutingMode.VPN_ONLY

    @property
    def uses_vpn(self) -> bool:
        return self is not RoutingMode.TOR_ONLY


class ShuffleAlgorithm(str, Enum):
    """VPN profile shuffling strategies."""

    RANDOM = "random"
    SEQUENTIAL = "sequential"
    ROUND_ROBIN = "round_robin"
    NO_REPEAT = "no_repeat"

    @property
    def label(self) -> str:
        return {
            ShuffleAlgorithm.RANDOM: "Random",
            ShuffleAlgorithm.SEQUENTIAL: "Sequential",
            ShuffleAlgorithm.ROUND_ROBIN: "Round Robin",
            ShuffleAlgorithm.NO_REPEAT: "No Repeat",
        }[self]


def _clamp_interval(value: int) -> int:
    """Clamp a rotation interval into the specification's 5s--5m window."""
    return max(
        constants.ROTATION_MIN_SECONDS,
        min(constants.ROTATION_MAX_SECONDS, int(value)),
    )


class _Base(BaseModel):
    model_config = {"extra": "ignore", "validate_assignment": True}


class TorSettings(_Base):
    """Tor engine configuration."""

    enabled: bool = True
    socks_port: int = Field(default=constants.TOR_SOCKS_PORT, ge=1, le=65535)
    control_port: int = Field(default=constants.TOR_CONTROL_PORT, ge=1, le=65535)
    dns_port: int = Field(default=constants.TOR_DNS_PORT, ge=1, le=65535)
    trans_port: int = Field(default=constants.TOR_TRANS_PORT, ge=1, le=65535)

    #: Launch and own a private Tor daemon instead of attaching to the system one.
    manage_daemon: bool = True
    binary: str = "tor"
    #: Seconds to allow *without progress* during bootstrap. A healthy
    #: bootstrap emits a line every few seconds; a stuck one emits nothing.
    bootstrap_timeout: int = Field(default=120, ge=15, le=600)

    #: Transparent system-wide redirection through Tor's TransPort (needs root).
    transparent_routing: bool = True

    rotation_enabled: bool = True
    rotation_interval: int = Field(default=60)
    verify_exit_after_rotation: bool = True
    rotation_retries: int = Field(default=3, ge=0, le=10)

    exit_countries: list[str] = Field(default_factory=list)
    strict_exit_nodes: bool = False

    @field_validator("rotation_interval")
    @classmethod
    def _rotation_bounds(cls, value: int) -> int:
        return _clamp_interval(value)

    @field_validator("exit_countries")
    @classmethod
    def _normalise_countries(cls, value: list[str]) -> list[str]:
        return [c.strip().upper() for c in value if c and c.strip()]


class VPNSettings(_Base):
    """VPN engine and shuffle engine configuration."""

    active_profile: str | None = None
    openvpn_binary: str = "openvpn"
    wireguard_binary: str = "wg-quick"
    wg_tool_binary: str = "wg"
    connect_timeout: int = Field(default=60, ge=10, le=300)

    shuffle_enabled: bool = False
    shuffle_algorithm: ShuffleAlgorithm = ShuffleAlgorithm.RANDOM
    shuffle_interval: int = Field(default=120)
    shuffle_pool: list[str] = Field(default_factory=list)

    killswitch_enabled: bool = True
    verify_tunnel: bool = True
    reconnect_retries: int = Field(default=3, ge=0, le=10)

    @field_validator("shuffle_interval")
    @classmethod
    def _shuffle_bounds(cls, value: int) -> int:
        return _clamp_interval(value)


class DNSSettings(_Base):
    """DNS Shield configuration -- an independent protection layer."""

    enabled: bool = False
    listen_host: str = "127.0.0.1"
    listen_port: int = Field(default=constants.DNS_SHIELD_PORT, ge=1, le=65535)
    fallback_port: int = Field(default=constants.DNS_SHIELD_FALLBACK_PORT, ge=1, le=65535)

    upstream: list[str] = Field(default_factory=lambda: ["1.1.1.1", "9.9.9.9"])
    #: Prefer Tor's DNSPort as the upstream resolver whenever Tor is running.
    prefer_tor_dns: bool = True

    block_ads: bool = True
    block_trackers: bool = True
    block_malware: bool = True

    #: Blocklist file names (relative to ``dns/``) that are currently enabled.
    enabled_blocklists: list[str] = Field(default_factory=list)

    #: Rewrite ``/etc/resolv.conf`` so the whole system uses the Shield.
    manage_resolv_conf: bool = True
    cache_ttl: int = Field(default=300, ge=0, le=86400)
    block_ipv6_answers: bool = False


class VerificationSettings(_Base):
    """Verification engine configuration."""

    enabled: bool = True
    check_dns_leak: bool = True
    check_ipv6_leak: bool = True
    check_routing: bool = True
    #: Refuse to mark a mode connected if any single check fails.
    strict: bool = True
    timeout: float = Field(default=20.0, ge=5.0, le=120.0)


class InterfaceSettings(_Base):
    """TUI presentation preferences."""

    show_png_banner: bool = True
    banner_width: int = Field(default=72, ge=24, le=200)
    splash_seconds: float = Field(default=2.0, ge=0.0, le=10.0)
    activity_log_lines: int = Field(default=500, ge=50, le=5000)
    confirm_quit: bool = True


class NextronConfig(_Base):
    """Root configuration document."""

    version: str = constants.VERSION
    routing_mode: RoutingMode = RoutingMode.TOR_ONLY
    log_level: str = "INFO"

    tor: TorSettings = Field(default_factory=TorSettings)
    vpn: VPNSettings = Field(default_factory=VPNSettings)
    dns: DNSSettings = Field(default_factory=DNSSettings)
    verification: VerificationSettings = Field(default_factory=VerificationSettings)
    interface: InterfaceSettings = Field(default_factory=InterfaceSettings)

    @field_validator("log_level")
    @classmethod
    def _valid_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            return "INFO"
        return level

    def to_toml(self) -> str:
        return tomli_w.dumps(_toml_ready(self.model_dump(mode="json")))


def _toml_ready(value: Any) -> Any:
    """Strip ``None`` values -- TOML has no null and Pydantic round-trips fine."""
    if isinstance(value, dict):
        return {k: _toml_ready(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_toml_ready(v) for v in value]
    return value


class ConfigManager:
    """Load, mutate and atomically persist :class:`NextronConfig`."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or paths.config_file()
        self._config = NextronConfig()

    # -- properties --------------------------------------------------------- #

    @property
    def path(self) -> Path:
        return self._path

    @property
    def config(self) -> NextronConfig:
        return self._config

    # -- persistence -------------------------------------------------------- #

    def load(self) -> NextronConfig:
        """Read the config file, falling back to defaults on a broken file."""
        paths.ensure_layout()
        if not self._path.exists():
            log.info("No configuration found, writing defaults to %s", self._path)
            self._config = NextronConfig()
            self.save()
            return self._config

        try:
            raw = tomllib.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            log.error("Configuration unreadable (%s); using defaults", exc)
            self._config = NextronConfig()
            return self._config

        try:
            self._config = NextronConfig.model_validate(raw)
        except ValidationError as exc:
            log.error("Configuration invalid (%s); using defaults", exc.error_count())
            self._config = NextronConfig()
            return self._config

        log.debug("Configuration loaded from %s", self._path)
        return self._config

    def save(self) -> None:
        """Write the configuration atomically (temp file + rename)."""
        paths.ensure_layout()
        tmp = self._path.with_suffix(".toml.tmp")
        try:
            tmp.write_text(self._config.to_toml(), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:  # pragma: no cover - disk full / permissions
            raise ConfigError(f"Cannot write configuration: {exc}") from exc
        log.debug("Configuration saved to %s", self._path)

    def update(self, **changes: Any) -> NextronConfig:
        """Apply top-level changes, validate, persist and return the config."""
        data = self._config.model_dump()
        data.update(changes)
        try:
            self._config = NextronConfig.model_validate(data)
        except ValidationError as exc:
            raise ConfigError(str(exc)) from exc
        self.save()
        return self._config

    def replace(self, config: NextronConfig) -> NextronConfig:
        self._config = config
        self.save()
        return self._config
