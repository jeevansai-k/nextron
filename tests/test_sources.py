"""The blocklist source catalogue and its downloader."""

from __future__ import annotations

import httpx
import pytest

from nextron.dns.sources import (
    BlocklistSource,
    SourceCatalogue,
    SourceFetcher,
    source_url_of,
)
from nextron.storage import paths

SAMPLE_LIST = """\
[Adblock Plus 2.0]
! Title: Test list
||ads.example.com^
||tracker.example.net^$third-party
example.com##.cosmetic
@@||allowed.example.com^
"""

COSMETIC_ONLY = "! Title: Cosmetic\nexample.com##.ad\nexample.org##.banner\n"


# -- filenames -------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://easylist-downloads.adblockplus.org/easyprivacy.txt",
            "adblockplus-easyprivacy.txt",
        ),
        (
            "https://raw.githubusercontent.com/hoshsadiq/adblock-nocoin-list/master/nocoin.txt",
            "hoshsadiq-nocoin.txt",
        ),
        ("https://v.firebog.net/hosts/static/w3kbl.txt", "firebog-w3kbl.txt"),
    ],
)
def test_filenames_are_readable_and_stable(url, expected):
    assert BlocklistSource(url).filename == expected


def test_percent_encoded_names_are_decoded():
    source = BlocklistSource(
        "https://raw.githubusercontent.com/Dandelion/adfilt/master/Anti-%27X%27%20List.txt"
    )
    assert "%27" not in source.filename
    assert source.filename.endswith(".txt")


# -- catalogue -------------------------------------------------------------- #


@pytest.fixture
def catalogue_file():
    path = paths.dns_dir() / "sources.txt"
    path.write_text(
        "# NEXTRON sources\n"
        "# a genuine comment line\n"
        "https://example.com/one.txt\n"
        "https://example.com/two.txt  a note\n"
        "# https://example.com/three.txt  unreachable: HTTP 404\n"
        "https://example.com/one.txt\n"        # duplicate
        "not-a-url\n",
        encoding="utf-8",
    )
    return path


def test_catalogue_parsing(catalogue_file):
    catalogue = SourceCatalogue()
    catalogue.load()

    assert len(catalogue) == 3
    assert len(catalogue.enabled()) == 2
    urls = [source.url for source in catalogue]
    assert urls.count("https://example.com/one.txt") == 1      # de-duplicated
    assert "not-a-url" not in urls

    noted = next(s for s in catalogue if s.url.endswith("two.txt"))
    assert noted.note == "a note"

    disabled = next(s for s in catalogue if s.url.endswith("three.txt"))
    assert disabled.enabled is False
    assert disabled.note == "unreachable: HTTP 404"


def test_catalogue_round_trip(catalogue_file):
    catalogue = SourceCatalogue()
    catalogue.load()
    catalogue.set_enabled("https://example.com/one.txt", False)

    reloaded = SourceCatalogue()
    reloaded.load()
    assert len(reloaded) == 3
    one = next(s for s in reloaded if s.url.endswith("one.txt"))
    assert one.enabled is False


def test_missing_catalogue_is_not_an_error():
    catalogue = SourceCatalogue()
    assert catalogue.load() == []
    assert catalogue.exists is False


# -- fetching --------------------------------------------------------------- #


def _transport(handler):
    return httpx.MockTransport(handler)


@pytest.fixture
def patched_client(monkeypatch):
    """Route the fetcher's AsyncClient through a mock transport."""

    def install(handler):
        original = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = _transport(handler)
            return original(*args, **kwargs)

        monkeypatch.setattr("nextron.dns.sources.httpx.AsyncClient", factory)

    return install


async def test_a_list_is_downloaded_converted_and_written(patched_client):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=SAMPLE_LIST)

    patched_client(handler)
    source = BlocklistSource("https://example.com/list.txt")
    report = await SourceFetcher().fetch_all([source])

    assert len(report.succeeded) == 1
    outcome = report.outcomes[0]
    assert outcome.domains == 2                    # cosmetic rule not counted
    assert outcome.path is not None and outcome.path.is_file()

    written = outcome.path.read_text(encoding="utf-8")
    assert "ads.example.com" in written
    assert "tracker.example.net" in written
    assert "##" not in written                     # converted, not copied
    assert source_url_of(outcome.path) == source.url


async def test_cosmetic_only_lists_are_reported_separately(patched_client):
    patched_client(lambda request: httpx.Response(200, text=COSMETIC_ONLY))
    report = await SourceFetcher().fetch_all(
        [BlocklistSource("https://example.com/cosmetic.txt")]
    )

    outcome = report.outcomes[0]
    assert outcome.ok is False
    assert outcome.not_applicable is True
    assert outcome.failed is False                 # not an error
    assert report.failed == []
    assert len(report.not_applicable) == 1
    assert "cosmetic" in outcome.error


async def test_http_errors_are_reported_with_the_status(patched_client):
    patched_client(lambda request: httpx.Response(404, text="nope"))
    report = await SourceFetcher().fetch_all(
        [BlocklistSource("https://example.com/gone.txt")]
    )
    assert report.failed[0].error == "HTTP 404"
    assert report.not_applicable == []


async def test_a_broken_content_encoding_is_retried_uncompressed(patched_client):
    """A 404 page with a bogus gzip header must report the status, not a decode error."""
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.headers.get("accept-encoding", ""))
        if len(attempts) == 1:
            return httpx.Response(
                200, content=b"not really gzip", headers={"Content-Encoding": "gzip"}
            )
        return httpx.Response(404, text="Not Found")

    patched_client(handler)
    report = await SourceFetcher().fetch_all(
        [BlocklistSource("https://example.com/broken.txt")]
    )
    assert len(attempts) == 2
    assert attempts[1] == "identity"
    assert report.failed[0].error == "HTTP 404"


async def test_progress_is_reported_per_source(patched_client):
    patched_client(lambda request: httpx.Response(200, text=SAMPLE_LIST))
    seen: list[tuple[int, int]] = []

    sources = [BlocklistSource(f"https://example.com/{i}.txt") for i in range(4)]
    await SourceFetcher(concurrency=2).fetch_all(
        sources, progress=lambda done, total, outcome: seen.append((done, total))
    )
    assert [done for done, _ in seen] == [1, 2, 3, 4]
    assert {total for _, total in seen} == {4}


async def test_report_summary_covers_all_three_outcomes(patched_client):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("good.txt"):
            return httpx.Response(200, text=SAMPLE_LIST)
        if request.url.path.endswith("cosmetic.txt"):
            return httpx.Response(200, text=COSMETIC_ONLY)
        return httpx.Response(500, text="boom")

    patched_client(handler)
    report = await SourceFetcher().fetch_all(
        [
            BlocklistSource("https://example.com/good.txt"),
            BlocklistSource("https://example.com/cosmetic.txt"),
            BlocklistSource("https://example.com/broken.txt"),
        ]
    )
    assert len(report.succeeded) == 1
    assert len(report.not_applicable) == 1
    assert len(report.failed) == 1
    assert "1/3 lists downloaded" in report.summary
    assert "cosmetic-only" in report.summary
    assert "unreachable" in report.summary


async def test_an_empty_catalogue_fetches_nothing():
    report = await SourceFetcher().fetch_all([])
    assert report.outcomes == []
    assert report.total_domains == 0
