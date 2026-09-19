"""The NEXTRON runtime -- the single object that owns every engine.

Both front ends (the Textual TUI and the Typer CLI) drive this class, so the
behaviour of ``nextron connect`` and pressing *Enter* on the mode screen is
guaranteed to be identical.

Lifecycle::

    runtime = NextronRuntime()
    await runtime.start()            # storage, libraries, stale-rule cleanup
    await runtime.connect(mode)      # routing sequence + verification
    ...
    await runtime.shutdown()         # reverse teardown, always safe to call
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from nextron.core import constants, process
from nextron.core.config import (
    ConfigManager,
    NextronConfig,
    RoutingMode,
    ShuffleAlgorithm,
)
from nextron.core.context import EngineContext
from nextron.core.events import EventBus, EventType
from nextron.core.exceptions import NextronError
from nextron.core.verification import VerificationReport
from nextron.dns.shield import DNSShield
from nextron.dns.sources import FetchReport, SourceCatalogue, SourceFetcher
from nextron.routing.manager import RoutingManager
from nextron.scheduler.tor_scheduler import TorScheduler
from nextron.scheduler.vpn_scheduler import VPNScheduler
from nextron.state.manager import AppState, ServiceStatus, StateManager
from nextron.storage import paths
from nextron.storage.database import Database
from nextron.tor.engine import TorEngine
from nextron.utils.logging import setup_logging
from nextron.vpn.manager import VPNEngine
from nextron.vpn.profiles import VPNProfile

log = logging.getLogger(__name__)

__all__ = ["NextronRuntime"]

#: How often the watchdog re-checks tunnel health.
_WATCHDOG_INTERVAL = 45.0
#: How often the full verification checklist is re-run while connected.
_REVALIDATE_EVERY = 8


class NextronRuntime:
    """Own the whole application: configuration, engines and schedulers."""

    def __init__(
        self, *, config_path: Path | None = None, console_logs: bool = False
    ) -> None:
        self.config_manager = ConfigManager(config_path)
        self.config_manager.load()
        setup_logging(self.config_manager.config.log_level, console=console_logs)

        self.bus = EventBus()
        self.state_manager = StateManager(
            self.bus, routing_mode=self.config_manager.config.routing_mode
        )
        self.database = Database()
        self.context = EngineContext(
            config_manager=self.config_manager,
            bus=self.bus,
            state=self.state_manager,
            database=self.database,
        )

        self.tor = TorEngine(self.context)
        self.vpn = VPNEngine(self.context)
        self.dns = DNSShield(self.context)
        self.routing = RoutingManager(self.context, self.tor, self.vpn, self.dns)

        self.tor_scheduler = TorScheduler(self.context, self.tor)
        self.vpn_scheduler = VPNScheduler(self.context, self.vpn)

        self._watchdog: asyncio.Task[None] | None = None
        self._started = False
        self._shutting_down = False

    # -- properties --------------------------------------------------------- #

    @property
    def config(self) -> NextronConfig:
        return self.config_manager.config

    @property
    def state(self) -> AppState:
        return self.state_manager.state

    @property
    def mode(self) -> RoutingMode:
        return self.routing.mode or self.config.routing_mode

    @property
    def connected(self) -> bool:
        return self.routing.active and self.state.route_status is ServiceStatus.ACTIVE

    # -- lifecycle ---------------------------------------------------------- #

    async def start(self) -> None:
        """Prepare storage and libraries; does not establish any route."""
        if self._started:
            return

        self.state_manager.update(privileged=process.is_root())
        try:
            await self.database.connect()
            await self.database.start_session(
                self.config.routing_mode.value, constants.VERSION
            )
        except NextronError as exc:
            log.warning("Statistics disabled: %s", exc)

        self.vpn.load_library()
        self.import_from_sources()
        self._show_selected_profile()
        await self.routing.cleanup_stale_rules()

        self._started = True
        self.bus.emit(
            EventType.APP_READY,
            f"{constants.APP_NAME} {constants.VERSION} ready",
            privileged=process.is_root(),
        )
        log.info(
            "%s %s started (%s)",
            constants.APP_NAME,
            constants.VERSION,
            "root" if process.is_root() else "unprivileged",
        )

    async def shutdown(self) -> None:
        """Tear everything down in reverse order. Safe to call twice."""
        if self._shutting_down:
            return
        self._shutting_down = True
        log.info("Shutting down")

        self._stop_watchdog()
        await asyncio.gather(
            self.tor_scheduler.stop(), self.vpn_scheduler.stop(), return_exceptions=True
        )
        try:
            await self.routing.teardown()
        except Exception:  # pragma: no cover - shutdown must not raise
            log.exception("Routing teardown failed")

        try:
            await self.database.end_session()
            await self.database.close()
        except Exception:  # pragma: no cover
            log.debug("Database shutdown issue", exc_info=True)

        self.bus.emit(EventType.APP_SHUTDOWN, "NEXTRON stopped")
        await self.bus.drain()
        self._started = False
        self._shutting_down = False

    async def update_blocklists(self, *, progress=None) -> FetchReport:
        """Download every enabled catalogue source and reload the Shield."""
        catalogue = SourceCatalogue()
        catalogue.load()
        sources = catalogue.enabled()
        if not sources:
            self.context.activity(
                "No blocklist sources are enabled; add lists under "
                "Sources/DNS list, or a sources.txt catalogue to download from",
                level="warning",
            )
            return FetchReport(finished_at=datetime.now())

        self.context.activity(f"Downloading {len(sources)} blocklist(s)...")
        report = await SourceFetcher().fetch_all(sources, progress=progress)
        self.context.activity(f"Blocklist update: {report.summary}")

        # Enable everything that was just written, then reload the merged set.
        written = [outcome.path.name for outcome in report.succeeded if outcome.path]
        if written:
            enabled = list(self.config.dns.enabled_blocklists)
            for name in written:
                if name not in enabled:
                    enabled.append(name)
            self.config.dns.enabled_blocklists = enabled
            self.config_manager.save()
            await self.dns.reload_lists()
        return report

    def _show_selected_profile(self) -> None:
        """Put the configured profile on the dashboard before connecting."""
        profile = self.vpn.library.get(self.config.vpn.active_profile)
        if profile is None:
            candidates = self.vpn.library.favorites() or self.vpn.library.all()
            profile = candidates[0] if candidates else None
        if profile is not None:
            self.state_manager.update_vpn(
                profile_id=profile.id,
                profile_name=profile.name,
                protocol=profile.protocol.label,
                endpoint=profile.endpoint,
            )

    # -- the Sources drop-box ------------------------------------------------ #

    def import_from_sources(self) -> tuple[int, int]:
        """Make the library match ``Sources/`` exactly. Returns ``(vpn, dns)``.

        A mirror, not an import: a file added to the drop-box appears, a file
        removed from it disappears, and a file edited there is re-read. The
        folder the user manages is the only thing that decides what NEXTRON
        holds, so there is never a copy left behind that they cannot see.

        Cheap enough to run on startup and whenever a library screen opens.
        """
        return self._sync_vpn_sources(), self._sync_dns_sources()

    # -- VPN profiles -------------------------------------------------------- #

    def _vpn_source_files(self) -> dict[str, Path]:
        """Every importable file in the drop-box, keyed by its contents."""
        from nextron.vpn.profiles import fingerprint

        found: dict[str, Path] = {}
        for path in sorted(paths.vpn_sources_dir().glob("*")):
            if (
                not path.is_file()
                or path.suffix.lower() not in constants.VPN_IMPORT_SUFFIXES
            ):
                continue
            try:
                found.setdefault(
                    fingerprint(path.read_text(encoding="utf-8", errors="replace")),
                    path,
                )
            except OSError as exc:  # pragma: no cover - unreadable drop-box file
                self.context.activity(
                    f"Sources: cannot read {path.name} ({exc})", level="warning"
                )
        return found

    def _sync_vpn_sources(self) -> int:
        """Import what is new in the drop-box and drop what has left it."""
        from nextron.core.exceptions import NextronError
        from nextron.vpn.profiles import fingerprint

        wanted = self._vpn_source_files()
        held: dict[str, object] = {}
        for profile in self.vpn.library.all():
            try:
                held[fingerprint(profile.read_config())] = profile
            except NextronError:  # pragma: no cover - unreadable profile
                continue

        changed = 0

        for stamp, path in wanted.items():
            if stamp in held:
                continue
            try:
                profile = self.vpn.library.import_file(path)
            except NextronError as exc:
                self.context.activity(
                    f"Sources: {path.name} skipped ({exc})", level="warning"
                )
                continue
            changed += 1
            self.context.activity(f"Sources: imported VPN profile '{profile.name}'")

        for stamp, profile in held.items():
            if stamp in wanted:
                continue
            if self._forget_profile(profile):
                changed += 1

        if changed:
            self.vpn.refresh_pool()
            self.state_manager.update_vpn(pool_size=self.vpn.shuffle.pool_size)
        return changed

    def _forget_profile(self, profile) -> bool:
        """Drop a profile whose file has left the drop-box, and every trace.

        A connected profile is left alone: pulling the configuration out from
        under a live tunnel would be a worse surprise than a stale row.
        """
        from nextron.core.exceptions import NextronError

        current = self.vpn.current
        if current is not None and current.id == profile.id:
            self.context.activity(
                f"Sources: '{profile.name}' was removed from the drop-box but is "
                "connected; it will go when you disconnect",
                level="warning",
            )
            return False

        try:
            self.vpn.library.delete(profile.id)
        except NextronError as exc:  # pragma: no cover - unwritable library
            self.context.activity(
                f"Sources: cannot remove '{profile.name}' ({exc})", level="warning"
            )
            return False

        settings = self.config.vpn
        if settings.active_profile == profile.id:
            settings.active_profile = None
        settings.shuffle_pool = [
            pid for pid in settings.shuffle_pool if pid != profile.id
        ]
        self.config_manager.save()
        self.context.activity(
            f"Sources: '{profile.name}' removed (no longer in VPN Profiles)"
        )
        return True

    # -- blocklists ---------------------------------------------------------- #

    @staticmethod
    def _is_url_catalogue(path: Path) -> bool:
        """True when a dropped file lists blocklist *addresses*, not domains.

        People collect both kinds in the same folder. A file of ``https://``
        lines is a download catalogue -- importing it as a blocklist would add
        a list that blocks nothing at all.
        """
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False

        urls = content = 0
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith(("!", "#")):
                continue
            content += 1
            if line.split()[0].startswith(("http://", "https://")):
                urls += 1
            if content >= 200:
                break
        return content > 0 and urls >= content * 0.8

    def _merge_catalogue(self, path: Path) -> int:
        """Fold a dropped catalogue of URLs into the download list.

        Returns the number of addresses added. Existing entries -- including
        ones the user disabled -- are kept exactly as they are.
        """
        from nextron.dns.sources import BlocklistSource, SourceCatalogue

        catalogue = SourceCatalogue()
        catalogue.load()
        known = {source.url for source in catalogue}

        dropped = SourceCatalogue(path)
        added = [
            BlocklistSource(source.url, True, source.note)
            for source in dropped.load()
            if source.url not in known
        ]
        if added:
            catalogue.replace(catalogue.all() + added)
        return len(added)

    def _downloaded_names(self) -> set[str]:
        """Blocklists that came off the network, not out of the drop-box.

        They are named after their catalogue address, so the catalogue tells us
        which files ``U`` produced. Those are NEXTRON's to keep; everything
        else in the blocklist directory belongs to ``Sources/DNS list``.
        """
        from nextron.dns.sources import SourceCatalogue

        catalogue = SourceCatalogue()
        catalogue.load()
        return {source.filename for source in catalogue}

    def _sync_dns_sources(self) -> int:
        """Mirror ``Sources/DNS list``: import what is new, drop what has left."""
        from nextron.core.exceptions import NextronError

        enabled = list(self.config.dns.enabled_blocklists)
        held = {entry.name for entry in self.dns.library.discover()}
        downloaded = self._downloaded_names()
        wanted: set[str] = set()
        changed = 0

        for path in sorted(paths.dns_sources_dir().glob("*")):
            if (
                not path.is_file()
                or path.suffix.lower() not in constants.BLOCKLIST_SUFFIXES
            ):
                continue

            if self._is_url_catalogue(path):
                added = self._merge_catalogue(path)
                if added:
                    changed += 1
                    self.context.activity(
                        f"Sources: '{path.name}' holds {added:,} blocklist "
                        "address(es) -- press D then U to download them"
                    )
                continue

            wanted.add(path.name)
            if path.name in held:
                continue
            try:
                entry = self.dns.library.import_file(path)
            except NextronError as exc:
                self.context.activity(
                    f"Sources: {path.name} skipped ({exc})", level="warning"
                )
                continue
            changed += 1
            if entry.name not in enabled:
                enabled.append(entry.name)
            self.context.activity(
                f"Sources: imported blocklist '{entry.name}' "
                f"({entry.domains:,} domains)"
            )

        for name in sorted(held - wanted - downloaded):
            try:
                self.dns.library.remove(name)
            except NextronError as exc:  # pragma: no cover - unwritable directory
                self.context.activity(
                    f"Sources: cannot remove '{name}' ({exc})", level="warning"
                )
                continue
            changed += 1
            enabled = [item for item in enabled if item != name]
            self.context.activity(
                f"Sources: '{name}' removed (no longer in DNS list)"
            )

        if changed:
            self.config.dns.enabled_blocklists = enabled
            self.config_manager.save()
        return changed

    # -- routing ------------------------------------------------------------ #

    def select_mode(self, mode: RoutingMode) -> RoutingMode:
        """Choose the mode to connect next. Starts nothing."""
        self.config.routing_mode = mode
        self.config_manager.save()
        if not self.routing.active:
            self.state_manager.update(routing_mode=mode)
        self.context.activity(f"Routing mode selected: {mode.label}")
        return mode

    async def connect(
        self,
        mode: RoutingMode | None = None,
        *,
        vpn_profile: str | None = None,
        start_schedulers: bool = True,
    ) -> VerificationReport:
        """Establish a routing mode, then start the configured schedulers."""
        target = mode or self.config.routing_mode
        report = await self.routing.switch_mode(target, vpn_profile=vpn_profile)
        self.state_manager.reset_session()

        if start_schedulers:
            await self._start_schedulers_for(target)
        self._start_watchdog()
        return report

    async def disconnect(self) -> None:
        """Stop the schedulers and tear the route down."""
        self._stop_watchdog()
        await asyncio.gather(
            self.tor_scheduler.stop(), self.vpn_scheduler.stop(), return_exceptions=True
        )
        await self.routing.teardown()

    async def switch_mode(
        self, mode: RoutingMode, *, vpn_profile: str | None = None
    ) -> VerificationReport:
        """Change routing mode from a live session."""
        await self.disconnect()
        return await self.connect(mode, vpn_profile=vpn_profile)

    async def _start_schedulers_for(self, mode: RoutingMode) -> None:
        if mode.uses_tor and self.config.tor.rotation_enabled:
            await self.tor_scheduler.start()
        if (
            mode.uses_vpn
            and self.config.vpn.shuffle_enabled
            and self.vpn.shuffle.pool_size > 1
        ):
            await self.vpn_scheduler.start()

    # -- Tor actions -------------------------------------------------------- #

    async def rotate_now(self) -> bool:
        """Rotate the Tor identity immediately (Space in the TUI)."""
        if not self.tor.running:
            self.context.activity(
                "Tor is not running; nothing to rotate", level="warning"
            )
            return False
        if self.tor_scheduler.running:
            # Fire through the scheduler so the countdown restarts cleanly.
            self.tor_scheduler.trigger()
            return True
        return await self.tor.rotate(reason="manual")

    async def toggle_tor_rotation(self) -> bool:
        """Toggle the Tor scheduler (R in the TUI)."""
        if not self.mode.uses_tor:
            self.context.activity(
                f"{self.mode.label} does not use Tor", level="warning"
            )
            return False
        return await self.tor_scheduler.toggle()

    def set_tor_interval(self, seconds: int) -> int:
        return self.tor_scheduler.set_interval(seconds)

    # -- VPN actions -------------------------------------------------------- #

    async def toggle_vpn_shuffle(self) -> bool:
        """Toggle the VPN shuffle scheduler."""
        if not self.mode.uses_vpn:
            self.context.activity(
                f"{self.mode.label} does not use a VPN", level="warning"
            )
            return False
        if self.vpn.shuffle.pool_size < 2 and not self.vpn_scheduler.running:
            self.context.activity(
                "Add at least two profiles to the rotation pool first",
                level="warning",
            )
            return False
        return await self.vpn_scheduler.toggle()

    def set_vpn_interval(self, seconds: int) -> int:
        return self.vpn_scheduler.set_interval(seconds)

    def set_shuffle_algorithm(self, algorithm: ShuffleAlgorithm) -> None:
        self.config.vpn.shuffle_algorithm = algorithm
        self.vpn.shuffle.set_algorithm(algorithm)
        self.state_manager.update_vpn(shuffle_algorithm=algorithm)
        self.config_manager.save()
        self.context.activity(f"Shuffle algorithm: {algorithm.label}")

    async def switch_profile(self, profile: VPNProfile | str) -> VPNProfile | None:
        """Activate a specific VPN profile now."""
        if not self.mode.uses_vpn:
            self.context.activity(
                f"{self.mode.label} does not use a VPN", level="warning"
            )
            return None
        return await self.vpn.switch(profile, reason="manual")

    def set_active_profile(self, profile: VPNProfile) -> None:
        self.config.vpn.active_profile = profile.id
        self.config_manager.save()
        # Show it straight away: a selection that changes nothing on screen
        # looks like it did not register.
        if self.vpn.current is None:
            self.state_manager.update_vpn(
                profile_id=profile.id,
                profile_name=profile.name,
                protocol=profile.protocol.label,
                endpoint=profile.endpoint,
            )
        self.context.activity(f"Active profile set to '{profile.name}'")

    def set_shuffle_pool(self, profile_ids: list[str]) -> None:
        self.config.vpn.shuffle_pool = list(profile_ids)
        self.config_manager.save()
        self.vpn.refresh_pool()
        self.context.activity(
            f"Rotation pool: {self.vpn.shuffle.pool_size} profile(s)"
        )

    def import_vpn_profile(self, source: Path) -> VPNProfile:
        """Import a profile by copying it into the drop-box first.

        The library mirrors ``Sources/VPN Profiles``, so anything imported from
        elsewhere has to land there too -- otherwise the next scan would see a
        profile with no file behind it and remove it again.
        """
        stored = self.adopt_into_sources(Path(source), paths.vpn_sources_dir())
        profile = self.vpn.library.import_file(stored)
        self.vpn.refresh_pool()
        self.context.activity(
            f"Imported {profile.protocol.label} profile '{profile.name}'"
        )
        return profile

    @staticmethod
    def adopt_into_sources(source: Path, folder: Path) -> Path:
        """Copy *source* into the drop-box, without overwriting anything.

        Returns the path of the copy, or of the existing file when one there
        already has exactly these contents.
        """
        import shutil

        from nextron.core.exceptions import NextronError
        from nextron.vpn.profiles import fingerprint

        source = Path(source).expanduser()
        if not source.is_file():
            raise NextronError(f"No such file: {source}")
        folder.mkdir(parents=True, exist_ok=True)
        if source.parent.resolve() == folder.resolve():
            return source

        try:
            stamp = fingerprint(source.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            raise NextronError(f"Cannot read {source}: {exc}") from exc

        target = folder / source.name
        counter = 2
        while target.exists():
            try:
                existing = fingerprint(
                    target.read_text(encoding="utf-8", errors="replace")
                )
            except OSError:  # pragma: no cover - unreadable drop-box file
                existing = None
            if existing == stamp:
                return target
            target = folder / f"{source.stem} ({counter}){source.suffix}"
            counter += 1

        try:
            shutil.copyfile(source, target)
        except OSError as exc:
            raise NextronError(f"Cannot copy into {folder}: {exc}") from exc
        return target

    # -- DNS actions -------------------------------------------------------- #

    async def toggle_dns_shield(self) -> bool:
        """Toggle the DNS Shield (D in the TUI) -- works in every mode."""
        tor_port = self.tor.dns_port if self.tor.running else None
        try:
            return await self.dns.toggle(tor_dns_port=tor_port)
        except NextronError as exc:
            self.context.failure(f"DNS Shield: {exc}")
            return self.dns.running

    # -- health ------------------------------------------------------------- #

    def _start_watchdog(self) -> None:
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = asyncio.create_task(
                self._watchdog_loop(), name="health-watchdog"
            )

    def _stop_watchdog(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None

    async def _watchdog_loop(self) -> None:
        """Keep a long-running session honest: re-check the tunnel and re-verify."""
        ticks = 0
        try:
            while True:
                await asyncio.sleep(_WATCHDOG_INTERVAL)
                ticks += 1
                if self.routing.busy:
                    continue

                if self.mode.uses_vpn and self.vpn.current is not None:
                    await self.vpn.watchdog()
                if self.mode.uses_tor and self.tor.running:
                    await self.tor.refresh(fetch_ip=False)
                if ticks % _REVALIDATE_EVERY == 0:
                    await self.routing.revalidate()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover
            log.exception("Health watchdog stopped")
