"""Engine-level behaviour that can be verified without root or a network."""

from __future__ import annotations

import asyncio

import pytest

from nextron.core.config import TorSettings
from nextron.core.exceptions import (
    VPNConnectionError,
    VPNCredentialsRequired,
    VPNError,
)
from nextron.storage import paths
from nextron.tor.daemon import _BOOTSTRAP_RE, TorDaemon
from nextron.vpn.openvpn import OpenVPNBackend
from nextron.vpn.wireguard import WireGuardBackend

# -- Tor daemon ------------------------------------------------------------- #


def test_generated_torrc_contains_every_required_port(bus):
    settings = TorSettings(
        socks_port=9150, control_port=9151, dns_port=9153, trans_port=9140
    )
    torrc = TorDaemon(settings, bus).build_torrc()
    assert "SocksPort 127.0.0.1:9150" in torrc
    assert "ControlPort 127.0.0.1:9151" in torrc
    assert "DNSPort 127.0.0.1:9153" in torrc
    assert "TransPort 127.0.0.1:9140" in torrc
    assert "CookieAuthentication 1" in torrc
    assert f"DataDirectory {paths.tor_data_dir()}" in torrc


def test_transparent_routing_can_be_switched_off(bus):
    torrc = TorDaemon(TorSettings(transparent_routing=False), bus).build_torrc()
    assert "TransPort" not in torrc


def test_exit_country_selection_is_rendered(bus):
    settings = TorSettings(exit_countries=["DE", "NL"], strict_exit_nodes=True)
    torrc = TorDaemon(settings, bus).build_torrc()
    assert "ExitNodes {de},{nl}" in torrc
    assert "StrictNodes 1" in torrc


def test_torrc_is_written_to_the_runtime_directory(bus):
    daemon = TorDaemon(TorSettings(), bus)
    path = daemon.write_torrc()
    assert path == paths.runtime_dir() / "torrc"
    assert "SocksPort" in path.read_text()


@pytest.mark.parametrize(
    "line,percent,phase",
    [
        (
            "Sep 18 01:00:00.000 [notice] Bootstrapped 45% "
            "(requesting_descriptors): Asking for relay descriptors",
            45,
            "Asking for relay descriptors",
        ),
        ("[notice] Bootstrapped 100% (done): Done", 100, "Done"),
        ("[notice] Bootstrapped 0%", 0, None),
    ],
)
def test_bootstrap_lines_are_parsed(line, percent, phase):
    match = _BOOTSTRAP_RE.search(line)
    assert match is not None
    assert int(match.group("percent")) == percent
    if phase is not None:
        assert match.group("summary") == phase


# -- OpenVPN ---------------------------------------------------------------- #


def test_socks_proxy_is_injected_for_vpn_over_tor(profiles):
    berlin = profiles.by_name("berlin")
    config = OpenVPNBackend().runtime_config(berlin, socks_proxy=("127.0.0.1", 9050))
    text = config.read_text()
    assert "socks-proxy 127.0.0.1 9050" in text
    assert "socks-proxy-retry" in text
    # A loopback hop needs no route exclusion.
    assert "route 127.0.0.1" not in text
    # The user's original profile is never modified.
    assert "socks-proxy" not in berlin.read_config()


def test_remote_socks_hop_gets_a_route_exclusion(profiles):
    berlin = profiles.by_name("berlin")
    text = OpenVPNBackend().runtime_config(
        berlin, socks_proxy=("192.0.2.10", 1080)
    ).read_text()
    assert "route 192.0.2.10 255.255.255.255 net_gateway" in text


def test_udp_profiles_cannot_use_a_socks_hop(profiles):
    tokyo = profiles.by_name("tokyo")
    with pytest.raises(VPNConnectionError, match="TCP OpenVPN profile"):
        OpenVPNBackend().runtime_config(tokyo, socks_proxy=("127.0.0.1", 9050))


def test_runtime_config_is_private(profiles):
    config = OpenVPNBackend().runtime_config(profiles.by_name("berlin"))
    assert oct(config.stat().st_mode)[-3:] == "600"


def test_credentials_directive_replaces_any_existing_one(profiles, tmp_path):
    creds = tmp_path / "creds.txt"
    creds.write_text("user\npass\n", encoding="utf-8")
    profile = profiles.by_name("berlin")
    profile.path.write_text(
        profile.read_config() + "auth-user-pass\n", encoding="utf-8"
    )
    updated = profiles.set_auth_file(profile.id, str(creds))

    text = OpenVPNBackend().runtime_config(updated).read_text()
    assert f"auth-user-pass {creds}" in text
    assert text.count("auth-user-pass") == 1


def test_a_profile_that_wants_a_password_is_refused_before_openvpn_starts(profiles):
    """OpenVPN with no console hands the question to systemd and waits there.

    The connection then dies on the timeout, minutes later, with a deprecation
    warning as its only explanation. Refuse it immediately instead.
    """
    profile = profiles.by_name("berlin")
    profile.path.write_text(
        profile.read_config() + "auth-user-pass\n", encoding="utf-8"
    )
    reloaded = profiles.get(profile.id)

    with pytest.raises(VPNCredentialsRequired, match="username and password"):
        OpenVPNBackend().runtime_config(reloaded)


def test_a_credentials_file_makes_the_same_profile_acceptable(profiles, tmp_path):
    creds = tmp_path / "creds.txt"
    creds.write_text("user\npass\n", encoding="utf-8")
    profile = profiles.by_name("berlin")
    profile.path.write_text(
        profile.read_config() + "auth-user-pass\n", encoding="utf-8"
    )
    updated = profiles.set_auth_file(profile.id, str(creds))

    text = OpenVPNBackend().runtime_config(updated).read_text()
    assert f"auth-user-pass {creds}" in text


def test_openvpn_is_told_never_to_ask_interactively(profiles):
    text = OpenVPNBackend().runtime_config(profiles.by_name("berlin")).read_text()
    assert "auth-retry nointeract" in text


async def test_openvpn_output_reaches_the_reader_and_the_log(profiles, monkeypatch):
    """The reader must see OpenVPN's own lines, and the log must still be written.

    ``--log-append`` used to be passed, which redirects OpenVPN's stdout into
    the file and leaves the pipe empty: "Initialization Sequence Completed"
    never arrived and every tunnel timed out however well it was going.
    """
    from nextron.core import process
    from nextron.vpn import openvpn as module

    spawned: list[tuple[str, ...]] = []

    async def fake_spawn(*command, **kwargs):
        spawned.append(command)
        # A tiny stand-in for openvpn: greet, then report success.
        return await asyncio.create_subprocess_exec(
            "printf",
            "starting up\nTUN/TAP device tun9 opened\n"
            "Initialization Sequence Completed\n",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

    monkeypatch.setattr(process, "spawn_privileged", fake_spawn)
    monkeypatch.setattr(module.process, "spawn_privileged", fake_spawn)

    backend = OpenVPNBackend()
    result = await backend.connect(profiles.by_name("berlin"), timeout=10)

    assert result.ready
    assert result.interface == "tun9"

    # No output redirection may be handed to openvpn.
    assert not [arg for arg in spawned[0] if arg.startswith("--log")]

    # The reader saw the lines...
    assert any("Initialization Sequence Completed" in line for line in backend.log_tail)
    # ...and wrote them to the log itself.
    logged = (paths.logs_dir() / "openvpn.log").read_text(encoding="utf-8")
    assert "Initialization Sequence Completed" in logged


def test_a_timeout_reports_something_better_than_startup_chatter():
    from nextron.vpn.openvpn import _meaningful_tail

    lines = (
        "2026-01-01 DEPRECATED OPTION: --max-routes option ignored.",
        "2026-01-01 OpenVPN 2.6.19 x86_64-pc-linux-gnu",
        "2026-01-01 library versions: OpenSSL 3.0.13",
        "2026-01-01 TLS Error: TLS key negotiation failed to occur within 60s",
    )
    tail = _meaningful_tail(lines)
    assert "TLS key negotiation failed" in tail
    assert "DEPRECATED" not in tail

    # Nothing but chatter: say that rather than nothing at all.
    assert "DEPRECATED" in _meaningful_tail(lines[:1])


async def test_a_missing_password_is_not_retried(profiles, context, monkeypatch):
    """Three attempts at a deterministic failure is three times the wait."""
    from nextron.vpn.manager import VPNEngine

    profile = profiles.by_name("berlin")
    profile.path.write_text(
        profile.read_config() + "auth-user-pass\n", encoding="utf-8"
    )

    context.config.vpn.reconnect_retries = 3
    manager = VPNEngine(context)
    manager.library.load()

    attempts = 0

    async def counting_bring_up(target):
        nonlocal attempts
        attempts += 1
        raise VPNCredentialsRequired("needs a username and password")

    monkeypatch.setattr(manager, "_bring_up", counting_bring_up)

    with pytest.raises(VPNError):
        await manager.connect(profile.name)
    assert attempts == 1


# -- WireGuard -------------------------------------------------------------- #


def test_interface_names_fit_the_kernel_limit(profiles):
    for profile in profiles.all():
        name = WireGuardBackend.interface_name(profile)
        assert 0 < len(name) <= 15
        assert name.startswith("nx")


def test_wireguard_runtime_config_is_named_after_the_interface(profiles):
    profile = profiles.by_name("amsterdam")
    backend = WireGuardBackend()
    config = backend.runtime_config(profile)
    assert config.stem == WireGuardBackend.interface_name(profile)
    assert "[Interface]" in config.read_text()
    assert oct(config.stat().st_mode)[-3:] == "600"


# -- circuit selection ------------------------------------------------------ #


class _FakeCircuit:
    """Stand-in for stem's circuit records."""

    def __init__(self, ident, status, purpose, path):
        self.id = ident
        self.status = status
        self.purpose = purpose
        self.path = path
        self.created = None


class _FakeStream:
    def __init__(self, ident, circ_id):
        self.id = ident
        self.circ_id = circ_id


def _controller(bus, circuits, streams=()):
    """A TorController wired to fixed circuit and stream tables."""
    from nextron.core.config import TorSettings
    from nextron.tor.controller import TorController

    controller = TorController(TorSettings(), bus)

    class _Stub:
        def is_alive(self):
            return True

        def get_circuits(self):
            return circuits

        def get_streams(self):
            return list(streams)

    controller._controller = _Stub()
    return controller


async def test_the_circuit_carrying_traffic_is_chosen(bus):
    """One-hop directory circuits must never be reported as "the" circuit."""
    circuits = [
        _FakeCircuit("1", "BUILT", "GENERAL", [("AAA", "DirGuard")]),
        _FakeCircuit(
            "9", "BUILT", "GENERAL", [("B", "Guard"), ("C", "Middle"), ("D", "Exit")]
        ),
        _FakeCircuit(
            "12", "BUILT", "GENERAL", [("E", "G2"), ("F", "M2"), ("G", "Spare")]
        ),
    ]
    controller = _controller(bus, circuits, streams=[_FakeStream("61", "9")])

    active = await controller.active_circuit()
    assert active is not None
    assert active.id == "9"            # the one with a stream attached
    assert active.hops == 3


async def test_conflux_circuits_are_eligible(bus):
    circuits = [
        _FakeCircuit("1", "BUILT", "GENERAL", [("A", "DirGuard")]),
        _FakeCircuit(
            "5",
            "BUILT",
            "CONFLUX_LINKED",
            [("B", "Guard"), ("C", "Middle"), ("D", "Exit")],
        ),
    ]
    controller = _controller(bus, circuits, streams=[_FakeStream("7", "5")])
    active = await controller.active_circuit()
    assert active is not None and active.id == "5"


async def test_a_multi_hop_circuit_wins_when_no_stream_is_attached(bus):
    """Newest-by-id used to win, which returned a one-hop directory circuit."""
    circuits = [
        _FakeCircuit(
            "3", "BUILT", "GENERAL", [("B", "Guard"), ("C", "Middle"), ("D", "Exit")]
        ),
        _FakeCircuit("14", "BUILT", "GENERAL", [("A", "DirGuard")]),
    ]
    controller = _controller(bus, circuits)
    active = await controller.active_circuit()
    assert active is not None
    assert active.id == "3"
    assert active.hops == 3


async def test_internal_purposes_are_ignored(bus):
    circuits = [
        _FakeCircuit(
            "13",
            "BUILT",
            "HS_VANGUARDS",
            [("A", "G"), ("B", "M"), ("C", "E")],
        ),
        _FakeCircuit("14", "EXTENDED", "GENERAL", []),
    ]
    controller = _controller(bus, circuits)
    assert await controller.active_circuit() is None


async def test_no_circuits_is_not_an_error(bus):
    assert await _controller(bus, []).active_circuit() is None


async def test_a_lone_one_hop_circuit_is_not_reported_as_the_path(bus):
    """Showing a directory circuit as "your path" would be a lie."""
    circuits = [
        _FakeCircuit("1", "BUILT", "GENERAL", [("A", "DirGuard")]),
        _FakeCircuit("2", "BUILT", "GENERAL", [("B", "DirGuard2")]),
    ]
    assert await _controller(bus, circuits).active_circuit() is None


async def test_a_one_hop_circuit_is_still_honoured_when_it_carries_a_stream(bus):
    """If tor says traffic is on it, report it -- the stream table is truth."""
    circuits = [_FakeCircuit("1", "BUILT", "GENERAL", [("A", "OnlyHop")])]
    controller = _controller(bus, circuits, streams=[_FakeStream("5", "1")])
    active = await controller.active_circuit()
    assert active is not None and active.id == "1"
