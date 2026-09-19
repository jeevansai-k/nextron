"""The local DNS resolver that powers the DNS Shield.

A small asyncio DNS server (UDP + TCP) that answers every query itself:

* blocked names are sinkholed immediately -- no upstream query is ever sent,
  so a blocked tracker never learns the user looked it up;
* everything else is forwarded to the configured upstream, which is Tor's
  DNSPort whenever Tor is running (so DNS cannot leak outside the tunnel) and
  the configured plain resolvers otherwise;
* answers are cached with a bounded TTL to keep a long session responsive.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from dnslib import AAAA, QTYPE, RCODE, RR, A, DNSHeader, DNSRecord

from nextron.dns.blocklist import BlocklistLibrary

log = logging.getLogger(__name__)

__all__ = ["DNSResolver", "DNSServer", "ResolverStats"]

_SINKHOLE_V4 = "0.0.0.0"
_SINKHOLE_V6 = "::"
_MAX_CACHE_ENTRIES = 8192
_UPSTREAM_TIMEOUT = 4.0


@dataclass(slots=True)
class ResolverStats:
    """Counters exposed on the dashboard and persisted periodically."""

    queries: int = 0
    blocked: int = 0
    forwarded: int = 0
    cached: int = 0
    failures: int = 0
    last_blocked: str | None = None
    recent_blocked: list[str] = field(default_factory=list)

    @property
    def block_rate(self) -> float:
        return (self.blocked / self.queries * 100.0) if self.queries else 0.0

    def note_blocked(self, domain: str) -> None:
        self.blocked += 1
        self.last_blocked = domain
        self.recent_blocked.append(domain)
        if len(self.recent_blocked) > 50:
            del self.recent_blocked[0]


class _Cache:
    """A tiny TTL cache keyed by ``(qname, qtype)``."""

    def __init__(self, ttl: int = 300, capacity: int = _MAX_CACHE_ENTRIES) -> None:
        self._ttl = ttl
        self._capacity = capacity
        self._entries: dict[tuple[str, int], tuple[float, bytes]] = {}

    def get(self, key: tuple[str, int]) -> bytes | None:
        if self._ttl <= 0:
            return None
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires, payload = entry
        if expires < time.monotonic():
            self._entries.pop(key, None)
            return None
        return payload

    def set(self, key: tuple[str, int], payload: bytes) -> None:
        if self._ttl <= 0:
            return
        if len(self._entries) >= self._capacity:
            # Cheap eviction: drop the oldest quarter of the cache.
            for stale in sorted(self._entries, key=lambda k: self._entries[k][0])[
                : self._capacity // 4
            ]:
                self._entries.pop(stale, None)
        self._entries[key] = (time.monotonic() + self._ttl, payload)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


class DNSResolver:
    """Decide, per query, whether to sinkhole, serve from cache, or forward."""

    def __init__(
        self,
        library: BlocklistLibrary,
        *,
        upstream: list[str] | None = None,
        upstream_port: int = 53,
        cache_ttl: int = 300,
        block_ipv6: bool = False,
        on_blocked=None,
    ) -> None:
        self._library = library
        self._upstream = list(upstream or ["1.1.1.1"])
        self._upstream_port = upstream_port
        self._cache = _Cache(cache_ttl)
        self._block_ipv6 = block_ipv6
        self._on_blocked = on_blocked
        self.stats = ResolverStats()

    # -- configuration ------------------------------------------------------ #

    @property
    def upstream(self) -> tuple[str, ...]:
        return tuple(self._upstream)

    @property
    def upstream_port(self) -> int:
        return self._upstream_port

    def set_upstream(self, servers: list[str], port: int = 53) -> None:
        if list(servers) == self._upstream and port == self._upstream_port:
            return
        self._upstream = list(servers) or ["1.1.1.1"]
        self._upstream_port = port
        self._cache.clear()
        log.info(
            "DNS upstream set to %s:%s", ", ".join(self._upstream), self._upstream_port
        )

    def clear_cache(self) -> None:
        self._cache.clear()

    @property
    def cache_size(self) -> int:
        return len(self._cache)

    # -- query handling ----------------------------------------------------- #

    async def resolve(self, payload: bytes) -> bytes:
        """Answer one wire-format DNS query."""
        self.stats.queries += 1
        try:
            request = DNSRecord.parse(payload)
        except Exception:
            self.stats.failures += 1
            return b""

        if not request.questions:
            return self._servfail(request)

        question = request.q
        qname = str(question.qname).rstrip(".")
        qtype = question.qtype

        if self._library.is_blocked(qname):
            self.stats.note_blocked(qname)
            if self._on_blocked is not None:
                try:
                    self._on_blocked(qname)
                except Exception:  # pragma: no cover
                    pass
            log.debug("DNS Shield blocked %s", qname)
            return self._sinkhole(request)

        if self._block_ipv6 and qtype == QTYPE.AAAA:
            # Denying AAAA keeps traffic on the IPv4 tunnel and closes a common
            # IPv6 leak path on dual-stack networks.
            return self._empty(request)

        key = (qname.lower(), qtype)
        cached = self._cache.get(key)
        if cached is not None:
            self.stats.cached += 1
            return _rewrite_id(cached, request.header.id)

        answer = await self._forward(payload)
        if answer is None:
            self.stats.failures += 1
            return self._servfail(request)

        self.stats.forwarded += 1
        self._cache.set(key, answer)
        return answer

    async def _forward(self, payload: bytes) -> bytes | None:
        """Send the query to each upstream in turn until one answers."""
        for server in self._upstream:
            try:
                return await asyncio.wait_for(
                    _query_upstream(payload, server, self._upstream_port),
                    timeout=_UPSTREAM_TIMEOUT,
                )
            except (TimeoutError, OSError) as exc:
                log.debug("Upstream %s failed: %s", server, exc)
                continue
        return None

    # -- synthetic answers -------------------------------------------------- #

    def _sinkhole(self, request: DNSRecord) -> bytes:
        """Answer a blocked name with an unroutable address."""
        reply = request.reply()
        question = request.q
        qname = question.qname
        if question.qtype == QTYPE.AAAA:
            reply.add_answer(
                RR(qname, QTYPE.AAAA, rdata=AAAA(_SINKHOLE_V6), ttl=60)
            )
        elif question.qtype in (QTYPE.A, QTYPE.ANY):
            reply.add_answer(RR(qname, QTYPE.A, rdata=A(_SINKHOLE_V4), ttl=60))
        else:
            reply.header.rcode = RCODE.NXDOMAIN
        return reply.pack()

    @staticmethod
    def _empty(request: DNSRecord) -> bytes:
        reply = request.reply()
        return reply.pack()

    @staticmethod
    def _servfail(request: DNSRecord) -> bytes:
        reply = DNSRecord(
            DNSHeader(id=request.header.id, qr=1, ra=1, rcode=RCODE.SERVFAIL),
            q=request.q if request.questions else None,
        )
        return reply.pack()


async def _query_upstream(payload: bytes, server: str, port: int) -> bytes:
    """Forward one query over UDP and return the raw response."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[bytes] = loop.create_future()

    class _Protocol(asyncio.DatagramProtocol):
        def connection_made(self, transport) -> None:
            transport.sendto(payload)

        def datagram_received(self, data: bytes, _addr) -> None:
            if not future.done():
                future.set_result(data)

        def error_received(self, exc) -> None:
            if not future.done():
                future.set_exception(exc)

        def connection_lost(self, exc) -> None:
            if not future.done() and exc is not None:
                future.set_exception(exc)

    transport, _ = await loop.create_datagram_endpoint(
        _Protocol, remote_addr=(server, port)
    )
    try:
        return await future
    finally:
        transport.close()


def _rewrite_id(payload: bytes, new_id: int) -> bytes:
    """Replace the transaction id of a cached response."""
    if len(payload) < 2:
        return payload
    return new_id.to_bytes(2, "big") + payload[2:]


class _UDPHandler(asyncio.DatagramProtocol):
    """Serve UDP queries."""

    def __init__(self, resolver: DNSResolver) -> None:
        self._resolver = resolver
        self._transport: asyncio.DatagramTransport | None = None
        self._tasks: set[asyncio.Task] = set()

    def connection_made(self, transport) -> None:
        self._transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        task = asyncio.get_running_loop().create_task(self._answer(data, addr))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _answer(self, data: bytes, addr) -> None:
        try:
            reply = await self._resolver.resolve(data)
        except Exception:  # pragma: no cover - never kill the listener
            log.exception("DNS query handling failed")
            return
        if reply and self._transport is not None:
            self._transport.sendto(reply, addr)


class DNSServer:
    """Bind the resolver to a UDP and TCP port."""

    def __init__(
        self, resolver: DNSResolver, host: str = "127.0.0.1", port: int = 53
    ) -> None:
        self._resolver = resolver
        self._host = host
        self._port = port
        self._udp_transport: asyncio.DatagramTransport | None = None
        self._tcp_server: asyncio.Server | None = None

    # -- properties --------------------------------------------------------- #

    @property
    def running(self) -> bool:
        return self._udp_transport is not None

    @property
    def address(self) -> str:
        return f"{self._host}:{self._port}"

    @property
    def port(self) -> int:
        return self._port

    # -- lifecycle ---------------------------------------------------------- #

    async def start(self, *, fallback_port: int | None = None) -> int:
        """Start listening, optionally retrying on *fallback_port*.

        Port 53 needs root (or ``CAP_NET_BIND_SERVICE``); the fallback lets an
        unprivileged session still run the Shield on a high port.
        """
        loop = asyncio.get_running_loop()
        for candidate in (self._port, fallback_port):
            if candidate is None:
                continue
            try:
                transport, _ = await loop.create_datagram_endpoint(
                    lambda: _UDPHandler(self._resolver),
                    local_addr=(self._host, candidate),
                )
            except OSError as exc:
                log.warning("Cannot bind DNS on %s:%s (%s)", self._host, candidate, exc)
                continue

            self._udp_transport = transport
            # Read the port back from the socket: an ephemeral request (port 0)
            # and a kernel-chosen port must both be reported accurately.
            socket_object = transport.get_extra_info("socket")
            if socket_object is not None:
                try:
                    candidate = int(socket_object.getsockname()[1])
                except (OSError, IndexError, TypeError):  # pragma: no cover
                    pass
            self._port = candidate
            try:
                self._tcp_server = await asyncio.start_server(
                    self._handle_tcp, self._host, candidate
                )
            except OSError as exc:  # UDP alone is still a working resolver
                log.debug("TCP DNS listener unavailable on %s: %s", candidate, exc)
                self._tcp_server = None

            log.info("DNS Shield listening on %s (UDP%s)", self.address,
                     "+TCP" if self._tcp_server else "")
            return candidate

        raise OSError(
            f"Could not bind a DNS listener on {self._host} "
            f"(tried ports {self._port}"
            + (f" and {fallback_port}" if fallback_port else "")
            + "). Run NEXTRON with sudo to use port 53."
        )

    async def stop(self) -> None:
        if self._udp_transport is not None:
            self._udp_transport.close()
            self._udp_transport = None
        if self._tcp_server is not None:
            self._tcp_server.close()
            try:
                await self._tcp_server.wait_closed()
            except Exception:  # pragma: no cover
                pass
            self._tcp_server = None
        log.info("DNS Shield listener stopped")

    async def _handle_tcp(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """DNS over TCP: a two-byte length prefix followed by the message."""
        try:
            header = await reader.readexactly(2)
            length = int.from_bytes(header, "big")
            payload = await reader.readexactly(length)
            reply = await self._resolver.resolve(payload)
            if reply:
                writer.write(len(reply).to_bytes(2, "big") + reply)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except Exception:  # pragma: no cover
            log.exception("TCP DNS query failed")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # pragma: no cover
                pass
