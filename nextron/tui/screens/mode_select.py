"""Routing mode selection -- exactly one mode may be active."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import ListItem, ListView, Static

from nextron.core import constants, process
from nextron.core.config import RoutingMode
from nextron.routing.modes import describe
from nextron.tui.screens.base import NextronScreen
from nextron.tui.widgets import signal

__all__ = ["ModeSelectScreen"]


class ModeSelectScreen(NextronScreen):
    """Pick one of the four routing modes; Enter establishes it."""

    subtitle = "Routing mode"

    BINDINGS = [
        Binding("enter", "activate", "Select"),
        Binding("escape", "close", "Return"),
    ]

    def compose_body(self) -> ComposeResult:
        yield Static(
            Text(
                "Enter selects a mode; nothing is started here. Return to the "
                "dashboard and press C to connect it.",
                style=constants.INK_MUTED,
            ),
            classes="screen-hint",
        )
        with Horizontal(classes="screen-body") as body:
            body.border_title = "Routing modes"
            with Vertical(id="mode-list-column"):
                yield ListView(
                    *[self._item(mode) for mode in RoutingMode], id="mode-list"
                )
            with VerticalScroll(id="mode-detail-column"):
                yield Static("", id="mode-detail")

    def _item(self, mode: RoutingMode) -> ListItem:
        verdict = _verdict(self._readiness(mode))
        glyph, colour = {
            "ready": ("✓", constants.SIGNAL_OK),
            "partial": ("!", constants.SIGNAL_WARN),
            "blocked": ("✗", constants.SIGNAL_ERR),
        }[verdict]
        text = Text(no_wrap=True)
        text.append(f"{glyph} ", style=f"bold {colour}")
        text.append(f"{mode.label:<14}", style=f"bold {constants.INK_TEXT}")
        text.append_text(signal.chain(mode))
        item = ListItem(Static(text))
        item.data_mode = mode  # type: ignore[attr-defined]
        return item

    # -- readiness ---------------------------------------------------------- #

    def _readiness(self, mode: RoutingMode) -> list[tuple[bool | None, str]]:
        """What this machine has, and lacks, for *mode*.

        ``True`` is satisfied, ``None`` works but degraded, ``False`` blocks the
        mode outright -- so a mode that cannot connect says why before C does.
        """
        runtime = self.app.runtime
        checks: list[tuple[bool | None, str]] = []
        root = process.can_escalate()

        if mode.uses_tor:
            settings = runtime.config.tor
            if not settings.manage_daemon:
                checks.append((True, "Uses the system Tor daemon"))
            elif process.which(settings.binary):
                checks.append((True, "Tor is installed"))
            else:
                checks.append((False, "Tor is not installed (sudo apt install tor)"))

        if mode.uses_vpn:
            library = runtime.vpn.library
            profiles = library.all()
            if mode is RoutingMode.VPN_OVER_TOR:
                tcp = [p for p in profiles if p.supports_socks]
                checks.append(
                    (bool(tcp), f"{len(tcp)} TCP OpenVPN profile(s) (needed over Tor)")
                )
            else:
                checks.append(
                    (bool(profiles), f"{len(profiles)} VPN profile(s) imported")
                )
            checks.append(
                (
                    root,
                    "Root available for the tunnel"
                    if root
                    else "Needs root for the tunnel -- run 'sudo -v' or sudo nextron",
                )
            )
        elif mode.uses_tor:
            checks.append(
                (
                    True if root else None,
                    "System-wide routing (root)"
                    if root
                    else "No root: SOCKS proxy only, apps must point at it",
                )
            )
        return checks

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
        text.append(f"{descriptor.label}\n", style=f"bold {constants.ACCENT}")
        text.append_text(signal.chain(mode))
        text.append("\n\n")
        text.append(f"{descriptor.summary}\n\n", style=constants.INK_MUTED)

        text.append("On this machine\n", style=f"bold {constants.INK_TEXT}")
        for ok, line in self._readiness(mode):
            glyph, colour = (
                ("✓", constants.SIGNAL_OK)
                if ok
                else ("!", constants.SIGNAL_WARN)
                if ok is None
                else ("✗", constants.SIGNAL_ERR)
            )
            text.append(f"  {glyph} ", style=f"bold {colour}")
            text.append(f"{line}\n", style=constants.INK_TEXT)

        text.append("\nWhat C will do\n", style=f"bold {constants.INK_TEXT}")
        for index, step in enumerate(descriptor.sequence, start=1):
            text.append(f"  {index}  ", style=constants.INK_FAINT)
            text.append(f"{step}\n", style=constants.INK_TEXT)

        if descriptor.notes:
            text.append("\nNotes\n", style=f"bold {constants.INK_TEXT}")
            for note in descriptor.notes:
                text.append(f"  ∙ {note}\n", style=constants.INK_MUTED)

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


def _verdict(checks: list[tuple[bool | None, str]]) -> str:
    if any(ok is False for ok, _ in checks):
        return "blocked"
    if any(ok is None for ok, _ in checks):
        return "partial"
    return "ready"
