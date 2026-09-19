"""Regression tests for the Tor start-up failures found in the field.

Each test here corresponds to a defect that reached a user:

* the system ``tor.service`` holding 9050 while its ControlPort is closed, so
  NEXTRON's own daemon died with "Address already in use";
* a dead daemon being waited on for the full bootstrap timeout;
* a stall being reported as "0%" because teardown reset the counter first;
* the ControlPort being dialled on the configured port rather than the one the
  daemon actually opened.
"""

from __future__ import annotations

import asyncio
import dataclasses
import socket

import pytest

from nextron.core.config import TorSettings
from nextron.core.exceptions import TorBootstrapError
from nextron.tor.daemon import TorDaemon, TorPorts, _strip_log_prefix
from nextron.utils import net


@pytest.fixture
def occupied_port():
    """A port held open for the duration of a test."""
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    yield port
    holder.close()


# -- port probing ----------------------------------------------------------- #


def test_can_bind_detects_an_occupied_port(occupied_port):
    assert net.can_bind("127.0.0.1", occupied_port) is False
    assert net.listening_on("127.0.0.1", occupied_port) is True


def test_a_free_port_is_bindable():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    assert net.can_bind("127.0.0.1", free) is True


def test_the_preferred_port_is_kept_when_free():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    assert net.find_free_port(free) == free


def test_an_occupied_port_is_stepped_around(occupied_port):
    chosen = net.find_free_port(occupied_port)
    assert chosen != occupied_port
    assert net.can_bind("127.0.0.1", chosen)


def test_excluded_ports_are_never_reused():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    assert net.find_free_port(free, exclude={free}) != free


# -- daemon port resolution ------------------------------------------------- #


def test_ports_default_to_the_configured_preferences(bus):
    daemon = TorDaemon(TorSettings(), bus)
    ports = daemon.ports
    assert (ports.socks, ports.control) == (9050, 9051)
    assert ports.relocated is False


def test_the_daemon_steps_around_a_busy_socks_port(bus, occupied_port):
    """The exact failure from the bug report: something already owns SOCKS."""
    settings = TorSettings(socks_port=occupied_port, transparent_routing=False)
    daemon = TorDaemon(settings, bus)

    ports = daemon.resolve_ports()
    assert ports.socks != occupied_port
    assert ports.relocated is True
    assert any("SOCKS" in moved for moved in ports.moved)

    # And the generated torrc must use the port it can actually bind.
    torrc = daemon.build_torrc()
    assert f"SocksPort 127.0.0.1:{ports.socks}" in torrc
    assert f"SocksPort 127.0.0.1:{occupied_port}" not in torrc


def test_resolved_ports_never_collide(bus):
    """Two preferences that resolve into the same band must stay distinct."""
    settings = TorSettings(
        socks_port=9050, control_port=9050, dns_port=9050, trans_port=9050
    )
    ports = TorDaemon(settings, bus).resolve_ports()
    assert len({ports.socks, ports.control, ports.dns, ports.trans}) == 4


def test_every_resolved_port_reaches_the_torrc(bus):
    settings = TorSettings(transparent_routing=True)
    daemon = TorDaemon(settings, bus)
    ports = daemon.resolve_ports()
    torrc = daemon.build_torrc()

    assert f"SocksPort 127.0.0.1:{ports.socks}" in torrc
    assert f"ControlPort 127.0.0.1:{ports.control}" in torrc
    assert f"DNSPort 127.0.0.1:{ports.dns}" in torrc
    assert f"TransPort 127.0.0.1:{ports.trans}" in torrc


def test_port_properties_follow_resolution(bus, occupied_port):
    settings = TorSettings(socks_port=occupied_port, transparent_routing=False)
    daemon = TorDaemon(settings, bus)
    daemon.resolve_ports()
    assert daemon.socks_port == daemon.ports.socks
    assert daemon.socks_port != occupied_port


# -- failure reporting ------------------------------------------------------ #


def test_a_bind_failure_names_the_cause_and_the_fix(bus):
    daemon = TorDaemon(TorSettings(), bus)
    message = daemon._explain_exit(
        1, "Could not bind to 127.0.0.1:9050: Address already in use"
    )
    assert "exited immediately (code 1)" in message
    assert "Address already in use" in message
    assert "systemctl stop tor" in message


def test_a_permission_failure_points_at_the_data_directory(bus):
    message = TorDaemon(TorSettings(), bus)._explain_exit(
        1, "Permission denied when opening DataDirectory"
    )
    assert "writable" in message


def test_log_prefixes_are_stripped():
    assert (
        _strip_log_prefix("Sep 18 21:14:40.000 [warn] Could not bind to 127.0.0.1:9050")
        == "Could not bind to 127.0.0.1:9050"
    )
    assert _strip_log_prefix("[err] Reading config failed") == "Reading config failed"
    assert _strip_log_prefix("plain text") == "plain text"


async def test_a_stall_reports_the_percentage_it_reached(bus):
    """The message used to say 0% because teardown ran before the f-string."""
    daemon = TorDaemon(TorSettings(), bus)
    daemon._bootstrap_percent = 62
    daemon._bootstrap_phase = "Loading relay descriptors"

    message = await daemon._explain_stall(120.0)
    assert "62%" in message
    assert "Loading relay descriptors" in message
    # And the counters are reset by the teardown that follows.
    assert daemon.bootstrap_percent == 0


async def test_a_stall_at_zero_blames_the_network(bus):
    daemon = TorDaemon(TorSettings(), bus)
    daemon._bootstrap_percent = 0
    message = await daemon._explain_stall(120.0)
    assert "could not reach the network" in message
    assert "firewall" in message


async def test_the_ceiling_message_differs_from_a_stall(bus):
    daemon = TorDaemon(TorSettings(), bus)
    daemon._bootstrap_percent = 50
    stall = await daemon._explain_stall(120.0)
    ceiling = await TorDaemon(TorSettings(), bus)._explain_stall(600.0, ceiling=True)
    assert "stopped making progress" in stall
    assert "still bootstrapping" in ceiling


# -- fail fast on a dead daemon --------------------------------------------- #


async def test_a_daemon_that_dies_is_reported_at_once(bus, monkeypatch):
    """A dead process must not be waited on for the whole bootstrap timeout."""
    settings = TorSettings(
        bootstrap_timeout=600, transparent_routing=False, binary="/bin/false"
    )
    daemon = TorDaemon(settings, bus)
    monkeypatch.setattr("nextron.core.process.which", lambda binary: "/bin/false")

    # Never attach to a daemon that happens to be running on this machine.
    async def no_listener(host, port, **kwargs):
        return False

    monkeypatch.setattr("nextron.utils.net.is_port_open", no_listener)

    began = asyncio.get_running_loop().time()
    with pytest.raises(TorBootstrapError) as failure:
        await daemon.start()
    elapsed = asyncio.get_running_loop().time() - began

    assert elapsed < 30, f"took {elapsed:.1f}s; should fail as soon as tor exits"
    assert "exited immediately" in str(failure.value)


async def test_a_bind_error_in_the_output_reaches_the_exception(bus, monkeypatch):
    """The real tor message must survive into the error the user sees."""
    script = (
        "echo 'Sep 18 21:14:40.000 [warn] Could not bind to 127.0.0.1:9050: "
        "Address already in use. Is Tor already running?' >&2; "
        "echo '[err] Reading config failed--see warnings above.' >&2; exit 1"
    )
    settings = TorSettings(bootstrap_timeout=600, transparent_routing=False)
    daemon = TorDaemon(settings, bus)

    monkeypatch.setattr("nextron.core.process.which", lambda binary: "/bin/sh")

    # Never attach to a daemon that happens to be running on this machine.
    async def no_listener(host, port, **kwargs):
        return False

    monkeypatch.setattr("nextron.utils.net.is_port_open", no_listener)

    real_spawn = __import__("nextron.core.process", fromlist=["spawn"]).spawn

    async def fake_spawn(*command, **kwargs):
        return await real_spawn("/bin/sh", "-c", script, **kwargs)

    monkeypatch.setattr("nextron.core.process.spawn", fake_spawn)

    with pytest.raises(TorBootstrapError) as failure:
        await daemon.start()

    message = str(failure.value)
    assert "Address already in use" in message
    assert "systemctl stop tor" in message


# -- attaching to a foreign daemon ------------------------------------------ #


async def test_a_socks_only_daemon_explains_the_missing_control_port(
    bus, occupied_port, monkeypatch
):
    """Debian's tor: SOCKS open, ControlPort closed. The advice must say so."""
    settings = TorSettings(
        manage_daemon=False, socks_port=occupied_port, control_port=occupied_port + 1
    )
    daemon = TorDaemon(settings, bus)

    async def closed(host, port, **kwargs):
        return False

    monkeypatch.setattr("nextron.utils.net.is_port_open", closed)

    with pytest.raises(TorBootstrapError) as failure:
        await daemon.start()

    message = str(failure.value)
    assert "no ControlPort" in message
    assert "/etc/tor/torrc" in message
    assert "Manage own daemon" in message


def test_ports_snapshot_is_immutable():
    ports = TorPorts(socks=1, control=2, dns=3, trans=4, moved=("SOCKS 9050→1",))
    with pytest.raises((AttributeError, TypeError, dataclasses.FrozenInstanceError)):
        ports.socks = 5  # type: ignore[misc]
    assert ports.relocated is True


async def test_the_root_cause_is_preferred_over_the_summary(bus):
    """Tor's first error names the problem; the later ones just summarise it."""
    daemon = TorDaemon(TorSettings(), bus)
    daemon._log_tail.extend(
        [
            "Sep 18 21:14:40.000 [notice] Tor 0.4.9.11 opening log file.",
            "Sep 18 21:14:40.000 [warn] Could not bind to 127.0.0.1:9050: "
            "Address already in use. Is Tor already running?",
            "Sep 18 21:14:40.000 [warn] Failed to parse/validate config: "
            "Failed to bind one of the listener ports.",
            "Sep 18 21:14:40.000 [err] Reading config failed--see warnings above.",
        ]
    )
    reason = await daemon._failure_reason()
    assert reason.startswith("Could not bind to 127.0.0.1:9050")


async def test_a_summary_is_used_when_no_root_cause_was_logged(bus):
    daemon = TorDaemon(TorSettings(), bus)
    daemon._log_tail.extend(
        [
            "[notice] starting",
            "[err] Reading config failed--see warnings above.",
        ]
    )
    assert await daemon._failure_reason() == "Reading config failed--see warnings above."


async def test_no_output_at_all_yields_an_empty_reason(bus):
    assert await TorDaemon(TorSettings(), bus)._failure_reason() == ""


def test_conflux_circuits_count_as_traffic_carrying():
    """Tor 0.4.8+ carries streams on CONFLUX_LINKED circuits."""
    from nextron.tor.controller import _TRAFFIC_PURPOSES

    assert "CONFLUX_LINKED" in _TRAFFIC_PURPOSES
    assert "GENERAL" in _TRAFFIC_PURPOSES
    assert "HS_VANGUARDS" not in _TRAFFIC_PURPOSES
    assert "DIR_FETCH" not in _TRAFFIC_PURPOSES
