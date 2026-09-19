"""Shared fixtures.

Every test runs against an isolated ``NEXTRON_HOME`` so nothing touches the
developer's real ``~/.config/nextron``.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def render_text(renderable, width: int = 120) -> str:
    """Render any Rich renderable (or Textual content) to plain text."""
    from rich.console import Console

    console = Console(width=width, no_color=True, legacy_windows=False)
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point NEXTRON at a throwaway configuration root."""
    home = tmp_path / "nextron-home"
    home.mkdir()
    monkeypatch.setenv("NEXTRON_HOME", str(home))

    from nextron.storage import paths

    paths.ensure_layout()
    return home


@pytest.fixture
def config_manager():
    from nextron.core.config import ConfigManager

    manager = ConfigManager()
    manager.load()
    return manager


@pytest.fixture
def bus():
    from nextron.core.events import EventBus

    return EventBus()


@pytest.fixture
def context(config_manager, bus):
    """An :class:`EngineContext` with a real (empty) database."""
    from nextron.core.context import EngineContext
    from nextron.state.manager import StateManager
    from nextron.storage.database import Database

    return EngineContext(
        config_manager=config_manager,
        bus=bus,
        state=StateManager(bus),
        database=Database(),
    )


@pytest.fixture
def drop_box() -> Path:
    """``Sources/VPN Profiles`` -- where a real user's profiles come from.

    The library mirrors this folder, so test profiles have to live here too;
    a profile with no file behind it is removed on the next scan by design.
    """
    from nextron.storage import paths

    paths.ensure_layout()
    return paths.vpn_sources_dir()


@pytest.fixture
def sample_ovpn(drop_box: Path) -> Path:
    path = drop_box / "berlin.ovpn"
    path.write_text(
        "client\ndev tun\nproto tcp\nremote vpn.example.de 443\n"
        "resolv-retry infinite\nnobind\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def sample_wg(drop_box: Path) -> Path:
    path = drop_box / "amsterdam.wgconf"
    path.write_text(
        "[Interface]\nPrivateKey = aaaa\nAddress = 10.6.0.2/32\n\n"
        "[Peer]\nPublicKey = bbbb\nAllowedIPs = 0.0.0.0/0\n"
        "Endpoint = ams.example.nl:51820\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def sample_udp_ovpn(drop_box: Path) -> Path:
    path = drop_box / "tokyo.ovpn"
    path.write_text(
        "client\ndev tun\nproto udp\nremote jp.example.com 1194\n", encoding="utf-8"
    )
    return path


@pytest.fixture
def profiles(sample_ovpn, sample_wg, sample_udp_ovpn):
    """A loaded library holding one TCP OpenVPN, one UDP OpenVPN and one WireGuard."""
    from nextron.vpn.profiles import ProfileLibrary

    library = ProfileLibrary()
    library.load()
    library.import_file(sample_ovpn)
    library.import_file(sample_wg)
    library.import_file(sample_udp_ovpn)
    return library
