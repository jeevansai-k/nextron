"""The rotation interval ladder used by every time-changing element."""

from __future__ import annotations

import pytest

from nextron.core import constants
from nextron.utils import intervals as iv


def test_the_ladder_matches_the_requested_steps():
    """15s, 30s, then every 30 seconds up to 5 minutes."""
    assert iv.PRESETS[0] == 15
    assert iv.PRESETS[1] == 30
    assert iv.PRESETS[2] == 60
    assert iv.PRESETS[-1] == 300
    # From 1m onwards the gap is always 30s.
    gaps = {b - a for a, b in zip(iv.PRESETS[2:], iv.PRESETS[3:], strict=False)}
    assert gaps == {30}
    assert len(iv.PRESETS) == 11


def test_every_preset_is_inside_the_specified_window():
    for preset in iv.PRESETS:
        assert constants.ROTATION_MIN_SECONDS <= preset <= constants.ROTATION_MAX_SECONDS


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (15, "15s"),
        (30, "30s"),
        (60, "1m"),
        (90, "1m 30s"),
        (120, "2m"),
        (270, "4m 30s"),
        (300, "5m"),
    ],
)
def test_labels(seconds, expected):
    assert iv.label(seconds) == expected


@pytest.mark.parametrize(
    "seconds,expected", [(5, 15), (20, 15), (23, 30), (70, 60), (200, 210), (999, 300)]
)
def test_arbitrary_values_snap_to_the_nearest_rung(seconds, expected):
    assert iv.nearest(seconds) == expected


def test_stepping_moves_one_rung():
    assert iv.step(60, +1) == 90
    assert iv.step(60, -1) == 30
    assert iv.step(30, +1) == 60
    assert iv.step(30, -1) == 15


def test_stepping_stops_at_both_ends():
    assert iv.step(15, -1) == 15
    assert iv.step(300, +1) == 300


def test_an_off_ladder_value_lands_on_the_ladder_first():
    # A hand-edited 45s snaps to 30s rather than skipping to 60s.
    assert iv.step(45, +1) == 30
    assert iv.step(45, -1) == 30


def test_cycling_wraps_around():
    assert iv.cycle(15) == 30
    assert iv.cycle(120) == 150
    assert iv.cycle(300) == 15


def test_walking_the_whole_ladder_up_and_down():
    value = iv.PRESETS[0]
    for expected in iv.PRESETS[1:]:
        value = iv.step(value, +1)
        assert value == expected
    for expected in reversed(iv.PRESETS[:-1]):
        value = iv.step(value, -1)
        assert value == expected


def test_strip_shows_arrows_only_where_movement_is_possible():
    assert iv.strip(60) == "◂ 1m ▸"
    assert iv.strip(15).strip() == "15s ▸"
    assert iv.strip(300).strip() == "◂ 5m"


def test_ladder_marks_the_current_value():
    rendered = iv.ladder(60)
    assert "[1m]" in rendered
    assert "30s" in rendered and "1m 30s" in rendered


def test_the_config_still_accepts_any_value_in_the_window():
    """Presets drive the UI; a hand-edited config keeps the full 5s-5m range."""
    from nextron.core.config import NextronConfig

    config = NextronConfig()
    config.tor.rotation_interval = 7
    assert config.tor.rotation_interval == 7
    config.tor.rotation_interval = 5000
    assert config.tor.rotation_interval == constants.ROTATION_MAX_SECONDS
