"""Dashboard panels.

``InfoPanel`` is a focusable, bordered key/value panel. The dashboard owns
several (route, Tor, VPN, DNS Shield, session) and refreshes them from one
:class:`~nextron.state.manager.AppState` snapshot.

A panel can also show:

* a *lead* -- a renderable above the rows (the route chain, a progress bar);
* an *empty state* -- one sentence instead of a column of ``--`` when the
  engine has nothing to report, saying why and what to press.
"""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual.widgets import Static

from nextron.core import constants

__all__ = ["CountdownPanel", "InfoPanel", "SchedulerLine"]

Row = tuple[str, str | Text]

#: Width of the label column: wide enough for "Last rotation".
_LABEL_WIDTH = 13


class InfoPanel(Static):
    """A bordered panel showing aligned label/value rows."""

    can_focus = True

    def __init__(self, title: str, *, panel_id: str | None = None, **kwargs) -> None:
        super().__init__("", id=panel_id, **kwargs)
        self._title = title
        self._rows: list[Row] = []
        self._note: str | Text | None = None
        self._lead: RenderableType | None = None
        self._empty: Text | None = None

    def on_mount(self) -> None:
        self.border_title = self._title
        self.refresh_content()

    # -- content ------------------------------------------------------------ #

    def set_rows(
        self,
        rows: list[Row],
        *,
        note: str | Text | None = None,
        lead: RenderableType | None = None,
    ) -> None:
        """Replace the panel's rows, with an optional lead and footer note."""
        self._rows = rows
        self._note = note
        self._lead = lead
        self._empty = None
        if self.is_mounted:
            self.refresh_content()

    def set_empty(self, message: str | Text, *, hint: Text | None = None) -> None:
        """Show one explanatory line (and a key hint) instead of empty rows."""
        text = (
            message.copy()
            if isinstance(message, Text)
            else Text(message, style=constants.INK_MUTED)
        )
        if hint is not None:
            text.append("\n")
            text.append_text(hint)
        self._empty = text
        self._rows = []
        self._note = None
        self._lead = None
        if self.is_mounted:
            self.refresh_content()

    def set_state_class(
        self,
        *,
        active: bool = False,
        error: bool = False,
        busy: bool = False,
        unused: bool = False,
        warn: bool = False,
    ) -> None:
        """Tint the border title to reflect the engine's state."""
        self.set_class(warn and not error, "-panel-warn")
        self.set_class(active and not error, "-panel-active")
        self.set_class(error, "-panel-error")
        self.set_class(busy and not error, "-panel-busy")
        self.set_class(unused and not (active or error or busy), "-panel-unused")

    def refresh_content(self) -> None:
        self.update(self.build_renderable())

    def build_renderable(self) -> RenderableType:
        if self._empty is not None:
            return self._empty
        table = self.build_table()
        if self._lead is None:
            return table
        return Group(self._lead, Text(""), table)

    def build_table(self) -> Table:
        """The panel's rows as a Rich grid (separate so it can be measured)."""
        table = Table.grid(padding=(0, 1), expand=True)
        table.add_column(
            justify="left",
            style=constants.INK_MUTED,
            no_wrap=True,
            width=_LABEL_WIDTH,
        )
        # ratio=1 sends every spare column to the value, so value edges line
        # up across panels of different content.
        table.add_column(
            justify="left", style=constants.INK_TEXT, overflow="fold", ratio=1
        )

        for label, value in self._rows:
            rendered = value if isinstance(value, Text) else _value(value)
            table.add_row(Text(label, no_wrap=True), rendered)

        if self._note:
            note = (
                self._note
                if isinstance(self._note, Text)
                else Text(self._note, style=constants.SIGNAL_ERR)
            )
            table.add_row("", note)
        return table


def _value(value: object) -> Text:
    """Plain values; a bare ``--`` is drawn faint so real data stands out."""
    raw = str(value)
    if raw in {"--", "never", ""}:
        return Text(raw or "--", style=constants.INK_FAINT)
    return Text(raw)


class SchedulerLine:
    """What one scheduler row in :class:`CountdownPanel` shows."""

    __slots__ = (
        "clock",
        "detail",
        "enabled",
        "interval",
        "keys",
        "running",
        "title",
        "unused",
    )

    def __init__(
        self,
        title: str,
        *,
        clock: str = "--:--",
        interval: str = "1m",
        detail: str = "",
        keys: str = "",
        enabled: bool = False,
        running: bool = False,
        unused: bool = False,
    ) -> None:
        self.title = title
        #: The live route does not use this engine at all.
        self.unused = unused
        self.clock = clock
        self.interval = interval
        self.detail = detail
        self.keys = keys
        self.enabled = enabled
        self.running = running


class CountdownPanel(Static):
    """Tor and VPN countdowns, each with its switch and interval stepper.

    Three honest states per scheduler: *running* (a live countdown), *armed*
    (switched on, waits for a connection) and *off* (and which key turns it on).
    """

    can_focus = True

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self._lines = (
            SchedulerLine("Tor identity", keys="[ ]", detail="R"),
            SchedulerLine("VPN profile", keys=", .", detail="F"),
        )

    def on_mount(self) -> None:
        self.border_title = "Schedulers"
        self.refresh_content()

    def set_lines(self, tor: SchedulerLine, vpn: SchedulerLine) -> None:
        self._lines = (tor, vpn)
        if self.is_mounted:
            self.refresh_content()

    # Backwards-compatible entry point used by older callers and tests.
    def set_countdowns(
        self,
        *,
        tor_clock: str,
        tor_interval: str,
        tor_detail: str,
        vpn_clock: str,
        vpn_interval: str,
        vpn_detail: str,
        tor_keys: str = "[ ]",
        vpn_keys: str = ", .",
    ) -> None:
        self.set_lines(
            SchedulerLine(
                "Tor identity",
                clock=tor_clock,
                interval=tor_interval,
                detail=tor_detail,
                keys=tor_keys,
                enabled=tor_clock != "--:--",
                running=tor_clock != "--:--",
            ),
            SchedulerLine(
                "VPN profile",
                clock=vpn_clock,
                interval=vpn_interval,
                detail=vpn_detail,
                keys=vpn_keys,
                enabled=vpn_clock != "--:--",
                running=vpn_clock != "--:--",
            ),
        )

    def refresh_content(self) -> None:
        table = Table.grid(expand=True, padding=(0, 1))
        table.add_column(ratio=1, no_wrap=True)
        table.add_column(justify="right", no_wrap=True)
        for index, line in enumerate(self._lines):
            if index:
                table.add_row(Text(""), Text(""))
            table.add_row(*_scheduler_rows(line)[0])
            table.add_row(*_scheduler_rows(line)[1])
        self.update(table)


def _scheduler_rows(line: SchedulerLine) -> tuple[tuple[Text, Text], tuple[Text, Text]]:
    """Two rows per scheduler: title + state, then countdown + stepper."""
    title = Text(line.title, style=f"bold {constants.INK_TEXT}")
    if line.running:
        state = Text("◉ running", style=f"bold {constants.SIGNAL_OK}")
        clock = Text(line.clock, style=f"bold {constants.INK_TEXT}")
        clock.append("  until next", style=constants.INK_FAINT)
    elif line.enabled and line.unused:
        state = Text("◌ on", style=constants.INK_MUTED)
        clock = Text("not used by this mode", style=constants.INK_FAINT)
    elif line.enabled:
        state = Text("◌ armed", style=f"bold {constants.SIGNAL_BUSY}")
        clock = Text("starts on connect", style=constants.INK_MUTED)
    else:
        state = Text("◌ off", style=constants.INK_FAINT)
        clock = Text(no_wrap=True)
        clock.append(line.detail or "", style=f"bold {constants.ACCENT}")
        clock.append(" to turn on", style=constants.INK_FAINT)

    stepper = Text(no_wrap=True)
    stepper.append(f"every {line.interval}", style=constants.INK_MUTED)
    if line.keys:
        stepper.append(f"  {line.keys}", style=f"bold {constants.ACCENT}")
    return (title, state), (clock, stepper)
