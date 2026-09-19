"""The Logs screen: the full session log with level filtering."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import RichLog, Static

from nextron.core import constants
from nextron.storage import paths
from nextron.tui.screens.base import NextronScreen
from nextron.utils.logging import LogRecordEntry, get_ring_buffer

__all__ = ["LogsScreen"]

_FILTERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("All", ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")),
    ("Info+", ("INFO", "WARNING", "ERROR", "CRITICAL")),
    ("Warnings+", ("WARNING", "ERROR", "CRITICAL")),
    ("Errors", ("ERROR", "CRITICAL")),
)

_STYLES = {
    "DEBUG": constants.COLOR_SECONDARY,
    "INFO": constants.COLOR_TEXT,
    "WARNING": constants.COLOR_SURFACE,
    "ERROR": constants.COLOR_PRIMARY,
    "CRITICAL": constants.COLOR_PRIMARY,
}


class LogsScreen(NextronScreen):
    """Structured logs, live-updating, with a level filter."""

    subtitle = "Logs"

    BINDINGS = [
        Binding("f", "cycle_filter", "Filter"),
        Binding("c", "clear_view", "Clear view"),
        Binding("t", "toggle_follow", "Follow"),
        Binding("escape", "close", "Return"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._filter = 1  # Info+
        self._follow = True

    def compose_body(self) -> ComposeResult:
        yield Static("", id="logs-header", classes="screen-hint")
        with Vertical(classes="screen-body") as body:
            body.border_title = "Session log"
            yield RichLog(
                id="logs-view", markup=False, wrap=True, highlight=False, max_lines=4000
            )

    def on_mount(self) -> None:
        super().on_mount()
        self._render_header()
        self._repaint()
        get_ring_buffer().subscribe(self._on_record)
        self.query_one(RichLog).focus()

    def on_unmount(self) -> None:
        get_ring_buffer().unsubscribe(self._on_record)

    # -- rendering ---------------------------------------------------------- #

    def _render_header(self) -> None:
        name, _ = _FILTERS[self._filter]
        text = Text(no_wrap=True)
        text.append("Filter ", style=constants.COLOR_SURFACE)
        text.append(name, style=f"bold {constants.COLOR_TEXT}")
        text.append("   Follow ", style=constants.COLOR_SURFACE)
        text.append("on" if self._follow else "off", style=f"bold {constants.COLOR_TEXT}")
        text.append(
            f"   File {paths.logs_dir() / 'nextron.log'}", style=constants.COLOR_SURFACE
        )
        self.query_one("#logs-header", Static).update(text)

    def _levels(self) -> tuple[str, ...]:
        return _FILTERS[self._filter][1]

    def _line(self, entry: LogRecordEntry) -> Text:
        line = Text(no_wrap=False)
        line.append(f"{entry.clock} ", style=constants.COLOR_SECONDARY)
        style = _STYLES.get(entry.level, constants.COLOR_TEXT)
        line.append(f"{entry.level:<8}", style=style)
        line.append(f"{entry.source:<22} ", style=constants.COLOR_SECONDARY)
        line.append(entry.message, style=_STYLES.get(entry.level, constants.COLOR_TEXT))
        return line

    def _repaint(self) -> None:
        view = self.query_one(RichLog)
        view.clear()
        levels = self._levels()
        for entry in get_ring_buffer().entries():
            if entry.level in levels:
                view.write(self._line(entry))

    def _on_record(self, entry: LogRecordEntry) -> None:
        if not self.is_mounted or not self._follow:
            return
        if entry.level not in self._levels():
            return
        try:
            self.query_one(RichLog).write(self._line(entry))
        except Exception:  # pragma: no cover - screen torn down mid-write
            pass

    # -- actions ------------------------------------------------------------ #

    def action_cycle_filter(self) -> None:
        self._filter = (self._filter + 1) % len(_FILTERS)
        self._render_header()
        self._repaint()

    def action_clear_view(self) -> None:
        self.query_one(RichLog).clear()

    def action_toggle_follow(self) -> None:
        self._follow = not self._follow
        self._render_header()
