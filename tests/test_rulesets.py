"""Kill switch and transparent-routing rulesets (pure generation, no root)."""

from __future__ import annotations

from nextron.routing.transparent import TransparentRouter
from nextron.vpn.killswitch import KillSwitch, _resolve_hosts


def test_killswitch_ruleset_defaults_to_drop():
    ruleset = KillSwitch().build_nft_ruleset(("185.1.2.3",), ("tun0",), True)
    assert "policy drop" in ruleset
    assert 'oifname "lo" accept' in ruleset
    assert "ct state established,related accept" in ruleset
    assert 'oifname "tun0" accept' in ruleset
    assert "ip daddr 185.1.2.3 accept" in ruleset
    # DHCP must survive or the machine loses its lease mid-session.
    assert "udp dport { 67, 68 } accept" in ruleset


def test_killswitch_ruleset_can_exclude_the_lan():
    ruleset = KillSwitch().build_nft_ruleset((), (), False)
    assert "192.168.0.0/16" not in ruleset
    assert "policy drop" in ruleset


def test_killswitch_handles_ipv6_endpoints():
    ruleset = KillSwitch().build_nft_ruleset(("2a01:4f8::1",), (), False)
    assert "ip6 daddr 2a01:4f8::1 accept" in ruleset


def test_killswitch_reports_why_it_is_unavailable_when_disabled():
    switch = KillSwitch(enabled=False)
    usable, reason = switch.available()
    assert usable is False
    assert reason == "disabled in settings"
    assert switch.status().label.startswith("Off")


def test_endpoint_hosts_are_resolved_to_addresses():
    assert _resolve_hosts(["203.0.113.7:443"]) == ["203.0.113.7"]
    assert _resolve_hosts([""]) == []
    assert _resolve_hosts(["no-such-host.invalid"]) == []


def test_transparent_ruleset_exempts_the_tor_uid():
    ruleset = TransparentRouter().build_ruleset(
        trans_port=9040, dns_port=9053, tor_uid=123
    )
    # Without this the daemon's own connections are redirected into itself.
    assert "meta skuid 123 return" in ruleset
    assert 'oifname "lo" return' in ruleset
    assert "udp dport 53 redirect to :9053" in ruleset
    assert "tcp dport 53 redirect to :9053" in ruleset
    assert "meta l4proto tcp redirect to :9040" in ruleset
    assert "127.0.0.0/8" in ruleset


def test_transparent_routing_refuses_when_tor_runs_as_the_current_user(monkeypatch):
    """Honest degradation: SOCKS mode instead of a broken redirect."""
    import os

    monkeypatch.setattr(
        "nextron.routing.transparent.tor_daemon_uid", lambda: (os.geteuid(), "me")
    )
    monkeypatch.setattr("nextron.core.process.which", lambda binary: "/usr/sbin/nft")
    monkeypatch.setattr("nextron.core.process.is_root", lambda: True)

    usable, reason = TransparentRouter().available(daemon_is_ours=True)
    assert usable is False
    assert "current user" in reason


def test_transparent_routing_needs_nftables(monkeypatch):
    monkeypatch.setattr("nextron.core.process.which", lambda binary: None)
    usable, reason = TransparentRouter().available(daemon_is_ours=False)
    assert usable is False
    assert "nftables" in reason
