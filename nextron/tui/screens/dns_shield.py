"""The DNS Shield screen: blocklists, whitelist and the local resolver."""

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
from nextron.dns.sources import SourceCatalogue
from nextron.storage import paths
from nextron.tui.screens.base import NextronScreen
from nextron.tui.screens.modals import ConfirmScreen, PromptScreen
from nextron.utils import filedialog
from nextron.utils import format as fmt

__all__ = ["DNSShieldScreen"]


class DNSShieldScreen(NextronScreen):
    """Ad, tracker and malware blocking -- independent of the routing mode."""

    subtitle = "DNS Shield"

    BINDINGS = [
        Binding("t", "toggle_shield", "Enable / disable"),
        Binding("space", "toggle_list", "List on/off"),
        Binding("i", "import_list", "Import (browse)"),
        Binding("o", "import_path", "Import (path)", show=False),
        Binding("u", "update_lists", "Download lists"),
        Binding("x,delete", "remove_list", "Remove"),
        Binding("w", "whitelist_add", "Whitelist"),
        Binding("r", "reload_lists", "Reload"),
        Binding("escape", "close", "Return"),
    ]

    def compose_body(self) -> ComposeResult:
        yield Static(
            Text(
                "U downloads every enabled source in the catalogue. "
                "I opens your file browser to import local .txt/.hosts/.list "
                "files; O imports a typed path.",
                style=constants.COLOR_SURFACE,
            ),
            classes="screen-hint",
        )
        yield Static("", id="dns-summary", classes="screen-hint")
        with Vertical(classes="screen-body") as body:
            body.border_title = "Blocklists"
            yield DataTable(id="dns-table", cursor_type="row")
            yield Static("", id="dns-progress")
            yield Static("", id="dns-footer")

    def on_mount(self) -> None:
        super().on_mount()
        _, imported = self.app.runtime.import_from_sources()
        if imported:
            self.app.notify(
                f"Imported {imported} list(s) from Sources/DNS list",
                title="DNS Shield",
            )
        table = self.query_one(DataTable)
        table.add_columns(
            "On", "List", "Format", "Domains", "Skipped", "Size", "Modified"
        )
        self.reload()
        table.focus()
        # discover() only stats the files; parse them so the Domains column and
        # the blocked-domain total are real numbers the moment the screen opens.
        self.app.run_action_task(
            self.app.runtime.dns.reload_lists(), on_done=self.reload
        )

    # -- rendering ---------------------------------------------------------- #

    def reload(self) -> None:
        runtime = self.app.runtime
        settings = runtime.config.dns
        library = runtime.dns.library
        library.discover(list(settings.enabled_blocklists) or None)

        table = self.query_one(DataTable)
        table.clear()
        for entry in library.files:
            table.add_row(
                Text("◉" if entry.enabled else "◌", style=constants.COLOR_ACCENT),
                entry.name,
                entry.format,
                fmt.thousands(entry.domains) if entry.domains else "--",
                fmt.thousands(entry.skipped) if entry.skipped else "--",
                f"{entry.size_bytes / 1024:.0f} KiB",
                entry.modified.strftime("%Y-%m-%d") if entry.modified else "--",
                key=entry.name,
            )

        state = runtime.state.dns
        summary = Text(no_wrap=True)
        summary.append("Shield ", style=constants.COLOR_SURFACE)
        summary.append(
            f"{state.status.label}", style=f"bold {constants.COLOR_TEXT}"
        )
        if state.listen:
            summary.append(f" on {state.listen}", style=constants.COLOR_TEXT)
        summary.append("   Upstream ", style=constants.COLOR_SURFACE)
        summary.append(
            ("Tor DNSPort " if state.using_tor_dns else "")
            + (", ".join(state.upstream) or "--"),
            style=constants.COLOR_TEXT,
        )
        summary.append("   Blocked ", style=constants.COLOR_SURFACE)
        summary.append(
            f"{fmt.thousands(state.blocked_domains)} domains",
            style=f"bold {constants.COLOR_TEXT}",
        )
        catalogue = SourceCatalogue()
        catalogue.load()
        summary.append("   Sources ", style=constants.COLOR_SURFACE)
        if catalogue.exists:
            summary.append(
                f"{len(catalogue.enabled())}/{len(catalogue)} enabled",
                style=f"bold {constants.COLOR_TEXT}",
            )
            summary.append("  U to download", style=constants.COLOR_ACCENT)
        else:
            summary.append("none", style=f"bold {constants.COLOR_TEXT}")
            summary.append(
                f"  drop lists into {paths.dns_sources_dir()}",
                style=constants.COLOR_ACCENT,
            )
        self.query_one("#dns-summary", Static).update(summary)

        footer = Text(no_wrap=True)
        footer.append(
            f"Queries {fmt.thousands(state.queries_total)} . "
            f"blocked {fmt.thousands(state.queries_blocked)} "
            f"({fmt.percentage(state.block_rate)}) . "
            f"whitelist {fmt.thousands(state.whitelisted_domains)} . "
            f"system DNS {'redirected' if state.resolv_conf_managed else 'unchanged'}",
            style=constants.COLOR_SURFACE,
        )
        footer.append(
            f"   Supported: {', '.join(constants.BLOCKLIST_SUFFIXES)}",
            style=constants.COLOR_SURFACE,
        )
        self.query_one("#dns-footer", Static).update(footer)

    def _selected_name(self) -> str | None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except Exception:
            return None
        return str(row_key.value)

    # -- actions ------------------------------------------------------------ #

    def action_toggle_shield(self) -> None:
        self.app.run_action_task(
            self.app.runtime.toggle_dns_shield(),
            "Toggling DNS Shield",
            on_done=self.reload,
        )

    def action_toggle_list(self) -> None:
        name = self._selected_name()
        if name is None:
            return
        entry = next(
            (f for f in self.app.runtime.dns.library.files if f.name == name), None
        )
        if entry is None:
            return
        self.app.run_action_task(
            self.app.runtime.dns.set_blocklist_enabled(name, not entry.enabled),
            f"{'Disabling' if entry.enabled else 'Enabling'} {name}",
            on_done=self.reload,
        )

    def action_import_list(self) -> None:
        """Open the desktop file browser and import every list selected."""
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
            title="NEXTRON -- import ad-blocking lists",
            file_filter=filedialog.BLOCKLIST_FILTER,
            multiple=True,
        )
        if result.error:
            self.app.notify(result.error, severity="warning", title="Import")
            return
        if result.cancelled or not result.paths:
            return
        await self._import_paths(result.paths)

    def action_import_path(self) -> None:
        """Import from a typed path (or several, separated by spaces)."""

        def handle(raw: str | None) -> None:
            if not raw:
                return
            paths = [Path(token).expanduser() for token in shlex.split(raw)]
            self.app.run_action_task(self._import_paths(paths), "Importing blocklists")

        self.app.push_screen(
            PromptScreen(
                "Path to a .txt, .hosts or .list blocklist\n"
                "(several may be given, separated by spaces):",
                title="Import blocklist",
                placeholder="~/Downloads/StevenBlack.hosts",
            ),
            handle,
        )

    async def _import_paths(self, paths) -> None:
        """Import every list, enable it, then reload the merged domain set."""
        imported: list[str] = []
        failures: list[str] = []
        for path in paths:
            try:
                name = await self.app.runtime.dns.import_blocklist(Path(path))
            except NextronError as exc:
                failures.append(f"{Path(path).name}: {exc}")
            else:
                imported.append(name)

        self.reload()
        if imported:
            blocked = self.app.runtime.state.dns.blocked_domains
            self.app.notify(
                f"Imported and enabled {len(imported)} list(s); "
                f"{blocked:,} domains blocked",
                title="DNS Shield",
            )
        if failures:
            self.app.notify(
                "\n".join(failures[:3]) + ("\n..." if len(failures) > 3 else ""),
                severity="error",
                title=f"{len(failures)} import(s) failed",
            )

    def action_remove_list(self) -> None:
        name = self._selected_name()
        if name is None:
            return

        def handle(confirmed: bool | None) -> None:
            if not confirmed:
                return
            self.app.run_action_task(
                self.app.runtime.dns.remove_blocklist(name),
                f"Removing {name}",
                on_done=self.reload,
            )

        self.app.push_screen(
            ConfirmScreen(
                f"Remove the blocklist '{name}' from the library?",
                title="Remove blocklist",
                confirm_label="Remove",
                focus_confirm=False,
            ),
            handle,
        )

    def action_whitelist_add(self) -> None:
        def handle(domain: str | None) -> None:
            if not domain:
                return
            try:
                self.app.run_action_task(
                    self.app.runtime.dns.whitelist_add(domain),
                    f"Whitelisting {domain}",
                    on_done=self.reload,
                )
            except NextronError as exc:
                self.app.notify(str(exc), severity="error")

        self.app.push_screen(
            PromptScreen(
                "Domain to whitelist (never blocked):",
                title="Whitelist",
                placeholder="example.com",
            ),
            handle,
        )

    def action_update_lists(self) -> None:
        """Download every enabled source and rebuild the blocked domain set."""
        catalogue = SourceCatalogue()
        catalogue.load()
        if not catalogue.enabled():
            self.app.notify(
                "No download catalogue. Drop your lists straight into "
                f"{paths.dns_sources_dir()}, or put a sources.txt in "
                f"{paths.dns_dir()} to download from.",
                severity="warning",
                title="DNS Shield",
            )
            return

        def handle(confirmed: bool | None) -> None:
            if confirmed:
                self.app.run_action_task(self._update_lists(), on_done=self.reload)

        self.app.push_screen(
            ConfirmScreen(
                f"Download {len(catalogue.enabled())} blocklist(s) now?\n"
                "This contacts each list's publisher directly. Nothing about "
                "you is sent -- only a plain request for the file.",
                title="Download blocklists",
                confirm_label="Download",
            ),
            handle,
        )

    async def _update_lists(self) -> None:
        report = await self.app.runtime.update_blocklists(progress=self._on_progress)
        self._set_progress(
            f"{report.summary} in {report.duration_seconds:.0f}s", done=True
        )
        self.app.notify(report.summary, title="Blocklists updated")

    def _on_progress(self, done: int, total: int, outcome) -> None:
        self._set_progress(f"[{done}/{total}] {outcome.marker} {outcome.label}")

    def _set_progress(self, message: str, *, done: bool = False) -> None:
        try:
            widget = self.query_one("#dns-progress", Static)
        except Exception:  # pragma: no cover - screen torn down mid-download
            return
        widget.update(
            Text(
                message,
                style=constants.COLOR_ACCENT if done else constants.COLOR_SURFACE,
            )
        )

    def action_reload_lists(self) -> None:
        self.app.run_action_task(
            self.app.runtime.dns.reload_lists(),
            "Reloading blocklists",
            on_done=self.reload,
        )
