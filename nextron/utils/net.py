"""Network probes used by the verification engine and the dashboard.

Every probe is async, bounded by a timeout, and returns ``None`` (or ``False``)
instead of raising, so a flaky network can never take a long running session
down. Probes accept an optional SOCKS proxy so an IP can be measured *through*
Tor as well as directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import httpx
import psutil

from nextron.core import constants

log = logging.getLogger(__name__)

__all__ = [
    "IPInfo",
    "default_route_device",
    "interface_addresses",
    "interface_exists",
    "ip_info",
    "ipv6_reachable",
    "is_port_open",
    "public_ip",
    "resolve_via",
    "socks_proxy_url",
    "tor_check",
    "wait_for_port",
]


@dataclass(frozen=True, slots=True)
class IPInfo:
    """Geolocation facts for an address (best effort, no API keys)."""

    ip: str
    country: str | None = None
    region: str | None = None
    city: str | None = None
    org: str | None = None

    @property
    def summary(self) -> str:
        parts = [p for p in (self.city, self.country) if p]
        return ", ".join(parts) if parts else "unknown"


def socks_proxy_url(port: int, host: str = "127.0.0.1") -> str:
    """Return the SOCKS5 URL for Tor, resolving names through Tor (``5h``)."""
    return f"socks5h://{host}:{port}"


def _client(proxy: str | None, timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        proxy=proxy,
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": f"{constants.APP_NAME}/{constants.VERSION}"},
    )


async def public_ip(
    *,
    proxy: str | None = None,
    timeout: float = constants.NETWORK_TIMEOUT,
) -> str | None:
    """Return the public IPv4 address, or ``None`` when unreachable."""
    async with _client(proxy, timeout) as client:
        for endpoint in constants.IP_LOOKUP_ENDPOINTS:
            try:
                response = await client.get(endpoint)
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:  # network, JSON, HTTP -- all non fatal
                log.debug("IP lookup failed via %s (%s)", endpoint, exc)
                continue
            ip = payload.get("ip") or payload.get("query")
            if ip:
                return str(ip).strip()
    return None


async def ip_info(
    ip: str | None = None,
    *,
    proxy: str | None = None,
    timeout: float = constants.NETWORK_TIMEOUT,
) -> IPInfo | None:
    """Look up country/city/org for *ip* (or for the current exit address)."""
    target = ip
    if target is None:
        target = await public_ip(proxy=proxy, timeout=timeout)
    if not target:
        return None

    url = constants.GEO_LOOKUP_ENDPOINT.format(ip=target)
    async with _client(proxy, timeout) as client:
        try:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            log.debug("Geo lookup failed for %s (%s)", target, exc)
            return IPInfo(ip=target)

    return IPInfo(
        ip=str(payload.get("ip", target)),
        country=payload.get("country"),
        region=payload.get("region"),
        city=payload.get("city"),
        org=payload.get("org"),
    )


async def tor_check(
    *,
    proxy: str | None = None,
    timeout: float = constants.NETWORK_TIMEOUT,
) -> tuple[bool, str | None]:
    """Ask the Tor Project whether this connection exits through Tor.

    Returns ``(is_tor, exit_ip)``. This is the authoritative routing-correctness
    check for every Tor-bearing mode.
    """
    async with _client(proxy, timeout) as client:
        try:
            response = await client.get(constants.TOR_CHECK_ENDPOINT)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            log.debug("Tor check unreachable (%s)", exc)
            return False, None
    return bool(payload.get("IsTor")), payload.get("IP")


async def ipv6_reachable(*, timeout: float = 8.0) -> str | None:
    """Return the public IPv6 address if IPv6 traffic escapes the tunnel."""
    async with _client(None, timeout) as client:
        try:
            response = await client.get(constants.IPV6_PROBE_ENDPOINT)
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return None
    ip = payload.get("ip")
    return str(ip) if ip and ":" in str(ip) else None


async def is_port_open(
    host: str, port: int, *, timeout: float = 3.0
) -> bool:
    """True when a TCP connection to ``host:port`` succeeds."""
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except (TimeoutError, OSError):
        return False
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return True


def can_bind(host: str, port: int) -> bool:
    """True when a listener can be opened on ``host:port`` right now.

    Binding is the same test the daemon itself will perform, so it detects the
    cases a connect-probe misses -- a socket held by another user, or one bound
    to a wildcard address.
    """
    for family, socket_type, proto, _canonical, address in socket.getaddrinfo(
        host, port, proto=socket.IPPROTO_TCP
    ):
        try:
            with socket.socket(family, socket_type, proto) as probe:
                # Deliberately *not* SO_REUSEADDR: we want to know whether the
                # port is genuinely free, not whether we could share it.
                probe.bind(address)
        except OSError:
            return False
    return True


def listener_uid(port: int, host: str = "127.0.0.1") -> int | None:
    """uid of the process listening on ``host:port``, or ``None``.

    Needed to decide whether a Tor daemon can be exempted from transparent
    redirection: the exemption matches on uid, and a daemon NEXTRON merely
    attached to is just as exemptable as one it started -- as long as it is
    not running as the user whose traffic is being redirected.
    """
    try:
        packed = socket.inet_pton(socket.AF_INET, host)
    except OSError:
        return None
    wanted = f"{int.from_bytes(packed, 'little'):08X}:{port:04X}"

    try:
        lines = Path("/proc/net/tcp").read_text().splitlines()[1:]
    except OSError:  # pragma: no cover - not Linux
        return None

    for line in lines:
        fields = line.split()
        # local_address, state 0A == LISTEN, uid
        if len(fields) > 7 and fields[1].upper() == wanted and fields[3] == "0A":
            try:
                return int(fields[7])
            except ValueError:  # pragma: no cover
                return None
    return None


def listening_on(host: str, port: int) -> bool:
    """True when something already holds ``host:port``."""
    return not can_bind(host, port)


def find_free_port(
    preferred: int,
    *,
    host: str = "127.0.0.1",
    exclude: Iterable[int] = (),
    offset: int = 200,
    limit: int = 64,
) -> int:
    """Return *preferred* when it is free, otherwise a nearby free port.

    The search starts at ``preferred + offset`` so the relationship between the
    ports stays recognisable: a SOCKS preference of 9050 becomes 9250 and a
    ControlPort of 9051 becomes 9251, which is far easier to reason about than
    an arbitrary ephemeral number.
    """
    taken = set(exclude)
    if preferred not in taken and can_bind(host, preferred):
        return preferred

    for candidate in range(preferred + offset, preferred + offset + limit):
        if candidate > 65535:
            break
        if candidate in taken:
            continue
        if can_bind(host, candidate):
            return candidate

    raise OSError(
        f"No free port near {preferred} on {host} "
        f"(tried {preferred} and {preferred + offset}-{preferred + offset + limit})"
    )


async def wait_for_port(
    host: str,
    port: int,
    *,
    timeout: float = 30.0,
    interval: float = 0.5,
) -> bool:
    """Poll ``host:port`` until it accepts connections or *timeout* elapses."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await is_port_open(host, port, timeout=interval * 2):
            return True
        await asyncio.sleep(interval)
    return False


async def resolve_via(
    domain: str,
    server: str,
    port: int = 53,
    *,
    timeout: float = 5.0,
) -> list[str]:
    """Resolve *domain* against a specific DNS *server* over UDP.

    Used by the DNS functionality and DNS-leak checks: the answer proves the
    resolver is reachable, and the resolver we asked proves where DNS went.
    """
    from dnslib import QTYPE, DNSRecord

    query = DNSRecord.question(domain, "A")
    loop = asyncio.get_running_loop()

    def _query() -> bytes:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(query.pack(), (server, port))
            data, _ = sock.recvfrom(4096)
            return data

    try:
        raw = await asyncio.wait_for(loop.run_in_executor(None, _query), timeout + 1)
    except Exception as exc:
        log.debug("DNS query for %s via %s:%s failed (%s)", domain, server, port, exc)
        return []

    try:
        answer = DNSRecord.parse(raw)
    except Exception:
        return []

    return [
        str(rr.rdata)
        for rr in answer.rr
        if QTYPE.get(rr.rtype) in {"A", "AAAA"}
    ]


def interface_addresses(name: str) -> list[str]:
    """Return the IPv4/IPv6 addresses assigned to interface *name*."""
    try:
        interfaces = psutil.net_if_addrs()
    except Exception:  # pragma: no cover
        return []
    return [
        addr.address
        for addr in interfaces.get(name, [])
        if addr.family in (socket.AF_INET, socket.AF_INET6)
    ]


def interface_exists(name: str) -> bool:
    """True when interface *name* is present on the system."""
    try:
        return name in psutil.net_if_addrs()
    except Exception:  # pragma: no cover
        return False


def tunnel_interfaces() -> list[str]:
    """Return every interface that looks like a VPN tunnel."""
    try:
        names = list(psutil.net_if_addrs())
    except Exception:  # pragma: no cover
        return []
    return [n for n in names if n.startswith(("tun", "tap", "wg", "nordlynx", "proton"))]


def default_route_device() -> str | None:
    """Best-effort read of the current default route device from /proc."""
    try:
        with open("/proc/net/route", encoding="utf-8") as handle:
            next(handle)  # header
            for line in handle:
                fields = line.split()
                if len(fields) > 2 and fields[1] == "00000000":
                    return fields[0]
    except (OSError, StopIteration):
        return None
    return None
