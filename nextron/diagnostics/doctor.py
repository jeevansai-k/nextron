"""Doctor -- the automatic system health report.

Answers one question honestly: *will NEXTRON actually work on this machine, and
which capabilities are unavailable?* Every check reports a status and, when it
fails, a concrete command the user can run to fix it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass, field
from enum import Enum

from nextron.core import constants, process
from nextron.core.config import NextronConfig
from nextron.diagnostics.leaks import (
    classify_nameservers,
    dns_leak_probe,
    ipv6_leak_probe,
)
from nextron.storage import paths
from nextron.utils import net
from nextron.vpn.profiles import ProfileLibrary

log = logging.getLogger(__name__)

__all__ = ["Doctor", "DoctorReport", "Finding", "Status"]


class Status(str, Enum):
    """Outcome of one diagnostic."""

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    INFO = "info"

    @property
    def marker(self) -> str:
        return {
            Status.OK: "✓",
            Status.WARN: "!",
            Status.FAIL: "✗",
            Status.INFO: ".",
        }[self]

    @property
    def color(self) -> str:
        return {
            Status.OK: constants.COLOR_ACCENT,
            Status.WARN: constants.COLOR_SURFACE,
            Status.FAIL: constants.COLOR_PRIMARY,
            Status.INFO: constants.COLOR_SECONDARY,
        }[self]


@dataclass(frozen=True, slots=True)
class Finding:
    """A single line of the health report."""

    section: str
    name: str
    status: Status
    detail: str = ""
    hint: str = ""

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        line = f"{self.status.marker} {self.name}: {self.detail}"
        return f"{line}\n    -> {self.hint}" if self.hint else line


@dataclass(slots=True)
class DoctorReport:
    """The full report."""

    findings: list[Finding] = field(default_factory=list)

    def add(
        self,
        section: str,
        name: str,
        status: Status,
        detail: str = "",
        hint: str = "",
    ) -> Finding:
        finding = Finding(section, name, status, detail, hint)
        self.findings.append(finding)
        return finding

    @property
    def failures(self) -> list[Finding]:
        return [f for f in self.findings if f.status is Status.FAIL]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.status is Status.WARN]

    @property
    def healthy(self) -> bool:
        return not self.failures

    @property
    def summary(self) -> str:
        total = len(self.findings)
        good = len([f for f in self.findings if f.status is Status.OK])
        parts = [f"{good}/{total} checks OK"]
        if self.failures:
            parts.append(f"{len(self.failures)} failure(s)")
        if self.warnings:
            parts.append(f"{len(self.warnings)} warning(s)")
        return ", ".join(parts)

    def sections(self) -> dict[str, list[Finding]]:
        grouped: dict[str, list[Finding]] = {}
        for finding in self.findings:
            grouped.setdefault(finding.section, []).append(finding)
        return grouped


class Doctor:
    """Run the diagnostics. Usable from the TUI and from ``nextron doctor``."""

    def __init__(self, config: NextronConfig | None = None) -> None:
        self._config = config or NextronConfig()

    async def run(self, *, network: bool = True) -> DoctorReport:
        """Execute every diagnostic; *network* disables online probes."""
        report = DoctorReport()
        self._check_privileges(report)
        self._check_binaries(report)
        await self._check_services(report)
        self._check_storage(report)
        self._check_profiles(report)
        self._check_dns_config(report)
        if network:
            await self._check_network(report)
        else:
            report.add(
                "Network", "Online probes", Status.INFO, "skipped (--offline)"
            )
        log.info("Doctor finished: %s", report.summary)
        return report

    # -- privileges --------------------------------------------------------- #

    def _check_privileges(self, report: DoctorReport) -> None:
        section = "Privileges"
        if process.is_root():
            report.add(section, "Root access", Status.OK, "running as root")
        elif process.sudo_available():
            report.add(
                section,
                "Root access",
                Status.WARN,
                "not root; privileged actions will use sudo -n",
                "Run 'sudo -v' first, or start NEXTRON with sudo, so VPN "
                "tunnels and the kill switch can be created",
            )
        else:
            report.add(
                section,
                "Root access",
                Status.FAIL,
                "neither root nor sudo is available",
                "VPN tunnels, the kill switch, transparent routing and DNS on "
                "port 53 all require root",
            )

        for tool in ("nft", "iptables"):
            if process.which(tool):
                report.add(section, f"{tool} available", Status.OK, process.which(tool))
                break
        else:
            report.add(
                section,
                "Firewall tooling",
                Status.FAIL,
                "neither nft nor iptables found",
                "sudo apt install nftables iptables",
            )

    # -- binaries ----------------------------------------------------------- #

    def _check_binaries(self, report: DoctorReport) -> None:
        section = "Dependencies"
        expectations = (
            ("tor", self._config.tor.binary, Status.FAIL, "sudo apt install tor"),
            (
                "openvpn",
                self._config.vpn.openvpn_binary,
                Status.WARN,
                "sudo apt install openvpn",
            ),
            (
                "wg-quick",
                self._config.vpn.wireguard_binary,
                Status.WARN,
                "sudo apt install wireguard-tools",
            ),
            ("ip", "ip", Status.WARN, "sudo apt install iproute2"),
            ("curl", "curl", Status.INFO, "sudo apt install curl"),
        )
        for label, binary, missing_status, hint in expectations:
            found = process.which(binary)
            if found:
                report.add(section, f"{label} installed", Status.OK, found)
            else:
                report.add(
                    section,
                    f"{label} installed",
                    missing_status,
                    "not found on PATH",
                    hint,
                )

    # -- services ----------------------------------------------------------- #

    async def _check_services(self, report: DoctorReport) -> None:
        section = "Services"
        tor = self._config.tor

        control = await net.is_port_open("127.0.0.1", tor.control_port, timeout=2)
        socks = await net.is_port_open("127.0.0.1", tor.socks_port, timeout=2)

        if control or socks:
            detail = []
            if socks:
                detail.append(f"SOCKS {tor.socks_port} open")
            if control:
                detail.append(f"ControlPort {tor.control_port} open")
            report.add(
                section, "Tor daemon running", Status.OK, ", ".join(detail)
            )
        else:
            report.add(
                section,
                "Tor daemon running",
                Status.INFO,
                "not running (NEXTRON will launch its own)",
                "No action needed unless tor.manage_daemon is false",
            )

        report.add(
            section,
            "SOCKS proxy",
            Status.OK if socks else Status.INFO,
            f"127.0.0.1:{tor.socks_port} "
            + ("accepting connections" if socks else "closed"),
        )

        # The common trap: the distribution's tor.service owns 9050 while its
        # ControlPort is off, so a private daemon cannot use the same port and
        # the running one cannot be driven. NEXTRON relocates, but say so here.
        if socks and not control and tor.manage_daemon:
            from nextron.utils import net as net_utils

            relocated = net_utils.find_free_port(tor.socks_port)
            report.add(
                section,
                "Tor port conflict",
                Status.INFO,
                f"another Tor owns {tor.socks_port} with no ControlPort; "
                f"NEXTRON will run its own daemon on {relocated}",
                "To share the system daemon instead, add 'ControlPort 9051' and "
                "'CookieAuthentication 1' to /etc/tor/torrc, restart it, and "
                "turn off 'Manage own daemon' in Settings",
            )

        if shutil.which("systemctl"):
            outcome = await process.run(
                "systemctl", "is-active", "tor", timeout=8, quiet=True
            )
            state = outcome.output or "unknown"
            report.add(
                section,
                "System tor.service",
                Status.INFO,
                state,
                "Needed only for system-wide transparent routing"
                if state != "active"
                else "",
            )

    # -- storage ------------------------------------------------------------ #

    def _check_storage(self, report: DoctorReport) -> None:
        section = "Storage"
        try:
            root = paths.ensure_layout()
        except OSError as exc:
            report.add(
                section,
                "Configuration directory",
                Status.FAIL,
                str(exc),
                "Check permissions on ~/.config",
            )
            return

        writable = os.access(root, os.W_OK)
        report.add(
            section,
            "Configuration directory",
            Status.OK if writable else Status.FAIL,
            f"{root} ({'writable' if writable else 'not writable'})",
        )
        report.add(
            section,
            "Statistics database",
            Status.OK if paths.database_file().exists() else Status.INFO,
            str(paths.database_file()),
        )
        banner = paths.banner_png()
        report.add(
            section,
            "Banner asset",
            Status.OK if banner.is_file() else Status.WARN,
            str(banner) if banner.is_file() else "missing; the ASCII banner will be used",
        )

    # -- profiles ----------------------------------------------------------- #

    def _check_profiles(self, report: DoctorReport) -> None:
        section = "VPN"
        library = ProfileLibrary()
        try:
            profiles = library.load()
        except Exception as exc:
            report.add(section, "Profile library", Status.FAIL, str(exc))
            return

        if not profiles:
            report.add(
                section,
                "VPN availability",
                Status.WARN,
                "no profiles imported",
                "nextron vpn import <file.ovpn|.conf|.wgconf|.json>",
            )
            return

        openvpn = [p for p in profiles if p.protocol.value == "openvpn"]
        wireguard = [p for p in profiles if p.protocol.value == "wireguard"]
        report.add(
            section,
            "VPN availability",
            Status.OK,
            f"{len(profiles)} profiles ({len(openvpn)} OpenVPN, "
            f"{len(wireguard)} WireGuard)",
        )
        tcp_capable = [p for p in openvpn if p.tcp]
        report.add(
            section,
            "VPN over Tor capable",
            Status.OK if tcp_capable else Status.INFO,
            f"{len(tcp_capable)} TCP OpenVPN profile(s)"
            if tcp_capable
            else "no TCP OpenVPN profile",
            "" if tcp_capable else "VPN over Tor needs a profile with 'proto tcp'",
        )

        # An embedded client certificate that has run out produces a TLS error
        # that explains nothing, so say it plainly here instead.
        from nextron.vpn.certs import expires_within

        expired = [p for p in profiles if p.expired]
        expiring = [p for p in profiles if expires_within(p.expires_at, 30)]
        dated = [p for p in profiles if p.expires_at]
        if expired:
            report.add(
                section,
                "Profile certificates",
                Status.WARN,
                f"{len(expired)} profile(s) have an expired certificate: "
                + ", ".join(p.name for p in expired[:3]),
                "Download a fresh profile from the provider and re-import it",
            )
        elif expiring:
            report.add(
                section,
                "Profile certificates",
                Status.WARN,
                ", ".join(f"{p.name} expires {p.expiry}" for p in expiring[:3]),
                "Refresh these profiles from the provider before they lapse",
            )
        elif dated:
            report.add(
                section,
                "Profile certificates",
                Status.OK,
                f"{len(dated)} profile(s) checked, none expiring within 30 days",
            )

    # -- dns ---------------------------------------------------------------- #

    def _check_dns_config(self, report: DoctorReport) -> None:
        section = "DNS Shield"
        dns = self._config.dns
        report.add(
            section,
            "DNS Shield configured",
            Status.OK if dns.enabled else Status.INFO,
            "enabled" if dns.enabled else "disabled",
        )

        from nextron.dns.blocklist import BlocklistLibrary

        blocklists = BlocklistLibrary()
        files = blocklists.discover(list(dns.enabled_blocklists) or None)
        if files:
            active = [f for f in files if f.enabled]
            report.add(
                section,
                "Blocklists",
                Status.OK if active else Status.WARN,
                f"{len(active)} of {len(files)} lists enabled",
            )
        else:
            report.add(
                section,
                "Blocklists",
                Status.WARN,
                "no blocklists imported",
                f"Drop .txt/.hosts/.list files into {paths.dns_dir()}",
            )

        can_bind_53 = process.is_root() or process.sudo_available()
        report.add(
            section,
            "Port 53 usable",
            Status.OK if can_bind_53 else Status.WARN,
            "root available" if can_bind_53 else "will fall back to port "
            f"{dns.fallback_port}",
        )

        resolvers = classify_nameservers()
        report.add(
            section,
            "System resolvers",
            Status.OK if resolvers.any_configured else Status.FAIL,
            resolvers.summary,
        )

    # -- network ------------------------------------------------------------ #

    async def _check_network(self, report: DoctorReport) -> None:
        section = "Network"
        ip, ipv6_result, leak_result = await asyncio.gather(
            net.public_ip(timeout=12),
            ipv6_leak_probe(timeout=8),
            dns_leak_probe(
                shield_running=False, shield_uses_tor=False, tunnel_interface=None
            ),
        )

        if ip:
            info = await net.ip_info(ip, timeout=10)
            where = f" ({info.summary})" if info and info.country else ""
            report.add(section, "Public IP", Status.OK, f"{ip}{where}")
        else:
            report.add(
                section,
                "Public IP",
                Status.FAIL,
                "could not be determined",
                "Check the internet connection",
            )

        v6_ok, v6_detail = ipv6_result
        report.add(
            section,
            "IPv6 leak",
            Status.OK if v6_ok else Status.WARN,
            v6_detail,
            "" if v6_ok else "Disable IPv6 or use a tunnel that carries it",
        )

        dns_ok, dns_detail = leak_result
        report.add(
            section,
            "DNS leak",
            Status.OK if dns_ok else Status.WARN,
            dns_detail,
            "" if dns_ok else "Enable the DNS Shield so queries stay protected",
        )

        device = net.default_route_device()
        report.add(
            section,
            "Default route",
            Status.OK if device else Status.WARN,
            device or "no default route",
        )
        tunnels = net.tunnel_interfaces()
        report.add(
            section,
            "Tunnel interfaces",
            Status.INFO,
            ", ".join(tunnels) if tunnels else "none present",
        )
