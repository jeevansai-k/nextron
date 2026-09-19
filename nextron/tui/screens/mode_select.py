"""Routing mode selection -- exactly one mode may be active."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import ListItem, ListView, Static

from nextron.core import constants
from nextron.core.config import RoutingMode
from nextron.routing.modes import describe
from nextron.tui.screens.base import NextronScreen

__all__ = ["ModeSelectScreen"]


class ModeSelectScreen(NextronScreen):
    """Pick one of the four routing modes; Enter establishes it."""

    subtitle = "Routing Mode"

    BINDINGS = [
        Binding("enter", "activate", "Select"),
        Binding("escape", "close", "Return"),
    ]

    def compose_body(self) -> ComposeResult:
        yield Static(
            Text(
                "Enter selects a mode; nothing is started here. Return to the "
                "dashboard and press C to connect it.",
                style=constants.COLOR_SURFACE,
            ),
            classes="screen-hint",
        )
        with Horizontal(classes="screen-body") as body:
            body.border_title = "Routing Modes"
            with Vertical():
                yield ListView(
                    *[self._item(mode) for mode in RoutingMode], id="mode-list"
                )
            with Vertical():
                yield Static("", id="mode-detail")

    @staticmethod
    def _item(mode: RoutingMode) -> ListItem:
        text = Text(no_wrap=True)
        text.append(f"{mode.label:<14}", style=f"bold {constants.COLOR_TEXT}")
        text.append(mode.chain, style=constants.COLOR_SURFACE)
        item = ListItem(Static(text))
        item.data_mode = mode  # type: ignore[attr-defined]
        return item

    def on_mount(self) -> None:
        super().on_mount()
        listing = self.query_one("#mode-list", ListView)
        current = self.app.runtime.config.routing_mode
        listing.index = list(RoutingMode).index(current)
        listing.focus()
        self._render_detail(current)

    # -- detail ------------------------------------------------------------- #

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        mode = getattr(event.item, "data_mode", None) if event.item else None
        if mode is not None:
            self._render_detail(mode)

    def _render_detail(self, mode: RoutingMode) -> None:
        descriptor = describe(mode)
        text = Text()
        text.append(f"{descriptor.label}\n", style=f"bold {constants.COLOR_ACCENT}")
        text.append(f"{descriptor.chain}\n\n", style=constants.COLOR_TEXT)
        text.append(f"{descriptor.summary}\n\n", style=constants.COLOR_SURFACE)
        text.append("Execution sequence\n", style=f"bold {constants.COLOR_TEXT}")
        for index, step in enumerate(descriptor.sequence, start=1):
            text.append(f"  {index}. {step}\n", style=constants.COLOR_TEXT)

        if descriptor.requires_vpn_profile:
            profiles = len(self.app.runtime.vpn.library)
            text.append("\nRequires a VPN profile: ", style=constants.COLOR_SURFACE)
            text.append(
                f"{profiles} available\n",
                style=constants.COLOR_ACCENT if profiles else constants.COLOR_PRIMARY,
            )
        if descriptor.notes:
            text.append("\nNotes\n", style=f"bold {constants.COLOR_TEXT}")
            for note in descriptor.notes:
                text.append(f"  - {note}\n", style=constants.COLOR_SURFACE)

        self.query_one("#mode-detail", Static).update(text)

    # -- actions ------------------------------------------------------------ #

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        event.stop()
        self.action_activate()

    def action_activate(self) -> None:
        """Select the mode. Connecting is a separate, deliberate act (C)."""
        listing = self.query_one("#mode-list", ListView)
        item = listing.highlighted_child
        mode = getattr(item, "data_mode", None) if item else None
        if mode is None:
            return

        self.app.runtime.select_mode(mode)
        self.app.pop_screen()

        if self.app.runtime.routing.active:
            self.app.notify(
                f"{mode.label} selected. Press C to disconnect the current "
                "route and bring this one up.",
                title="Mode selected",
                severity="warning",
                timeout=8,
            )
        else:
            self.app.notify(
                f"{mode.label} selected -- press C to connect.",
                title="Mode selected",
            )
