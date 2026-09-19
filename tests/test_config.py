"""Configuration validation, clamping and persistence."""

from __future__ import annotations

import tomllib

import pytest

from nextron.core import constants
from nextron.core.config import (
    ConfigManager,
    NextronConfig,
    RoutingMode,
    ShuffleAlgorithm,
)


def test_defaults_are_written_on_first_load(config_manager):
    assert config_manager.path.is_file()
    assert config_manager.config.routing_mode is RoutingMode.TOR_ONLY


@pytest.mark.parametrize(
    "given,expected",
    [
        (1, constants.ROTATION_MIN_SECONDS),
        (0, constants.ROTATION_MIN_SECONDS),
        (-30, constants.ROTATION_MIN_SECONDS),
        (5, 5),
        (60, 60),
        (300, 300),
        (301, constants.ROTATION_MAX_SECONDS),
        (99999, constants.ROTATION_MAX_SECONDS),
    ],
)
def test_rotation_intervals_are_clamped(given, expected):
    config = NextronConfig()
    config.tor.rotation_interval = given
    config.vpn.shuffle_interval = given
    assert config.tor.rotation_interval == expected
    assert config.vpn.shuffle_interval == expected


def test_routing_mode_metadata():
    assert RoutingMode.TOR_OVER_VPN.label == "Tor over VPN"
    assert RoutingMode.VPN_OVER_TOR.chain == "You -> Tor -> VPN -> Internet"
    assert RoutingMode.TOR_ONLY.uses_tor and not RoutingMode.TOR_ONLY.uses_vpn
    assert RoutingMode.VPN_ONLY.uses_vpn and not RoutingMode.VPN_ONLY.uses_tor
    assert all(mode.uses_tor for mode in RoutingMode if mode is not RoutingMode.VPN_ONLY)


def test_shuffle_algorithms_cover_the_specification():
    assert {a.label for a in ShuffleAlgorithm} == {
        "Random",
        "Sequential",
        "Round Robin",
        "No Repeat",
    }


def test_round_trip_through_toml(config_manager):
    config_manager.config.routing_mode = RoutingMode.VPN_OVER_TOR
    config_manager.config.tor.exit_countries = ["de", "nl"]
    config_manager.save()

    raw = tomllib.loads(config_manager.path.read_text())
    assert raw["routing_mode"] == "vpn_over_tor"
    assert raw["tor"]["exit_countries"] == ["DE", "NL"]

    reloaded = ConfigManager().load()
    assert reloaded.routing_mode is RoutingMode.VPN_OVER_TOR
    assert reloaded.tor.exit_countries == ["DE", "NL"]


def test_broken_config_falls_back_to_defaults(config_manager):
    config_manager.path.write_text("this is not = valid = toml [[", encoding="utf-8")
    config = ConfigManager().load()
    assert config.routing_mode is RoutingMode.TOR_ONLY


def test_unknown_keys_are_ignored(config_manager):
    config_manager.path.write_text(
        'routing_mode = "vpn_only"\nnot_a_real_key = 42\n', encoding="utf-8"
    )
    config = ConfigManager().load()
    assert config.routing_mode is RoutingMode.VPN_ONLY


def test_invalid_log_level_is_normalised():
    assert NextronConfig(log_level="loud").log_level == "INFO"
    assert NextronConfig(log_level="debug").log_level == "DEBUG"
