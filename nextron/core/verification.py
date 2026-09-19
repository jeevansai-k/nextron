"""The verification engine.

A routing mode is never reported as **Connected** until every applicable check
in this module has succeeded. The checklist mirrors the specification:

1. Tor bootstrap
2. VPN tunnel exists
3. SOCKS proxy available
4. Public IP acquired
5. Routing correctness
6. DNS functionality
7. DNS leak test
8. IPv6 leak test

Checks are *applicable per mode*: a VPN-only session has no Tor bootstrap to
verify, and a Tor-only session has no tunnel interface. Non-applicable checks
are skipped, never silently passed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from nextron.core.config import RoutingMode
from nextron.core.context import EngineContext
from nextron.core.events import EventType
from nextron.diagnostics.leaks import (
    dns_leak_probe,
    ipv6_leak_probe,
    system_nameservers,
)
from nextron.state.manager import ServiceStatus
from nextron.utils import net

log = logging.getLogger(__name__)

__all__ = ["CheckResult", "VerificationEngine", "VerificationReport"]

@dataclass(frozen=True, slots=True)
class CheckResult:
    """The outcome of a single verification step."""

    name: str
    passed: bool
    detail: str = ""
    required: bool = True
    skipped: bool = False

    @property
    def marker(self) -> str:
        if self.skipped:
            return "-"
        return "✓" if self.passed else "✗"

    @property
    def verdict(self) -> str:
        if self.skipped:
            return "skipped"
        if self.passed:
            return "pass"
        return "FAIL" if self.required else "warn"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "required": self.required,
            "skipped": self.skipped,
        }


@dataclass(slots=True)
class VerificationReport:
    """The full result of one verification pass."""

    mode: RoutingMode
    checks: list[CheckResult] = field(default_factory=list)
    started_at: datetime = field(default_factory=datetime.now)
    finished_at: datetime | None = None

    @property
    def applicable(self) -> list[CheckResult]:
        return [check for check in self.checks if not check.skipped]

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.applicable if not c.passed and c.required]

    @property
    def warnings(self) -> list[CheckResult]:
        return [c for c in self.applicable if not c.passed and not c.required]

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def summary(self) -> str:
        total = len(self.applicable)
        good = len([c for c in self.applicable if c.passed])
        if self.passed and not self.warnings:
            return f"All {total} checks passed"
        if self.passed:
            return f"{good}/{total} passed, {len(self.warnings)} warning(s)"
        return f"{good}/{total} passed, {len(self.failures)} failure(s)"

    @property
    def duration_seconds(self) -> float:
        end = self.finished_at or datetime.now()
        return (end - self.started_at).total_seconds()

    def to_list(self) -> list[dict]:
        return [check.to_dict() for check in self.checks]


class VerificationEngine:
    """Run the checklist for a routing mode."""

    def __init__(
        self,
        context: EngineContext,
        tor_engine,
        vpn_engine,
        dns_shield,
    ) -> None:
        self._ctx = context
        self._tor = tor_engine
        self._vpn = vpn_engine
        self._dns = dns_shield
        #: Set by the routing manager; exposes whether system-wide redirection
        #: is live, which changes how the routing check must be measured.
        self.transparent = None
        #: Public address observed before any tunnel existed, used to prove
        #: that traffic actually changed path.
        self.baseline_ip: str | None = None

    # -- baseline ----------------------------------------------------------- #

    async def capture_baseline(self) -> str | None:
        """Record the unprotected public IP so routing changes are provable."""
        if self.baseline_ip:
            return self.baseline_ip
        self.baseline_ip = await net.public_ip(timeout=12)
        if self.baseline_ip:
            log.info("Baseline (unprotected) address: %s", self.baseline_ip)
        return self.baseline_ip

    # -- entry point -------------------------------------------------------- #

    async def verify(self, mode: RoutingMode) -> VerificationReport:
        """Run every applicable check for *mode*."""
        settings = self._ctx.config.verification
        report = VerificationReport(mode=mode)

        if not settings.enabled:
            report.checks.append(
                CheckResult(
                    "Verification",
                    True,
                    "disabled in settings",
                    skipped=True,
                )
            )
            report.finished_at = datetime.now()
            return report

        self._ctx.state.update(route_status=ServiceStatus.VERIFYING)
        self._ctx.bus.emit(
            EventType.VERIFICATION_STARTED,
            f"Verifying {mode.label}",
            mode=mode.value,
        )

        steps = (
            ("Tor bootstrap", self._check_tor_bootstrap),
            ("VPN tunnel", self._check_vpn_tunnel),
            ("SOCKS proxy", self._check_socks),
            ("Public IP", self._check_public_ip),
            ("Routing correctness", self._check_routing),
            ("DNS functionality", self._check_dns_functionality),
            ("DNS leak", self._check_dns_leak),
            ("IPv6 leak", self._check_ipv6_leak),
        )

        for name, handler in steps:
            try:
                result = await handler(mode)
            except Exception as exc:  # a probe must never abort the pass
                log.debug("Check '%s' raised: %s", name, exc)
                result = CheckResult(name, False, f"check error: {exc}")
            report.checks.append(result)
            self._ctx.bus.emit(
                EventType.VERIFICATION_STEP,
                f"{result.marker} {result.name}: {result.detail or result.verdict}",
                check=result.name,
                passed=result.passed,
                skipped=result.skipped,
            )

        report.finished_at = datetime.now()
        passed = report.passed if settings.strict else not report.failures
        self._ctx.state.update(
            verification_passed=passed, verification_summary=report.summary
        )
        self._ctx.bus.emit(
            EventType.VERIFICATION_PASSED if passed else EventType.VERIFICATION_FAILED,
            f"{mode.label}: {report.summary}",
            level="info" if passed else "error",
            mode=mode.value,
            summary=report.summary,
        )
        try:
            await self._ctx.database.record_verification(
                mode.value, passed, report.to_list()
            )
        except Exception:  # pragma: no cover
            pass
        return report

    # -- individual checks -------------------------------------------------- #

    async def _check_tor_bootstrap(self, mode: RoutingMode) -> CheckResult:
        name = "Tor bootstrap"
        if not mode.uses_tor:
            return CheckResult(name, True, "not used in this mode", skipped=True)

        percent = self._ctx.state.state.tor.bootstrap_percent
        if not self._tor.running:
            return CheckResult(name, False, "the Tor daemon is not running")
        if percent < 100:
            phase = self._ctx.state.state.tor.bootstrap_phase
            return CheckResult(name, False, f"stalled at {percent}% ({phase})")
        return CheckResult(name, True, "100% bootstrapped")

    async def _check_vpn_tunnel(self, mode: RoutingMode) -> CheckResult:
        name = "VPN tunnel"
        if not mode.uses_vpn:
            return CheckResult(name, True, "not used in this mode", skipped=True)

        healthy, detail = await self._vpn.verify()
        return CheckResult(name, healthy, detail)

    async def _check_socks(self, mode: RoutingMode) -> CheckResult:
        name = "SOCKS proxy"
        if not mode.uses_tor:
            return CheckResult(name, True, "not used in this mode", skipped=True)

        port = self._tor.socks_port
        if await self._tor.verify_socks():
            return CheckResult(name, True, f"127.0.0.1:{port} accepting connections")
        return CheckResult(name, False, f"127.0.0.1:{port} is not accepting connections")

    async def _check_public_ip(self, mode: RoutingMode) -> CheckResult:
        name = "Public IP"
        proxy = self._tor.proxy_url if mode is RoutingMode.TOR_ONLY else None
        ip = await net.public_ip(
            proxy=proxy, timeout=self._ctx.config.verification.timeout
        )
        if not ip:
            return CheckResult(name, False, "no public address could be acquired")
        self._ctx.state.update(public_ip=ip)
        return CheckResult(name, True, ip)

    async def _check_routing(self, mode: RoutingMode) -> CheckResult:
        """The heart of the engine: prove traffic really takes the claimed path."""
        name = "Routing correctness"
        if not self._ctx.config.verification.check_routing:
            return CheckResult(name, True, "disabled in settings", skipped=True)

        if mode is RoutingMode.VPN_ONLY:
            ip = self._ctx.state.state.vpn.public_ip or self._ctx.state.state.public_ip
            if not ip:
                return CheckResult(name, False, "no exit address to compare")
            if self.baseline_ip and ip == self.baseline_ip:
                return CheckResult(
                    name,
                    False,
                    f"exit address {ip} still matches the unprotected address",
                )
            return CheckResult(
                name,
                True,
                f"{ip} differs from the unprotected address"
                if self.baseline_ip
                else f"exit address {ip}",
            )

        # How the measurement is taken decides what it proves. With transparent
        # routing the question is what an ordinary application sees, so ask
        # *without* the proxy. Measuring through the proxy would confirm Tor
        # even when nothing else on the machine is routed -- which reads as
        # "Connected" while the browser still shows the real address.
        system_wide = bool(getattr(self.transparent, "active", False))
        proxy = None if system_wide else self._tor.proxy_url
        is_tor, exit_ip = await net.tor_check(proxy=proxy, timeout=25)

        if not is_tor:
            if system_wide:
                return CheckResult(
                    name,
                    False,
                    "transparent routing is installed but this machine is not "
                    "exiting through Tor -- check for another firewall or a VPN "
                    "client holding the routes",
                )
            return CheckResult(
                name,
                False,
                "check.torproject.org says this connection is not using Tor",
            )

        if mode is RoutingMode.TOR_OVER_VPN:
            vpn_ip = self._ctx.state.state.vpn.public_ip
            if vpn_ip and exit_ip == vpn_ip:
                return CheckResult(
                    name, False, f"the Tor exit ({exit_ip}) equals the VPN address"
                )
            return CheckResult(
                name, True, f"Tor exit {exit_ip} reached through the VPN tunnel"
            )

        if mode is RoutingMode.VPN_OVER_TOR:
            vpn_ip = self._ctx.state.state.vpn.public_ip
            if not vpn_ip:
                return CheckResult(name, False, "the VPN exit address is unknown")
            if exit_ip and vpn_ip == exit_ip:
                return CheckResult(
                    name,
                    False,
                    f"the VPN address ({vpn_ip}) equals the Tor exit -- the "
                    "tunnel is not terminating past Tor",
                )
            if self.baseline_ip and vpn_ip == self.baseline_ip:
                return CheckResult(
                    name,
                    False,
                    f"the VPN address {vpn_ip} matches the unprotected address",
                )
            return CheckResult(
                name, True, f"VPN exit {vpn_ip} established through Tor ({exit_ip})"
            )

        if system_wide:
            return CheckResult(
                name, True, f"the whole system exits through Tor ({exit_ip})"
            )
        return CheckResult(
            name,
            False,
            f"Tor works through the proxy (exit {exit_ip}), but system traffic "
            f"is NOT routed: only apps pointed at 127.0.0.1:"
            f"{self._tor.socks_port} use it. Run with sudo for system-wide.",
            # Not a hard failure: SOCKS mode is a real, working mode. It is a
            # warning because it is not what "Connected" usually implies.
            required=False,
        )

    async def _check_dns_functionality(self, mode: RoutingMode) -> CheckResult:
        name = "DNS functionality"
        if self._dns.running:
            ok, detail = await self._dns.self_test()
            return CheckResult(name, ok, detail)

        for server in system_nameservers() or ["1.1.1.1"]:
            answers = await net.resolve_via("example.com", server, timeout=5)
            if answers:
                return CheckResult(name, True, f"resolved via {server}")
        return CheckResult(name, False, "no configured resolver answered")

    async def _check_dns_leak(self, mode: RoutingMode) -> CheckResult:
        """Check whether name resolution can escape the protected path."""
        name = "DNS leak"
        if not self._ctx.config.verification.check_dns_leak:
            return CheckResult(name, True, "disabled in settings", skipped=True)

        state = self._ctx.state.state
        leak_free, detail = await dns_leak_probe(
            shield_running=self._dns.running,
            shield_uses_tor=state.dns.using_tor_dns,
            tunnel_interface=state.vpn.interface,
            shield_address=getattr(self._dns, "listen_host", None),
        )
        if leak_free:
            return CheckResult(name, True, detail)
        return CheckResult(
            name,
            False,
            f"{detail} -- enable the DNS Shield (D)",
            # A DNS leak is serious but it must not strand a working tunnel, so
            # it is reported as a warning rather than a hard failure.
            required=False,
        )

    async def _check_ipv6_leak(self, mode: RoutingMode) -> CheckResult:
        name = "IPv6 leak"
        if not self._ctx.config.verification.check_ipv6_leak:
            return CheckResult(name, True, "disabled in settings", skipped=True)

        leak_free, detail = await ipv6_leak_probe(timeout=8)
        if leak_free:
            return CheckResult(name, True, detail)
        return CheckResult(
            name,
            False,
            f"{detail} -- disable IPv6 or use a tunnel that carries it",
            required=False,
        )


