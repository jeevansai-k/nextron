"""Shared screen scaffolding: the header bar and a common screen base."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Static

from nextron.core import constants
from nextron.state.manager import AppState
from nextron.tui.widgets.pulse import marker_for, pulse_style

__all__ = ["HeaderBar", "NextronScreen"]


class HeaderBar(Static):
    """The application header: name, routing chain and live status."""

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
        line = Text(no_wrap=True, overflow="ellipsis")
        line.append(f"{constants.APP_NAME} ", style=f"bold {constants.COLOR_TEXT}")
        line.append(f"v{constants.VERSION}", style=constants.COLOR_SURFACE)

        if state is not None:
            line.append("   ", style="")
            line.append(state.routing_mode.label, style=f"bold {constants.COLOR_TEXT}")
            line.append(f"  {state.routing_mode.chain}", style=constants.COLOR_SURFACE)
            line.append("   ", style="")
            line.append(
                marker_for(state.route_status, self._frame) + " ",
                style=f"bold {pulse_style(state.route_status, self._frame)}",
            )
            line.append(
                state.route_status.label, style=f"bold {constants.COLOR_TEXT}"
            )
            if state.public_ip:
                line.append(f"   {state.public_ip}", style=constants.COLOR_SURFACE)
                if state.exit_country:
                    line.append(f" ({state.exit_country})", style=constants.COLOR_SURFACE)
        if subtitle:
            line.append(f"   {subtitle}", style=constants.COLOR_SURFACE)
        self.update(line)


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
