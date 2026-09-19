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
from nextron.tui.screens.base import HeaderBar, NextronScreen
from nextron.tui.widgets import pulse
from nextron.tui.widgets.activity import ActivityLog
from nextron.tui.widgets.menu import MenuEntry, NavMenu
from nextron.tui.widgets.panels import CountdownPanel, InfoPanel
from nextron.tui.widgets.pulse import marker_for, pulse_style
from nextron.utils import format as fmt
from nextron.utils import intervals

__all__ = ["DashboardScreen"]

MENU_ENTRIES = [
    MenuEntry("C", "Connect / Disconnect", "toggle_route", "⌁", "Session"),
    MenuEntry("M", "Routing Mode", "mode_select", "⇄", "Session"),
    MenuEntry("R", "Tor Rotation", "toggle_rotation", "↻", "Session"),
    MenuEntry("F", "VPN Shuffle", "toggle_shuffle", "⇆", "Session"),
    MenuEntry("V", "VPN Library", "vpn_library", "❏", "Libraries"),
    MenuEntry("P", "Rotation Pool", "profiles", "⇅", "Libraries"),
    MenuEntry("D", "DNS Shield", "dns_shield", "✜", "Libraries"),
    MenuEntry("X", "Doctor", "doctor", "✚", "System"),
    MenuEntry("L", "Logs", "logs", "⌸", "System"),
    MenuEntry("S", "Settings", "settings", "⌥", "System"),
    MenuEntry("A", "About", "about", "⍰", "System"),
]


def _reach(state: AppState, transparent, socks_port: int) -> Text:
    """Say plainly whether the whole system is routed, or only SOCKS clients."""
    if not state.routing_mode.uses_tor:
        return Text("tunnel", style=constants.COLOR_TEXT)
    if transparent.active:
        return Text(transparent.label, style=constants.COLOR_TEXT)

    text = Text(no_wrap=True)
    text.append("SOCKS only -> ", style=constants.COLOR_SURFACE)
    text.append(f"127.0.0.1:{socks_port}", style=f"bold {constants.COLOR_ACCENT}")
    return text


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
            with VerticalScroll(id=Column.LEFT.value):
                yield NavMenu(MENU_ENTRIES, id="nav-menu")
                yield from self._compose_column(Column.LEFT)
            with VerticalScroll(id=Column.MIDDLE.value):
                yield from self._compose_column(Column.MIDDLE)
            with Vertical(id=Column.RIGHT.value):
                yield from self._compose_column(Column.RIGHT)
        yield Footer()

    def _compose_column(self, column: Column) -> ComposeResult:
        """Build every panel assigned to *column*, in registry order.

        The last panel of a column is marked ``-fills-column`` so it takes up
        the leftover height: without it the three columns end on three
        different rows and the borders read as a staircase.
        """
        runtime = self.app.runtime
        specs = panels_in(column)
        for panel in specs:
            last = panel is specs[-1]
            if panel.key == "countdown":
                yield CountdownPanel(id=panel.id)
            elif panel.key == "activity":
                yield ActivityLog(
                    runtime.bus,
                    lines=runtime.config.interface.activity_log_lines,
                    id=panel.id,
                )
            else:
                yield InfoPanel(
                    panel.title,
                    panel_id=panel.id,
                    classes="-fills-column" if last else "",
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
        text.append(status.label, style=f"bold {constants.COLOR_TEXT}")
        if extra:
            text.append(f"  {extra}", style=constants.COLOR_SURFACE)
        return text

    def _render_activity(self, state: AppState) -> None:
        """The activity log is event-driven; it needs no per-tick redraw."""

    def _render_menu(self, state: AppState) -> None:
        """Show each menu entry's live value on the right of its row."""
        runtime = self.app.runtime
        pool = runtime.vpn.shuffle.pool_size
        self.query_one(NavMenu).set_states(
            {
                "toggle_route": state.route_status.label,
                "mode_select": state.routing_mode.label,
                "toggle_rotation": (
                    intervals.label(state.tor.rotation_interval)
                    if state.tor.rotation_enabled
                    else "off"
                ),
                "toggle_shuffle": (
                    intervals.label(state.vpn.shuffle_interval)
                    if state.vpn.shuffle_enabled
                    else "off"
                ),
                "vpn_library": f"{len(runtime.vpn.library)} profiles",
                "profiles": f"{pool} in pool",
                "dns_shield": (
                    f"{state.dns.blocked_domains:,} blocked"
                    if state.dns.status is ServiceStatus.ACTIVE
                    else "disabled"
                ),
                "doctor": "",
                "logs": "",
                "settings": "",
                "about": constants.VERSION,
            }
        )

    def _render_routing(self, state: AppState) -> None:
        panel = self.query_one("#panel-routing", InfoPanel)
        runtime = self.app.runtime
        transparent = runtime.routing.transparent.status()
        rows = [
            (
                "Mode",
                Text(state.routing_mode.label, style=f"bold {constants.COLOR_TEXT}"),
            ),
            ("Chain", state.routing_mode.chain),
            ("Status", self._status_text(state.route_status)),
            (
                "Verified",
                state.verification_summary
                or ("yes" if state.verification_passed else "not yet"),
            ),
            ("Reach", _reach(state, transparent, runtime.tor.socks_port)),
            ("Privileges", "root" if state.privileged else "user (sudo for tunnels)"),
        ]
        panel.set_rows(
            rows,
            note=state.last_error if state.last_error else None,
        )
        panel.set_state_class(
            active=state.route_status is ServiceStatus.ACTIVE,
            error=state.route_status is ServiceStatus.ERROR,
        )

    def _render_session(self, state: AppState) -> None:
        panel = self.query_one("#panel-session", InfoPanel)
        panel.set_rows(
            [
                ("Uptime", fmt.clock_duration(state.uptime_seconds)),
                ("Public IP", state.public_ip or "--"),
                ("Exit country", state.exit_country or "--"),
                ("Rotations", fmt.thousands(state.tor.rotations)),
                ("VPN switches", fmt.thousands(state.vpn.switches)),
                ("DNS blocked", fmt.thousands(state.dns.queries_blocked)),
            ]
        )

    def _render_countdowns(self, state: AppState) -> None:
        panel = self.query_one("#panel-countdown", CountdownPanel)
        panel.set_countdowns(
            tor_clock=fmt.countdown(state.tor.seconds_to_rotation),
            tor_interval=intervals.strip(state.tor.rotation_interval),
            tor_detail="rotating (R)" if state.tor.rotation_enabled else "off (R)",
            vpn_clock=fmt.countdown(state.vpn.seconds_to_shuffle),
            vpn_interval=intervals.strip(state.vpn.shuffle_interval),
            vpn_detail=(
                f"{state.vpn.shuffle_algorithm.label} (F)"
                if state.vpn.shuffle_enabled
                else "off (F)"
            ),
        )

    def _render_tor(self, state: AppState) -> None:
        panel = self.query_one("#panel-tor", InfoPanel)
        tor = state.tor
        bootstrap = (
            f"{tor.bootstrap_percent}%"
            + (f" -- {tor.bootstrap_phase}" if tor.bootstrap_phase else "")
        )
        rows = [
            ("Status", self._status_text(tor.status, tor.version or "")),
            ("Bootstrap", bootstrap),
            ("Exit IP", tor.exit_ip or "--"),
            ("Exit country", tor.exit_country or "--"),
            ("Circuit", tor.circuit_id or "--"),
            (
                "Path",
                fmt.truncate(" -> ".join(tor.circuit_path), 46)
                if tor.circuit_path
                else "--",
            ),
            ("SOCKS", f"127.0.0.1:{tor.socks_port}"),
            ("Last rotation", fmt.relative(tor.last_rotation)),
            ("Tor uptime", _engine_uptime(tor.started_at)),
        ]
        panel.set_rows(rows, note=tor.error)
        panel.set_state_class(
            active=tor.status is ServiceStatus.ACTIVE,
            error=tor.status is ServiceStatus.ERROR,
        )

    def _render_vpn(self, state: AppState) -> None:
        panel = self.query_one("#panel-vpn", InfoPanel)
        vpn = state.vpn
        rows = [
            ("Status", self._status_text(vpn.status, vpn.protocol or "")),
            ("Profile", vpn.profile_name or "--"),
            ("Interface", vpn.interface or "--"),
            ("Tunnel IP", vpn.tunnel_ip or "--"),
            ("VPN IP", vpn.public_ip or "--"),
            ("Country", vpn.country or "--"),
            ("Endpoint", fmt.truncate(vpn.endpoint, 34)),
            (
                "Kill switch",
                "armed" if vpn.killswitch_active else "off",
            ),
            ("Pool", f"{vpn.pool_size} profile(s)"),
            ("Last switch", fmt.relative(vpn.last_switch)),
        ]
        panel.set_rows(rows, note=vpn.error)
        panel.set_state_class(
            active=vpn.status is ServiceStatus.ACTIVE,
            error=vpn.status is ServiceStatus.ERROR,
        )

    def _render_dns(self, state: AppState) -> None:
        panel = self.query_one("#panel-dns", InfoPanel)
        dns = state.dns
        rows = [
            ("Status", self._status_text(dns.status)),
            ("Listening", dns.listen or "--"),
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
