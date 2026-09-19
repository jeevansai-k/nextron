"""The About screen -- developer information, as required by the specification."""

from __future__ import annotations

import webbrowser

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from nextron.core import constants
from nextron.storage import paths
from nextron.tui.screens.base import NextronScreen
from nextron.tui.widgets.banner import BannerWidget

__all__ = ["AboutScreen"]


class AboutScreen(NextronScreen):
    """Version, developer, GitHub, contact and licence."""

    subtitle = "About"

    BINDINGS = [
        Binding("g", "open_github", "Open GitHub"),
        Binding("e", "copy_email", "Copy email"),
        Binding("escape", "close", "Return"),
    ]

    def compose_body(self) -> ComposeResult:
        with VerticalScroll(classes="screen-body") as body:
            body.border_title = f"About {constants.APP_NAME}"
            yield BannerWidget(max_rows=14, id="about-banner")
            with Vertical(id="about-card"):
                yield Static("", id="about-details")
                yield Static("", id="about-paths")

    def on_mount(self) -> None:
        super().on_mount()
        self._render_card()

    def _render_card(self) -> None:
        details = Text()
        rows = (
            ("Version", constants.VERSION),
            ("Developer", constants.DEVELOPER),
            ("GitHub", constants.GITHUB_URL.replace("https://", "")),
            ("Email", constants.EMAIL),
            ("License", constants.LICENSE),
            ("Tagline", constants.TAGLINE),
        )
        for label, value in rows:
            details.append(f"{label:<13}: ", style=constants.COLOR_SURFACE)
            details.append(f"{value}\n", style=constants.COLOR_TEXT)

        details.append("\n[G] ", style=f"bold {constants.COLOR_ACCENT}")
        details.append("Open GitHub in the system browser", style=constants.COLOR_TEXT)
        details.append("\n[E] ", style=f"bold {constants.COLOR_ACCENT}")
        details.append("Copy the email address", style=constants.COLOR_TEXT)
        details.append("\nEsc ", style=f"bold {constants.COLOR_ACCENT}")
        details.append("Return", style=constants.COLOR_TEXT)
        self.query_one("#about-details", Static).update(details)

        stats = Text()
        stats.append("\nLocal storage (nothing leaves this device)\n",
                     style=f"bold {constants.COLOR_TEXT}")
        for label, path in (
            ("config", paths.config_file()),
            ("profiles", paths.profiles_dir()),
            ("blocklists", paths.dns_dir()),
            ("database", paths.database_file()),
            ("logs", paths.logs_dir()),
            ("whitelist", paths.whitelist_file()),
        ):
            stats.append(f"  {label:<11}: ", style=constants.COLOR_SURFACE)
            stats.append(f"{path}\n", style=constants.COLOR_TEXT)
        self.query_one("#about-paths", Static).update(stats)

    # -- actions ------------------------------------------------------------ #

    def action_open_github(self) -> None:
        """Open the repository -- only ever on an explicit key press."""
        try:
            opened = webbrowser.open(constants.GITHUB_URL, new=2)
        except Exception:  # pragma: no cover - headless systems
            opened = False
        if opened:
            self.app.notify(f"Opened {constants.GITHUB_URL}", title="About")
        else:
            self.app.notify(
                f"No browser available. The URL is {constants.GITHUB_URL}",
                severity="warning",
                title="About",
            )

    def action_copy_email(self) -> None:
        try:
            self.app.copy_to_clipboard(constants.EMAIL)
            self.app.notify(f"Copied {constants.EMAIL}", title="About")
        except Exception:  # pragma: no cover
            self.app.notify(
                f"Clipboard unavailable. The address is {constants.EMAIL}",
                severity="warning",
                title="About",
            )
