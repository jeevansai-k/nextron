"""DNS Shield -- an independent protection layer.

The Shield is *not* a routing mode: it works with all four of them, and it can
be toggled on and off at any time without touching the tunnel. It owns

* the blocklist / whitelist library,
* the local resolver and its listener,
* ``/etc/resolv.conf`` takeover (so the whole system is protected, not just
  one application),
* upstream selection: Tor's DNSPort whenever Tor is up, which is what stops
  DNS from leaking around the tunnel.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from nextron.core import constants, process
from nextron.core.context import EngineContext
from nextron.core.events import EventType
from nextron.core.exceptions import DNSError
from nextron.dns.blocklist import BlocklistLibrary
from nextron.dns.resolver import DNSResolver, DNSServer
from nextron.state.manager import ServiceStatus
from nextron.storage import paths

log = logging.getLogger(__name__)

__all__ = ["DNSShield"]

_RESOLV_CONF = Path("/etc/resolv.conf")
_BACKUP_NAME = "resolv.conf.nextron-backup"
_STATS_FLUSH_SECONDS = 30.0


class DNSShield:
    """Ad, tracker and malware blocking with a local resolver."""

    def __init__(self, context: EngineContext) -> None:
        self._ctx = context
        settings = context.config.dns

        self.library = BlocklistLibrary()
        self._resolver = DNSResolver(
            self.library,
            upstream=settings.upstream,
            cache_ttl=settings.cache_ttl,
            block_ipv6=settings.block_ipv6_answers,
            on_blocked=self._on_blocked,
        )
        self._server = DNSServer(
            self._resolver, settings.listen_host, settings.listen_port
        )
        self._resolv_managed = False
        self._flush_task: asyncio.Task[None] | None = None
        self._flushed_queries = 0
        self._flushed_blocked = 0
        self._tor_dns_port: int | None = None

    # -- properties --------------------------------------------------------- #

    @property
    def settings(self):
        return self._ctx.config.dns

    @property
    def running(self) -> bool:
        return self._server.running

    @property
    def resolver(self) -> DNSResolver:
        return self._resolver

    @property
    def address(self) -> str:
        return self._server.address

    # -- lifecycle ---------------------------------------------------------- #

    async def start(self, *, tor_dns_port: int | None = None) -> None:
        """Load the lists, bind the resolver and take over system DNS."""
        if self.running:
            await self.reload_lists()
            self.use_tor_dns(tor_dns_port)
            return

        state = self._ctx.state
        state.update_dns(status=ServiceStatus.STARTING, error=None)
        self._ctx.activity("Starting DNS Shield")

        blocked = await asyncio.to_thread(
            self.library.load, list(self.settings.enabled_blocklists) or None
        )
        self.use_tor_dns(tor_dns_port)

        # Rebuild the listener from the *current* settings: the host and port
        # may have been changed in the Settings screen since construction.
        self._server = DNSServer(
            self._resolver, self.settings.listen_host, self.settings.listen_port
        )

        try:
            port = await self._server.start(fallback_port=self.settings.fallback_port)
        except OSError as exc:
            state.update_dns(status=ServiceStatus.ERROR, error=str(exc))
            raise DNSError(str(exc)) from exc

        if port != self.settings.listen_port:
            self._ctx.activity(
                f"DNS Shield fell back to port {port} "
                f"(port {self.settings.listen_port} needs root)",
                level="warning",
            )

        if self.settings.manage_resolv_conf and port == constants.DNS_SHIELD_PORT:
            await self._take_over_resolv_conf()

        self._start_flusher()
        state.update_dns(
            status=ServiceStatus.ACTIVE,
            listen=self._server.address,
            upstream=self._resolver.upstream,
            using_tor_dns=self._tor_dns_port is not None,
            blocked_domains=blocked,
            whitelisted_domains=self.library.whitelist_count,
            active_blocklists=self.library.active_names,
            resolv_conf_managed=self._resolv_managed,
            error=None,
        )
        self._ctx.bus.emit(
            EventType.DNS_ENABLED,
            f"DNS Shield active on {self._server.address} "
            f"({blocked:,} domains blocked)",
            listen=self._server.address,
            blocked=blocked,
        )

    async def stop(self) -> None:
        """Restore system DNS and stop the listener."""
        if not self.running:
            self._ctx.state.update_dns(status=ServiceStatus.DISABLED)
            return

        self._ctx.state.update_dns(status=ServiceStatus.STOPPING)
        await self._flush_stats()
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except (asyncio.CancelledError, Exception):
                pass
            self._flush_task = None

        await self._restore_resolv_conf()
        await self._server.stop()
        self._ctx.state.update_dns(
            status=ServiceStatus.DISABLED,
            listen=None,
            using_tor_dns=False,
            resolv_conf_managed=False,
        )
        self._ctx.bus.emit(EventType.DNS_DISABLED, "DNS Shield disabled")

    async def toggle(self, *, tor_dns_port: int | None = None) -> bool:
        """Flip the Shield on or off. Returns the new enabled state."""
        if self.running:
            await self.stop()
            self._ctx.config.dns.enabled = False
        else:
            await self.start(tor_dns_port=tor_dns_port)
            self._ctx.config.dns.enabled = True
        self._ctx.save_config()
        return self.running

    # -- upstream ----------------------------------------------------------- #

    def use_tor_dns(self, port: int | None) -> None:
        """Send upstream queries through Tor's DNSPort when it is available."""
        if port is not None and self.settings.prefer_tor_dns:
            self._tor_dns_port = port
            self._resolver.set_upstream(["127.0.0.1"], port)
        else:
            self._tor_dns_port = None
            self._resolver.set_upstream(list(self.settings.upstream), 53)
        self._ctx.state.update_dns(
            upstream=self._resolver.upstream,
            using_tor_dns=self._tor_dns_port is not None,
        )

    # -- lists -------------------------------------------------------------- #

    async def reload_lists(self) -> int:
        """Re-parse every enabled blocklist plus the whitelist."""
        blocked = await asyncio.to_thread(
            self.library.load, list(self.settings.enabled_blocklists) or None
        )
        self._resolver.clear_cache()
        self._ctx.state.update_dns(
            blocked_domains=blocked,
            whitelisted_domains=self.library.whitelist_count,
            active_blocklists=self.library.active_names,
        )
        self._ctx.bus.emit(
            EventType.DNS_RELOADED,
            f"Blocklists reloaded ({blocked:,} domains)",
            blocked=blocked,
        )
        return blocked

    async def import_blocklist(self, source: Path) -> str:
        """Import a list, by way of ``Sources/DNS list``.

        The library mirrors that folder, so a list imported from anywhere else
        has to be copied in or the next scan would remove it again.
        """
        from nextron.core.runtime import NextronRuntime
        from nextron.storage import paths

        stored = await asyncio.to_thread(
            NextronRuntime.adopt_into_sources, Path(source), paths.dns_sources_dir()
        )
        entry = await asyncio.to_thread(self.library.import_file, stored)
        enabled = list(self.settings.enabled_blocklists)
        if entry.name not in enabled:
            enabled.append(entry.name)
            self.settings.enabled_blocklists = enabled
            self._ctx.save_config()
        await self.reload_lists()
        return entry.name

    async def set_blocklist_enabled(self, name: str, enabled: bool) -> None:
        self.library.set_enabled(name, enabled)
        selected = list(self.settings.enabled_blocklists)
        if enabled and name not in selected:
            selected.append(name)
        elif not enabled and name in selected:
            selected.remove(name)
        self.settings.enabled_blocklists = selected
        self._ctx.save_config()
        await self.reload_lists()

    async def remove_blocklist(self, name: str) -> None:
        self.library.remove(name)
        selected = [n for n in self.settings.enabled_blocklists if n != name]
        self.settings.enabled_blocklists = selected
        self._ctx.save_config()
        await self.reload_lists()

    async def whitelist_add(self, domain: str) -> bool:
        added = self.library.whitelist_add(domain)
        if added:
            self._resolver.clear_cache()
            self._ctx.state.update_dns(
                whitelisted_domains=self.library.whitelist_count,
                blocked_domains=self.library.blocked_count,
            )
            self._ctx.activity(f"Whitelisted {domain}")
        return added

    async def whitelist_remove(self, domain: str) -> bool:
        removed = self.library.whitelist_remove(domain)
        if removed:
            await self.reload_lists()
        return removed

    # -- verification ------------------------------------------------------- #

    @property
    def listen_host(self) -> str:
        """The address the listener is actually bound to."""
        return self.settings.listen_host

    @property
    def listen_port(self) -> int:
        """The port the listener actually bound to (may be the fallback)."""
        return self._server.port

    async def self_test(self) -> tuple[bool, str]:
        """Resolve a known-good name through our own listener."""
        if not self.running:
            return False, "the Shield is not running"
        from nextron.utils import net

        answers = await net.resolve_via(
            "example.com", self.settings.listen_host, self._server.port, timeout=6
        )
        if answers:
            return True, f"resolver answered with {answers[0]}"
        return False, "the local resolver did not answer"

    # -- resolv.conf -------------------------------------------------------- #

    @property
    def backup_path(self) -> Path:
        return paths.runtime_dir() / _BACKUP_NAME

    async def _take_over_resolv_conf(self) -> None:
        """Point the system at the Shield, keeping a restorable backup."""
        if not process.is_root() and not process.sudo_available():
            self._ctx.activity(
                "System DNS not redirected (needs root); the Shield is "
                f"reachable at {self._server.address}",
                level="warning",
            )
            return

        content = (
            f"# Written by {constants.APP_NAME} -- the original file was saved to\n"
            f"# {self.backup_path}\n"
            f"nameserver {self.settings.listen_host}\n"
            "options edns0 trust-ad\n"
        )
        try:
            if _RESOLV_CONF.exists() and not self.backup_path.exists():
                await asyncio.to_thread(
                    shutil.copy2, _RESOLV_CONF, self.backup_path
                )
        except OSError as exc:
            log.warning("Could not back up resolv.conf: %s", exc)

        outcome = await process.run_privileged(
            "tee", str(_RESOLV_CONF), stdin=content, timeout=15, quiet=True
        )
        if not outcome.ok:
            self._ctx.activity(
                "Could not rewrite /etc/resolv.conf; only applications pointed "
                f"at {self._server.address} are protected",
                level="warning",
            )
            return

        self._resolv_managed = True
        self._ctx.activity("System DNS redirected to the DNS Shield")

    async def _restore_resolv_conf(self) -> None:
        """Put the original resolver configuration back."""
        if not self._resolv_managed:
            return
        if self.backup_path.is_file():
            outcome = await process.run_privileged(
                "tee",
                str(_RESOLV_CONF),
                stdin=self.backup_path.read_text(encoding="utf-8"),
                timeout=15,
                quiet=True,
            )
            restored = outcome.ok
        else:  # nothing to restore from -- fall back to a sane public resolver
            fallback = "\n".join(
                f"nameserver {server}" for server in self.settings.upstream
            )
            outcome = await process.run_privileged(
                "tee", str(_RESOLV_CONF), stdin=fallback + "\n", timeout=15, quiet=True
            )
            restored = outcome.ok

        self._resolv_managed = False
        if restored:
            self._ctx.activity("System DNS restored")
        else:
            self._ctx.activity(
                f"Could not restore /etc/resolv.conf -- a copy is at {self.backup_path}",
                level="warning",
            )

    # -- statistics --------------------------------------------------------- #

    def _on_blocked(self, domain: str) -> None:
        """Called from the resolver for every blocked query."""
        stats = self._resolver.stats
        self._ctx.state.update_dns(
            queries_total=stats.queries, queries_blocked=stats.blocked
        )
        if stats.blocked % 25 == 1:  # keep the activity log readable
            self._ctx.bus.emit(
                EventType.DNS_BLOCKED,
                f"Blocked {domain}",
                domain=domain,
                total=stats.blocked,
            )

    def _start_flusher(self) -> None:
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(
                self._flush_loop(), name="dns-stats-flusher"
            )

    async def _flush_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(_STATS_FLUSH_SECONDS)
                await self._flush_stats()
        except asyncio.CancelledError:
            raise

    async def _flush_stats(self) -> None:
        """Persist the delta since the last flush and refresh the dashboard."""
        stats = self._resolver.stats
        queries_delta = stats.queries - self._flushed_queries
        blocked_delta = stats.blocked - self._flushed_blocked
        self._flushed_queries = stats.queries
        self._flushed_blocked = stats.blocked

        self._ctx.state.update_dns(
            queries_total=stats.queries, queries_blocked=stats.blocked
        )
        if queries_delta or blocked_delta:
            try:
                await self._ctx.database.bump_dns_counters(
                    queries=queries_delta, blocked=blocked_delta
                )
            except Exception as exc:  # pragma: no cover
                log.debug("DNS counters not persisted: %s", exc)
