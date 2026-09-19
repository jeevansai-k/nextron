"""The NEXTRON command line interface.

``nextron`` with no arguments launches the TUI. The sub-commands exist for the
things a terminal is better at than a full-screen interface: scripted imports,
a health report you can pipe, and a headless session for a server with no
interactive terminal attached.
"""

from __future__ import annotations

import asyncio
import json
import signal
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from nextron.core import constants
from nextron.core.config import ConfigManager, RoutingMode
from nextron.core.exceptions import NextronError
from nextron.core.runtime import NextronRuntime
from nextron.diagnostics.doctor import Doctor
from nextron.storage import paths
from nextron.utils import filedialog
from nextron.utils import format as fmt
from nextron.utils.banner import render_banner
from nextron.utils.logging import setup_logging

console = Console()

app = typer.Typer(
    name=constants.APP_SLUG,
    help=f"{constants.APP_NAME} -- {constants.TAGLINE}",
    add_completion=False,
    no_args_is_help=False,
    rich_markup_mode="rich",
)
vpn_app = typer.Typer(help="Manage the local VPN profile library.")
dns_app = typer.Typer(help="Manage DNS Shield blocklists.")
config_app = typer.Typer(help="Inspect the local configuration.")
app.add_typer(vpn_app, name="vpn")
app.add_typer(dns_app, name="dns")
app.add_typer(config_app, name="config")

_MODE_CHOICES = ", ".join(mode.value for mode in RoutingMode)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _parse_mode(value: str | None) -> RoutingMode | None:
    if not value:
        return None
    try:
        return RoutingMode(value.strip().lower().replace("-", "_"))
    except ValueError as exc:
        raise typer.BadParameter(
            f"Unknown mode '{value}'. Choose one of: {_MODE_CHOICES}"
        ) from exc


def _print_banner() -> None:
    result = render_banner(width=min(72, console.width - 2), max_height=14)
    console.print(result.renderable)
    console.print(
        Text(constants.TAGLINE, style=f"italic {constants.COLOR_SURFACE}"),
        justify="center",
    )


async def _none():
    return None


async def _live_socks_port(preferred: int) -> int | None:
    """The Tor SOCKS port that is actually listening, if any.

    NEXTRON's own daemon is preferred over any other Tor on the machine: it
    records the port it chose in the torrc it generated, which matters when it
    had to move off 9050 to make room for the system daemon.
    """
    import re

    from nextron.utils import net

    candidates: list[int] = []
    torrc = paths.runtime_dir() / "torrc"
    try:
        match = re.search(r"^SocksPort\s+\S+:(\d+)", torrc.read_text(), re.MULTILINE)
    except OSError:
        match = None
    if match:
        candidates.append(int(match.group(1)))

    candidates += [preferred, preferred + 200]
    for candidate in dict.fromkeys(candidates):
        if await net.is_port_open("127.0.0.1", candidate, timeout=1.5):
            return candidate
    return None


def _browse(file_filter, title: str) -> list[Path]:
    """Open the desktop file browser and return the selection."""
    backend = filedialog.available()
    if backend is None:
        _fail(
            f"No file browser available ({filedialog.unavailable_reason()}). "
            "Pass the file paths as arguments instead."
        )
        return []

    console.print(
        Text(
            f"Opening {backend}... select one or more files.",
            style=constants.COLOR_SURFACE,
        )
    )
    result = asyncio.run(
        filedialog.pick_files(title=title, file_filter=file_filter, multiple=True)
    )
    if result.error:
        _fail(result.error)
        return []
    if result.cancelled or not result.paths:
        console.print(Text("Nothing selected.", style=constants.COLOR_SURFACE))
        return []
    return list(result.paths)


def _fail(message: str) -> None:
    console.print(Text(f"✗ {message}", style=constants.COLOR_PRIMARY))
    raise typer.Exit(code=1)


def _ok(message: str) -> None:
    console.print(Text(f"✓ {message}", style=constants.COLOR_ACCENT))


# --------------------------------------------------------------------------- #
# root
# --------------------------------------------------------------------------- #


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", "-V", help="Print the version and exit."
    ),
) -> None:
    """Launch the terminal interface when no sub-command is given."""
    if version:
        console.print(f"{constants.APP_NAME} {constants.VERSION}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        run(mode=None, connect=False)


@app.command()
def run(
    mode: str = typer.Option(
        None, "--mode", "-m", help=f"Routing mode to preselect ({_MODE_CHOICES})."
    ),
    connect: bool = typer.Option(
        False, "--connect", "-c", help="Establish the routing mode on startup."
    ),
) -> None:
    """Start the NEXTRON terminal interface."""
    from nextron.tui.app import run_app

    target = _parse_mode(mode)
    if target is not None:
        manager = ConfigManager()
        manager.load()
        manager.update(routing_mode=target)

    raise typer.Exit(code=run_app(auto_connect=connect, mode=target))


@app.command()
def connect(
    mode: str = typer.Argument(
        None, help=f"Routing mode to establish ({_MODE_CHOICES})."
    ),
    profile: str = typer.Option(
        None, "--profile", "-p", help="VPN profile name or id to use."
    ),
    rotate: bool = typer.Option(
        None,
        "--rotate/--no-rotate",
        help="Override the Tor rotation scheduler for this session.",
    ),
    shuffle: bool = typer.Option(
        None,
        "--shuffle/--no-shuffle",
        help="Override the VPN shuffle scheduler for this session.",
    ),
) -> None:
    """Run a headless session: establish a mode and hold it until Ctrl+C."""
    target = _parse_mode(mode)
    raise typer.Exit(code=asyncio.run(_headless(target, profile, rotate, shuffle)))


async def _headless(
    mode: RoutingMode | None,
    profile: str | None,
    rotate: bool | None,
    shuffle: bool | None,
) -> int:
    setup_logging("INFO", console=True)
    runtime = NextronRuntime(console_logs=True)
    if rotate is not None:
        runtime.config.tor.rotation_enabled = rotate
    if shuffle is not None:
        runtime.config.vpn.shuffle_enabled = shuffle

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - non-POSIX
            pass

    try:
        await runtime.start()
        report = await runtime.connect(mode, vpn_profile=profile)
    except NextronError as exc:
        console.print(Text(f"✗ {exc}", style=constants.COLOR_PRIMARY))
        await runtime.shutdown()
        return 1

    table = Table(title="Verification", box=None, pad_edge=False)
    table.add_column("", width=2)
    table.add_column("Check")
    table.add_column("Detail", overflow="fold")
    for check in report.checks:
        table.add_row(check.marker, check.name, check.detail or check.verdict)
    console.print(table)
    _ok(f"{runtime.mode.label} active -- {report.summary}")
    console.print(
        Text(
            "Holding the session open. Press Ctrl+C to tear it down cleanly.",
            style=constants.COLOR_SURFACE,
        )
    )

    try:
        await stop.wait()
    finally:
        console.print(Text("Tearing down...", style=constants.COLOR_SURFACE))
        await runtime.shutdown()
    return 0


@app.command()
def doctor(
    offline: bool = typer.Option(
        False, "--offline", help="Skip every network probe."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Run the system health report."""
    setup_logging("WARNING")
    report = asyncio.run(Doctor(ConfigManager().load()).run(network=not offline))

    if as_json:
        console.print_json(
            json.dumps(
                {
                    "healthy": report.healthy,
                    "summary": report.summary,
                    "findings": [
                        {
                            "section": f.section,
                            "name": f.name,
                            "status": f.status.value,
                            "detail": f.detail,
                            "hint": f.hint,
                        }
                        for f in report.findings
                    ],
                }
            )
        )
        raise typer.Exit(code=0 if report.healthy else 1)

    for section, findings in report.sections().items():
        console.print(Text(f"\n{section}", style=f"bold {constants.COLOR_ACCENT}"))
        for finding in findings:
            line = Text("  ")
            line.append(f"{finding.status.marker} ", style=f"bold {finding.status.color}")
            line.append(f"{finding.name}: ", style=f"bold {constants.COLOR_TEXT}")
            line.append(finding.detail, style=constants.COLOR_TEXT)
            console.print(line)
            if finding.hint:
                console.print(
                    Text(f"      -> {finding.hint}", style=constants.COLOR_SURFACE)
                )

    console.print()
    verdict = "HEALTHY" if report.healthy else "NEEDS ATTENTION"
    style = constants.COLOR_ACCENT if report.healthy else constants.COLOR_SURFACE
    console.print(Text(f"{verdict} -- {report.summary}", style=f"bold {style}"))
    raise typer.Exit(code=0 if report.healthy else 1)


@app.command()
def ip() -> None:
    """Show the public address and its location as seen right now."""
    from nextron.utils import net

    async def probe() -> None:
        config = ConfigManager().load()
        port = await _live_socks_port(config.tor.socks_port)

        direct, tor_info = await asyncio.gather(
            net.ip_info(),
            net.ip_info(proxy=net.socks_proxy_url(port)) if port else _none(),
        )
        if direct is None and tor_info is None:
            _fail("Could not determine the public address")

        table = Table(box=None, pad_edge=False)
        table.add_column("")
        table.add_column("Direct")
        table.add_column(f"Through Tor (:{port})" if port else "Through Tor")
        for label, attribute in (
            ("Address", "ip"),
            ("Country", "country"),
            ("City", "city"),
            ("Network", "org"),
        ):
            table.add_row(
                label,
                str(getattr(direct, attribute, None) or "--"),
                str(getattr(tor_info, attribute, None) or "--"),
            )
        console.print(table)

        if tor_info is None:
            console.print(
                Text(
                    "No Tor SOCKS proxy is listening. Start a Tor mode first.",
                    style=constants.COLOR_SURFACE,
                )
            )
        elif direct and tor_info and direct.ip == tor_info.ip:
            console.print(
                Text(
                    "✗ Same address both ways -- traffic is NOT going through Tor.",
                    style=constants.COLOR_PRIMARY,
                )
            )
        else:
            console.print(
                Text(
                    "✓ Tor is working. Any app pointed at the proxy above gets "
                    "the right-hand address; everything else keeps the left one.",
                    style=constants.COLOR_ACCENT,
                )
            )

    setup_logging("WARNING")
    asyncio.run(probe())


@app.command()
def banner(
    ascii_only: bool = typer.Option(
        False, "--ascii", help="Force the ASCII fallback banner."
    ),
) -> None:
    """Print the official startup banner."""
    result = render_banner(
        width=min(96, console.width - 2), max_height=20, prefer_png=not ascii_only
    )
    console.print(result.renderable)
    console.print(
        Text(constants.TAGLINE, style=f"italic {constants.COLOR_SURFACE}"),
        justify="center",
    )
    console.print(
        Text(f"({result.kind} banner)", style=constants.COLOR_SECONDARY),
        justify="center",
    )


@app.command()
def about() -> None:
    """Show version, developer and licence information."""
    _print_banner()
    table = Table(box=None, pad_edge=False, show_header=False)
    table.add_column(style=constants.COLOR_SURFACE)
    table.add_column(style=constants.COLOR_TEXT)
    table.add_row("Version", constants.VERSION)
    table.add_row("Developer", constants.DEVELOPER)
    table.add_row("GitHub", constants.GITHUB_URL)
    table.add_row("Email", constants.EMAIL)
    table.add_row("License", constants.LICENSE)
    console.print(table)


# --------------------------------------------------------------------------- #
# vpn
# --------------------------------------------------------------------------- #


@vpn_app.command("list")
def vpn_list() -> None:
    """List every imported VPN profile."""
    from nextron.vpn.profiles import ProfileLibrary

    setup_logging("WARNING")
    config = ConfigManager().load()
    library = ProfileLibrary()
    profiles = library.load()
    if not profiles:
        console.print(
            Text(
                "No VPN profiles imported yet. Add one with: nextron vpn import <file>",
                style=constants.COLOR_SURFACE,
            )
        )
        return

    table = Table(title="VPN profiles", box=None, pad_edge=False)
    table.add_column("")
    table.add_column("Name")
    table.add_column("Protocol")
    table.add_column("Endpoint")
    table.add_column("TCP")
    table.add_column("Used")
    table.add_column("Id")
    for profile in profiles:
        markers = []
        if profile.id == config.vpn.active_profile:
            markers.append("▸")
        if profile.favorite:
            markers.append("✦")
        if profile.id in config.vpn.shuffle_pool:
            markers.append("↻")
        table.add_row(
            " ".join(markers),
            profile.name,
            profile.protocol.label,
            profile.endpoint or "--",
            fmt.boolean(profile.tcp),
            str(profile.use_count),
            profile.id,
        )
    console.print(table)
    console.print(
        Text("▸ active   ✦ favourite   ↻ in rotation pool", style=constants.COLOR_SURFACE)
    )


@vpn_app.command("import")
def vpn_import(
    paths: list[Path] = typer.Argument(
        None, help="One or more .ovpn, .conf, .wgconf or .json profiles."
    ),
    name: str = typer.Option(
        None, "--name", "-n", help="Override the name (single file only)."
    ),
    activate: bool = typer.Option(
        False, "--activate", "-a", help="Make the imported profile active."
    ),
    browse: bool = typer.Option(
        False, "--browse", "-b", help="Open the desktop file browser."
    ),
) -> None:
    """Import VPN profiles. With no arguments, opens your file browser."""
    from nextron.vpn.profiles import ProfileLibrary

    setup_logging("WARNING")
    chosen = list(paths or [])
    if browse or not chosen:
        chosen = _browse(filedialog.VPN_FILTER, "NEXTRON -- import VPN profiles")
        if not chosen:
            return
    if name and len(chosen) > 1:
        _fail("--name can only be used with a single file")
        return

    library = ProfileLibrary()
    library.load()
    imported = []
    failed = False
    for path in chosen:
        try:
            # Into the drop-box first: the library mirrors that folder.
            stored = NextronRuntime.adopt_into_sources(path, paths.vpn_sources_dir())
            profile = library.import_file(stored, name=name)
        except NextronError as exc:
            console.print(Text(f"✗ {path.name}: {exc}", style=constants.COLOR_PRIMARY))
            failed = True
            continue
        imported.append(profile)
        _ok(f"Imported '{profile.name}' ({profile.protocol.label}, id {profile.id})")

    if imported and activate:
        manager = ConfigManager()
        manager.load()
        manager.config.vpn.active_profile = imported[0].id
        manager.save()
        _ok(f"'{imported[0].name}' is now the active profile")
    if failed and not imported:
        raise typer.Exit(code=1)


@vpn_app.command("remove")
def vpn_remove(
    needle: str = typer.Argument(..., help="Profile name or id."),
    force: bool = typer.Option(False, "--force", "-f", help="Do not ask."),
) -> None:
    """Delete a VPN profile from the library."""
    from nextron.vpn.profiles import ProfileLibrary

    setup_logging("WARNING")
    library = ProfileLibrary()
    library.load()
    profile = library.resolve(needle)
    if profile is None:
        _fail(f"No profile matches '{needle}'")
        return
    if not force and not typer.confirm(f"Delete '{profile.name}'?"):
        raise typer.Exit(code=1)
    library.delete(profile.id)
    _ok(f"Deleted '{profile.name}'")


@vpn_app.command("activate")
def vpn_activate(needle: str = typer.Argument(..., help="Profile name or id.")) -> None:
    """Set the active VPN profile."""
    from nextron.vpn.profiles import ProfileLibrary

    setup_logging("WARNING")
    library = ProfileLibrary()
    library.load()
    profile = library.resolve(needle)
    if profile is None:
        _fail(f"No profile matches '{needle}'")
        return
    manager = ConfigManager()
    manager.load()
    manager.config.vpn.active_profile = profile.id
    manager.save()
    _ok(f"Active profile: {profile.name}")


@vpn_app.command("pool")
def vpn_pool(
    names: list[str] = typer.Argument(
        None, help="Profiles for the rotation pool (empty means every profile)."
    ),
) -> None:
    """Set the VPN shuffle rotation pool."""
    from nextron.vpn.profiles import ProfileLibrary

    setup_logging("WARNING")
    library = ProfileLibrary()
    library.load()
    manager = ConfigManager()
    manager.load()

    resolved: list[str] = []
    for needle in names or []:
        profile = library.resolve(needle)
        if profile is None:
            _fail(f"No profile matches '{needle}'")
            return
        resolved.append(profile.id)

    manager.config.vpn.shuffle_pool = resolved
    manager.save()
    if resolved:
        _ok(f"Rotation pool: {len(resolved)} profile(s)")
    else:
        _ok("Rotation pool cleared -- every profile will be used")


# --------------------------------------------------------------------------- #
# dns
# --------------------------------------------------------------------------- #


@dns_app.command("list")
def dns_list() -> None:
    """List the blocklists in the library."""
    from nextron.dns.blocklist import BlocklistLibrary

    setup_logging("WARNING")
    config = ConfigManager().load()
    library = BlocklistLibrary()
    files = library.discover(list(config.dns.enabled_blocklists) or None)
    if not files:
        console.print(
            Text(
                f"No blocklists yet. Drop .txt/.hosts/.list files into "
                f"{paths.dns_dir()} or use: nextron dns import <file>",
                style=constants.COLOR_SURFACE,
            )
        )
        return

    table = Table(title="DNS Shield blocklists", box=None, pad_edge=False)
    table.add_column("On")
    table.add_column("List")
    table.add_column("Format")
    table.add_column("Size")
    for entry in files:
        table.add_row(
            "◉" if entry.enabled else "◌",
            entry.name,
            entry.format,
            f"{entry.size_bytes / 1024:.0f} KiB",
        )
    console.print(table)

    total = library.load(list(config.dns.enabled_blocklists) or None)
    console.print(
        Text(
            f"{total:,} domains blocked, {library.whitelist_count:,} whitelisted",
            style=constants.COLOR_ACCENT,
        )
    )


@dns_app.command("import")
def dns_import(
    paths: list[Path] = typer.Argument(
        None, help="One or more .txt, .hosts or .list blocklists."
    ),
    browse: bool = typer.Option(
        False, "--browse", "-b", help="Open the desktop file browser."
    ),
) -> None:
    """Import blocklists and enable them. With no arguments, opens your file browser."""
    from nextron.dns.blocklist import BlocklistLibrary

    setup_logging("WARNING")
    chosen = list(paths or [])
    if browse or not chosen:
        chosen = _browse(filedialog.BLOCKLIST_FILTER, "NEXTRON -- import blocklists")
        if not chosen:
            return

    library = BlocklistLibrary()
    library.discover()
    manager = ConfigManager()
    manager.load()
    enabled = list(manager.config.dns.enabled_blocklists)

    imported = 0
    failed = False
    for path in chosen:
        try:
            stored = NextronRuntime.adopt_into_sources(path, paths.dns_sources_dir())
            entry = library.import_file(stored)
        except NextronError as exc:
            console.print(Text(f"✗ {path.name}: {exc}", style=constants.COLOR_PRIMARY))
            failed = True
            continue
        if entry.name not in enabled:
            enabled.append(entry.name)
        imported += 1
        _ok(f"Imported '{entry.name}' ({entry.domains:,} domains) and enabled it")

    if imported:
        manager.config.dns.enabled_blocklists = enabled
        manager.save()
        total = library.load(enabled)
        _ok(f"{total:,} domains blocked in total")
    if failed and not imported:
        raise typer.Exit(code=1)


@dns_app.command("sources")
def dns_sources(
    show_all: bool = typer.Option(
        False, "--all", "-a", help="Include the disabled sources."
    ),
) -> None:
    """List the blocklist sources in the catalogue."""
    from nextron.dns.sources import SourceCatalogue

    setup_logging("WARNING")
    catalogue = SourceCatalogue()
    catalogue.load()
    if not catalogue.exists:
        _fail(
            "No catalogue yet. Put a sources.txt in "
            f"{paths.dns_dir()} to download lists from it."
        )
        return

    table = Table(title="Blocklist sources", box=None, pad_edge=False)
    table.add_column("On")
    table.add_column("Source")
    table.add_column("Note", overflow="fold")
    for source in catalogue:
        if not source.enabled and not show_all:
            continue
        table.add_row(
            "◉" if source.enabled else "◌",
            fmt.truncate(source.url, 72),
            source.note or "",
        )
    console.print(table)
    console.print(
        Text(
            f"{len(catalogue.enabled())} enabled of {len(catalogue)} total"
            + ("" if show_all else "   (--all shows the disabled ones)"),
            style=constants.COLOR_SURFACE,
        )
    )


@dns_app.command("update")
def dns_update(
    concurrency: int = typer.Option(
        8, "--jobs", "-j", min=1, max=32, help="Parallel downloads."
    ),
    timeout: float = typer.Option(45.0, "--timeout", help="Seconds per download."),
) -> None:
    """Download every enabled source and convert it into a blocklist."""
    from nextron.dns.blocklist import BlocklistLibrary
    from nextron.dns.sources import SourceCatalogue, SourceFetcher

    setup_logging("WARNING")
    catalogue = SourceCatalogue()
    catalogue.load()
    sources = catalogue.enabled()
    if not sources:
        _fail(
            "No enabled sources. Drop your lists into "
            f"{paths.dns_sources_dir()}, or put a sources.txt catalogue in "
            f"{paths.dns_dir()} to download from."
        )
        return

    console.print(
        Text(
            f"Downloading {len(sources)} blocklist(s) with {concurrency} parallel "
            "requests...\n",
            style=constants.COLOR_SURFACE,
        )
    )

    def progress(done: int, total: int, outcome) -> None:
        style = (
            constants.COLOR_ACCENT
            if outcome.ok
            else constants.COLOR_SECONDARY
            if outcome.not_applicable
            else constants.COLOR_SURFACE
        )
        console.print(
            Text(f"  [{done:>3}/{total}] {outcome.marker} {outcome.label}", style=style)
        )

    fetcher = SourceFetcher(concurrency=concurrency, timeout=timeout)
    report = asyncio.run(fetcher.fetch_all(sources, progress=progress))

    manager = ConfigManager()
    manager.load()
    enabled = list(manager.config.dns.enabled_blocklists)
    for outcome in report.succeeded:
        if outcome.path and outcome.path.name not in enabled:
            enabled.append(outcome.path.name)
    manager.config.dns.enabled_blocklists = enabled
    manager.save()

    library = BlocklistLibrary()
    total = library.load(enabled)

    console.print()
    _ok(f"{report.summary} in {report.duration_seconds:.1f}s")
    _ok(f"{total:,} domains are now blocked across {len(library.files)} list(s)")
    if report.not_applicable:
        console.print(
            Text(
                f"{len(report.not_applicable)} list(s) hold only cosmetic rules, "
                "which DNS cannot enforce -- they were skipped, not failed.",
                style=constants.COLOR_SURFACE,
            )
        )
    if report.failed:
        console.print(
            Text(
                f"{len(report.failed)} source(s) unreachable:",
                style=constants.COLOR_SURFACE,
            )
        )
        for outcome in report.failed[:10]:
            console.print(
                Text(
                    f"  ✗ {outcome.source.host}: {outcome.error}",
                    style=constants.COLOR_SURFACE,
                )
            )


@dns_app.command("whitelist")
def dns_whitelist(
    domain: str = typer.Argument(None, help="Domain to add; omit to list the whitelist.")
) -> None:
    """Show or extend the DNS Shield whitelist."""
    from nextron.dns.blocklist import BlocklistLibrary

    setup_logging("WARNING")
    library = BlocklistLibrary()
    library.load_whitelist()

    if domain is None:
        entries = library.whitelist()
        if not entries:
            console.print(Text("The whitelist is empty.", style=constants.COLOR_SURFACE))
            return
        for entry in entries:
            console.print(Text(entry, style=constants.COLOR_TEXT))
        return

    try:
        added = library.whitelist_add(domain)
    except NextronError as exc:
        _fail(str(exc))
        return
    _ok(f"{domain} {'added to' if added else 'already in'} the whitelist")


@dns_app.command("check")
def dns_check(domain: str = typer.Argument(..., help="Domain to test.")) -> None:
    """Report whether a domain would be blocked by the enabled lists."""
    from nextron.dns.blocklist import BlocklistLibrary

    setup_logging("WARNING")
    config = ConfigManager().load()
    library = BlocklistLibrary()
    library.load(list(config.dns.enabled_blocklists) or None)

    if library.is_whitelisted(domain):
        _ok(f"{domain} is whitelisted (never blocked)")
    elif library.is_blocked(domain):
        console.print(
            Text(f"✗ {domain} would be BLOCKED", style=f"bold {constants.COLOR_PRIMARY}")
        )
    else:
        _ok(f"{domain} would be allowed")


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #


@config_app.command("path")
def config_path() -> None:
    """Print where NEXTRON keeps its local data."""
    table = Table(box=None, pad_edge=False, show_header=False)
    table.add_column(style=constants.COLOR_SURFACE)
    table.add_column(style=constants.COLOR_TEXT)
    for label, path in (
        ("config", paths.config_file()),
        ("drop VPN here", paths.vpn_sources_dir()),
        ("drop lists here", paths.dns_sources_dir()),
        ("profiles", paths.profiles_dir()),
        ("blocklists", paths.dns_dir()),
        ("whitelist", paths.whitelist_file()),
        ("database", paths.database_file()),
        ("logs", paths.logs_dir()),
        ("runtime", paths.runtime_dir()),
    ):
        table.add_row(label, str(path))
    console.print(table)


@config_app.command("show")
def config_show() -> None:
    """Print the effective configuration as TOML."""
    console.print(ConfigManager().load().to_toml())


@config_app.command("reset")
def config_reset(
    force: bool = typer.Option(False, "--force", "-f", help="Do not ask.")
) -> None:
    """Restore the default configuration (profiles and lists are untouched)."""
    from nextron.core.config import NextronConfig

    if not force and not typer.confirm(
        f"Overwrite {paths.config_file()} with defaults?"
    ):
        raise typer.Exit(code=1)
    manager = ConfigManager()
    manager.replace(NextronConfig())
    _ok(f"Defaults written to {paths.config_file()}")


def entrypoint() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":  # pragma: no cover
    entrypoint()
