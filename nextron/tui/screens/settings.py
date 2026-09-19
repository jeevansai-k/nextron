"""The Settings screen -- edit every persisted option from the keyboard."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import DataTable, Static

from nextron.core import constants
from nextron.core.config import ShuffleAlgorithm
from nextron.core.exceptions import NextronError
from nextron.tui.screens.base import NextronScreen
from nextron.tui.screens.modals import PromptScreen
from nextron.utils import intervals

__all__ = ["SettingsScreen"]


@dataclass(frozen=True, slots=True)
class Setting:
    """One editable configuration value."""

    section: str
    label: str
    path: str
    kind: str  # "bool" | "int" | "str" | "enum" | "list" | "interval"
    hint: str = ""
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[Any, ...] = ()


SETTINGS: tuple[Setting, ...] = (
    # Tor
    Setting("Tor", "Rotation enabled", "tor.rotation_enabled", "bool",
            "Run the Tor identity scheduler"),
    Setting("Tor", "Rotation interval", "tor.rotation_interval", "interval",
            "Enter steps: 15s, 30s, then every 30s to 5m",
            constants.ROTATION_MIN_SECONDS, constants.ROTATION_MAX_SECONDS),
    Setting("Tor", "Verify exit after rotation", "tor.verify_exit_after_rotation",
            "bool", "Confirm the exit address really changed"),
    Setting("Tor", "Manage own daemon", "tor.manage_daemon", "bool",
            "Launch a private tor process instead of using the system one"),
    Setting("Tor", "Transparent routing", "tor.transparent_routing", "bool",
            "Redirect all TCP through Tor's TransPort (needs root)"),
    Setting("Tor", "SOCKS port", "tor.socks_port", "int", "", 1, 65535),
    Setting("Tor", "Control port", "tor.control_port", "int", "", 1, 65535),
    Setting("Tor", "DNS port", "tor.dns_port", "int", "", 1, 65535),
    Setting("Tor", "Trans port", "tor.trans_port", "int", "", 1, 65535),
    Setting("Tor", "Bootstrap timeout", "tor.bootstrap_timeout", "int",
            "Seconds to allow without bootstrap progress", 15, 600),
    Setting("Tor", "Exit countries", "tor.exit_countries", "list",
            "Comma separated country codes, e.g. DE,NL"),
    Setting("Tor", "Strict exit nodes", "tor.strict_exit_nodes", "bool",
            "Never leave the chosen countries"),
    # VPN
    Setting("VPN", "Shuffle enabled", "vpn.shuffle_enabled", "bool",
            "Run the VPN profile scheduler"),
    Setting("VPN", "Shuffle interval", "vpn.shuffle_interval", "interval",
            "Enter steps: 15s, 30s, then every 30s to 5m",
            constants.ROTATION_MIN_SECONDS, constants.ROTATION_MAX_SECONDS),
    Setting("VPN", "Shuffle algorithm", "vpn.shuffle_algorithm", "enum",
            "Random, Sequential, Round Robin or No Repeat",
            choices=tuple(ShuffleAlgorithm)),
    Setting("VPN", "Kill switch", "vpn.killswitch_enabled", "bool",
            "Block all egress during VPN transitions"),
    Setting("VPN", "Verify tunnel", "vpn.verify_tunnel", "bool",
            "Read the public address after connecting"),
    Setting("VPN", "Connect timeout", "vpn.connect_timeout", "int", "", 10, 300),
    Setting("VPN", "Reconnect retries", "vpn.reconnect_retries", "int", "", 0, 10),
    # DNS
    Setting("DNS Shield", "Enabled", "dns.enabled", "bool",
            "Start the Shield with the routing mode"),
    Setting("DNS Shield", "Listen host", "dns.listen_host", "str", ""),
    Setting("DNS Shield", "Listen port", "dns.listen_port", "int",
            "53 needs root", 1, 65535),
    Setting("DNS Shield", "Fallback port", "dns.fallback_port", "int",
            "Used when port 53 is unavailable", 1, 65535),
    Setting("DNS Shield", "Upstream resolvers", "dns.upstream", "list",
            "Comma separated, used when Tor DNS is not available"),
    Setting("DNS Shield", "Prefer Tor DNS", "dns.prefer_tor_dns", "bool",
            "Send upstream queries through Tor's DNSPort"),
    Setting("DNS Shield", "Manage resolv.conf", "dns.manage_resolv_conf", "bool",
            "Redirect system DNS to the Shield"),
    Setting("DNS Shield", "Cache TTL", "dns.cache_ttl", "int",
            "Seconds; 0 disables caching", 0, 86400),
    Setting("DNS Shield", "Block AAAA answers", "dns.block_ipv6_answers", "bool",
            "Closes a common IPv6 leak path"),
    # Verification
    Setting("Verification", "Enabled", "verification.enabled", "bool",
            "Run the checklist before reporting Connected"),
    Setting("Verification", "Strict", "verification.strict", "bool",
            "Refuse a mode if any required check fails"),
    Setting("Verification", "Check routing", "verification.check_routing", "bool"),
    Setting("Verification", "Check DNS leak", "verification.check_dns_leak", "bool"),
    Setting("Verification", "Check IPv6 leak", "verification.check_ipv6_leak", "bool"),
    # Interface
    Setting("Interface", "PNG banner", "interface.show_png_banner", "bool",
            "Render assets/banner.png; ASCII is used automatically on failure"),
    Setting("Interface", "Banner width", "interface.banner_width", "int", "", 24, 200),
    Setting("Interface", "Splash seconds", "interface.splash_seconds", "int", "", 0, 10),
    Setting("Interface", "Activity log lines", "interface.activity_log_lines", "int",
            "", 50, 5000),
    Setting("Interface", "Confirm quit", "interface.confirm_quit", "bool"),
    Setting("Interface", "Log level", "log_level", "enum", "",
            choices=("DEBUG", "INFO", "WARNING", "ERROR")),
)


@dataclass(frozen=True, slots=True)
class SectionHeader:
    """A heading row. Not editable, and the cursor steps over it."""

    name: str


def _rows() -> tuple[SectionHeader | Setting, ...]:
    """Every setting, each group introduced by its own heading.

    Grouping is taken from the settings themselves rather than declared twice,
    so a setting added to a section cannot end up under the wrong heading.
    """
    built: list[SectionHeader | Setting] = []
    section: str | None = None
    for setting in SETTINGS:
        if setting.section != section:
            section = setting.section
            built.append(SectionHeader(section))
        built.append(setting)
    return tuple(built)


ROWS: tuple[SectionHeader | Setting, ...] = _rows()


class SettingsScreen(NextronScreen):
    """Every option in ``config.toml``, editable with Enter / Space."""

    subtitle = "Settings"

    BINDINGS = [
        Binding("enter", "edit", "Edit"),
        Binding("down", "cursor_down", "Down", show=False, priority=True),
        Binding("up", "cursor_up", "Up", show=False, priority=True),
        Binding("space", "edit", "Edit", show=False),
        Binding("r", "reload", "Reload file", show=False),
        Binding("escape", "close", "Return"),
    ]

    def compose_body(self) -> ComposeResult:
        yield Static(
            Text(
                "Enter edits the highlighted setting: switches toggle, choices "
                "cycle, intervals step through 15s / 30s / 1m ... 5m. Changes are "
                "written to config.toml immediately.",
                style=constants.COLOR_SURFACE,
            ),
            classes="screen-hint",
        )
        with Vertical(classes="screen-body") as body:
            body.border_title = "Configuration"
            yield DataTable(id="settings-table", cursor_type="row")
            yield Static("", id="settings-footer")

    def on_mount(self) -> None:
        super().on_mount()
        table = self.query_one(DataTable)
        table.add_columns("Setting", "Value", "Notes")
        self.reload()
        table.focus()
        self._move_to_first_setting()

    # -- value access ------------------------------------------------------- #

    def _get(self, setting: Setting) -> Any:
        target: Any = self.app.runtime.config
        parts = setting.path.split(".")
        for part in parts[:-1]:
            target = getattr(target, part)
        return getattr(target, parts[-1])

    def _set(self, setting: Setting, value: Any) -> None:
        target: Any = self.app.runtime.config
        parts = setting.path.split(".")
        for part in parts[:-1]:
            target = getattr(target, part)
        setattr(target, parts[-1], value)
        self.app.runtime.config_manager.save()

    @staticmethod
    def _format_value(value: Any, kind: str = "") -> str:
        if kind == "interval":
            return intervals.label(int(value))
        if isinstance(value, bool):
            return "on" if value else "off"
        if isinstance(value, (list, tuple)):
            return ", ".join(str(v) for v in value) or "--"
        if hasattr(value, "label"):
            return str(value.label)
        return str(value)

    # -- rendering ---------------------------------------------------------- #

    def reload(self) -> None:
        table = self.query_one(DataTable)
        cursor = table.cursor_row
        table.clear()
        for index, row in enumerate(ROWS):
            if isinstance(row, SectionHeader):
                table.add_row(
                    Text(row.name.upper(), style=f"bold {constants.COLOR_SECONDARY}"),
                    Text(""),
                    Text(""),
                    key=str(index),
                )
                continue
            table.add_row(
                # Indented, so the eye reads each block as belonging to the
                # heading above it rather than as one long list.
                Text(f"  {row.label}", style=constants.COLOR_TEXT),
                Text(
                    self._format_value(self._get(row), row.kind),
                    style=constants.COLOR_ACCENT,
                ),
                row.hint,
                key=str(index),
            )
        if 0 <= cursor < len(ROWS):
            table.move_cursor(row=cursor)
        self.query_one("#settings-footer", Static).update(
            Text(
                f"Stored in {self.app.runtime.config_manager.path}",
                style=constants.COLOR_SURFACE,
            )
        )

    def _selected(self) -> Setting | None:
        row = self._row_at(self.query_one(DataTable).cursor_row)
        return row if isinstance(row, Setting) else None

    @staticmethod
    def _row_at(index: int) -> SectionHeader | Setting | None:
        return ROWS[index] if 0 <= index < len(ROWS) else None

    @staticmethod
    def row_of(path: str) -> int:
        """The table row showing the setting at *path*."""
        for index, row in enumerate(ROWS):
            if isinstance(row, Setting) and row.path == path:
                return index
        raise KeyError(path)

    # -- cursor: headings are labels, so step over them ---------------------- #

    def _move_to_first_setting(self) -> None:
        table = self.query_one(DataTable)
        for index, row in enumerate(ROWS):
            if isinstance(row, Setting):
                table.move_cursor(row=index)
                return

    def action_cursor_down(self) -> None:
        self._step(1)

    def action_cursor_up(self) -> None:
        self._step(-1)

    def _step(self, direction: int) -> None:
        """Move one editable row in *direction*, skipping any heading."""
        table = self.query_one(DataTable)
        index = table.cursor_row + direction
        while 0 <= index < len(ROWS) and isinstance(ROWS[index], SectionHeader):
            index += direction
        if 0 <= index < len(ROWS):
            table.move_cursor(row=index)

    # -- editing ------------------------------------------------------------ #

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """DataTable consumes Enter itself, so act on its message instead."""
        event.stop()
        self.action_edit()

    def action_edit(self) -> None:
        setting = self._selected()
        if setting is None:
            return
        current = self._get(setting)

        if setting.kind == "bool":
            self._apply(setting, not bool(current))
            return

        if setting.kind == "enum":
            choices = list(setting.choices)
            try:
                index = choices.index(current)
            except ValueError:
                index = -1
            self._apply(setting, choices[(index + 1) % len(choices)])
            return

        if setting.kind == "interval":
            # The time-changing element: walk the preset ladder, wrapping round.
            self._apply(setting, intervals.cycle(int(current)))
            return

        def handle(value: str | None) -> None:
            if value is None:
                return
            self._apply(setting, value)

        bounds = ""
        if setting.minimum is not None and setting.maximum is not None:
            bounds = f" ({setting.minimum}-{setting.maximum})"
        self.app.push_screen(
            PromptScreen(
                f"{setting.label}{bounds}"
                + (f"\n{setting.hint}" if setting.hint else ""),
                title="Edit setting",
                value=self._format_value(current) if current not in (None, "--") else "",
            ),
            handle,
        )

    def _apply(self, setting: Setting, raw: Any) -> None:
        try:
            value = self._coerce(setting, raw)
        except (TypeError, ValueError):
            self.app.notify(
                f"'{raw}' is not valid for {setting.label}", severity="error"
            )
            return

        try:
            self._set(setting, value)
        except NextronError as exc:
            self.app.notify(str(exc), severity="error")
            return
        except Exception as exc:  # pydantic validation
            self.app.notify(f"Rejected: {exc}", severity="error")
            return

        self._sync_engines(setting)
        self.reload()
        self.app.notify(
            f"{setting.label} = "
            f"{self._format_value(self._get(setting), setting.kind)}",
            title="Settings",
        )

    @staticmethod
    def _coerce(setting: Setting, raw: Any) -> Any:
        if setting.kind == "bool":
            return bool(raw)
        if setting.kind == "enum":
            return raw
        if setting.kind in ("int", "interval"):
            value = int(str(raw).strip())
            if setting.minimum is not None:
                value = max(setting.minimum, value)
            if setting.maximum is not None:
                value = min(setting.maximum, value)
            return value
        if setting.kind == "list":
            return [
                part.strip()
                for part in str(raw).replace(";", ",").split(",")
                if part.strip() and part.strip() != "--"
            ]
        return str(raw).strip()

    def _sync_engines(self, setting: Setting) -> None:
        """Push a changed value into the live engines that care about it."""
        runtime = self.app.runtime
        if setting.path == "tor.rotation_interval":
            runtime.tor_scheduler.set_interval(runtime.config.tor.rotation_interval)
        elif setting.path == "vpn.shuffle_interval":
            runtime.vpn_scheduler.set_interval(runtime.config.vpn.shuffle_interval)
        elif setting.path == "vpn.shuffle_algorithm":
            runtime.vpn.shuffle.set_algorithm(runtime.config.vpn.shuffle_algorithm)
            runtime.state_manager.update_vpn(
                shuffle_algorithm=runtime.config.vpn.shuffle_algorithm
            )
        elif setting.path == "vpn.killswitch_enabled":
            runtime.vpn.killswitch.enabled = runtime.config.vpn.killswitch_enabled
        elif setting.path == "log_level":
            from nextron.utils.logging import setup_logging

            setup_logging(runtime.config.log_level)

    def action_reload(self) -> None:
        self.app.runtime.config_manager.load()
        self.reload()
        self.app.notify("Configuration reloaded from disk", title="Settings")
