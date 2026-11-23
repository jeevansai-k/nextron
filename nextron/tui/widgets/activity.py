"""The live activity log.

Subscribes to the event bus so every engine announcement appears immediately,
and replays the in-memory log ring buffer on mount so the panel is never empty
after a screen switch.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime

from rich.text import Text
from textual.widgets import RichLog

from nextron.core import constants
from nextron.core.events import Event, EventBus, EventType
from nextron.utils.logging import get_ring_buffer

__all__ = ["ActivityLog"]

#: ``level -> (glyph, glyph colour, message colour)``. The glyph carries the
#: signal colour; the message stays readable text, so a long log is not a wall
#: of red and green.
_LEVELS: dict[str, tuple[str, str, str]] = {
    "success": ("✓", constants.SIGNAL_OK, constants.INK_TEXT),
    "info": ("∙", constants.INK_FAINT, constants.INK_MUTED),
    "warning": ("!", constants.SIGNAL_WARN, constants.INK_TEXT),
    "error": ("✗", constants.SIGNAL_ERR, constants.SIGNAL_ERR),
    "critical": ("✗", constants.SIGNAL_ERR, constants.SIGNAL_ERR),
    "debug": ("∙", constants.INK_FAINT, constants.INK_FAINT),
}

#: Events that mean something just *worked*: shown with a green tick.
_SUCCESS = {
    EventType.APP_READY,
    EventType.ROUTE_UP,
    EventType.TOR_READY,
    EventType.TOR_ROTATED,
    EventType.VPN_CONNECTED,
    EventType.VPN_SWITCHED,
    EventType.DNS_ENABLED,
    EventType.VERIFICATION_PASSED,
}

#: Messages that begin with their own verdict glyph.
_LEADING_MARKERS = {"✓ ": "success", "! ": "warning", "✗ ": "error", "- ": "debug"}

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
            # RichLog's default is 78: every line was wrapped at 78 columns
            # and then cropped to the narrower panel, losing its ending.
            min_width=20,
            **kwargs,
        )
        self._bus = bus
        self._subscribed = False
        # RichLog wraps a line once, at the width it had when written. Lines
        # written before the first layout (the replay) were cropped to a
        # provisional width, so keep them and re-flow on every resize.
        self._lines: deque[Text] = deque(maxlen=lines)
        self._flowed_width = 0

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
        glyph, glyph_style, text_style = _LEVELS.get(level, _LEVELS["info"])
        line = Text(no_wrap=False)
        line.append(f"{stamp} ", style=constants.INK_FAINT)
        line.append(f"{glyph} ", style=f"bold {glyph_style}")
        line.append(message, style=text_style)
        self._lines.append(line)
        self.write(line)

    def on_resize(self) -> None:
        """Re-wrap every line at the new width instead of cropping it."""
        width = self.scrollable_content_region.width
        if width <= 0 or width == self._flowed_width:
            return
        self._flowed_width = width
        self.clear()
        for line in self._lines:
            self.write(line)

    def _replay(self) -> None:
        """Show what already happened before this widget existed."""
        for entry in get_ring_buffer().tail(40):
            if entry.level == "DEBUG":
                continue
            self.append(
                entry.message,
                level=entry.level.lower(),
                moment=entry.timestamp,
            )

    def _on_event(self, event: Event) -> None:
        if event.type in _MUTED or not event.message:
            return
        level = event.level
        message = event.message
        marker = _LEADING_MARKERS.get(message[:2])
        if marker is not None:
            # Verification steps carry their own ✓ / ! / ✗; use it as the glyph.
            level, message = marker, message[2:]
        elif level == "info" and event.type in _SUCCESS:
            level = "success"
        self.append(message, level=level, moment=event.timestamp)
