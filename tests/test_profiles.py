"""The VPN profile library: detection, import and organisation."""

from __future__ import annotations

import json

import pytest

from nextron.core.exceptions import VPNProfileError
from nextron.vpn.profiles import ProfileLibrary, VPNProtocol, detect_protocol


def test_protocol_detection_uses_content_first():
    assert detect_protocol("client\nremote host 1194\n") is VPNProtocol.OPENVPN
    assert detect_protocol("[Interface]\nPrivateKey = x\n") is VPNProtocol.WIREGUARD
    # A .conf extension is ambiguous: the body decides.
    assert (
        detect_protocol("[Interface]\nAddress = 10.0.0.1/32\n", ".conf")
        is VPNProtocol.WIREGUARD
    )
    assert detect_protocol("", ".ovpn") is VPNProtocol.OPENVPN
    with pytest.raises(VPNProfileError):
        detect_protocol("nothing recognisable here", ".cfg")


def test_import_extracts_endpoint_and_transport(profiles):
    berlin = profiles.by_name("berlin")
    assert berlin.protocol is VPNProtocol.OPENVPN
    assert berlin.endpoint == "vpn.example.de:443"
    assert berlin.tcp is True
    assert berlin.supports_socks is True

    amsterdam = profiles.by_name("amsterdam")
    assert amsterdam.protocol is VPNProtocol.WIREGUARD
    assert amsterdam.endpoint == "ams.example.nl:51820"
    assert amsterdam.supports_socks is False

    tokyo = profiles.by_name("tokyo")
    assert tokyo.tcp is False
    assert tokyo.supports_socks is False


def test_json_descriptor_import(tmp_path):
    descriptor = tmp_path / "oslo.json"
    descriptor.write_text(
        json.dumps(
            {
                "name": "Oslo Edge",
                "protocol": "openvpn",
                "config": "client\nproto tcp\nremote no.example.com 443\n",
            }
        ),
        encoding="utf-8",
    )
    library = ProfileLibrary()
    library.load()
    profile = library.import_file(descriptor)
    assert profile.name == "Oslo Edge"
    assert profile.protocol is VPNProtocol.OPENVPN
    assert profile.tcp is True


def test_json_descriptor_needs_a_config(tmp_path):
    descriptor = tmp_path / "broken.json"
    descriptor.write_text(json.dumps({"name": "x"}), encoding="utf-8")
    library = ProfileLibrary()
    library.load()
    with pytest.raises(VPNProfileError):
        library.import_file(descriptor)


def test_unsupported_suffix_is_rejected(tmp_path):
    bad = tmp_path / "profile.txt"
    bad.write_text("client\n", encoding="utf-8")
    library = ProfileLibrary()
    library.load()
    with pytest.raises(VPNProfileError):
        library.import_file(bad)


def test_library_persists_across_reloads(profiles):
    names = {profile.name for profile in profiles.all()}
    reloaded = ProfileLibrary()
    reloaded.load()
    assert {profile.name for profile in reloaded.all()} == names


def test_favourites_sort_first(profiles):
    paris = profiles.by_name("tokyo")
    profiles.toggle_favorite(paris.id)
    assert profiles.all()[0].name == "tokyo"
    assert profiles.all()[0].display.startswith("✦")


def test_rename_delete_and_resolve(profiles):
    berlin = profiles.by_name("berlin")
    renamed = profiles.rename(berlin.id, "Berlin TCP")
    assert renamed.name == "Berlin TCP"
    assert profiles.resolve("berlin").id == berlin.id          # prefix
    assert profiles.resolve(berlin.id).id == berlin.id          # id
    assert profiles.resolve("Berlin TCP").id == berlin.id       # exact name

    profiles.delete(berlin.id)
    assert profiles.get(berlin.id) is None
    assert not renamed.path.exists()


def test_rename_rejects_empty_names(profiles):
    profile = profiles.all()[0]
    with pytest.raises(VPNProfileError):
        profiles.rename(profile.id, "   ")


def test_duplicate_names_are_made_unique(profiles, sample_ovpn):
    again = profiles.import_file(sample_ovpn)
    assert again.name == "berlin (2)"


def test_pool_falls_back_to_every_profile(profiles):
    assert len(profiles.pool([])) == len(profiles)
    chosen = profiles.all()[0]
    assert [p.id for p in profiles.pool([chosen.id])] == [chosen.id]


def test_mark_used_counts_activations(profiles):
    profile = profiles.all()[0]
    updated = profiles.mark_used(profile.id)
    assert updated.use_count == 1
    assert updated.last_used is not None


def test_credentials_file_must_exist(profiles, tmp_path):
    profile = profiles.by_name("berlin")
    with pytest.raises(VPNProfileError):
        profiles.set_auth_file(profile.id, str(tmp_path / "nope.txt"))

    creds = tmp_path / "creds.txt"
    creds.write_text("user\n", encoding="utf-8")
    updated = profiles.set_auth_file(profile.id, str(creds))
    assert updated.auth_file == str(creds)


def test_orphan_files_are_adopted(sample_wg):
    from nextron.storage import paths

    (paths.profiles_dir() / "dropped.conf").write_text(
        sample_wg.read_text(), encoding="utf-8"
    )
    library = ProfileLibrary()
    library.load()
    assert library.by_name("dropped") is not None


def test_missing_files_are_dropped_from_the_index(profiles):
    profile = profiles.by_name("amsterdam")
    profile.path.unlink()
    reloaded = ProfileLibrary()
    reloaded.load()
    assert reloaded.by_name("amsterdam") is None


def test_a_config_without_a_server_address_is_rejected(tmp_path):
    """A stray or truncated .ovpn must not become a dud profile."""
    bad = tmp_path / "broken.ovpn"
    bad.write_text("client\ndev tun\nproto tcp\n", encoding="utf-8")
    library = ProfileLibrary()
    library.load()
    with pytest.raises(VPNProfileError, match="no server address"):
        library.import_file(bad)
    assert len(library) == 0


def test_a_wireguard_config_without_an_endpoint_is_rejected(tmp_path):
    bad = tmp_path / "broken.wgconf"
    bad.write_text(
        "[Interface]\nPrivateKey = aaaa\n\n[Peer]\nPublicKey = bbbb\n", encoding="utf-8"
    )
    library = ProfileLibrary()
    library.load()
    with pytest.raises(VPNProfileError, match="no server address"):
        library.import_file(bad)
