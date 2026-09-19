"""Doctor diagnostics."""

from __future__ import annotations

from nextron.core.config import NextronConfig
from nextron.diagnostics.doctor import Doctor, Status
from nextron.diagnostics.leaks import classify_nameservers, system_nameservers


async def test_offline_report_covers_every_section():
    report = await Doctor(NextronConfig()).run(network=False)
    sections = set(report.sections())
    assert {"Privileges", "Dependencies", "Services", "Storage", "VPN", "DNS Shield"} <= sections
    assert report.summary


async def test_report_notes_missing_profiles():
    report = await Doctor(NextronConfig()).run(network=False)
    finding = next(f for f in report.findings if f.name == "VPN availability")
    assert finding.status is Status.WARN
    assert "nextron vpn import" in finding.hint


async def test_report_sees_imported_profiles(profiles):
    report = await Doctor(NextronConfig()).run(network=False)
    finding = next(f for f in report.findings if f.name == "VPN availability")
    assert finding.status is Status.OK
    assert "3 profiles" in finding.detail

    over_tor = next(f for f in report.findings if f.name == "VPN over Tor capable")
    assert "1 TCP OpenVPN" in over_tor.detail


async def test_missing_tor_binary_is_a_failure(monkeypatch):
    monkeypatch.setattr(
        "nextron.core.process.which",
        lambda binary: None if binary == "tor" else f"/usr/bin/{binary}",
    )
    report = await Doctor(NextronConfig()).run(network=False)
    finding = next(f for f in report.findings if f.name == "tor installed")
    assert finding.status is Status.FAIL
    assert "apt install tor" in finding.hint
    assert not report.healthy


async def test_healthy_when_everything_resolves(monkeypatch):
    monkeypatch.setattr(
        "nextron.core.process.which", lambda binary: f"/usr/bin/{binary}"
    )
    monkeypatch.setattr("nextron.core.process.is_root", lambda: True)
    report = await Doctor(NextronConfig()).run(network=False)
    assert report.healthy, [f.name for f in report.failures]


def test_nameserver_classification():
    report = classify_nameservers(["127.0.0.53", "10.8.0.1", "1.1.1.1"])
    assert report.loopback == ("127.0.0.53",)
    assert report.private == ("10.8.0.1",)
    assert report.public == ("1.1.1.1",)
    assert report.any_configured is True


def test_nameserver_classification_handles_an_empty_file(tmp_path):
    missing = tmp_path / "resolv.conf"
    assert system_nameservers(missing) == []
    assert classify_nameservers([]).any_configured is False


# -- DNS leak probe precision ----------------------------------------------- #


async def test_leak_probe_names_the_shield_only_when_it_matches(monkeypatch):
    """A 127.0.0.53 stub is systemd-resolved, not the Shield."""
    from nextron.diagnostics import leaks

    monkeypatch.setattr(leaks, "system_nameservers", lambda path=None: ["127.0.0.53"])
    leak_free, detail = await leaks.dns_leak_probe(
        shield_running=True,
        shield_uses_tor=False,
        tunnel_interface=None,
        shield_address="127.0.0.1",
    )
    assert leak_free is False
    assert "stub resolver" in detail

    leak_free, detail = await leaks.dns_leak_probe(
        shield_running=True,
        shield_uses_tor=False,
        tunnel_interface=None,
        shield_address="127.0.0.53",
    )
    assert leak_free is True
    assert "local Shield" in detail


async def test_leak_probe_trusts_tor_dns(monkeypatch):
    from nextron.diagnostics import leaks

    leak_free, detail = await leaks.dns_leak_probe(
        shield_running=True, shield_uses_tor=True, tunnel_interface="tun0"
    )
    assert leak_free is True
    assert "DNSPort" in detail


async def test_leak_probe_flags_a_reachable_public_resolver(monkeypatch):
    from nextron.diagnostics import leaks

    monkeypatch.setattr(leaks, "system_nameservers", lambda path=None: ["8.8.8.8"])

    async def answers(*args, **kwargs):
        return ["93.184.216.34"]

    monkeypatch.setattr(leaks.net, "resolve_via", answers)
    leak_free, detail = await leaks.dns_leak_probe(
        shield_running=False, shield_uses_tor=False, tunnel_interface="tun0"
    )
    assert leak_free is False
    assert "8.8.8.8" in detail
    assert "outside the tunnel" in detail
