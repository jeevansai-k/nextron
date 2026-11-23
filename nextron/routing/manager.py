"""The routing manager -- the orchestrator behind the four modes.

Each mode has a strict execution sequence (see :mod:`nextron.routing.modes`).
The manager runs that sequence, hands the result to the verification engine,
and only then reports the mode as **Connected**. Teardown always happens in
reverse order so the machine is never left half-routed.
"""

from __future__ import annotations

import asyncio
import logging

from nextron.core.config import RoutingMode
from nextron.core.context import EngineContext
from nextron.core.events import EventType
from nextron.core.exceptions import NextronError, RoutingError
from nextron.core.verification import VerificationEngine, VerificationReport
from nextron.dns.shield import DNSShield
from nextron.routing.modes import describe
from nextron.routing.transparent import TransparentRouter
from nextron.state.manager import ServiceStatus
from nextron.tor.engine import TorEngine
from nextron.vpn.manager import VPNEngine

log = logging.getLogger(__name__)

__all__ = ["RoutingManager"]


class RoutingManager:
    """Establish, verify and tear down routing modes."""

    def __init__(
        self,
        context: EngineContext,
        tor: TorEngine,
        vpn: VPNEngine,
        dns: DNSShield,
    ) -> None:
        self._ctx = context
        self._tor = tor
        self._vpn = vpn
        self._dns = dns
        self.transparent = TransparentRouter()
        self.verification = VerificationEngine(context, tor, vpn, dns)
        self.verification.transparent = self.transparent

        self._mode: RoutingMode | None = None
        self._lock = asyncio.Lock()
        self._last_report: VerificationReport | None = None

    # -- properties --------------------------------------------------------- #

    @property
    def mode(self) -> RoutingMode | None:
        return self._mode

    @property
    def active(self) -> bool:
        return self._mode is not None

    @property
    def last_report(self) -> VerificationReport | None:
        return self._last_report

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    # -- establish ---------------------------------------------------------- #

    async def establish(
        self, mode: RoutingMode, *, vpn_profile: str | None = None
    ) -> VerificationReport:
        """Bring *mode* up and verify it. Raises :class:`RoutingError` on failure."""
        async with self._lock:
            if self._mode is not None:
                await self._teardown_locked()

            descriptor = describe(mode)
            state = self._ctx.state
            state.update(
                routing_mode=mode,
                route_status=ServiceStatus.STARTING,
                verification_passed=False,
                verification_summary="",
                last_error=None,
            )
            self._ctx.bus.emit(
                EventType.ROUTE_ESTABLISHING,
                f"Establishing {mode.label} ({descriptor.chain})",
                mode=mode.value,
            )
            try:
                return await self._establish_locked(mode, vpn_profile, descriptor)
            except asyncio.CancelledError:
                # The user cancelled (C again, or quit) part-way through. A
                # half-built route -- a tor daemon, an openvpn child, an armed
                # kill switch -- must not outlive the request that started it.
                state.update(
                    route_status=ServiceStatus.STOPPING,
                    last_error="Connection cancelled",
                )
                self._ctx.activity(
                    f"{mode.label} cancelled; cleaning up", level="warning"
                )
                await asyncio.shield(self._teardown_locked(quiet=True))
                state.update(last_error=None)
                raise

    async def _establish_locked(
        self, mode: RoutingMode, vpn_profile: str | None, descriptor
    ) -> VerificationReport:
        """The body of :meth:`establish`; the caller holds the lock."""
        state = self._ctx.state
        await self.verification.capture_baseline()

        try:
            if mode is RoutingMode.TOR_ONLY:
                await self._establish_tor_only()
            elif mode is RoutingMode.VPN_ONLY:
                await self._establish_vpn_only(vpn_profile)
            elif mode is RoutingMode.TOR_OVER_VPN:
                await self._establish_tor_over_vpn(vpn_profile)
            else:
                await self._establish_vpn_over_tor(vpn_profile)
        except Exception as exc:
            # Not only NextronError: an OSError from a missing directory or a
            # bug in a backend must still take the half-built route down.
            if not isinstance(exc, NextronError):
                log.exception("Unexpected failure establishing %s", mode.label)
            state.update(route_status=ServiceStatus.ERROR, last_error=str(exc))
            self._ctx.bus.emit(
                EventType.ROUTE_DOWN,
                f"{mode.label} failed: {exc}",
                level="error",
                mode=mode.value,
            )
            await self._teardown_locked(quiet=True)
            raise RoutingError(str(exc)) from exc

        self._mode = mode
        report = await self.verification.verify(mode)
        self._last_report = report

        strict = self._ctx.config.verification.strict
        if not report.passed and strict:
            state.update(route_status=ServiceStatus.ERROR)
            self._ctx.bus.emit(
                EventType.ROUTE_DOWN,
                f"{mode.label} rejected: {report.summary}",
                level="error",
                mode=mode.value,
            )
            failures = "; ".join(check.detail for check in report.failures)
            await self._teardown_locked(quiet=True)
            raise RoutingError(f"Verification failed -- {failures}")

        state.update(route_status=ServiceStatus.ACTIVE)
        self._ctx.bus.emit(
            EventType.ROUTE_UP,
            f"{mode.label} active -- {report.summary}",
            mode=mode.value,
            summary=report.summary,
        )
        await self._ctx.persist(
            "route.up", f"{mode.label} established", mode=mode.value
        )
        return report

    # -- per-mode sequences ------------------------------------------------- #

    async def _establish_tor_only(self) -> None:
        """You -> Tor -> Internet."""
        self._vpn.use_socks_proxy(None)
        await self._tor.start()
        await self._engage_transparent()
        await self._start_dns_shield(with_tor=True)

    async def _establish_vpn_only(self, vpn_profile: str | None) -> None:
        """You -> VPN -> Internet."""
        self._vpn.use_socks_proxy(None)
        await self._vpn.connect(vpn_profile, reason="mode:vpn_only")
        await self._start_dns_shield(with_tor=False)

    async def _establish_tor_over_vpn(self, vpn_profile: str | None) -> None:
        """You -> VPN -> Tor -> Internet."""
        self._vpn.use_socks_proxy(None)
        await self._vpn.connect(vpn_profile, reason="mode:tor_over_vpn")

        healthy, detail = await self._vpn.verify()
        if not healthy:
            raise RoutingError(f"VPN tunnel unusable before starting Tor: {detail}")
        self._ctx.activity(f"VPN tunnel verified ({detail}); starting Tor on top")

        # Tor is launched *after* the tunnel, so every relay connection it
        # opens is created inside the VPN.
        await self._tor.start()
        await self._engage_transparent()
        await self._start_dns_shield(with_tor=True)

    async def _establish_vpn_over_tor(self, vpn_profile: str | None) -> None:
        """You -> Tor -> VPN -> Internet."""
        await self._tor.start()
        if not await self._tor.verify_socks():
            raise RoutingError("Tor's SOCKS proxy never became available")

        target = (
            self._vpn.library.resolve(vpn_profile)
            if vpn_profile
            else self._vpn.resolve_target()
        )
        if target is None:
            raise RoutingError(f"No VPN profile matches '{vpn_profile}'")
        if not target.supports_socks:
            raise RoutingError(
                f"'{target.name}' cannot be tunnelled through Tor. VPN over Tor "
                "needs a TCP OpenVPN profile (proto tcp); WireGuard is UDP-only."
            )

        self._ctx.activity(
            f"Injecting SOCKS proxy 127.0.0.1:{self._tor.socks_port} into "
            f"'{target.name}'"
        )
        self._vpn.use_socks_proxy(("127.0.0.1", self._tor.socks_port))
        await self._vpn.connect(target, reason="mode:vpn_over_tor")
        await self._start_dns_shield(with_tor=False)

    # -- shared steps ------------------------------------------------------- #

    async def _engage_transparent(self) -> None:
        """Try to make Tor routing system-wide; degrade to SOCKS otherwise."""
        settings = self._ctx.config.tor
        if not settings.transparent_routing:
            self._ctx.activity(
                f"Transparent routing disabled; Tor SOCKS proxy on "
                f"127.0.0.1:{self._tor.socks_port}"
            )
            return

        # Never install a redirect without somewhere to redirect *to*: if Tor
        # is not actually listening on TransPort/DNSPort, these rules would
        # send every connection into a closed port and take the machine
        # offline. Check before touching the firewall, not after.
        daemon = self._tor.daemon
        ready, missing = await self._transparent_targets_ready()
        if not ready:
            self._ctx.activity(
                f"Transparent routing skipped: Tor is not listening on "
                f"{missing}. Staying in SOCKS mode "
                f"(127.0.0.1:{self._tor.socks_port})",
                level="warning",
            )
            return

        status = await self.transparent.engage(
            trans_port=self._tor.trans_port,
            dns_port=self._tor.dns_port,
            daemon_is_ours=settings.manage_daemon and daemon.runtime_uid is None,
            tor_uid=daemon.runtime_uid,
        )
        if status.active:
            self._ctx.activity(
                "Transparent routing engaged: every application on this machine "
                "now exits through Tor (IPv6 and QUIC are blocked so nothing "
                "slips past)"
            )
        else:
            self._ctx.activity(
                f"Running in SOCKS mode ({status.reason}). Only applications "
                f"pointed at 127.0.0.1:{self._tor.socks_port} use Tor",
                level="warning",
            )

    async def _transparent_targets_ready(self) -> tuple[bool, str]:
        """Confirm Tor's TransPort and DNSPort really answer."""
        from nextron.utils import net

        trans = self._tor.trans_port
        dns = self._tor.dns_port

        if not await net.is_port_open("127.0.0.1", trans, timeout=4):
            return False, f"TransPort {trans}"
        # UDP cannot be probed by connecting, so ask it to resolve something.
        if not await net.resolve_via("example.com", "127.0.0.1", dns, timeout=5):
            return False, f"DNSPort {dns}"
        return True, ""

    async def _start_dns_shield(self, *, with_tor: bool) -> None:
        """Start the Shield if enabled, pointing it at Tor's DNSPort when useful."""
        if not self._ctx.config.dns.enabled:
            return
        tor_port = self._tor.dns_port if with_tor else None
        try:
            await self._dns.start(tor_dns_port=tor_port)
        except NextronError as exc:
            # The Shield is an independent layer: it must not sink the route.
            self._ctx.activity(f"DNS Shield could not start: {exc}", level="warning")

    # -- teardown ----------------------------------------------------------- #

    async def teardown(self) -> None:
        """Tear the current mode down in reverse order."""
        async with self._lock:
            await self._teardown_locked()

    async def _teardown_locked(self, *, quiet: bool = False) -> None:
        mode = self._mode
        if not quiet and mode is not None:
            self._ctx.activity(f"Tearing down {mode.label}")
        self._ctx.state.update(route_status=ServiceStatus.STOPPING)

        # Every step runs even when an earlier one fails: a DNS restore that
        # raises must not leave Tor running or the kill switch armed.
        vpn = self._vpn
        steps = [
            ("transparent routing", self.transparent.active, self.transparent.release),
            ("DNS Shield", self._dns.running, self._dns.stop),
            (
                "VPN",
                # The kill switch counts: a first connect that failed leaves no
                # current profile, but the default-drop filter is still armed
                # and the machine is offline until somebody releases it.
                vpn.connected or vpn.current is not None or vpn.killswitch.active,
                vpn.disconnect,
            ),
            ("Tor", self._tor.running, self._tor.stop),
        ]
        for label, needed, step in steps:
            if not needed:
                continue
            try:
                await step()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Teardown of %s failed", label)
                self._ctx.activity(
                    f"Could not stop {label} cleanly: {exc}", level="error"
                )
        self._vpn.use_socks_proxy(None)

        self._mode = None
        self._ctx.state.update(
            route_status=ServiceStatus.IDLE,
            verification_passed=False,
            verification_summary="",
            public_ip=None,
            exit_country=None,
        )
        if not quiet and mode is not None:
            self._ctx.bus.emit(
                EventType.ROUTE_DOWN, f"{mode.label} torn down", mode=mode.value
            )

    async def switch_mode(
        self, mode: RoutingMode, *, vpn_profile: str | None = None
    ) -> VerificationReport:
        """Change routing mode, persisting the choice."""
        report = await self.establish(mode, vpn_profile=vpn_profile)
        self._ctx.config.routing_mode = mode
        self._ctx.save_config()
        return report

    # -- maintenance -------------------------------------------------------- #

    async def revalidate(self) -> VerificationReport | None:
        """Re-run verification for the live mode (used by the health watchdog)."""
        if self._mode is None or self._lock.locked():
            return None
        report = await self.verification.verify(self._mode)
        self._last_report = report
        if not report.passed:
            self._ctx.state.update(route_status=ServiceStatus.ERROR)
        elif self._ctx.state.state.route_status is ServiceStatus.ERROR:
            self._ctx.state.update(route_status=ServiceStatus.ACTIVE)
        return report

    async def cleanup_stale_rules(self) -> None:
        """Remove rules a previous crashed session may have left behind."""
        if await self.transparent.is_installed():
            self._ctx.activity("Removing stale transparent-routing rules")
            await self.transparent.release(force=True)
        if await self._vpn.killswitch.is_installed():
            self._ctx.activity("Removing a stale kill switch ruleset")
            await self._vpn.killswitch.release(force=True)
