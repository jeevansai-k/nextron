"""System-wide (transparent) Tor routing.

The reason this exists: without it NEXTRON only routes applications that were
told about its SOCKS proxy, so a browser keeps its real address and the tool
looks broken. Getting it right needs three things, all asserted here:

* Tor's own traffic exempted by uid -- otherwise the redirect loops into itself;
* the daemon actually running under a *different* uid, which needs root;
* the bypasses closed: IPv6 and QUIC/UDP would otherwise carry the real address.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from nextron.core.config import TorSettings
from nextron.routing.transparent import TransparentRouter, tor_daemon_uid
from nextron.tor.daemon import SYSTEM_RUNTIME_DIR, TorDaemon


@pytest.fixture
def ruleset() -> str:
    return TransparentRouter().build_ruleset(
        trans_port=9040, dns_port=9053, tor_uid=125
    )


# -- the redirect ----------------------------------------------------------- #


def test_tor_traffic_is_exempt_by_uid(ruleset):
    """Without this the daemon's own relay connections redirect into itself."""
    assert "meta skuid 125 return" in ruleset
    assert "meta skuid 125 accept" in ruleset


def test_tcp_and_dns_are_redirected(ruleset):
    assert "meta l4proto tcp redirect to :9040" in ruleset
    assert "udp dport 53 redirect to :9053" in ruleset
    assert "tcp dport 53 redirect to :9053" in ruleset


def test_loopback_and_local_networks_are_left_alone(ruleset):
    assert 'oifname "lo" return' in ruleset
    assert "192.168.0.0/16" in ruleset
    assert "127.0.0.0/8" in ruleset


# -- the bypasses ----------------------------------------------------------- #


def test_ipv6_is_dropped(ruleset):
    """A dual-stack host prefers IPv6; Tor's TransPort only carries IPv4."""
    assert "meta nfproto ipv6 drop" in ruleset


def test_udp_is_dropped_so_quic_cannot_escape(ruleset):
    """Browsers speak QUIC on UDP 443 and would bypass the TCP redirect."""
    assert "meta l4proto udp drop" in ruleset


def test_dhcp_and_link_local_still_work(ruleset):
    assert "udp dport { 67, 68 } accept" in ruleset
    assert "fe80::/10" in ruleset


def test_the_drops_come_after_the_accepts(ruleset):
    """Order decides the outcome: an early drop would cut the local network."""
    lines = [line.strip() for line in ruleset.splitlines()]
    filter_lines = lines[lines.index("chain output {", 6) :]
    udp_accept = filter_lines.index("udp dport { 67, 68 } accept")
    ipv6_accept = next(
        index for index, line in enumerate(filter_lines) if "fe80::/10" in line
    )
    ipv6_drop = filter_lines.index("meta nfproto ipv6 drop")
    udp_drop = filter_lines.index("meta l4proto udp drop")
    assert ipv6_accept < ipv6_drop
    assert udp_accept < udp_drop


def test_both_tables_are_emitted(ruleset):
    assert "table ip nextron_transparent {" in ruleset
    assert "table inet nextron_leakguard {" in ruleset


# -- when it is allowed to engage ------------------------------------------- #


def test_it_refuses_while_tor_runs_as_the_current_user():
    """The whole point: you cannot exempt yourself from your own redirect."""
    usable, reason = TransparentRouter().available(daemon_is_ours=True)
    assert usable is False
    assert "sudo" in reason


def test_it_accepts_a_daemon_under_another_account():
    usable, reason = TransparentRouter().available(
        daemon_is_ours=False, tor_uid=125
    )
    assert usable is True and reason is None


def test_it_refuses_a_daemon_sharing_our_uid():
    usable, reason = TransparentRouter().available(
        daemon_is_ours=False, tor_uid=os.geteuid()
    )
    assert usable is False
    assert "shares this process" in reason


def test_it_refuses_without_nftables(monkeypatch):
    monkeypatch.setattr("nextron.core.process.which", lambda binary: None)
    usable, reason = TransparentRouter().available(daemon_is_ours=False, tor_uid=125)
    assert usable is False and "nftables" in reason


# -- running the daemon under the Tor account -------------------------------- #


def test_unprivileged_keeps_a_private_daemon(bus):
    daemon = TorDaemon(TorSettings(transparent_routing=True), bus)
    daemon.prepare_runtime()
    assert daemon.runtime_uid is None
    assert daemon.runtime_user is None
    assert daemon.launch_command("/usr/sbin/tor", daemon.torrc_path)[0].endswith("tor")


def test_root_drops_the_daemon_to_the_tor_account(bus):
    uid, user = tor_daemon_uid()
    if uid is None:
        pytest.skip("no system Tor account on this machine")

    daemon = TorDaemon(TorSettings(transparent_routing=True), bus)
    with (
        patch("nextron.core.process.is_root", return_value=True),
        patch("pathlib.Path.mkdir"),
        patch("pathlib.Path.chmod"),
        patch("os.chown"),
    ):
        daemon.prepare_runtime()

    assert daemon.runtime_user == user
    assert daemon.runtime_uid == uid
    assert str(daemon.data_dir).startswith(SYSTEM_RUNTIME_DIR)
    assert f"DataDirectory {daemon.data_dir}" in daemon.build_torrc()

    argv = daemon.launch_command("/usr/sbin/tor", daemon.torrc_path)
    assert user in argv
    assert argv[-2:] == ("-f", str(daemon.torrc_path))


def test_root_without_transparent_routing_stays_private(bus):
    daemon = TorDaemon(TorSettings(transparent_routing=False), bus)
    with patch("nextron.core.process.is_root", return_value=True):
        daemon.prepare_runtime()
    assert daemon.runtime_uid is None


def test_a_failed_handover_falls_back_instead_of_crashing(bus):
    """A read-only /var/lib must degrade to SOCKS mode, not kill the session."""
    real_mkdir = Path.mkdir

    def refuse_system_dir(self, *args, **kwargs):
        if str(self).startswith(SYSTEM_RUNTIME_DIR):
            raise OSError("read-only")
        return real_mkdir(self, *args, **kwargs)

    daemon = TorDaemon(TorSettings(transparent_routing=True), bus)
    with (
        patch("nextron.core.process.is_root", return_value=True),
        patch("pathlib.Path.mkdir", refuse_system_dir),
    ):
        daemon.prepare_runtime()
    assert daemon.runtime_uid is None
    assert daemon.data_dir != Path(SYSTEM_RUNTIME_DIR) / "tor"


# -- sudo must not hide the user's own data ---------------------------------- #


def test_sudo_uses_the_invoking_users_configuration(monkeypatch):
    """`sudo nextron` must not start from an empty /root/.config."""
    from nextron.storage import paths

    monkeypatch.delenv("NEXTRON_HOME", raising=False)
    monkeypatch.setenv("SUDO_USER", os.environ.get("USER", "root"))
    with patch("os.geteuid", return_value=0):
        root = paths.config_root()
        owner = paths.invoking_user()

    assert "/root/" not in str(root)
    assert owner is not None


def test_no_sudo_means_no_ownership_fixing(monkeypatch):
    from nextron.storage import paths

    monkeypatch.delenv("SUDO_USER", raising=False)
    assert paths.invoking_user() is None


# -- knowing who owns the daemon we attached to ----------------------------- #


def test_listener_uid_reports_the_socket_owner():
    """Needed to exempt a daemon NEXTRON attached to rather than started."""
    import socket as socket_module

    from nextron.utils import net

    with socket_module.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        assert net.listener_uid(port) == os.getuid()

    assert net.listener_uid(port) is None      # gone once closed


def test_an_attached_system_daemon_is_exemptable(bus, monkeypatch):
    """The bug: attaching to the system daemon refused transparent routing.

    A daemon running as debian-tor is exemptable whether NEXTRON started it or
    merely connected to it.
    """
    daemon = TorDaemon(TorSettings(), bus)
    daemon._external = True
    monkeypatch.setattr("nextron.utils.net.listener_uid", lambda port, host="127.0.0.1": 125)
    monkeypatch.setattr("os.geteuid", lambda: 0)

    assert daemon.runtime_uid == 125
    usable, _ = TransparentRouter().available(
        daemon_is_ours=False, tor_uid=daemon.runtime_uid
    )
    assert usable is True


def test_an_attached_daemon_owned_by_us_is_not_exemptable(bus, monkeypatch):
    daemon = TorDaemon(TorSettings(), bus)
    daemon._external = True
    monkeypatch.setattr(
        "nextron.utils.net.listener_uid", lambda port, host="127.0.0.1": os.geteuid()
    )
    assert daemon.runtime_uid is None


def test_the_privileged_data_directory_stays_inside_the_apparmor_tree():
    """Distributions confine the tor binary to /var/lib/tor; outside it is denied."""
    assert SYSTEM_RUNTIME_DIR.startswith("/var/lib/tor")
