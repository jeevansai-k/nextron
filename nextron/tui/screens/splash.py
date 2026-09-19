"""The startup splash: the official banner, shown immediately after launch."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Static

from nextron.core import constants
from nextron.tui.widgets.banner import BannerWidget

__all__ = ["SplashScreen"]


class SplashScreen(Screen[None]):
    """Displays the PNG banner (ASCII on fallback) while the runtime starts."""

    BINDINGS = [
        ("escape", "skip", "Skip"),
        ("enter", "skip", "Skip"),
        ("space", "skip", "Skip"),
    ]

    def __init__(self, *, prefer_png: bool = True, seconds: float = 2.0) -> None:
        super().__init__()
        self._prefer_png = prefer_png
        self._seconds = seconds

    def compose(self) -> ComposeResult:
        with Vertical(id="splash-body"):
            yield BannerWidget(
                prefer_png=self._prefer_png, max_rows=18, id="splash-banner"
            )
            yield Static(
                Text(constants.TAGLINE, style=f"italic {constants.COLOR_SURFACE}"),
                id="splash-tagline",
            )
            yield Static("", id="splash-status")

    def on_mount(self) -> None:
        banner = self.query_one(BannerWidget)
        status = self.query_one("#splash-status", Static)
        status.update(
            Text(
                f"Initialising engines... ({banner.kind} banner)",
                style=constants.COLOR_ACCENT,
            )
        )
        if self._seconds > 0:
            self.set_timer(self._seconds, self.action_skip)

    def set_status(self, message: str) -> None:
        try:
            self.query_one("#splash-status", Static).update(
                Text(message, style=constants.COLOR_ACCENT)
            )
        except Exception:  # pragma: no cover - screen may already be gone
            pass

    def action_skip(self) -> None:
        if self.is_current:
            self.dismiss(None)
