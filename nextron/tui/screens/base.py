"""Shared screen scaffolding: the header bar and a common screen base."""

from __future__ import annotations

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Static

from nextron.core import constants
from nextron.state.manager import AppState, ServiceStatus
from nextron.tui.widgets import signal
from nextron.tui.widgets.pulse import marker_for

__all__ = ["HeaderBar", "NextronScreen", "socks_only"]


class HeaderBar(Static):
    """One line: who, which mode, and -- above all -- protected or exposed."""

    def __init__(self, **kwargs) -> None:
        super().__init__("", id="app-header", **kwargs)
        self._frame = 0

    def on_mount(self) -> None:
        self.render_state(None)

    def set_frame(self, frame: int) -> None:
        """Advance the connection animation shown beside the status."""
        self._frame = frame

    def render_state(self, state: AppState | None, *, subtitle: str = "") -> None:
        """Redraw the header from a state snapshot."""
        left = Text(no_wrap=True, overflow="ellipsis")
        left.append("⬥ ", style=f"bold {constants.ACCENT}")
        left.append(constants.APP_NAME, style=f"bold {constants.INK_TEXT}")

        if subtitle:
            left.append("  ›  ", style=constants.INK_FAINT)
            left.append(subtitle, style=f"bold {constants.ACCENT}")

        if state is not None:
            left.append("   ⏐  ", style=constants.INK_LINE)
            left.append(state.routing_mode.label, style=f"bold {constants.INK_TEXT}")
            left.append("  ")
            left.append_text(signal.chain(state.routing_mode, state))
            left.append("   ⏐  ", style=constants.INK_LINE)

            glyph, word, colour = signal.protection(
                state, socks_only=socks_only(self.app, state)
            )
            if word in {"CONNECTING", "DISCONNECTING"}:
                glyph = marker_for(state.route_status, self._frame)
            left.append(f"{glyph} {word}", style=f"bold {colour}")
            if state.public_ip and state.route_status is ServiceStatus.ACTIVE:
                left.append(f"  {state.public_ip}", style=constants.INK_TEXT)
                if state.exit_country:
                    left.append(
                        f" ∙ {state.exit_country}", style=constants.INK_MUTED
                    )
            elif word == "EXPOSED":
                left.append("  direct connection", style=constants.INK_MUTED)

        right = Text(f"v{constants.VERSION}", style=constants.INK_FAINT, no_wrap=True)
        grid = Table.grid(expand=True)
        grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        grid.add_column(justify="right", no_wrap=True)
        grid.add_row(left, right)
        self.update(grid)


def socks_only(app, state: AppState) -> bool:
    """Tor is carrying traffic, but only for apps that use its SOCKS port."""
    runtime = getattr(app, "runtime", None)
    if runtime is None or not state.routing_mode.uses_tor:
        return False
    if state.routing_mode.uses_vpn and state.vpn.status is ServiceStatus.ACTIVE:
        # Tor over VPN / VPN over Tor: the VPN tunnel is already system-wide.
        return False
    return not runtime.routing.transparent.active


class NextronScreen(Screen):
    """Base for every secondary screen: header, footer and an Esc binding."""

    BINDINGS = [
        Binding("escape", "close", "Return", show=True),
        Binding("q", "close", "Return", show=False),
    ]

    #: Shown after the version in the header.
    subtitle: str = ""

    def compose(self) -> ComposeResult:
        yield HeaderBar()
        yield from self.compose_body()
        yield Footer()

    def compose_body(self) -> ComposeResult:  # pragma: no cover - overridden
        return iter(())

    def on_mount(self) -> None:
        self.refresh_header()

    def refresh_header(self) -> None:
        try:
            header = self.query_one(HeaderBar)
        except Exception:
            return
        state = getattr(self.app, "runtime", None)
        header.render_state(
            state.state if state is not None else None, subtitle=self.subtitle
        )

    def action_close(self) -> None:
        self.app.pop_screen()
