"""The live activity log.

Subscribes to the event bus so every engine announcement appears immediately,
and replays the in-memory log ring buffer on mount so the panel is never empty
after a screen switch.
"""

from __future__ import annotations

from datetime import datetime

from rich.text import Text
from textual.widgets import RichLog

from nextron.core import constants
from nextron.core.events import Event, EventBus, EventType
from nextron.utils.logging import get_ring_buffer

__all__ = ["ActivityLog"]

_LEVEL_STYLES = {
    "info": constants.COLOR_TEXT,
    "success": constants.COLOR_ACCENT,
    "warning": constants.COLOR_SURFACE,
    "error": constants.COLOR_PRIMARY,
    "debug": constants.COLOR_SECONDARY,
}

#: Events that are too chatty for the activity panel.
_MUTED = {EventType.STATE_CHANGED, EventType.SCHEDULER_TICK, EventType.TOR_CIRCUIT}


class ActivityLog(RichLog):
    """A scrolling, colour-coded feed of engine events."""

    def __init__(self, bus: EventBus, *, lines: int = 500, **kwargs) -> None:
        super().__init__(
            max_lines=lines,
            highlight=False,
            markup=False,
            wrap=True,
            auto_scroll=True,
            **kwargs,
        )
        self._bus = bus
        self._subscribed = False

    def on_mount(self) -> None:
        self.border_title = "Activity"
        if not self._subscribed:
            self._bus.subscribe(None, self._on_event)
            self._subscribed = True
        self._replay()

    def on_unmount(self) -> None:
        if self._subscribed:
            self._bus.unsubscribe(None, self._on_event)
            self._subscribed = False

    # -- writing ------------------------------------------------------------ #

    def append(
        self, message: str, *, level: str = "info", moment: datetime | None = None
    ) -> None:
        """Write one line into the feed."""
        stamp = (moment or datetime.now()).strftime("%H:%M:%S")
        line = Text(no_wrap=False)
        line.append(f"{stamp} ", style=constants.COLOR_SECONDARY)
        line.append(
            message, style=_LEVEL_STYLES.get(level, constants.COLOR_TEXT)
        )
        self.write(line)

    def _replay(self) -> None:
        """Show what already happened before this widget existed."""
        for entry in get_ring_buffer().tail(40):
            if entry.level == "DEBUG":
                continue
            self.append(
                entry.message,
                level=entry.level.lower() if entry.level != "INFO" else "info",
                moment=entry.timestamp,
            )

    def _on_event(self, event: Event) -> None:
        if event.type in _MUTED or not event.message:
            return
        self.append(event.message, level=event.level, moment=event.timestamp)
