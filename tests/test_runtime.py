"""The runtime and routing manager, exercised without touching the network."""

from __future__ import annotations

import pytest

from nextron.core.config import RoutingMode
from nextron.core.exceptions import RoutingError
from nextron.core.runtime import NextronRuntime
from nextron.state.manager import ServiceStatus


@pytest.fixture
async def runtime(profiles):
    instance = NextronRuntime()
    await instance.start()
    yield instance
    await instance.shutdown()


async def test_runtime_starts_and_loads_the_library(runtime):
    assert len(runtime.vpn.library) == 3
    assert runtime.state.route_status is ServiceStatus.IDLE
    assert runtime.connected is False


async def test_shutdown_is_idempotent(runtime):
    await runtime.shutdown()
    await runtime.shutdown()


async def test_mode_change_is_persisted(runtime, monkeypatch):
    """Establishing a mode writes it back to config.toml."""
    from nextron.core.config import ConfigManager

    async def fake_establish(mode, *, vpn_profile=None):
        from nextron.core.verification import VerificationReport

        runtime.routing._mode = mode
        return VerificationReport(mode=mode)

    monkeypatch.setattr(runtime.routing, "establish", fake_establish)
    await runtime.connect(RoutingMode.VPN_ONLY, start_schedulers=False)
    assert ConfigManager().load().routing_mode is RoutingMode.VPN_ONLY


async def test_vpn_over_tor_refuses_a_wireguard_profile(runtime, monkeypatch):
    """WireGuard is UDP-only, so it cannot traverse Tor's SOCKS proxy."""

    async def fake_tor_start():
        return None

    monkeypatch.setattr(runtime.tor, "start", fake_tor_start)
    monkeypatch.setattr(runtime.tor, "verify_socks", lambda: _true())

    with pytest.raises(RoutingError, match="TCP OpenVPN"):
        await runtime.routing.establish(
            RoutingMode.VPN_OVER_TOR, vpn_profile="amsterdam"
        )


async def test_vpn_over_tor_refuses_a_udp_openvpn_profile(runtime, monkeypatch):
    async def fake_tor_start():
        return None

    monkeypatch.setattr(runtime.tor, "start", fake_tor_start)
    monkeypatch.setattr(runtime.tor, "verify_socks", lambda: _true())

    with pytest.raises(RoutingError, match="proto tcp"):
        await runtime.routing.establish(RoutingMode.VPN_OVER_TOR, vpn_profile="tokyo")


async def _true() -> bool:
    return True


async def test_toggling_rotation_requires_a_tor_mode(runtime):
    runtime.config.routing_mode = RoutingMode.VPN_ONLY
    assert await runtime.toggle_tor_rotation() is False


async def test_toggling_shuffle_requires_two_profiles(runtime):
    runtime.config.routing_mode = RoutingMode.VPN_ONLY
    runtime.set_shuffle_pool([runtime.vpn.library.all()[0].id])
    assert await runtime.toggle_vpn_shuffle() is False


async def test_shuffle_pool_and_algorithm_round_trip(runtime):
    from nextron.core.config import ConfigManager, ShuffleAlgorithm

    ids = [profile.id for profile in runtime.vpn.library.all()[:2]]
    runtime.set_shuffle_pool(ids)
    runtime.set_shuffle_algorithm(ShuffleAlgorithm.NO_REPEAT)

    stored = ConfigManager().load()
    assert stored.vpn.shuffle_pool == ids
    assert stored.vpn.shuffle_algorithm is ShuffleAlgorithm.NO_REPEAT
    assert runtime.vpn.shuffle.pool_size == 2


async def test_importing_a_profile_updates_the_pool(runtime, tmp_path):
    source = tmp_path / "oslo.ovpn"
    source.write_text("client\nproto tcp\nremote no.example.com 443\n", encoding="utf-8")
    profile = runtime.import_vpn_profile(source)
    assert profile.name == "oslo"
    assert runtime.vpn.shuffle.pool_size == 4


async def test_rotate_without_tor_is_refused(runtime):
    assert await runtime.rotate_now() is False


async def test_teardown_leaves_no_mode_active(runtime):
    await runtime.routing.teardown()
    assert runtime.routing.mode is None
    assert runtime.state.route_status is ServiceStatus.IDLE
