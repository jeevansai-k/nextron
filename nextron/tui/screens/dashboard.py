"""The dashboard -- the live view of the whole session.

Everything the specification asks for is on this screen: the routing mode, the
Tor / VPN / DNS Shield status, public and VPN addresses, exit country, circuit
id, both independent countdowns, session uptime and the activity log.
"""

from __future__ import annotations

from datetime import datetime

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer

from nextron.core import constants
from nextron.state.manager import AppState, ServiceStatus
from nextron.tui.layouts.dashboard import PANELS, Column, panels_in
from nextron.tui.screens.base import HeaderBar, NextronScreen, socks_only
from nextron.tui.widgets import pulse, signal
from nextron.tui.widgets.activity import ActivityLog
from nextron.tui.widgets.menu import MenuEntry, NavMenu
from nextron.tui.widgets.panels import CountdownPanel, InfoPanel, SchedulerLine
from nextron.tui.widgets.pulse import marker_for, pulse_style
from nextron.utils import format as fmt
from nextron.utils import intervals

__all__ = ["DashboardScreen"]

MENU_ENTRIES = [
    MenuEntry("C", "Connect", "toggle_route", "⌁", "Session"),
    MenuEntry("M", "Routing mode", "mode_select", "⇄", "Session"),
    MenuEntry("R", "Tor rotation", "toggle_rotation", "↻", "Session"),
    MenuEntry("F", "VPN shuffle", "toggle_shuffle", "⇆", "Session"),
    MenuEntry("V", "VPN library", "vpn_library", "❏", "Libraries"),
    MenuEntry("P", "Rotation pool", "profiles", "⇅", "Libraries"),
    MenuEntry("D", "DNS Shield", "dns_shield", "✜", "Libraries"),
    MenuEntry("X", "Doctor", "doctor", "✚", "System"),
    MenuEntry("L", "Logs", "logs", "⌸", "System"),
    MenuEntry("S", "Settings", "settings", "⌥", "System"),
    MenuEntry("A", "About", "about", "⍰", "System"),
]


def _reach(state: AppState, transparent, socks_port: int) -> Text:
    """Say plainly whether the whole system is routed, or only SOCKS clients."""
    if not state.routing_mode.uses_tor:
        return Text("system-wide (tunnel)", style=constants.INK_TEXT)
    if transparent.active:
        return Text(transparent.label, style=constants.INK_TEXT)

    text = Text(no_wrap=True)
    text.append("SOCKS only ", style=f"bold {constants.SIGNAL_WARN}")
    text.append("⟶ ", style=constants.INK_FAINT)
    text.append(f"127.0.0.1:{socks_port}", style=f"bold {constants.ACCENT}")
    text.append("  point apps here", style=constants.INK_FAINT)
    return text


def _dash(value: object | None) -> str:
    return "--" if value in (None, "") else str(value)


def _engine_uptime(started_at: datetime | None) -> str:
    """Render how long an engine has been up."""
    if started_at is None:
        return "--"
    return fmt.duration((datetime.now() - started_at).total_seconds())


class DashboardScreen(NextronScreen):
    """The primary screen; every engine action is one keystroke away."""

    BINDINGS = [
        Binding("space", "rotate_now", "Rotate Tor"),
        Binding("r", "toggle_rotation", "Tor rotation"),
        Binding("f", "toggle_shuffle", "VPN shuffle", show=False),
        Binding("c", "toggle_route", "Connect"),
        Binding("m", "mode_select", "Mode"),
        Binding("v", "vpn_library", "VPN"),
        Binding("p", "profiles", "Pool"),
        Binding("d", "dns_shield", "DNS"),
        Binding("x", "doctor", "Doctor", show=False),
        Binding("l", "logs", "Logs"),
        Binding("s", "settings", "Settings"),
        Binding("a", "about", "About"),
        Binding("left_square_bracket", "tor_interval_down", "Tor −", show=False),
        Binding("right_square_bracket", "tor_interval_up", "Tor +", show=False),
        Binding("comma", "vpn_interval_down", "VPN −", show=False),
        Binding("full_stop", "vpn_interval_up", "VPN +", show=False),
        Binding("i", "show_report", "Report", show=False),
        Binding("left", "focus_previous_panel", "Prev panel", show=False),
        Binding("right", "focus_next_panel", "Next panel", show=False),
        Binding("q", "request_quit", "Quit"),
        # The dashboard is the root screen: Esc has nothing to return to.
        Binding("escape", "close", "", show=False),
    ]

    #: Frame counter for the connection animation.
    _frame = 0

    def compose(self) -> ComposeResult:
        yield HeaderBar()
        with Horizontal(id="dashboard-body"):
            with Vertical(id=Column.LEFT.value):
                yield NavMenu(MENU_ENTRIES, id="nav-menu")
                yield from self._compose_column(Column.LEFT)
            with VerticalScroll(id=Column.MIDDLE.value):
                yield from self._compose_column(Column.MIDDLE)
            with Vertical(id=Column.RIGHT.value):
                yield from self._compose_column(Column.RIGHT)
        yield Footer()

    def _compose_column(self, column: Column) -> ComposeResult:
        """Build every panel assigned to *column*, in registry order.

        Consecutive panels that share a ``group`` are laid out side by side.
        """
        specs = panels_in(column)
        index = 0
        while index < len(specs):
            panel = specs[index]
            if panel.group is None:
                yield self._build_panel(panel)
                index += 1
                continue
            members = []
            while index < len(specs) and specs[index].group == panel.group:
                members.append(specs[index])
                index += 1
            with Horizontal(id=panel.group):
                for member in members:
                    yield self._build_panel(member)

    def _build_panel(self, panel):
        runtime = self.app.runtime
        if panel.key == "countdown":
            return CountdownPanel(id=panel.id)
        if panel.key == "activity":
            return ActivityLog(
                runtime.bus,
                lines=runtime.config.interface.activity_log_lines,
                id=panel.id,
            )
        return InfoPanel(
            panel.title,
            panel_id=panel.id,
            classes="-fills-column" if panel.fills else "",
        )

    # -- lifecycle ---------------------------------------------------------- #

    def on_mount(self) -> None:
        runtime = self.app.runtime
        runtime.state_manager.watch(self._on_state)
        self.set_interval(1.0, self._tick)
        self.set_interval(pulse.FRAME_SECONDS, self._advance_animation)
        self.refresh_all(runtime.state)
        self.query_one(NavMenu).focus()

    def on_unmount(self) -> None:
        self.app.runtime.state_manager.unwatch(self._on_state)

    def _on_state(self, state: AppState) -> None:
        """State watcher -- called from engine code, so stay cheap and safe."""
        if self.is_mounted:
            self.refresh_all(state)

    def _tick(self) -> None:
        """Once-a-second refresh so uptime and countdowns keep moving."""
        if self.is_mounted:
            self.refresh_all(self.app.runtime.state)

    def _advance_animation(self) -> None:
        """Drive the connection animation, and only while there is one.

        Nothing repaints while every engine is settled, so an idle dashboard
        costs exactly what it did before.
        """
        if not self.is_mounted:
            return
        state = self.app.runtime.state
        if not any(
            pulse.animated(status)
            for status in (state.route_status, state.tor.status, state.vpn.status)
        ):
            return
        self._frame += 1
        self.refresh_all(state)

    # -- rendering ---------------------------------------------------------- #

    def refresh_all(self, state: AppState) -> None:
        """Redraw the header and every registered panel from one snapshot."""
        try:
            header = self.query_one(HeaderBar)
            header.set_frame(self._frame)
            header.render_state(state)
            self._render_menu(state)
            for panel in PANELS:
                getattr(self, panel.renderer)(state)
        except Exception:
            # The screen may be mid-teardown; never let a redraw crash the app.
            return

    def _status_text(self, status: ServiceStatus, extra: str = "") -> Text:
        text = Text(no_wrap=True)
        text.append(
            marker_for(status, self._frame) + " ",
            style=f"bold {pulse_style(status, self._frame)}",
        )
        text.append(status.label, style=f"bold {status.color}")
        if extra:
            text.append(f"  {extra}", style=constants.INK_MUTED)
        return text

    def _render_activity(self, state: AppState) -> None:
        """The activity log is event-driven; it needs no per-tick redraw."""

    def _render_menu(self, state: AppState) -> None:
        """Show each menu entry's live value on the right of its row."""
        runtime = self.app.runtime
        pool = runtime.vpn.shuffle.pool_size
        route = state.route_status
        route_value = {
            ServiceStatus.ACTIVE: ("connected", constants.SIGNAL_OK),
            ServiceStatus.ERROR: ("failed", constants.SIGNAL_ERR),
            ServiceStatus.IDLE: ("idle", constants.INK_FAINT),
        }.get(route, (route.label.lower(), constants.SIGNAL_BUSY))

        def switch(enabled: bool, interval: int) -> tuple[str, str]:
            if enabled:
                return intervals.label(interval), constants.SIGNAL_OK
            return "off", constants.INK_FAINT

        dns_on = state.dns.status is ServiceStatus.ACTIVE
        self.query_one(NavMenu).set_states(
            {
                "toggle_route": route_value,
                "mode_select": (state.routing_mode.label, constants.INK_MUTED),
                "toggle_rotation": switch(
                    state.tor.rotation_enabled, state.tor.rotation_interval
                ),
                "toggle_shuffle": switch(
                    state.vpn.shuffle_enabled, state.vpn.shuffle_interval
                ),
                "vpn_library": (
                    f"{len(runtime.vpn.library)} profiles",
                    constants.INK_MUTED,
                ),
                "profiles": (f"{pool} in pool", constants.INK_MUTED),
                "dns_shield": (
                    (f"{state.dns.blocked_domains:,} blocked", constants.SIGNAL_OK)
                    if dns_on
                    else ("disabled", constants.INK_FAINT)
                ),
                "doctor": "",
                "logs": "",
                "settings": "",
                "about": (constants.VERSION, constants.INK_FAINT),
            }
        )

    # -- the route hero ------------------------------------------------------ #

    def _render_routing(self, state: AppState) -> None:
        panel = self.query_one("#panel-routing", InfoPanel)
        runtime = self.app.runtime
        mode = state.routing_mode
        status = state.route_status

        lead = Text(no_wrap=True, overflow="ellipsis")
        socks = socks_only(self.app, state)
        glyph, word, colour = signal.protection(state, socks_only=socks)
        if status.is_busy:
            glyph = marker_for(status, self._frame)
        lead.append(f"{glyph} {word}", style=f"bold {colour}")
        lead.append("   ")
        lead.append(mode.label, style=f"bold {constants.INK_TEXT}")
        lead.append("   ")
        lead.append_text(signal.chain(mode, state))

        if status is ServiceStatus.IDLE and not state.last_error:
            # The empty state: say what is going on and what to press.
            message = lead.copy()
            message.append("\n\n")
            message.append(
                "Not connected. Your traffic is using the direct connection.",
                style=constants.INK_MUTED,
            )
            hint = signal.key_hint(
                ("C", f"connect {mode.label}"),
                ("M", "change mode"),
                ("X", "check this machine"),
            )
            panel.set_empty(message, hint=hint)
            panel.set_state_class()
            return

        rows: list[tuple[str, str | Text]] = []
        if status.is_busy and mode.uses_tor and state.tor.status in (
            ServiceStatus.STARTING,
            ServiceStatus.BOOTSTRAPPING,
        ):
            progress = signal.bar(state.tor.bootstrap_percent)
            if state.tor.bootstrap_phase:
                progress.append(
                    f"  {fmt.truncate(state.tor.bootstrap_phase, 34)}",
                    style=constants.INK_MUTED,
                )
            rows.append(("Tor bootstrap", progress))

        if status is ServiceStatus.ACTIVE:
            exit_text = Text(
                _dash(state.public_ip), style=f"bold {constants.INK_TEXT}"
            )
            if state.exit_country:
                exit_text.append(f"  {state.exit_country}", style=constants.INK_MUTED)
            rows.append(("Exit", exit_text))

        verified = state.verification_summary or (
            "yes" if state.verification_passed else "not yet"
        )
        rows.append(
            (
                "Verified",
                Text(
                    verified,
                    style=(
                        constants.SIGNAL_WARN
                        if "warning" in verified
                        else constants.SIGNAL_OK
                    )
                    if state.verification_passed
                    else constants.INK_MUTED,
                ),
            )
        )
        transparent = runtime.routing.transparent.status()
        rows.append(("Reach", _reach(state, transparent, runtime.tor.socks_port)))
        rows.append(
            (
                "Privileges",
                Text("root", style=constants.SIGNAL_OK)
                if state.privileged
                else Text(
                    "user  (VPN and system-wide routing need root)",
                    style=constants.INK_MUTED,
                ),
            )
        )

        note: Text | None = None
        if state.last_error:
            note = Text(state.last_error, style=constants.SIGNAL_ERR)
            if status in (ServiceStatus.ERROR, ServiceStatus.IDLE):
                note.append("\n")
                note.append_text(
                    signal.key_hint(
                        ("C", "try again"), ("X", "run the doctor"), ("L", "logs")
                    )
                )
        elif status.is_busy:
            note = signal.key_hint(("C", "cancel"))
        panel.set_rows(rows, note=note, lead=lead)
        panel.set_state_class(
            active=status is ServiceStatus.ACTIVE and colour == constants.SIGNAL_OK,
            error=status is ServiceStatus.ERROR,
            busy=status.is_busy,
            warn=status is ServiceStatus.ACTIVE and colour != constants.SIGNAL_OK,
        )

    def _render_session(self, state: AppState) -> None:
        panel = self.query_one("#panel-session", InfoPanel)
        panel.set_rows(
            [
                ("Uptime", fmt.clock_duration(state.uptime_seconds)),
                ("Public IP", _dash(state.public_ip)),
                ("Exit country", _dash(state.exit_country)),
                ("Rotations", fmt.thousands(state.tor.rotations)),
                ("VPN switches", fmt.thousands(state.vpn.switches)),
                ("DNS blocked", fmt.thousands(state.dns.queries_blocked)),
            ]
        )

    def _render_countdowns(self, state: AppState) -> None:
        panel = self.query_one("#panel-countdown", CountdownPanel)
        runtime = self.app.runtime
        live = runtime.routing.active
        panel.set_lines(
            SchedulerLine(
                "Tor identity",
                clock=fmt.countdown(state.tor.seconds_to_rotation),
                interval=intervals.label(state.tor.rotation_interval),
                detail="R",
                keys="[ ]",
                enabled=state.tor.rotation_enabled,
                running=runtime.tor_scheduler.running,
                unused=live and not state.routing_mode.uses_tor,
            ),
            SchedulerLine(
                "VPN profile",
                clock=fmt.countdown(state.vpn.seconds_to_shuffle),
                interval=intervals.label(state.vpn.shuffle_interval),
                detail="F",
                keys=", .",
                enabled=state.vpn.shuffle_enabled,
                running=runtime.vpn_scheduler.running,
                unused=live and not state.routing_mode.uses_vpn,
            ),
        )

    def _unused(
        self, panel: InfoPanel, state: AppState, engine: str, extra: str = ""
    ) -> None:
        """An engine the current mode does not use: one line, not ten dashes."""
        message = Text(
            f"Not used in {state.routing_mode.label}.", style=constants.INK_FAINT
        )
        if extra:
            message.append(f"\n{extra}", style=constants.INK_MUTED)
        hint = signal.key_hint(("M", f"pick a mode with {engine}"))
        panel.set_empty(message, hint=hint)
        panel.set_state_class(unused=True)

    def _render_tor(self, state: AppState) -> None:
        panel = self.query_one("#panel-tor", InfoPanel)
        tor = state.tor
        if not state.routing_mode.uses_tor and tor.status in (
            ServiceStatus.IDLE,
            ServiceStatus.DISABLED,
        ):
            self._unused(panel, state, "Tor")
            return

        rows: list[tuple[str, str | Text]] = [
            ("Status", self._status_text(tor.status, tor.version or "")),
        ]
        if tor.status in (ServiceStatus.STARTING, ServiceStatus.BOOTSTRAPPING):
            rows.append(("Bootstrap", signal.bar(tor.bootstrap_percent, width=16)))
        rows += [
            ("Exit IP", _dash(tor.exit_ip)),
            ("Exit country", _dash(tor.exit_country)),
            ("Circuit", _dash(tor.circuit_id)),
            (
                "Path",
                " ⟶ ".join(tor.circuit_path) if tor.circuit_path else "--",
            ),
            ("SOCKS", f"127.0.0.1:{tor.socks_port}"),
            ("Last rotation", fmt.relative(tor.last_rotation)),
            ("Tor uptime", _engine_uptime(tor.started_at)),
        ]
        panel.set_rows(rows, note=tor.error)
        panel.set_state_class(
            active=tor.status is ServiceStatus.ACTIVE,
            error=tor.status is ServiceStatus.ERROR,
            busy=tor.status.is_busy,
        )

    def _render_vpn(self, state: AppState) -> None:
        panel = self.query_one("#panel-vpn", InfoPanel)
        vpn = state.vpn
        if not state.routing_mode.uses_vpn and vpn.status in (
            ServiceStatus.IDLE,
            ServiceStatus.DISABLED,
        ):
            extra = f"Selected profile: {vpn.profile_name}" if vpn.profile_name else ""
            self._unused(panel, state, "a VPN", extra)
            return
        if vpn.profile_name is None and vpn.status is ServiceStatus.IDLE:
            panel.set_empty(
                Text("No VPN profile yet.", style=constants.INK_MUTED),
                hint=signal.key_hint(("V", "import a .ovpn or WireGuard file")),
            )
            panel.set_state_class(unused=True)
            return

        killswitch = (
            Text("◉ armed", style=f"bold {constants.SIGNAL_OK}")
            if vpn.killswitch_active
            else Text("◌ off", style=constants.INK_FAINT)
        )
        rows: list[tuple[str, str | Text]] = [
            ("Status", self._status_text(vpn.status, vpn.protocol or "")),
            ("Profile", _dash(vpn.profile_name)),
            ("Interface", _dash(vpn.interface)),
            ("Tunnel IP", _dash(vpn.tunnel_ip)),
            ("VPN IP", _dash(vpn.public_ip)),
            ("Country", _dash(vpn.country)),
            ("Endpoint", fmt.truncate(vpn.endpoint, 34) if vpn.endpoint else "--"),
            ("Kill switch", killswitch),
            ("Pool", f"{vpn.pool_size} profile(s)"),
            ("Last switch", fmt.relative(vpn.last_switch)),
        ]
        panel.set_rows(rows, note=vpn.error)
        panel.set_state_class(
            active=vpn.status is ServiceStatus.ACTIVE,
            error=vpn.status is ServiceStatus.ERROR,
            busy=vpn.status.is_busy,
        )

    def _render_dns(self, state: AppState) -> None:
        panel = self.query_one("#panel-dns", InfoPanel)
        dns = state.dns
        if dns.status in (ServiceStatus.IDLE, ServiceStatus.DISABLED) and not dns.error:
            runtime = self.app.runtime
            enabled = runtime.config.dns.enabled
            message = Text(no_wrap=False)
            message.append_text(self._status_text(dns.status))
            message.append(
                "   starts with the route"
                if enabled
                else "   switched off in Settings",
                style=constants.INK_MUTED,
            )
            message.append(
                f"\n{fmt.thousands(dns.blocked_domains)} domains on "
                f"{len(dns.active_blocklists)} list(s)",
                style=constants.INK_FAINT,
            )
            panel.set_empty(message, hint=signal.key_hint(("D", "lists and toggle")))
            panel.set_state_class(unused=not enabled)
            return

        rows = [
            ("Status", self._status_text(dns.status)),
            ("Listening", _dash(dns.listen)),
            (
                "Upstream",
                ("Tor DNSPort " if dns.using_tor_dns else "")
                + (", ".join(dns.upstream) if dns.upstream else "--"),
            ),
            ("Blocked", f"{fmt.thousands(dns.blocked_domains)} domains"),
            ("Whitelist", fmt.thousands(dns.whitelisted_domains)),
            ("Lists", f"{len(dns.active_blocklists)} active"),
            (
                "Queries",
                f"{fmt.thousands(dns.queries_total)} "
                f"({fmt.thousands(dns.queries_blocked)} blocked, "
                f"{fmt.percentage(dns.block_rate)})",
            ),
            ("System DNS", "redirected" if dns.resolv_conf_managed else "unchanged"),
        ]
        panel.set_rows(rows, note=dns.error)
        panel.set_state_class(
            active=dns.status is ServiceStatus.ACTIVE,
            error=dns.status is ServiceStatus.ERROR,
            busy=dns.status.is_busy,
        )

    # -- panel navigation --------------------------------------------------- #

    def action_focus_next_panel(self) -> None:
        self.focus_next()

    def action_focus_previous_panel(self) -> None:
        self.focus_previous()

    # -- menu --------------------------------------------------------------- #

    def on_nav_menu_activated(self, message: NavMenu.Activated) -> None:
        """Run a menu entry, whether it was clicked or reached with Enter.

        The same ``action_*`` method the entry's key binding runs, so clicking
        and typing the letter are indistinguishable.
        """
        message.stop()
        action = getattr(self, f"action_{message.entry.action}", None)
        if action is None:
            action = getattr(self.app, f"action_{message.entry.action}", None)
        if action is None:
            self.app.notify(
                f"'{message.entry.label}' has no action bound to it.",
                severity="error",
            )
            return
        action()

    # -- actions ------------------------------------------------------------ #

    def action_rotate_now(self) -> None:
        self.app.run_action_task(
            self.app.runtime.rotate_now(), "Rotating Tor identity"
        )

    def action_toggle_rotation(self) -> None:
        self.app.run_action_task(
            self.app.runtime.toggle_tor_rotation(), "Toggling Tor rotation"
        )

    def action_toggle_shuffle(self) -> None:
        self.app.run_action_task(
            self.app.runtime.toggle_vpn_shuffle(), "Toggling VPN shuffle"
        )

    def action_tor_interval_up(self) -> None:
        self._step_tor_interval(+1)

    def action_tor_interval_down(self) -> None:
        self._step_tor_interval(-1)

    def _step_tor_interval(self, direction: int) -> None:
        runtime = self.app.runtime
        current = runtime.config.tor.rotation_interval
        applied = runtime.set_tor_interval(intervals.step(current, direction))
        if applied != current:
            self.app.notify(
                f"Tor rotation every {intervals.label(applied)}", title="Interval"
            )
        self.refresh_all(runtime.state)

    def action_vpn_interval_up(self) -> None:
        self._step_vpn_interval(+1)

    def action_vpn_interval_down(self) -> None:
        self._step_vpn_interval(-1)

    def _step_vpn_interval(self, direction: int) -> None:
        runtime = self.app.runtime
        current = runtime.config.vpn.shuffle_interval
        applied = runtime.set_vpn_interval(intervals.step(current, direction))
        if applied != current:
            self.app.notify(
                f"VPN shuffle every {intervals.label(applied)}", title="Interval"
            )
        self.refresh_all(runtime.state)

    def action_toggle_route(self) -> None:
        self.app.toggle_route()

    def action_mode_select(self) -> None:
        from nextron.tui.screens.mode_select import ModeSelectScreen

        self.app.push_screen(ModeSelectScreen())

    def action_vpn_library(self) -> None:
        from nextron.tui.screens.vpn_library import VPNLibraryScreen

        self.app.push_screen(VPNLibraryScreen())

    def action_profiles(self) -> None:
        from nextron.tui.screens.profiles import RotationPoolScreen

        self.app.push_screen(RotationPoolScreen())

    def action_dns_shield(self) -> None:
        from nextron.tui.screens.dns_shield import DNSShieldScreen

        self.app.push_screen(DNSShieldScreen())

    def action_doctor(self) -> None:
        from nextron.tui.screens.doctor import DoctorScreen

        self.app.push_screen(DoctorScreen())

    def action_logs(self) -> None:
        from nextron.tui.screens.logs import LogsScreen

        self.app.push_screen(LogsScreen())

    def action_settings(self) -> None:
        from nextron.tui.screens.settings import SettingsScreen

        self.app.push_screen(SettingsScreen())

    def action_about(self) -> None:
        from nextron.tui.screens.about import AboutScreen

        self.app.push_screen(AboutScreen())

    def action_show_report(self) -> None:
        report = self.app.runtime.routing.last_report
        if report is None:
            self.app.notify("No verification has run yet.", severity="warning")
            return
        from nextron.tui.screens.modals import ReportScreen

        self.app.push_screen(ReportScreen(report))

    def action_request_quit(self) -> None:
        self.app.action_request_quit()

    def action_close(self) -> None:
        """The dashboard is the root screen: Esc must not pop it."""
        return
