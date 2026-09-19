"""Rotation interval presets and the stepper used by the interface.

The specification allows any interval from 5 seconds to 5 minutes. The
interface walks a fixed ladder of sensible values instead of free-typing
numbers: 15s, 30s, then every 30 seconds up to 5 minutes.
"""

from __future__ import annotations

from nextron.core import constants

__all__ = [
    "PRESETS",
    "index_of",
    "label",
    "ladder",
    "nearest",
    "step",
    "strip",
]

PRESETS: tuple[int, ...] = constants.ROTATION_PRESETS


def label(seconds: int) -> str:
    """``90`` -> ``"1m 30s"``, ``60`` -> ``"1m"``, ``15`` -> ``"15s"``."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, remainder = divmod(seconds, 60)
    if remainder:
        return f"{minutes}m {remainder}s"
    return f"{minutes}m"


def nearest(seconds: int) -> int:
    """Snap an arbitrary interval onto the closest preset."""
    return min(PRESETS, key=lambda preset: (abs(preset - int(seconds)), preset))


def index_of(seconds: int) -> int:
    """Position of *seconds* on the ladder, snapping if it is not a preset."""
    return PRESETS.index(nearest(seconds))


def step(seconds: int, direction: int) -> int:
    """Move one rung up (``+1``) or down (``-1``), stopping at the ends."""
    position = index_of(seconds)
    if int(seconds) not in PRESETS and direction:
        # An off-ladder value snaps onto the ladder first, so the first press
        # lands on a real preset instead of skipping past one.
        return PRESETS[position]
    target = max(0, min(len(PRESETS) - 1, position + direction))
    return PRESETS[target]


def cycle(seconds: int) -> int:
    """Move one rung up, wrapping around to the shortest preset at the top."""
    return PRESETS[(index_of(seconds) + 1) % len(PRESETS)]


def strip(seconds: int) -> str:
    """A compact stepper: ``"◂ 1m ▸"``, with the arrow dropped at each end."""
    position = index_of(seconds)
    left = "◂" if position > 0 else " "
    right = "▸" if position < len(PRESETS) - 1 else " "
    return f"{left} {label(nearest(seconds))} {right}"


def ladder(seconds: int, span: int = 2) -> str:
    """Neighbouring presets around the current one, e.g. ``"30s . [1m] . 1m 30s"``."""
    position = index_of(seconds)
    start = max(0, position - span)
    end = min(len(PRESETS), position + span + 1)
    parts = [
        f"[{label(preset)}]" if offset + start == position else label(preset)
        for offset, preset in enumerate(PRESETS[start:end])
    ]
    return " . ".join(parts)
