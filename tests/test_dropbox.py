"""The ``Sources/`` drop-box.

Users copy ``.ovpn`` files and blocklists in with their own file manager;
NEXTRON picks them up on startup and whenever a library screen opens, without
moving or renaming the originals.
"""

from __future__ import annotations

import pytest

from nextron.core.runtime import NextronRuntime
from nextron.storage import paths


@pytest.fixture
async def runtime(profiles):
    instance = NextronRuntime()
    await instance.start()
    yield instance
    await instance.shutdown()


def test_the_layout_creates_both_drop_folders_and_a_readme(isolated_home):
    paths.ensure_layout()
    assert paths.sources_dir() == isolated_home / "Sources"
    assert paths.vpn_sources_dir().name == "VPN Profiles"
    assert paths.dns_sources_dir().name == "DNS list"
    assert paths.vpn_sources_dir().is_dir()
    assert paths.dns_sources_dir().is_dir()
    assert (paths.sources_dir() / "README.txt").is_file()


async def test_a_dropped_profile_is_imported_once(runtime):
    before = len(runtime.vpn.library)

    dropped = paths.vpn_sources_dir() / "oslo.ovpn"
    dropped.write_text(
        "client\ndev tun\nproto tcp\nremote no.example.com 443\n", encoding="utf-8"
    )

    assert runtime.import_from_sources()[0] == 1
    assert len(runtime.vpn.library) == before + 1
    assert any("oslo" in p.name.lower() for p in runtime.vpn.library.all())

    # Running again -- as every startup and every open of the V screen does --
    # must not produce a second copy.
    assert runtime.import_from_sources()[0] == 0
    assert len(runtime.vpn.library) == before + 1

    # The user's own file is left exactly where they put it.
    assert dropped.is_file()


async def test_a_renamed_copy_of_the_same_profile_is_not_imported_twice(runtime):
    body = "client\ndev tun\nproto tcp\nremote se.example.com 443\n"
    (paths.vpn_sources_dir() / "stockholm.ovpn").write_text(body, encoding="utf-8")
    assert runtime.import_from_sources()[0] == 1
    count = len(runtime.vpn.library)

    (paths.vpn_sources_dir() / "stockholm (copy).ovpn").write_text(
        body, encoding="utf-8"
    )
    assert runtime.import_from_sources()[0] == 0
    assert len(runtime.vpn.library) == count


async def test_a_dropped_blocklist_is_imported_and_switched_on(runtime):
    dropped = paths.dns_sources_dir() / "my-ads.txt"
    dropped.write_text(
        "||ads.example.com^\n||tracker.example.net^\n! a comment\n", encoding="utf-8"
    )

    assert runtime.import_from_sources()[1] == 1
    names = {entry.name for entry in runtime.dns.library.discover()}
    assert "my-ads.txt" in names
    assert "my-ads.txt" in runtime.config.dns.enabled_blocklists

    assert runtime.import_from_sources()[1] == 0
    assert dropped.is_file()


async def test_unrelated_files_in_the_drop_box_are_ignored(runtime):
    (paths.vpn_sources_dir() / "notes.md").write_text("not a profile", encoding="utf-8")
    (paths.dns_sources_dir() / "photo.png").write_bytes(b"\x89PNG\r\n")
    (paths.vpn_sources_dir() / "a-folder").mkdir()

    assert runtime.import_from_sources() == (0, 0)


async def test_an_unparseable_profile_is_reported_not_crashed(runtime):
    (paths.vpn_sources_dir() / "broken.ovpn").write_text("garbage", encoding="utf-8")
    before = len(runtime.vpn.library)

    assert runtime.import_from_sources()[0] == 0
    assert len(runtime.vpn.library) == before


def test_the_drop_box_honours_an_explicit_override(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXTRON_SOURCES", str(tmp_path / "elsewhere"))
    assert paths.sources_dir() == tmp_path / "elsewhere"


async def test_a_dropped_catalogue_of_urls_becomes_the_download_list(runtime):
    """A file of https:// lines is a catalogue, not a list of domains.

    Imported as a blocklist it would block nothing at all, so it has to reach
    the catalogue that ``U`` downloads from instead.
    """
    from nextron.dns.sources import SourceCatalogue

    (paths.dns_sources_dir() / "my-sources.txt").write_text(
        "https://example.com/ads.txt\n"
        "https://example.net/trackers.txt\n"
        "https://example.org/malware.txt\n",
        encoding="utf-8",
    )

    assert runtime.import_from_sources()[1] == 1

    catalogue = SourceCatalogue()
    catalogue.load()
    assert len(catalogue) == 3
    assert len(catalogue.enabled()) == 3

    # It is a catalogue, so it must not also appear as a blocklist.
    assert "my-sources.txt" not in {e.name for e in runtime.dns.library.discover()}
    assert "my-sources.txt" not in runtime.config.dns.enabled_blocklists

    # Re-scanning adds nothing.
    assert runtime.import_from_sources()[1] == 0
    catalogue.load()
    assert len(catalogue) == 3


async def test_a_dropped_catalogue_never_re_enables_a_disabled_source(runtime):
    from nextron.dns.sources import SourceCatalogue

    (paths.dns_sources_dir() / "my-sources.txt").write_text(
        "https://example.com/ads.txt\nhttps://example.net/trackers.txt\n",
        encoding="utf-8",
    )
    runtime.import_from_sources()

    catalogue = SourceCatalogue()
    catalogue.load()
    catalogue.set_enabled("https://example.com/ads.txt", False)

    # A second drop of the same addresses respects the user's choice.
    (paths.dns_sources_dir() / "again.txt").write_text(
        "https://example.com/ads.txt\nhttps://example.org/extra.txt\n",
        encoding="utf-8",
    )
    runtime.import_from_sources()

    catalogue.load()
    states = {source.url: source.enabled for source in catalogue}
    assert states["https://example.com/ads.txt"] is False
    assert states["https://example.org/extra.txt"] is True


async def test_a_domain_list_is_still_imported_as_a_blocklist(runtime):
    """A handful of URLs inside a real filter list must not fool the detector."""
    (paths.dns_sources_dir() / "filters.txt").write_text(
        "! Homepage: https://example.com/about\n"
        "! Source: https://example.com/list.txt\n"
        "||ads.example.com^\n"
        "||tracker.example.net^\n"
        "0.0.0.0 spy.example.org\n",
        encoding="utf-8",
    )

    assert runtime.import_from_sources()[1] == 1
    assert "filters.txt" in {e.name for e in runtime.dns.library.discover()}
    assert "filters.txt" in runtime.config.dns.enabled_blocklists


# -- the drop-box is the library, not a one-way import ----------------------- #


async def test_removing_a_file_removes_the_profile(runtime):
    """Delete it in the file manager, and it is gone from NEXTRON."""
    dropped = paths.vpn_sources_dir() / "oslo.ovpn"
    dropped.write_text(
        "client\ndev tun\nproto tcp\nremote no.example.com 443\n", encoding="utf-8"
    )
    runtime.import_from_sources()
    assert any(p.name == "oslo" for p in runtime.vpn.library.all())
    stored = next(p for p in runtime.vpn.library.all() if p.name == "oslo").path

    dropped.unlink()
    runtime.import_from_sources()

    assert not any(p.name == "oslo" for p in runtime.vpn.library.all())
    assert not stored.exists(), "the stored copy must go too"


async def test_emptying_the_folder_empties_the_library(runtime):
    for path in paths.vpn_sources_dir().glob("*"):
        path.unlink()
    runtime.import_from_sources()
    assert runtime.vpn.library.all() == []
    assert runtime.config.vpn.active_profile is None
    assert runtime.config.vpn.shuffle_pool == []


async def test_a_removed_profile_leaves_the_pool_and_the_active_slot(runtime):
    profile = runtime.vpn.library.all()[0]
    runtime.set_active_profile(profile)
    runtime.set_shuffle_pool([p.id for p in runtime.vpn.library.all()])
    assert profile.id in runtime.config.vpn.shuffle_pool

    source = next(
        path
        for path in paths.vpn_sources_dir().glob("*")
        if path.stem == profile.name
    )
    source.unlink()
    runtime.import_from_sources()

    assert runtime.config.vpn.active_profile != profile.id
    assert profile.id not in runtime.config.vpn.shuffle_pool


async def test_editing_a_file_re_reads_it(runtime):
    dropped = paths.vpn_sources_dir() / "edited.ovpn"
    dropped.write_text(
        "client\ndev tun\nproto tcp\nremote first.example.com 443\n", encoding="utf-8"
    )
    runtime.import_from_sources()
    before = next(p for p in runtime.vpn.library.all() if p.name == "edited")
    assert before.endpoint == "first.example.com:443"

    dropped.write_text(
        "client\ndev tun\nproto tcp\nremote second.example.com 443\n", encoding="utf-8"
    )
    runtime.import_from_sources()

    endpoints = {p.endpoint for p in runtime.vpn.library.all() if "edited" in p.name}
    assert endpoints == {"second.example.com:443"}


async def test_a_connected_profile_is_not_pulled_out_from_under_the_tunnel(
    runtime, monkeypatch
):
    profile = runtime.vpn.library.all()[0]
    monkeypatch.setattr(type(runtime.vpn), "current", property(lambda self: profile))

    source = next(
        path
        for path in paths.vpn_sources_dir().glob("*")
        if path.stem == profile.name
    )
    source.unlink()
    runtime.import_from_sources()

    assert any(p.id == profile.id for p in runtime.vpn.library.all())


async def test_removing_a_list_removes_the_blocklist(runtime):
    dropped = paths.dns_sources_dir() / "my-ads.txt"
    dropped.write_text("||ads.example.com^\n", encoding="utf-8")
    runtime.import_from_sources()
    assert "my-ads.txt" in {e.name for e in runtime.dns.library.discover()}

    dropped.unlink()
    runtime.import_from_sources()

    assert "my-ads.txt" not in {e.name for e in runtime.dns.library.discover()}
    assert "my-ads.txt" not in runtime.config.dns.enabled_blocklists


async def test_a_downloaded_list_is_never_removed_by_the_mirror(runtime):
    """``U`` downloads belong to NEXTRON; only the drop-box mirrors."""
    from nextron.dns.sources import BlocklistSource, SourceCatalogue

    catalogue = SourceCatalogue()
    catalogue.replace([BlocklistSource("https://example.com/ads.txt")])
    downloaded = paths.dns_dir() / catalogue.all()[0].filename
    downloaded.write_text("||tracker.example.net^\n", encoding="utf-8")
    runtime.dns.library.discover()

    runtime.import_from_sources()

    assert downloaded.exists()
    assert downloaded.name in {e.name for e in runtime.dns.library.discover()}


async def test_importing_from_elsewhere_copies_into_the_drop_box(runtime, tmp_path):
    """Otherwise the next scan would delete what the user just imported."""
    outside = tmp_path / "elsewhere.ovpn"
    outside.write_text(
        "client\ndev tun\nproto tcp\nremote out.example.com 443\n", encoding="utf-8"
    )

    profile = runtime.import_vpn_profile(outside)
    assert (paths.vpn_sources_dir() / "elsewhere.ovpn").is_file()

    runtime.import_from_sources()
    assert any(p.id == profile.id for p in runtime.vpn.library.all())
