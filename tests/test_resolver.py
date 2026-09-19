"""The DNS Shield resolver: sinkholing, caching and forwarding decisions."""

from __future__ import annotations

import pytest
from dnslib import QTYPE, RCODE, RR, A, DNSRecord

from nextron.dns.blocklist import BlocklistLibrary
from nextron.dns.resolver import DNSResolver, DNSServer
from nextron.storage import paths


@pytest.fixture
def library() -> BlocklistLibrary:
    (paths.dns_dir() / "ads.hosts").write_text(
        "0.0.0.0 ads.example.com\n0.0.0.0 tracker.example.net\n", encoding="utf-8"
    )
    instance = BlocklistLibrary()
    instance.load()
    return instance


@pytest.fixture
def resolver(library, monkeypatch) -> DNSResolver:
    """A resolver whose upstream is a stub -- no real DNS traffic in tests."""
    instance = DNSResolver(library, upstream=["203.0.113.53"], cache_ttl=300)

    async def fake_forward(payload: bytes) -> bytes:
        request = DNSRecord.parse(payload)
        reply = request.reply()
        reply.add_answer(RR(request.q.qname, QTYPE.A, rdata=A("93.184.216.34"), ttl=60))
        return reply.pack()

    monkeypatch.setattr(instance, "_forward", fake_forward)
    return instance


def _free_udp_port() -> int:
    """Ask the kernel for an unused UDP port."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def _ask(resolver: DNSResolver, name: str, qtype: str = "A") -> DNSRecord:
    query = DNSRecord.question(name, qtype).pack()
    return DNSRecord.parse(await resolver.resolve(query))


async def test_blocked_name_is_sinkholed_without_an_upstream_query(resolver):
    answer = await _ask(resolver, "ads.example.com")
    assert [str(rr.rdata) for rr in answer.rr] == ["0.0.0.0"]
    assert resolver.stats.blocked == 1
    assert resolver.stats.forwarded == 0


async def test_blocked_subdomain_is_sinkholed(resolver):
    answer = await _ask(resolver, "cdn.ads.example.com")
    assert [str(rr.rdata) for rr in answer.rr] == ["0.0.0.0"]


async def test_blocked_aaaa_returns_the_v6_sinkhole(resolver):
    answer = await _ask(resolver, "ads.example.com", "AAAA")
    assert [str(rr.rdata) for rr in answer.rr] == ["::"]


async def test_allowed_name_is_forwarded(resolver):
    answer = await _ask(resolver, "example.com")
    assert [str(rr.rdata) for rr in answer.rr] == ["93.184.216.34"]
    assert resolver.stats.forwarded == 1
    assert resolver.stats.blocked == 0


async def test_second_identical_query_is_served_from_cache(resolver):
    await _ask(resolver, "example.com")
    await _ask(resolver, "example.com")
    assert resolver.stats.forwarded == 1
    assert resolver.stats.cached == 1


async def test_cached_reply_keeps_the_caller_transaction_id(resolver):
    await _ask(resolver, "example.com")
    query = DNSRecord.question("example.com", "A")
    query.header.id = 4242
    reply = DNSRecord.parse(await resolver.resolve(query.pack()))
    assert reply.header.id == 4242


async def test_blocked_callback_fires(library):
    seen: list[str] = []
    resolver = DNSResolver(library, on_blocked=seen.append)
    await _ask(resolver, "tracker.example.net")
    assert seen == ["tracker.example.net"]


async def test_upstream_failure_becomes_servfail(library):
    resolver = DNSResolver(library, upstream=["203.0.113.53"])

    async def always_fail(payload: bytes) -> bytes | None:
        return None

    resolver._forward = always_fail  # type: ignore[assignment]
    answer = await _ask(resolver, "example.com")
    assert answer.header.rcode == RCODE.SERVFAIL
    assert resolver.stats.failures == 1


async def test_malformed_query_is_ignored(resolver):
    assert await resolver.resolve(b"\x00\x01garbage") == b""


async def test_block_ipv6_answers_returns_an_empty_aaaa(library):
    resolver = DNSResolver(library, block_ipv6=True)
    answer = await _ask(resolver, "example.com", "AAAA")
    assert answer.rr == []


async def test_stats_track_the_block_rate(resolver):
    await _ask(resolver, "ads.example.com")
    await _ask(resolver, "example.com")
    assert resolver.stats.queries == 2
    assert 49 < resolver.stats.block_rate < 51


async def test_server_binds_and_serves_over_udp(resolver):
    server = DNSServer(resolver, "127.0.0.1", 0)
    port = await server.start()
    try:
        assert port > 0
        assert server.running
        assert server.address.startswith("127.0.0.1:")
    finally:
        await server.stop()
    assert not server.running


async def test_server_falls_back_when_the_first_port_is_unavailable(
    resolver, monkeypatch
):
    """Port 53 needs root; an unprivileged session must still get a listener."""
    import asyncio

    loop = asyncio.get_running_loop()
    original = loop.create_datagram_endpoint
    refused: list[int] = []

    async def picky(factory, *args, local_addr=None, **kwargs):
        if local_addr is not None and local_addr[1] == 53:
            refused.append(53)
            raise PermissionError("port 53 requires root")
        return await original(factory, *args, local_addr=local_addr, **kwargs)

    monkeypatch.setattr(loop, "create_datagram_endpoint", picky)

    server = DNSServer(resolver, "127.0.0.1", 53)
    port = await server.start(fallback_port=0)
    try:
        assert refused == [53]
        assert port not in (0, 53)
        assert server.address == f"127.0.0.1:{port}"
    finally:
        await server.stop()


async def test_server_reports_a_failure_when_nothing_can_bind(resolver, monkeypatch):
    import asyncio

    loop = asyncio.get_running_loop()

    async def always_refuse(*args, **kwargs):
        raise PermissionError("no ports for you")

    monkeypatch.setattr(loop, "create_datagram_endpoint", always_refuse)
    server = DNSServer(resolver, "127.0.0.1", 53)
    with pytest.raises(OSError, match="Could not bind"):
        await server.start(fallback_port=5353)


async def test_shield_honours_a_port_changed_after_construction(context):
    """Editing dns.listen_port in Settings must actually move the listener."""
    from nextron.dns.shield import DNSShield
    from nextron.storage import paths

    (paths.dns_dir() / "ads.hosts").write_text(
        "0.0.0.0 ads.example.com\n", encoding="utf-8"
    )
    context.config.dns.manage_resolv_conf = False
    shield = DNSShield(context)

    # The Shield was built while the port was still the default 53.
    free_port = _free_udp_port()
    context.config.dns.listen_port = free_port
    context.config.dns.fallback_port = _free_udp_port()
    await shield.start()
    try:
        assert shield.running
        assert shield.listen_port == free_port
        assert context.state.state.dns.listen == f"127.0.0.1:{shield.listen_port}"
        ok, detail = await shield.self_test()
        assert ok, detail
    finally:
        await shield.stop()
