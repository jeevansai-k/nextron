"""The verification engine: per-mode applicability and pass/fail aggregation."""

from __future__ import annotations

from nextron.core.config import RoutingMode
from nextron.core.verification import CheckResult, VerificationEngine, VerificationReport
from nextron.state.manager import ServiceStatus


class FakeTor:
    def __init__(self, *, running=True, socks=True, is_tor=True, exit_ip="185.1.2.3"):
        self.running = running
        self.socks_port = 9050
        self.proxy_url = "socks5h://127.0.0.1:9050"
        self._socks = socks
        self._is_tor = is_tor
        self._exit_ip = exit_ip

    async def verify_socks(self):
        return self._socks

    async def confirm_tor_routing(self):
        return self._is_tor, self._exit_ip


class FakeVPN:
    def __init__(self, *, healthy=True, detail="tun0 up (10.8.0.2)"):
        self._healthy = healthy
        self._detail = detail

    async def verify(self):
        return self._healthy, self._detail


class FakeDNS:
    def __init__(self, *, running=True, ok=True):
        self.running = running
        self._ok = ok

    async def self_test(self):
        return self._ok, "resolver answered with 93.184.216.34"


def _engine(context, **kwargs) -> VerificationEngine:
    return VerificationEngine(
        context,
        kwargs.get("tor", FakeTor()),
        kwargs.get("vpn", FakeVPN()),
        kwargs.get("dns", FakeDNS()),
    )


def _patch_network(monkeypatch, *, ip="185.1.2.3", is_tor=True, ipv6=None):
    async def public_ip(**_):
        return ip

    async def tor_check(**_):
        return is_tor, ip

    async def ipv6_reachable(**_):
        return ipv6

    async def resolve_via(*_, **__):
        return ["93.184.216.34"]

    monkeypatch.setattr("nextron.utils.net.public_ip", public_ip)
    monkeypatch.setattr("nextron.utils.net.tor_check", tor_check)
    monkeypatch.setattr("nextron.utils.net.ipv6_reachable", ipv6_reachable)
    monkeypatch.setattr("nextron.utils.net.resolve_via", resolve_via)
    monkeypatch.setattr("nextron.diagnostics.leaks.net.ipv6_reachable", ipv6_reachable)
    monkeypatch.setattr("nextron.diagnostics.leaks.net.resolve_via", resolve_via)


# -- report aggregation ----------------------------------------------------- #


def test_report_summary_counts_only_applicable_checks():
    report = VerificationReport(mode=RoutingMode.TOR_ONLY)
    report.checks = [
        CheckResult("A", True, "ok"),
        CheckResult("B", True, "n/a", skipped=True),
        CheckResult("C", False, "leaks", required=False),
    ]
    assert len(report.applicable) == 2
    assert report.passed is True
    assert report.warnings and not report.failures
    assert "1 warning" in report.summary


def test_a_required_failure_fails_the_report():
    report = VerificationReport(mode=RoutingMode.VPN_ONLY)
    report.checks = [CheckResult("Tunnel", False, "gone", required=True)]
    assert report.passed is False
    assert "1 failure" in report.summary


# -- per-mode applicability ------------------------------------------------- #


async def test_tor_only_skips_vpn_checks(context, monkeypatch):
    _patch_network(monkeypatch)
    context.state.update_tor(status=ServiceStatus.ACTIVE, bootstrap_percent=100)
    engine = _engine(context)

    report = await engine.verify(RoutingMode.TOR_ONLY)
    checks = {check.name: check for check in report.checks}
    assert checks["VPN tunnel"].skipped is True
    assert checks["Tor bootstrap"].passed is True
    assert checks["SOCKS proxy"].skipped is False
    assert report.passed is True


async def test_vpn_only_skips_tor_checks(context, monkeypatch):
    _patch_network(monkeypatch, ip="203.0.113.9")
    context.state.update_vpn(status=ServiceStatus.ACTIVE, public_ip="203.0.113.9")
    engine = _engine(context)
    engine.baseline_ip = "1.2.3.4"

    report = await engine.verify(RoutingMode.VPN_ONLY)
    checks = {check.name: check for check in report.checks}
    assert checks["Tor bootstrap"].skipped is True
    assert checks["SOCKS proxy"].skipped is True
    assert checks["VPN tunnel"].passed is True
    assert checks["Routing correctness"].passed is True


async def test_vpn_only_fails_when_the_address_did_not_change(context, monkeypatch):
    _patch_network(monkeypatch, ip="1.2.3.4")
    context.state.update_vpn(status=ServiceStatus.ACTIVE, public_ip="1.2.3.4")
    engine = _engine(context)
    engine.baseline_ip = "1.2.3.4"

    report = await engine.verify(RoutingMode.VPN_ONLY)
    routing = next(c for c in report.checks if c.name == "Routing correctness")
    assert routing.passed is False
    assert report.passed is False


async def test_tor_bearing_mode_fails_when_tor_project_says_no(context, monkeypatch):
    _patch_network(monkeypatch, is_tor=False)
    context.state.update_tor(status=ServiceStatus.ACTIVE, bootstrap_percent=100)
    engine = _engine(context)

    report = await engine.verify(RoutingMode.TOR_ONLY)
    routing = next(c for c in report.checks if c.name == "Routing correctness")
    assert routing.passed is False
    assert "not using Tor" in routing.detail


async def test_tor_over_vpn_rejects_an_exit_equal_to_the_vpn_address(
    context, monkeypatch
):
    _patch_network(monkeypatch, ip="203.0.113.9")
    context.state.update_tor(status=ServiceStatus.ACTIVE, bootstrap_percent=100)
    context.state.update_vpn(status=ServiceStatus.ACTIVE, public_ip="203.0.113.9")
    engine = _engine(context)

    report = await engine.verify(RoutingMode.TOR_OVER_VPN)
    routing = next(c for c in report.checks if c.name == "Routing correctness")
    assert routing.passed is False
    assert "equals the VPN address" in routing.detail


async def test_vpn_over_tor_requires_the_vpn_to_terminate_past_tor(
    context, monkeypatch
):
    _patch_network(monkeypatch, ip="185.1.2.3")
    context.state.update_tor(status=ServiceStatus.ACTIVE, bootstrap_percent=100)
    context.state.update_vpn(status=ServiceStatus.ACTIVE, public_ip="185.1.2.3")
    engine = _engine(context)

    report = await engine.verify(RoutingMode.VPN_OVER_TOR)
    routing = next(c for c in report.checks if c.name == "Routing correctness")
    assert routing.passed is False
    assert "not terminating past Tor" in routing.detail


async def test_vpn_over_tor_passes_with_a_distinct_vpn_exit(context, monkeypatch):
    _patch_network(monkeypatch, ip="185.1.2.3")
    context.state.update_tor(status=ServiceStatus.ACTIVE, bootstrap_percent=100)
    context.state.update_vpn(status=ServiceStatus.ACTIVE, public_ip="203.0.113.9")
    engine = _engine(context)
    engine.baseline_ip = "1.2.3.4"

    report = await engine.verify(RoutingMode.VPN_OVER_TOR)
    routing = next(c for c in report.checks if c.name == "Routing correctness")
    assert routing.passed is True
    assert report.passed is True


async def test_bootstrap_below_100_is_a_failure(context, monkeypatch):
    _patch_network(monkeypatch)
    context.state.update_tor(bootstrap_percent=45, bootstrap_phase="Loading relays")
    engine = _engine(context)

    report = await engine.verify(RoutingMode.TOR_ONLY)
    bootstrap = next(c for c in report.checks if c.name == "Tor bootstrap")
    assert bootstrap.passed is False
    assert "45%" in bootstrap.detail


async def test_ipv6_leak_is_a_warning_not_a_failure(context, monkeypatch):
    _patch_network(monkeypatch, ipv6="2001:db8::1")
    context.state.update_tor(status=ServiceStatus.ACTIVE, bootstrap_percent=100)
    engine = _engine(context)

    report = await engine.verify(RoutingMode.TOR_ONLY)
    leak = next(c for c in report.checks if c.name == "IPv6 leak")
    assert leak.passed is False
    assert leak.required is False
    assert report.passed is True  # a warning must not strand a working tunnel


async def test_disabled_verification_is_reported_as_skipped(context, monkeypatch):
    _patch_network(monkeypatch)
    context.config.verification.enabled = False
    engine = _engine(context)

    report = await engine.verify(RoutingMode.TOR_ONLY)
    assert len(report.checks) == 1
    assert report.checks[0].skipped is True
    assert report.passed is True


async def test_verification_events_are_published(context, monkeypatch):
    from nextron.core.events import EventType

    _patch_network(monkeypatch)
    seen: list[str] = []
    context.bus.subscribe(EventType.VERIFICATION_STEP, lambda e: seen.append(e.message))
    context.state.update_tor(status=ServiceStatus.ACTIVE, bootstrap_percent=100)

    await _engine(context).verify(RoutingMode.TOR_ONLY)
    assert len(seen) == 8  # the full checklist, one event per step


# -- the check must measure what applications actually see ------------------- #


async def test_socks_only_is_reported_as_not_routed(context, monkeypatch):
    """It used to pass by measuring through the proxy, which always says Tor."""
    _patch_network(monkeypatch, ip="185.1.2.3", is_tor=True)
    context.state.update_tor(status=ServiceStatus.ACTIVE, bootstrap_percent=100)

    engine = _engine(context)
    engine.transparent = None                      # no system-wide redirection

    report = await engine.verify(RoutingMode.TOR_ONLY)
    routing = next(c for c in report.checks if c.name == "Routing correctness")

    assert routing.passed is False
    assert routing.required is False               # SOCKS mode still works
    assert "NOT routed" in routing.detail
    assert "sudo" in routing.detail


async def test_transparent_routing_is_measured_without_the_proxy(
    context, monkeypatch
):
    """With redirection live, the honest question is what a plain request sees."""
    used_proxy: list[object] = []

    async def tor_check(proxy=None, **kwargs):
        used_proxy.append(proxy)
        return True, "185.1.2.3"

    _patch_network(monkeypatch)
    monkeypatch.setattr("nextron.utils.net.tor_check", tor_check)
    context.state.update_tor(status=ServiceStatus.ACTIVE, bootstrap_percent=100)

    engine = _engine(context)
    engine.transparent = type("S", (), {"active": True})()

    report = await engine.verify(RoutingMode.TOR_ONLY)
    routing = next(c for c in report.checks if c.name == "Routing correctness")

    assert used_proxy == [None], "the system check must not go through the proxy"
    assert routing.passed is True
    assert "whole system" in routing.detail


async def test_transparent_routing_that_does_not_work_is_a_hard_failure(
    context, monkeypatch
):
    _patch_network(monkeypatch, is_tor=False)
    context.state.update_tor(status=ServiceStatus.ACTIVE, bootstrap_percent=100)

    engine = _engine(context)
    engine.transparent = type("S", (), {"active": True})()

    report = await engine.verify(RoutingMode.TOR_ONLY)
    routing = next(c for c in report.checks if c.name == "Routing correctness")
    assert routing.passed is False
    assert routing.required is True
    assert "not exiting through Tor" in routing.detail
