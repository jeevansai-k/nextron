"""The VPN profile library screen: import, rename, delete, favourite, activate."""

from __future__ import annotations

import shlex
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import DataTable, Static

from nextron.core import constants
from nextron.core.exceptions import NextronError
from nextron.tui.screens.base import NextronScreen
from nextron.tui.screens.modals import ConfirmScreen, PromptScreen
from nextron.utils import filedialog
from nextron.utils import format as fmt
from nextron.vpn.profiles import VPNProfile

__all__ = ["VPNLibraryScreen"]


class VPNLibraryScreen(NextronScreen):
    """Manage an unlimited number of local VPN profiles."""

    subtitle = "VPN Library"

    BINDINGS = [
        Binding("i", "import_profile", "Import (browse)"),
        Binding("o", "import_path", "Import (path)", show=False),
        Binding("r", "rename_profile", "Rename"),
        Binding("delete,x", "delete_profile", "Delete"),
        Binding("f", "toggle_favorite", "Favourite"),
        Binding("enter", "set_active", "Select"),
        Binding("c", "connect_profile", "Connect now", show=False),
        Binding("u", "set_credentials", "Credentials"),
        Binding("e", "export_profile", "Export", show=False),
        Binding("escape", "close", "Return"),
    ]

    def compose_body(self) -> ComposeResult:
        yield Static(
            Text(
                "Enter selects the active profile (▸) -- the one every "
                "VPN-bearing mode uses. I opens your file browser to import; "
                "files dropped in Sources/VPN Profiles appear here too. A "
                "profile marked 'needed' under Credentials wants a username "
                "and password: press U to point it at a file holding them.",
                style=constants.COLOR_SURFACE,
            ),
            classes="screen-hint",
        )
        with Vertical(classes="screen-body") as body:
            body.border_title = "Profiles"
            yield DataTable(id="profile-table", cursor_type="row", zebra_stripes=False)
            yield Static("", id="profile-footer")

    def on_mount(self) -> None:
        super().on_mount()
        # Anything dropped into Sources/VPN Profiles since the last look.
        imported, _ = self.app.runtime.import_from_sources()
        if imported:
            self.app.notify(
                f"Imported {imported} profile(s) from Sources/VPN Profiles",
                title="VPN Library",
            )
        table = self.query_one(DataTable)
        table.add_columns(
            "", "Name", "Protocol", "Endpoint", "TCP", "Cert", "Used", "Credentials"
        )
        self.reload()
        table.focus()

    # -- rendering ---------------------------------------------------------- #

    @staticmethod
    def _credentials_cell(profile) -> Text:
        """Say plainly whether a profile can connect as it stands.

        A profile asking for a username and password cannot come up until one
        is supplied, so "needed" has to be visible here rather than discovered
        as a failed connection.
        """
        if profile.auth_file:
            return Text(Path(profile.auth_file).name, style=constants.COLOR_TEXT)
        if profile.requires_credentials:
            return Text("needed -- press U", style=constants.COLOR_PRIMARY)
        return Text("--", style=constants.COLOR_SURFACE)

    def reload(self) -> None:
        runtime = self.app.runtime
        table = self.query_one(DataTable)
        table.clear()
        active_id = runtime.config.vpn.active_profile
        pool = set(runtime.config.vpn.shuffle_pool)

        for profile in runtime.vpn.library.all():
            markers = []
            if profile.id == active_id:
                markers.append("▸")
            if profile.favorite:
                markers.append("✦")
            if profile.id in pool:
                markers.append("↻")
            table.add_row(
                Text(" ".join(markers), style=constants.COLOR_ACCENT),
                profile.name,
                profile.protocol.label,
                fmt.truncate(profile.endpoint, 30),
                fmt.boolean(profile.tcp),
                Text(
                    profile.expiry,
                    style=constants.COLOR_PRIMARY
                    if profile.expired
                    else constants.COLOR_TEXT,
                ),
                str(profile.use_count),
                self._credentials_cell(profile),
                key=profile.id,
            )

        count = len(runtime.vpn.library)
        self.query_one("#profile-footer", Static).update(
            Text(
                f"{count} profile(s)   ▸ active   ✦ favourite   ↻ in rotation pool"
                "   Cert = when the embedded certificate expires",
                style=constants.COLOR_SURFACE,
            )
        )

    def _selected(self) -> VPNProfile | None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except Exception:
            return None
        return self.app.runtime.vpn.library.get(str(row_key.value))

    # -- actions ------------------------------------------------------------ #

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """DataTable consumes Enter itself, so act on its message instead."""
        event.stop()
        self.action_set_active()

    def action_import_profile(self) -> None:
        """Open the desktop file browser and import everything selected."""
        if filedialog.available() is None:
            self.app.notify(
                f"File browser unavailable: {filedialog.unavailable_reason()}. "
                "Type a path instead.",
                severity="warning",
                title="Import",
            )
            self.action_import_path()
            return
        self.app.run_action_task(self._browse_and_import(), "Opening file browser")

    async def _browse_and_import(self) -> None:
        result = await filedialog.pick_files(
            title="NEXTRON -- import VPN profiles",
            file_filter=filedialog.VPN_FILTER,
            multiple=True,
        )
        if result.error:
            self.app.notify(result.error, severity="warning", title="Import")
            return
        if result.cancelled or not result.paths:
            return
        self._import_paths(result.paths)

    def action_import_path(self) -> None:
        """Import from a typed path (or several, separated by spaces)."""

        def handle(raw: str | None) -> None:
            if not raw:
                return
            paths = [Path(token).expanduser() for token in shlex.split(raw)]
            self._import_paths(paths)

        self.app.push_screen(
            PromptScreen(
                "Path to a .ovpn, .conf, .wgconf or .json profile\n"
                "(several may be given, separated by spaces):",
                title="Import VPN profile",
                placeholder="~/Downloads/berlin.ovpn",
            ),
            handle,
        )

    def _import_paths(self, paths) -> None:
        """Import every path, reporting successes and failures together."""
        imported: list[str] = []
        failures: list[str] = []
        for path in paths:
            try:
                profile = self.app.runtime.import_vpn_profile(Path(path))
            except NextronError as exc:
                failures.append(f"{Path(path).name}: {exc}")
            else:
                imported.append(f"{profile.name} ({profile.protocol.label})")

        self.reload()
        if imported:
            self.app.notify(
                f"Imported {len(imported)}: " + ", ".join(imported[:4])
                + (" ..." if len(imported) > 4 else ""),
                title="VPN Library",
            )
        if failures:
            self.app.notify(
                "\n".join(failures[:3]) + ("\n..." if len(failures) > 3 else ""),
                severity="error",
                title=f"{len(failures)} import(s) failed",
            )

    def action_rename_profile(self) -> None:
        profile = self._selected()
        if profile is None:
            return

        def handle(name: str | None) -> None:
            if not name:
                return
            try:
                self.app.runtime.vpn.library.rename(profile.id, name)
            except NextronError as exc:
                self.app.notify(str(exc), severity="error")
                return
            self.reload()

        self.app.push_screen(
            PromptScreen(
                f"New name for '{profile.name}':",
                title="Rename profile",
                value=profile.name,
            ),
            handle,
        )

    def action_delete_profile(self) -> None:
        profile = self._selected()
        if profile is None:
            return

        def handle(confirmed: bool | None) -> None:
            if not confirmed:
                return
            try:
                self.app.runtime.vpn.library.delete(profile.id)
            except NextronError as exc:
                self.app.notify(str(exc), severity="error")
                return
            self.app.runtime.vpn.refresh_pool()
            self.reload()
            self.app.notify(f"Deleted '{profile.name}'", title="VPN Library")

        self.app.push_screen(
            ConfirmScreen(
                f"Delete the profile '{profile.name}'?\n"
                "The configuration file is removed from the library.",
                title="Delete profile",
                confirm_label="Delete",
                focus_confirm=False,
            ),
            handle,
        )

    def action_toggle_favorite(self) -> None:
        profile = self._selected()
        if profile is None:
            return
        self.app.runtime.vpn.library.toggle_favorite(profile.id)
        self.reload()

    def action_set_active(self) -> None:
        profile = self._selected()
        if profile is None:
            return
        self.app.runtime.set_active_profile(profile)
        self.reload()
        suffix = (
            "  Press C on the dashboard to reconnect with it."
            if self.app.runtime.routing.active
            else "  It will be used the next time you connect."
        )
        self.app.notify(
            f"Active profile: {profile.name}.{suffix}", title="VPN Library"
        )

    def action_connect_profile(self) -> None:
        profile = self._selected()
        if profile is None:
            return
        self.app.run_action_task(
            self.app.runtime.switch_profile(profile), f"Connecting '{profile.name}'"
        )

    def action_set_credentials(self) -> None:
        profile = self._selected()
        if profile is None:
            return

        def handle(path: str | None) -> None:
            try:
                self.app.runtime.vpn.library.set_auth_file(profile.id, path)
            except NextronError as exc:
                self.app.notify(str(exc), severity="error")
                return
            self.reload()

        self.app.push_screen(
            PromptScreen(
                "Path to an existing OpenVPN credentials file "
                "(leave empty to clear).\n"
                "NEXTRON never asks for or stores VPN passwords itself.",
                title="Credentials file",
                value=profile.auth_file or "",
            ),
            handle,
        )

    def action_export_profile(self) -> None:
        profile = self._selected()
        if profile is None:
            return

        def handle(path: str | None) -> None:
            if not path:
                return
            try:
                target = self.app.runtime.vpn.library.export(profile.id, Path(path))
            except NextronError as exc:
                self.app.notify(str(exc), severity="error")
                return
            self.app.notify(f"Exported to {target}", title="VPN Library")

        self.app.push_screen(
            PromptScreen("Export to which directory or file?", title="Export profile"),
            handle,
        )
