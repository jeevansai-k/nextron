"""The connection animation.

While an engine is coming up its marker spins; once it is carrying traffic the
marker breathes slowly. Anything settled -- idle, disabled, failed -- keeps the
still marker it always had, so movement on screen always means *something is
happening right now*.

Kept as plain frame lookups rather than a widget so the header, the routing
panel and both engine panels can animate from one shared frame counter and stay
in step with each other.
"""

from __future__ import annotations

from nextron.state.manager import ServiceStatus

__all__ = [
    "FRAME_SECONDS",
    "animated",
    "marker_for",
    "pulse_style",
]

#: How long one frame lasts. Fast enough to read as motion, slow enough that a
#: terminal over SSH is not repainting constantly.
FRAME_SECONDS = 0.12

#: Coming up: a spinner, the universal "working on it" shape.
_BUSY_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

#: Carrying traffic: a slow breath, eight frames per step so it is calm.
_ACTIVE_FRAMES = ("◉", "◉", "◉", "◌")
_ACTIVE_HOLD = 6

#: Dimmed while the breath is at its smallest, so the pulse reads in colour too.
_ACTIVE_DIM = (False, False, False, True)


def animated(status: ServiceStatus) -> bool:
    """True when *status* is one the interface should keep moving."""
    return status.is_busy or status is ServiceStatus.ACTIVE


def marker_for(status: ServiceStatus, frame: int) -> str:
    """The marker to draw for *status* on animation *frame*."""
    if status.is_busy:
        return _BUSY_FRAMES[frame % len(_BUSY_FRAMES)]
    if status is ServiceStatus.ACTIVE:
        step = (frame // _ACTIVE_HOLD) % len(_ACTIVE_FRAMES)
        return _ACTIVE_FRAMES[step]
    return status.marker


def pulse_style(status: ServiceStatus, frame: int) -> str:
    """The colour for *status* on *frame* -- dimmed at the bottom of a breath."""
    if status is ServiceStatus.ACTIVE:
        step = (frame // _ACTIVE_HOLD) % len(_ACTIVE_FRAMES)
        if _ACTIVE_DIM[step]:
            from nextron.core import constants

            return constants.COLOR_SECONDARY
    return status.color
