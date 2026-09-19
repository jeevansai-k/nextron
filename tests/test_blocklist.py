"""Blocklist parsing, matching and whitelist handling."""

from __future__ import annotations

import pytest

from nextron.core.exceptions import DNSError
from nextron.dns.blocklist import BlocklistLibrary
from nextron.storage import paths


@pytest.fixture
def library() -> BlocklistLibrary:
    dns = paths.dns_dir()
    (dns / "ads.hosts").write_text(
        "# hosts style\n"
        "0.0.0.0 ads.example.com\n"
        "127.0.0.1 doubleclick.test\n"
        "0.0.0.0 localhost\n"
        "this line is not a domain at all\n",
        encoding="utf-8",
    )
    (dns / "malware.txt").write_text(
        "! adblock style\n||malware.example.org^\nphish.example.io\nnot_a_domain\n",
        encoding="utf-8",
    )
    (dns / "trackers.list").write_text(
        "metrics.example.com\nanalytics.example.com\n", encoding="utf-8"
    )
    (dns / "ignored.md").write_text("nope\n", encoding="utf-8")
    instance = BlocklistLibrary()
    instance.load()
    return instance


def test_all_three_formats_are_discovered(library):
    assert {f.name for f in library.files} == {
        "ads.hosts",
        "malware.txt",
        "trackers.list",
    }
    assert {f.format for f in library.files} == {"hosts", "txt", "list"}


def test_hosts_adblock_and_plain_entries_all_parse(library):
    for domain in (
        "ads.example.com",
        "doubleclick.test",
        "malware.example.org",
        "phish.example.io",
        "metrics.example.com",
    ):
        assert library.is_blocked(domain), domain


def test_subdomains_are_blocked_by_a_parent_entry(library):
    assert library.is_blocked("deep.sub.ads.example.com")
    assert not library.is_blocked("example.com")
    assert not library.is_blocked("notads.example.com")


def test_malformed_lines_are_counted_not_blocked(library):
    ads = next(f for f in library.files if f.name == "ads.hosts")
    assert ads.invalid >= 1
    assert not library.is_blocked("this")


def test_localhost_is_never_blocked(library):
    assert not library.is_blocked("localhost")


def test_whitelist_overrides_a_blocklist(library):
    assert library.is_blocked("metrics.example.com")
    assert library.whitelist_add("metrics.example.com") is True
    assert not library.is_blocked("metrics.example.com")
    assert library.whitelist_add("metrics.example.com") is False


def test_whitelisting_a_parent_unblocks_children(library):
    library.whitelist_add("example.com")
    assert not library.is_blocked("ads.example.com")


def test_whitelist_rejects_nonsense(library):
    with pytest.raises(DNSError):
        library.whitelist_add("not a domain")


def test_disabling_a_list_stops_its_domains_being_blocked(library):
    library.set_enabled("malware.txt", False)
    library.load(["ads.hosts", "trackers.list"])
    assert not library.is_blocked("malware.example.org")
    assert library.is_blocked("ads.example.com")


def test_removing_a_list_deletes_the_file(library):
    library.remove("trackers.list")
    assert not (paths.dns_dir() / "trackers.list").exists()
    with pytest.raises(DNSError):
        library.remove("trackers.list")


def test_importing_rejects_unsupported_formats(library, tmp_path):
    bad = tmp_path / "list.csv"
    bad.write_text("a,b\n", encoding="utf-8")
    with pytest.raises(DNSError):
        library.import_file(bad)


def test_importing_copies_and_counts(library, tmp_path):
    source = tmp_path / "extra.txt"
    source.write_text("spy.example.net\nbeacon.example.net\n", encoding="utf-8")
    entry = library.import_file(source)
    assert entry.domains == 2
    assert (paths.dns_dir() / entry.name).is_file()
