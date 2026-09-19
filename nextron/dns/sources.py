"""The blocklist source catalogue and its downloader.

NEXTRON ships a *catalogue* of upstream filter lists rather than copies of
them: the lists belong to their authors, they change daily, and bundling
snapshots would ship stale data under someone else's licence. The catalogue is
a plain text file of URLs, and :class:`SourceFetcher` turns it into blocklists
in ``~/.config/nextron/dns`` on request -- never automatically, because that is
network traffic the user did not ask for.

Each downloaded list is converted to plain domains by
:mod:`nextron.dns.adblock` and written with a provenance header, so it is
obvious later where every file came from.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from nextron.core import constants
from nextron.dns.adblock import ConversionResult, convert_text
from nextron.storage import paths

log = logging.getLogger(__name__)

__all__ = [
    "BlocklistSource",
    "FetchOutcome",
    "FetchReport",
    "SourceCatalogue",
    "SourceFetcher",
]

#: Name of the catalogue inside the DNS directory.
CATALOGUE_NAME = "sources.txt"
#: Downloaded lists are written with this suffix so they are easy to spot.
FETCHED_SUFFIX = ".txt"
#: A converted list smaller than this is almost certainly a failed download or
#: a purely cosmetic list; it is reported rather than written.
MIN_USEFUL_DOMAINS = 1

_SLUG_RE = re.compile(r"[^a-z0-9._-]+")
_HEADER_URL_RE = re.compile(r"^#\s*source:\s*(\S+)", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class BlocklistSource:
    """One upstream list in the catalogue."""

    url: str
    enabled: bool = True
    note: str = ""

    @property
    def host(self) -> str:
        return urlparse(self.url).netloc or self.url

    @property
    def filename(self) -> str:
        """A stable, readable file name derived from the URL."""
        parsed = urlparse(self.url)
        # Percent-encoded names are common in these catalogues; decode first so
        # "Anti-%27Christmas%20carols%27" becomes a readable file name.
        decoded_path = unquote(parsed.path)
        stem = Path(decoded_path).stem or parsed.netloc
        segments = [segment for segment in decoded_path.split("/") if segment]
        forges = ("raw.githubusercontent", "gitlab.", "bitbucket.")
        if segments and parsed.netloc.startswith(forges):
            # On a forge the first path segment is the account, which is far
            # more recognisable than "raw.githubusercontent".
            owner = segments[0]
        elif "." in parsed.netloc:
            owner = parsed.netloc.split(".")[-2]
        else:
            owner = parsed.netloc

        name = f"{owner}-{stem}" if owner and owner not in stem else stem
        slug = _SLUG_RE.sub("-", name.lower()).strip("-.") or "blocklist"
        return f"{slug[:64]}{FETCHED_SUFFIX}"


@dataclass(slots=True)
class FetchOutcome:
    """The result of fetching one source.

    Three outcomes, kept apart because they mean different things: the list
    was written, the list downloaded fine but holds nothing a DNS resolver can
    enforce (a purely cosmetic filter list -- not an error), or the download
    itself failed.
    """

    source: BlocklistSource
    ok: bool
    domains: int = 0
    path: Path | None = None
    error: str | None = None
    #: Downloaded successfully, but every rule was cosmetic / path-scoped.
    not_applicable: bool = False
    conversion: ConversionResult | None = None

    @property
    def failed(self) -> bool:
        return not self.ok and not self.not_applicable

    @property
    def marker(self) -> str:
        if self.ok:
            return "✓"
        return "-" if self.not_applicable else "✗"

    @property
    def label(self) -> str:
        if self.ok:
            return f"{self.source.filename}: {self.domains:,} domains"
        return f"{self.source.host}: {self.error}"


@dataclass(slots=True)
class FetchReport:
    """The result of fetching a whole catalogue."""

    outcomes: list[FetchOutcome] = field(default_factory=list)
    started_at: datetime = field(default_factory=datetime.now)
    finished_at: datetime | None = None

    @property
    def succeeded(self) -> list[FetchOutcome]:
        return [outcome for outcome in self.outcomes if outcome.ok]

    @property
    def not_applicable(self) -> list[FetchOutcome]:
        """Downloaded, but purely cosmetic -- nothing for DNS to enforce."""
        return [outcome for outcome in self.outcomes if outcome.not_applicable]

    @property
    def failed(self) -> list[FetchOutcome]:
        return [outcome for outcome in self.outcomes if outcome.failed]

    @property
    def total_domains(self) -> int:
        return sum(outcome.domains for outcome in self.succeeded)

    @property
    def summary(self) -> str:
        parts = [
            f"{len(self.succeeded)}/{len(self.outcomes)} lists downloaded",
            f"{self.total_domains:,} domains",
        ]
        if self.not_applicable:
            parts.append(f"{len(self.not_applicable)} cosmetic-only")
        if self.failed:
            parts.append(f"{len(self.failed)} unreachable")
        return ", ".join(parts)

    @property
    def duration_seconds(self) -> float:
        return ((self.finished_at or datetime.now()) - self.started_at).total_seconds()


class SourceCatalogue:
    """Read and write the list of upstream blocklist URLs."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (paths.dns_dir() / CATALOGUE_NAME)
        self._sources: list[BlocklistSource] = []

    # -- properties --------------------------------------------------------- #

    @property
    def path(self) -> Path:
        return self._path

    @property
    def exists(self) -> bool:
        return self._path.is_file()

    def __len__(self) -> int:
        return len(self._sources)

    def __iter__(self):
        return iter(self._sources)

    # -- access ------------------------------------------------------------- #

    def all(self) -> list[BlocklistSource]:
        return list(self._sources)

    def enabled(self) -> list[BlocklistSource]:
        return [source for source in self._sources if source.enabled]

    # -- persistence -------------------------------------------------------- #

    def load(self) -> list[BlocklistSource]:
        """Parse the catalogue. A line starting with ``#`` disables a source."""
        self._sources = []
        if not self.exists:
            return []

        seen: set[str] = set()
        try:
            text = self._path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.error("Cannot read the blocklist catalogue: %s", exc)
            return []

        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue

            enabled = True
            if line.startswith("#"):
                candidate = line.lstrip("#").strip()
                if not candidate.startswith(("http://", "https://")):
                    continue          # a genuine comment
                line, enabled = candidate, False

            url, _, note = line.partition("  ")
            url = url.strip()
            note = note.strip().lstrip('#').strip()
            if not url.startswith(("http://", "https://")) or url in seen:
                continue
            seen.add(url)
            self._sources.append(
                BlocklistSource(url=url, enabled=enabled, note=note.strip())
            )

        log.debug(
            "Blocklist catalogue: %d sources (%d enabled)",
            len(self._sources),
            len(self.enabled()),
        )
        return self.all()

    def save(self) -> None:
        paths.ensure_layout()
        lines = [
            f"# {constants.APP_NAME} blocklist sources",
            "# One URL per line. Prefix a line with '#' to disable that source.",
            f"# Download them with: {constants.APP_SLUG} dns update",
            "",
        ]
        for source in self._sources:
            prefix = "" if source.enabled else "# "
            suffix = f"  {source.note}" if source.note else ""
            lines.append(f"{prefix}{source.url}{suffix}")
        self._path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def replace(self, sources: list[BlocklistSource]) -> None:
        self._sources = list(sources)
        self.save()

    def set_enabled(self, url: str, enabled: bool) -> None:
        self._sources = [
            BlocklistSource(source.url, enabled, source.note)
            if source.url == url
            else source
            for source in self._sources
        ]
        self.save()


class SourceFetcher:
    """Download catalogue sources and convert them into domain blocklists."""

    def __init__(
        self,
        *,
        directory: Path | None = None,
        concurrency: int = 8,
        timeout: float = 45.0,
    ) -> None:
        self._dir = directory or paths.dns_dir()
        self._concurrency = max(1, concurrency)
        self._timeout = timeout

    async def fetch_all(
        self,
        sources: list[BlocklistSource],
        *,
        progress=None,
    ) -> FetchReport:
        """Download every source, at most ``concurrency`` at a time."""
        paths.ensure_layout()
        self._dir.mkdir(parents=True, exist_ok=True)

        report = FetchReport()
        if not sources:
            report.finished_at = datetime.now()
            return report

        semaphore = asyncio.Semaphore(self._concurrency)
        headers = {"User-Agent": f"{constants.APP_NAME}/{constants.VERSION}"}
        completed = 0

        async with httpx.AsyncClient(
            timeout=self._timeout, follow_redirects=True, headers=headers
        ) as client:

            async def worker(source: BlocklistSource) -> FetchOutcome:
                nonlocal completed
                async with semaphore:
                    outcome = await self._fetch_one(client, source)
                completed += 1
                if progress is not None:
                    try:
                        progress(completed, len(sources), outcome)
                    except Exception:  # pragma: no cover - a UI hook must not
                        pass           # break the download
                return outcome

            report.outcomes = list(
                await asyncio.gather(*(worker(source) for source in sources))
            )

        report.finished_at = datetime.now()
        log.info("Blocklist update finished: %s", report.summary)
        return report

    async def _fetch_one(
        self, client: httpx.AsyncClient, source: BlocklistSource
    ) -> FetchOutcome:
        try:
            response = await self._get(client, source.url)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return FetchOutcome(source, False, error=f"HTTP {exc.response.status_code}")
        except httpx.TimeoutException:
            return FetchOutcome(
                source, False, error=f"timed out after {self._timeout:.0f}s"
            )
        except httpx.HTTPError as exc:
            return FetchOutcome(source, False, error=type(exc).__name__)
        except Exception as exc:  # pragma: no cover - defensive
            return FetchOutcome(source, False, error=str(exc)[:80])

        try:
            conversion = await asyncio.to_thread(convert_text, response.text)
        except Exception as exc:  # pragma: no cover - defensive
            return FetchOutcome(source, False, error=f"could not parse ({exc})")

        if conversion.usable < MIN_USEFUL_DOMAINS:
            # Not an error: plenty of good lists are element-hiding only.
            return FetchOutcome(
                source,
                False,
                error="cosmetic-only list, nothing for DNS to block",
                not_applicable=True,
                conversion=conversion,
            )

        target = self._dir / source.filename
        try:
            target.write_text(self._render(source, conversion), encoding="utf-8")
        except OSError as exc:
            return FetchOutcome(source, False, error=f"cannot write: {exc}")

        return FetchOutcome(
            source,
            True,
            domains=conversion.usable,
            path=target,
            conversion=conversion,
        )

    @staticmethod
    async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response:
        """GET *url*, retrying uncompressed if the response will not decode.

        Some hosts answer an error page with a Content-Encoding header that
        does not match the body. Asking for identity encoding gets the real
        response back, so the user sees "HTTP 404" instead of a decoding error
        that says nothing about what went wrong.
        """
        try:
            return await client.get(url)
        except httpx.DecodingError:
            log.debug("Retrying %s without compression", url)
            return await client.get(url, headers={"Accept-Encoding": "identity"})

    @staticmethod
    def _render(source: BlocklistSource, conversion: ConversionResult) -> str:
        """Write the domains with a provenance header."""
        stamp = datetime.now().isoformat(timespec="seconds")
        header = [
            f"# Downloaded by {constants.APP_NAME} {constants.VERSION}",
            f"# source: {source.url}",
            f"# fetched: {stamp}",
            f"# {conversion.summary}",
            "#",
            "# Converted from filter-list syntax to plain domains: only rules a",
            "# DNS resolver can enforce are kept. Edit the catalogue, not this",
            "# file -- it is overwritten on the next update.",
            "",
        ]
        body = sorted(conversion.domains)
        if conversion.exceptions:
            header.append(
                f"# {len(conversion.exceptions):,} global exceptions were applied "
                "and removed from this list."
            )
            header.append("")
        return "\n".join(header + body) + "\n"


def source_url_of(path: Path) -> str | None:
    """Read the ``# source:`` header back out of a downloaded list."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            head = "".join(next(handle, "") for _ in range(8))
    except OSError:
        return None
    match = _HEADER_URL_RE.search(head)
    return match.group(1) if match else None
