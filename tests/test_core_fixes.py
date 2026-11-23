"""Regression tests for core bugs found in the November 2026 review.

Each test pins one failure that used to happen in a real session: a kill switch
left armed after a failed connect, a rotation switch that turned itself off, a
half-built route surviving a cancelled connect, and so on.
"""

from __future__ import annotations

import asyncio
import subprocess
import time

import pytest

from nextron.core import process
from nextron.core.config import RoutingMode
from nextron.core.events import EventType
from nextron.core.exceptions import RoutingError, VPNConnectionError
from nextron.core.runtime import NextronRuntime
from nextron.state.manager import ServiceStatus


@pytest.fixture
async def runtime(profiles):
    instance = NextronRuntime()
    await instance.start()
    yield instance
    await instance.shutdown()


@pytest.fixture(autouse=True)
def _fresh_sudo_probe(monkeypatch):
    monkeypatch.setattr(process, "_sudo_probe", None)


# -- privileges ------------------------------------------------------------ #


def test_sudo_available_means_sudo_actually_works(monkeypatch):
    """A sudo binary that wants a password is not 'available'."""
    monkeypatch.setattr(process, "sudo_installed", lambda: True)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, b"", b"password"),
    )
    assert process.sudo_available() is False


def test_sudo_available_is_true_with_cached_credentials(monkeypatch):
    monkeypatch.setattr(process, "sudo_installed", lambda: True)
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert process.sudo_available() is True
    assert process.sudo_available() is True
    assert len(calls) == 1, "the probe is cached"


def test_needs_password_recognises_sudo_refusals():
    assert process.needs_password("sudo: a password is required")
    assert process.needs_password("sudo: a terminal is required to read the password")
    assert not process.needs_password("AUTH_FAILED")


async def test_vpn_connect_without_root_fails_fast_without_retries(
    runtime, monkeypatch
):
    monkeypatch.setattr(process, "can_escalate", lambda: False)
    attempts = []

    async def bring_up(target):
        attempts.append(target)

    monkeypatch.setattr(runtime.vpn, "_bring_up", bring_up)
    began = time.monotonic()
    with pytest.raises(VPNConnectionError, match="needs root"):
        await runtime.vpn.connect("berlin")
    assert attempts == []
    assert time.monotonic() - began < 2
    assert runtime.state.vpn.status is ServiceStatus.ERROR


async def test_sudo_password_failure_is_not_retried(runtime, monkeypatch):
    monkeypatch.setattr(process, "can_escalate", lambda: True)
    runtime.config.vpn.killswitch_enabled = False
    attempts = []

    async def bring_up(target):
        attempts.append(target)
        raise VPNConnectionError("berlin: sudo: a password is required")

    monkeypatch.setattr(runtime.vpn, "_bring_up", bring_up)
    with pytest.raises(VPNConnectionError, match="sudo -v"):
        await runtime.vpn.connect("berlin")
    assert len(attempts) == 1


# -- kill switch ------------------------------------------------------------ #


async def test_failed_first_connect_releases_the_kill_switch(runtime, monkeypatch):
    """The machine must not stay offline because a VPN never came up."""
    released = []

    async def failing_connect(profile=None, *, reason="manual"):
        runtime.vpn.killswitch._active = True  # armed by _guard_transition
        raise VPNConnectionError("tunnel not established")

    async def release(*, force=False):
        released.append(force)
        runtime.vpn.killswitch._active = False
        return runtime.vpn.killswitch.status()

    monkeypatch.setattr(runtime.vpn, "connect", failing_connect)
    monkeypatch.setattr(runtime.vpn.killswitch, "release", release)
    monkeypatch.setattr(runtime.routing.verification, "capture_baseline", _noop)

    with pytest.raises(RoutingError):
        await runtime.routing.establish(RoutingMode.VPN_ONLY)

    assert released, "teardown released the kill switch"
    assert runtime.vpn.killswitch.active is False


# -- VPN switching ---------------------------------------------------------- #


async def test_switching_from_openvpn_to_wireguard_is_not_reported_as_failure(
    runtime, monkeypatch
):
    monkeypatch.setattr(process, "can_escalate", lambda: True)
    runtime.config.vpn.killswitch_enabled = False
    runtime.config.vpn.verify_tunnel = False

    berlin = runtime.vpn.library.resolve("berlin")
    amsterdam = runtime.vpn.library.resolve("amsterdam")
    runtime.vpn._current = berlin  # an OpenVPN tunnel is "up"
    openvpn_up = {"value": True}
    wireguard_up = {"value": False}

    monkeypatch.setattr(
        type(runtime.vpn._openvpn), "connected", property(lambda s: openvpn_up["value"])
    )
    monkeypatch.setattr(
        type(runtime.vpn._wireguard),
        "connected",
        property(lambda s: wireguard_up["value"]),
    )

    async def teardown():
        openvpn_up["value"] = False

    async def bring_up(target):
        wireguard_up["value"] = True

    monkeypatch.setattr(runtime.vpn, "_teardown_current", teardown)
    monkeypatch.setattr(runtime.vpn, "_bring_up", bring_up)

    connected = await runtime.vpn.connect(amsterdam)
    assert connected.id == amsterdam.id
    assert runtime.vpn.current.id == amsterdam.id
    assert runtime.state.vpn.status is ServiceStatus.ACTIVE


# -- schedulers -------------------------------------------------------------- #


async def test_stopping_the_scheduler_keeps_the_users_rotation_switch(runtime):
    """Disconnecting or quitting must not switch rotation off in config.toml."""
    from nextron.core.config import ConfigManager

    runtime.config.tor.rotation_enabled = True
    runtime.config_manager.save()

    await runtime.tor_scheduler.start()
    await runtime.tor_scheduler.stop()
    await runtime.disconnect()

    assert runtime.config.tor.rotation_enabled is True
    assert ConfigManager().load().tor.rotation_enabled is True


async def test_switch_state_is_seeded_from_config(profiles):
    from nextron.core.config import ConfigManager

    manager = ConfigManager()
    manager.load()
    manager.config.tor.rotation_enabled = True
    manager.config.vpn.shuffle_enabled = True
    manager.save()

    runtime = NextronRuntime()
    try:
        assert runtime.state.tor.rotation_enabled is True
        assert runtime.state.vpn.shuffle_enabled is True
    finally:
        await runtime.shutdown()


# -- routing: cancellation and teardown -------------------------------------- #


async def test_cancelling_a_connect_tears_the_half_built_route_down(
    runtime, monkeypatch
):
    started = asyncio.Event()
    stopped = []
    running = {"value": False}

    async def slow_start():
        running["value"] = True
        started.set()
        await asyncio.sleep(3600)

    async def stop():
        stopped.append(True)
        running["value"] = False

    monkeypatch.setattr(runtime.tor, "start", slow_start)
    monkeypatch.setattr(runtime.tor, "stop", stop)
    monkeypatch.setattr(type(runtime.tor), "running", property(lambda s: running["value"]))
    monkeypatch.setattr(runtime.routing.verification, "capture_baseline", _noop)

    task = asyncio.create_task(runtime.routing.establish(RoutingMode.TOR_ONLY))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert stopped, "the Tor daemon was stopped"
    assert runtime.routing.active is False
    assert runtime.routing.busy is False
    assert runtime.state.route_status is ServiceStatus.IDLE


async def test_teardown_keeps_going_when_one_step_fails(runtime, monkeypatch):
    stopped = []

    async def broken_dns_stop():
        raise OSError("resolv.conf is read-only")

    async def tor_stop():
        stopped.append("tor")

    monkeypatch.setattr(type(runtime.dns), "running", property(lambda s: True))
    monkeypatch.setattr(runtime.dns, "stop", broken_dns_stop)
    monkeypatch.setattr(type(runtime.tor), "running", property(lambda s: True))
    monkeypatch.setattr(runtime.tor, "stop", tor_stop)
    runtime.routing._mode = RoutingMode.TOR_ONLY

    await runtime.routing.teardown()

    assert stopped == ["tor"]
    assert runtime.routing.active is False
    assert runtime.state.route_status is ServiceStatus.IDLE


async def test_unexpected_errors_still_tear_the_route_down(runtime, monkeypatch):
    stopped = []
    running = {"value": False}

    async def exploding_start():
        running["value"] = True
        raise OSError("disk full")

    async def stop():
        stopped.append(True)
        running["value"] = False

    monkeypatch.setattr(runtime.tor, "start", exploding_start)
    monkeypatch.setattr(runtime.tor, "stop", stop)
    monkeypatch.setattr(type(runtime.tor), "running", property(lambda s: running["value"]))
    monkeypatch.setattr(runtime.routing.verification, "capture_baseline", _noop)

    with pytest.raises(RoutingError, match="disk full"):
        await runtime.routing.establish(RoutingMode.TOR_ONLY)
    assert stopped


# -- OpenVPN ----------------------------------------------------------------- #


async def test_openvpn_that_dies_immediately_fails_fast(profiles, monkeypatch):
    """No more waiting out the whole connect timeout for a process that exited."""
    from nextron.vpn.openvpn import OpenVPNBackend

    backend = OpenVPNBackend("sh")

    async def fake_spawn(*_args, **_kwargs):
        return await process.spawn("sh", "-c", "echo 'starting up'; exit 3")

    monkeypatch.setattr(process, "spawn_privileged", fake_spawn)
    profile = profiles.resolve("berlin")

    began = time.monotonic()
    with pytest.raises(VPNConnectionError, match="code 3"):
        await backend.connect(profile, timeout=30)
    assert time.monotonic() - began < 10


async def test_openvpn_sudo_refusal_is_reported_as_such(profiles, monkeypatch):
    from nextron.vpn.openvpn import OpenVPNBackend

    backend = OpenVPNBackend("sh")

    async def fake_spawn(*_args, **_kwargs):
        return await process.spawn(
            "sh", "-c", "echo 'sudo: a password is required'; exit 1"
        )

    monkeypatch.setattr(process, "spawn_privileged", fake_spawn)
    with pytest.raises(VPNConnectionError, match="password is required"):
        await backend.connect(profiles.resolve("berlin"), timeout=30)


# -- shutdown ----------------------------------------------------------------- #


async def test_second_shutdown_is_a_no_op(profiles):
    runtime = NextronRuntime()
    await runtime.start()
    events = []
    runtime.bus.subscribe(EventType.APP_SHUTDOWN, events.append)

    await runtime.shutdown()
    await runtime.shutdown()
    assert len(events) == 1


async def _noop(*_args, **_kwargs):
    return None


# -- honest status words -------------------------------------------------------- #


def test_socks_only_route_is_never_called_protected():
    """Tor up but no system redirection: only proxied apps are covered."""
    from nextron.state.manager import AppState
    from nextron.tui.widgets.signal import protection

    state = AppState(
        routing_mode=RoutingMode.TOR_ONLY,
        route_status=ServiceStatus.ACTIVE,
        verification_passed=True,
    )
    assert protection(state, socks_only=True)[1] == "PROXY ONLY"
    assert protection(state, socks_only=False)[1] == "PROTECTED"
    idle = AppState(routing_mode=RoutingMode.TOR_ONLY)
    assert protection(idle)[1] == "EXPOSED"


def test_a_failed_optional_check_is_marked_as_a_warning():
    from nextron.core.verification import CheckResult

    assert CheckResult("IPv6 leak", False, required=False).marker == "!"
    assert CheckResult("Tor bootstrap", False, required=True).marker == "✗"
    assert CheckResult("SOCKS proxy", True).marker == "✓"
