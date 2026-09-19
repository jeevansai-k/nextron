"""Dashboard panels.

``InfoPanel`` is a focusable, bordered key/value panel: the dashboard owns
several of them (routing, Tor, VPN, DNS Shield, session) and refreshes them
from a single :class:`~nextron.state.manager.AppState` snapshot.
"""

from __future__ import annotations

from rich.table import Table
from rich.text import Text
from textual.widgets import Static

from nextron.core import constants

__all__ = ["CountdownPanel", "InfoPanel"]

Row = tuple[str, str | Text]


class InfoPanel(Static):
    """A bordered panel showing aligned label/value rows."""

    can_focus = True

    def __init__(self, title: str, *, panel_id: str | None = None, **kwargs) -> None:
        super().__init__("", id=panel_id, **kwargs)
        self._title = title
        self._rows: list[Row] = []
        self._note: str | None = None

    def on_mount(self) -> None:
        self.border_title = self._title
        self.refresh_content()

    # -- content ------------------------------------------------------------ #

    def set_rows(self, rows: list[Row], *, note: str | None = None) -> None:
        """Replace the panel's rows (and optional footer note)."""
        self._rows = rows
        self._note = note
        if self.is_mounted:
            self.refresh_content()

    def set_state_class(self, *, active: bool = False, error: bool = False) -> None:
        """Tint the border to reflect the panel's engine state."""
        self.set_class(active and not error, "-panel-active")
        self.set_class(error, "-panel-error")

    def refresh_content(self) -> None:
        self.update(self.build_table())

    def build_table(self) -> Table:
        """The panel's rows as a Rich grid (separate so it can be measured)."""
        table = Table.grid(padding=(0, 1), expand=True)
        table.add_column(
            justify="left", style=constants.COLOR_SURFACE, no_wrap=True, width=14
        )
        # ratio=1 is what keeps the panels aligned: it sends every spare column
        # to the value, so the value edge sits at the same place in all of them.
        # Without it Rich shares the slack out by measured content width and
        # each panel lands its values somewhere else.
        table.add_column(
            justify="left", style=constants.COLOR_TEXT, overflow="fold", ratio=1
        )

        for label, value in self._rows:
            rendered = value if isinstance(value, Text) else Text(str(value))
            table.add_row(Text(label, no_wrap=True), rendered)

        if self._note:
            table.add_row("", Text(self._note, style=f"italic {constants.COLOR_SURFACE}"))
        return table


class CountdownPanel(Static):
    """Side-by-side Tor and VPN countdowns, each with an interval stepper.

    The stepper is the interface's time-changing element: it walks the preset
    ladder (15s, 30s, then every 30s to 5m) rather than asking for a number.
    """

    can_focus = True

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self._tor = ("--:--", "◂ 1m ▸", "off", "[ ]")
        self._vpn = ("--:--", "◂ 2m ▸", "off", ", .")

    def on_mount(self) -> None:
        self.border_title = "Rotation Schedulers"
        self.refresh_content()

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
        self._tor = (tor_clock, tor_interval, tor_detail, tor_keys)
        self._vpn = (vpn_clock, vpn_interval, vpn_detail, vpn_keys)
        if self.is_mounted:
            self.refresh_content()

    def refresh_content(self) -> None:
        table = Table.grid(expand=True, padding=(0, 2))
        table.add_column(justify="center", ratio=1)
        table.add_column(justify="center", ratio=1)

        table.add_row(
            Text("TOR IDENTITY", style=f"bold {constants.COLOR_SURFACE}"),
            Text("VPN PROFILE", style=f"bold {constants.COLOR_SURFACE}"),
        )
        table.add_row(
            Text(self._tor[0], style=f"bold {constants.COLOR_ACCENT}"),
            Text(self._vpn[0], style=f"bold {constants.COLOR_ACCENT}"),
        )
        table.add_row(
            _stepper(self._tor[1], self._tor[3]),
            _stepper(self._vpn[1], self._vpn[3]),
        )
        table.add_row(
            Text(self._tor[2], style=constants.COLOR_TEXT),
            Text(self._vpn[2], style=constants.COLOR_TEXT),
        )
        self.update(table)


def _stepper(interval: str, keys: str) -> Text:
    """``"◂ 1m ▸"`` plus the keys that move it, in one line."""
    text = Text(no_wrap=True)
    text.append(interval, style=f"bold {constants.COLOR_TEXT}")
    text.append(f"  {keys}", style=constants.COLOR_SECONDARY)
    return text
