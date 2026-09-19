"""The NEXTRON Textual application.

Keyboard-first, zero mouse required, zero GUI. The app owns the
:class:`~nextron.core.runtime.NextronRuntime` and translates key presses into
runtime calls; every long operation runs in a Textual worker so the interface
never blocks.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from textual.app import App
from textual.binding import Binding

from nextron.core import constants
from nextron.core.config import RoutingMode
from nextron.core.exceptions import NextronError
from nextron.core.runtime import NextronRuntime
from nextron.tui.screens.dashboard import DashboardScreen
from nextron.tui.screens.modals import ConfirmScreen, ReportScreen
from nextron.tui.screens.splash import SplashScreen
from nextron.tui.themes.palette import NEXTRON_THEME

log = logging.getLogger(__name__)

__all__ = ["NextronApp", "run_app", "run_app_async"]


class NextronApp(App[int]):
    """The terminal-native NEXTRON interface."""

    TITLE = constants.APP_NAME
    SUB_TITLE = constants.TAGLINE
    CSS_PATH = Path(__file__).parent / "themes" / "nextron.tcss"

    BINDINGS = [
        Binding("ctrl+q", "request_quit", "Quit", priority=True),
        Binding("ctrl+c", "request_quit", "Quit", show=False, priority=True),
    ]

    def __init__(
        self,
        runtime: NextronRuntime | None = None,
        *,
        auto_connect: bool = False,
        mode: RoutingMode | None = None,
    ) -> None:
        super().__init__()
        # The stylesheet is parsed during compose, which happens before
        # on_mount, so the theme has to be registered here.
        self.register_theme(NEXTRON_THEME)
        self.theme = "nextron"
        self.runtime = runtime or NextronRuntime()
        self._auto_connect = auto_connect
        self._startup_mode = mode
        self._quitting = False

    # -- lifecycle ---------------------------------------------------------- #

    def on_mount(self) -> None:
        interface = self.runtime.config.interface
        splash = SplashScreen(
            prefer_png=interface.show_png_banner,
            seconds=interface.splash_seconds,
        )
        self.push_screen(splash, self._after_splash)
        self.run_worker(self._boot(), exclusive=False)

    async def _boot(self) -> None:
        """Start the runtime while the splash banner is on screen."""
        try:
            await self.runtime.start()
        except NextronError as exc:
            log.error("Startup failed: %s", exc)
            self.notify(str(exc), severity="error", title="Startup")

    def _after_splash(self, _result: Any = None) -> None:
        """Swap the splash for the dashboard."""
        self.push_screen(DashboardScreen())
        if self._auto_connect:
            self.establish_mode(self._startup_mode or self.runtime.config.routing_mode)

    # -- worker helper ------------------------------------------------------ #

    def run_action_task(
        self,
        coroutine: Awaitable[Any],
        description: str = "",
        *,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        """Run *coroutine* in a worker, reporting failures as notifications."""

        async def runner() -> None:
            try:
                await coroutine
            except NextronError as exc:
                log.error("%s failed: %s", description or "Action", exc)
                self.notify(str(exc), severity="error", title=description or "Error")
            except Exception as exc:  # pragma: no cover - unexpected
                log.exception("%s crashed", description or "Action")
                self.notify(f"Unexpected error: {exc}", severity="error")
            finally:
                if on_done is not None:
                    try:
                        on_done()
                    except Exception:  # pragma: no cover
                        log.debug("Post-action refresh failed", exc_info=True)

        if description:
            self.notify(f"{description}...", timeout=3)
        self.run_worker(runner(), exclusive=False)

    # -- routing ------------------------------------------------------------ #

    def establish_mode(self, mode: RoutingMode) -> None:
        """Bring a routing mode up and show the verification report."""

        async def runner() -> None:
            try:
                report = await self.runtime.connect(mode)
            except NextronError as exc:
                self.notify(str(exc), severity="error", title=f"{mode.label} failed")
                return
            if report.passed:
                # A successful connection must land on the dashboard. Popping a
                # modal over it makes every key look dead until it is
                # dismissed, so warnings are announced and left for "I".
                message = f"{mode.label} active -- {report.summary}"
                if report.warnings:
                    message += "   (press I for the full report)"
                self.notify(
                    message,
                    title="Connected",
                    severity="warning" if report.warnings else "information",
                    timeout=8 if report.warnings else 5,
                )
                self._warn_if_socks_only(mode)
                return

            self.notify(
                f"{mode.label} active but unverified -- {report.summary}",
                title="Verification failed",
                severity="error",
            )
            self.push_screen(ReportScreen(report))

        self.notify(f"Establishing {mode.label}...", timeout=4)
        self.run_worker(runner(), exclusive=True, group="routing")

    def _warn_if_socks_only(self, mode: RoutingMode) -> None:
        """Nothing is routed system-wide in SOCKS mode -- say it outright.

        Without this the dashboard reads "Connected" while the browser still
        uses the direct connection, which looks exactly like a broken tool.
        """
        if not mode.uses_tor or self.runtime.routing.transparent.active:
            return
        port = self.runtime.tor.socks_port
        self.notify(
            f"Your browser is NOT routed yet. Only apps pointed at the proxy "
            f"use Tor:\n\n    SOCKS5 host 127.0.0.1   port {port}\n\n"
            "Set that in the browser's proxy settings (enable 'proxy DNS'), or "
            "run NEXTRON with sudo for system-wide routing.",
            title="SOCKS mode -- action needed",
            severity="warning",
            timeout=20,
        )

    def toggle_route(self) -> None:
        """Connect the selected mode, or disconnect the live one.

        Only the selected mode's services are touched: VPN Only never starts
        Tor, Tor Only never touches a tunnel.
        """
        if self.runtime.routing.active:
            live = self.runtime.routing.mode
            self.run_action_task(
                self.runtime.disconnect(),
                f"Disconnecting {live.label}" if live else "Disconnecting",
            )
        else:
            self.establish_mode(self.runtime.config.routing_mode)

    # -- quitting ----------------------------------------------------------- #

    def action_request_quit(self) -> None:
        """Confirm, tear the session down cleanly, then exit."""
        if self._quitting:
            return

        if not self.runtime.config.interface.confirm_quit:
            self._begin_shutdown()
            return

        note = (
            "This will tear down the active route, release the kill switch and "
            "restore system DNS."
            if self.runtime.routing.active
            else "No route is active."
        )

        def handle(confirmed: bool | None) -> None:
            if confirmed:
                self._begin_shutdown()

        self.push_screen(
            ConfirmScreen(
                f"Quit {constants.APP_NAME}?\n{note}",
                title="Quit",
                confirm_label="Quit",
                cancel_label="Stay",
            ),
            handle,
        )

    def _begin_shutdown(self) -> None:
        self._quitting = True
        self.notify("Shutting down...", timeout=3)

        async def runner() -> None:
            try:
                await self.runtime.shutdown()
            finally:
                self.exit(0)

        self.run_worker(runner(), exclusive=True, group="shutdown")


async def run_app_async(
    *, auto_connect: bool = False, mode: RoutingMode | None = None
) -> int:
    """Run the interface, guaranteeing a clean teardown on every exit path.

    The runtime is shut down in a ``finally`` on the *same* event loop that
    created the child processes, so a Ctrl+C, an unhandled error and an orderly
    quit all release the kill switch, restore ``/etc/resolv.conf`` and stop the
    Tor and VPN processes. ``shutdown()`` is idempotent, so the interactive
    quit path calling it first is harmless.
    """
    app = NextronApp(auto_connect=auto_connect, mode=mode)
    try:
        result = await app.run_async()
    finally:
        await app.runtime.shutdown()
    return int(result or 0)


def run_app(
    *, auto_connect: bool = False, mode: RoutingMode | None = None
) -> int:
    """Entry point used by the CLI."""
    try:
        return asyncio.run(run_app_async(auto_connect=auto_connect, mode=mode))
    except KeyboardInterrupt:  # pragma: no cover - user pressed Ctrl+C
        return 130
