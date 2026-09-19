"""Leak probes shared by the verification engine and Doctor diagnostics."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from nextron.utils import net

log = logging.getLogger(__name__)

__all__ = [
    "NameserverReport",
    "classify_nameservers",
    "dns_leak_probe",
    "ipv6_leak_probe",
    "system_nameservers",
]

RESOLV_CONF = Path("/etc/resolv.conf")
_NAMESERVER_RE = re.compile(r"^\s*nameserver\s+(\S+)", re.MULTILINE)

#: RFC1918 + link-local + loopback prefixes. A resolver inside one of these is
#: either local (the DNS Shield) or reachable only through the tunnel.
PRIVATE_PREFIXES = (
    "127.",
    "10.",
    "192.168.",
    "169.254.",
    *(f"172.{octet}." for octet in range(16, 32)),
)


def system_nameservers(path: Path | None = None) -> list[str]:
    """Read the nameservers configured in ``/etc/resolv.conf``."""
    target = path or RESOLV_CONF
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return []
    return _NAMESERVER_RE.findall(text)


@dataclass(frozen=True, slots=True)
class NameserverReport:
    """Where the system currently sends its DNS queries."""

    servers: tuple[str, ...]
    loopback: tuple[str, ...]
    private: tuple[str, ...]
    public: tuple[str, ...]

    @property
    def any_configured(self) -> bool:
        return bool(self.servers)

    @property
    def summary(self) -> str:
        return ", ".join(self.servers) if self.servers else "none configured"


def classify_nameservers(servers: list[str] | None = None) -> NameserverReport:
    """Split the configured resolvers into loopback / private / public."""
    entries = tuple(servers if servers is not None else system_nameservers())
    loopback = tuple(s for s in entries if s.startswith("127.") or s == "::1")
    private = tuple(
        s
        for s in entries
        if s not in loopback and s.startswith(PRIVATE_PREFIXES)
    )
    public = tuple(s for s in entries if s not in loopback and s not in private)
    return NameserverReport(
        servers=entries, loopback=loopback, private=private, public=public
    )


async def dns_leak_probe(
    *,
    shield_running: bool,
    shield_uses_tor: bool,
    tunnel_interface: str | None,
    shield_address: str | None = None,
) -> tuple[bool, str]:
    """Best-effort answer to "can a DNS query escape the protected path?".

    Returns ``(leak_free, detail)``. The probe is deliberately conservative: it
    reports a leak only when a *public* resolver is queried while a protected
    path is supposed to be carrying traffic, and it never claims a loopback
    resolver is the DNS Shield unless the address actually matches -- a
    ``127.0.0.53`` stub belongs to systemd-resolved, which forwards onwards on
    its own terms.
    """
    if shield_running and shield_uses_tor:
        return True, "queries are resolved through Tor's DNSPort"

    report = classify_nameservers()
    if not report.any_configured:
        return False, "no nameserver is configured"

    if shield_running and shield_address and shield_address in report.servers:
        return True, f"system DNS points at the local Shield ({shield_address})"

    if report.loopback and not report.public:
        # A local stub resolver is in the way. Where it forwards to cannot be
        # determined from here, so say that rather than implying safety.
        return (
            False,
            f"a local stub resolver ({', '.join(report.loopback)}) handles DNS; "
            "where it forwards cannot be verified -- point system DNS at the "
            "Shield to be sure",
        )

    if report.public:
        reachable = []
        for server in report.public:
            if await net.resolve_via("example.com", server, timeout=4):
                reachable.append(server)
        if reachable:
            where = "outside the tunnel" if tunnel_interface else "directly"
            return False, (
                f"public resolver(s) {', '.join(reachable)} answer {where}"
            )

    return True, f"resolvers stay inside the protected path ({report.summary})"


async def ipv6_leak_probe(*, timeout: float = 8.0) -> tuple[bool, str]:
    """Return ``(leak_free, detail)`` for IPv6 egress."""
    address = await net.ipv6_reachable(timeout=timeout)
    if address is None:
        return True, "no IPv6 traffic escapes"
    return False, f"IPv6 reaches the internet directly as {address}"
