"""Blocklist and whitelist handling for the DNS Shield.

Three formats are supported, and all three may be mixed in one library:

``.hosts``
    ``0.0.0.0 ads.example.com`` -- the classic hosts-file format.
``.txt`` / ``.list``
    One domain per line, ``#`` comments, optional ``||domain^`` Adblock-style
    entries and optional inline ``0.0.0.0`` prefixes.

Domains are stored once in a single set, so a million-entry library costs one
hash lookup per query, and subdomains are matched by walking the label chain
(``a.b.ads.example.com`` is blocked by ``ads.example.com``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from nextron.core import constants
from nextron.core.exceptions import DNSError
from nextron.dns.adblock import ConversionResult, convert_text, is_domain
from nextron.storage import paths

log = logging.getLogger(__name__)

__all__ = ["BlocklistFile", "BlocklistLibrary"]

#: Names the catalogue file uses; it is a source list, not a blocklist.
_CATALOGUE_NAMES = {"sources.txt"}


@dataclass(frozen=True, slots=True)
class BlocklistFile:
    """One blocklist on disk."""

    name: str
    path: Path
    enabled: bool
    domains: int = 0
    invalid: int = 0
    #: Valid filter rules a DNS resolver cannot express (cosmetic, path, scoped).
    skipped: int = 0
    size_bytes: int = 0
    modified: datetime | None = None

    @property
    def format(self) -> str:
        return self.path.suffix.lower().lstrip(".") or "txt"

    @property
    def display(self) -> str:
        marker = "on " if self.enabled else "off"
        return f"[{marker}] {self.name}"


class BlocklistLibrary:
    """Own the blocklist directory, the whitelist and the merged domain set."""

    def __init__(self, directory: Path | None = None) -> None:
        self._dir = directory or paths.dns_dir()
        self._blocked: set[str] = set()
        self._whitelist: set[str] = set()
        #: Global exceptions declared by the lists themselves (@@||domain^).
        self._exceptions: set[str] = set()
        self._files: dict[str, BlocklistFile] = {}

    # -- properties --------------------------------------------------------- #

    @property
    def directory(self) -> Path:
        return self._dir

    @property
    def blocked_count(self) -> int:
        return len(self._blocked)

    @property
    def whitelist_count(self) -> int:
        return len(self._whitelist)

    @property
    def exception_count(self) -> int:
        """Global exceptions the lists themselves declared."""
        return len(self._exceptions)

    @property
    def files(self) -> list[BlocklistFile]:
        return sorted(self._files.values(), key=lambda f: f.name.lower())

    @property
    def active_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.files if f.enabled)

    # -- discovery / loading ------------------------------------------------ #

    def discover(self, enabled_names: list[str] | None = None) -> list[BlocklistFile]:
        """Scan the DNS directory for supported blocklist files."""
        paths.ensure_layout()
        self._dir.mkdir(parents=True, exist_ok=True)
        enabled = set(enabled_names or [])
        self._files.clear()

        for candidate in sorted(self._dir.iterdir()):
            if (
                not candidate.is_file()
                or candidate.suffix.lower() not in constants.BLOCKLIST_SUFFIXES
                or candidate.name in _CATALOGUE_NAMES
            ):
                continue
            try:
                stat = candidate.stat()
            except OSError:
                continue
            self._files[candidate.name] = BlocklistFile(
                name=candidate.name,
                path=candidate,
                # A brand new library with no explicit selection starts enabled,
                # otherwise the Shield would silently block nothing.
                enabled=candidate.name in enabled if enabled_names is not None else True,
                size_bytes=stat.st_size,
                modified=datetime.fromtimestamp(stat.st_mtime),
            )
        return self.files

    def load(self, enabled_names: list[str] | None = None) -> int:
        """Parse every enabled list plus the whitelist. Returns domains blocked."""
        self.discover(enabled_names)
        self._blocked.clear()
        self._exceptions.clear()
        total_invalid = 0

        for name, entry in list(self._files.items()):
            if not entry.enabled:
                continue
            result = self._parse_file(entry.path)
            self._blocked.update(result.domains)
            # A list's own global exceptions (@@||domain^) behave like the
            # user's whitelist: they release a name the same list blocks.
            self._exceptions.update(result.exceptions)
            total_invalid += result.invalid
            self._files[name] = BlocklistFile(
                name=entry.name,
                path=entry.path,
                enabled=True,
                domains=len(result.domains),
                invalid=result.invalid,
                skipped=result.skipped,
                size_bytes=entry.size_bytes,
                modified=entry.modified,
            )

        self.load_whitelist()
        self._blocked -= self._whitelist
        self._blocked -= self._exceptions

        log.info(
            "DNS Shield lists loaded: %d domains blocked, %d whitelisted, "
            "%d list exception(s)%s",
            len(self._blocked),
            len(self._whitelist),
            len(self._exceptions),
            f", {total_invalid} malformed entries ignored" if total_invalid else "",
        )
        return len(self._blocked)

    def load_whitelist(self) -> int:
        """Read ``whitelist.txt``; whitelisted names are never blocked."""
        path = paths.whitelist_file()
        self._whitelist.clear()
        if path.is_file():
            self._whitelist.update(self._parse_file(path).domains)
        return len(self._whitelist)

    # -- parsing ------------------------------------------------------------ #

    @staticmethod
    def _parse_file(path: Path) -> ConversionResult:
        """Convert one list file into domains, exceptions and counters."""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.error("Cannot read blocklist %s: %s", path, exc)
            return ConversionResult()
        return convert_text(text)

    # -- matching ----------------------------------------------------------- #

    @staticmethod
    def _ancestors(domain: str) -> tuple[str, ...]:
        """Yield *domain* and each of its parent domains, most specific first.

        ``a.b.ads.example.com`` -> ``a.b.ads.example.com``, ``b.ads.example.com``,
        ``ads.example.com``, ``example.com``. Single-label names (``com``) are
        excluded so one stray TLD entry in a list cannot break the internet.
        """
        name = domain.strip().strip(".").lower()
        if not name:
            return ()
        labels = name.split(".")
        return tuple(
            ".".join(labels[index:]) for index in range(max(1, len(labels) - 1))
        )

    def is_blocked(self, domain: str) -> bool:
        """True when *domain* or a parent is blocked and nothing is whitelisted.

        The whitelist is evaluated over the whole label chain *before* the
        blocklist, so whitelisting ``example.com`` really does release every
        name beneath it -- even when a list blocks the subdomain explicitly.
        """
        candidates = self._ancestors(domain)
        if not candidates:
            return False
        if any(
            candidate in self._whitelist or candidate in self._exceptions
            for candidate in candidates
        ):
            return False
        return any(candidate in self._blocked for candidate in candidates)

    def is_whitelisted(self, domain: str) -> bool:
        """True when *domain* or any parent domain is whitelisted."""
        return any(
            candidate in self._whitelist for candidate in self._ancestors(domain)
        )

    # -- mutations ---------------------------------------------------------- #

    def import_file(self, source: Path, *, name: str | None = None) -> BlocklistFile:
        """Copy a blocklist into the library."""
        source = Path(source).expanduser()
        if not source.is_file():
            raise DNSError(f"No such file: {source}")
        if source.suffix.lower() not in constants.BLOCKLIST_SUFFIXES:
            supported = ", ".join(constants.BLOCKLIST_SUFFIXES)
            raise DNSError(
                f"Unsupported blocklist format '{source.suffix}'. Supported: {supported}"
            )

        paths.ensure_layout()
        target = self._dir / (name or source.name)
        if target.exists() and target.resolve() != source.resolve():
            stem, suffix = target.stem, target.suffix
            counter = 2
            while target.exists():
                target = self._dir / f"{stem}-{counter}{suffix}"
                counter += 1
        try:
            if target.resolve() != source.resolve():
                target.write_bytes(source.read_bytes())
        except OSError as exc:
            raise DNSError(f"Cannot import blocklist: {exc}") from exc

        result = self._parse_file(target)
        entry = BlocklistFile(
            name=target.name,
            path=target,
            enabled=True,
            domains=len(result.domains),
            invalid=result.invalid,
            skipped=result.skipped,
            size_bytes=target.stat().st_size,
            modified=datetime.fromtimestamp(target.stat().st_mtime),
        )
        self._files[entry.name] = entry
        log.info("Imported blocklist '%s' (%d domains)", entry.name, entry.domains)
        return entry

    def set_enabled(self, name: str, enabled: bool) -> BlocklistFile:
        entry = self._require(name)
        updated = BlocklistFile(
            name=entry.name,
            path=entry.path,
            enabled=enabled,
            domains=entry.domains,
            invalid=entry.invalid,
            skipped=entry.skipped,
            size_bytes=entry.size_bytes,
            modified=entry.modified,
        )
        self._files[name] = updated
        return updated

    def toggle(self, name: str) -> BlocklistFile:
        return self.set_enabled(name, not self._require(name).enabled)

    def remove(self, name: str) -> None:
        entry = self._require(name)
        try:
            entry.path.unlink(missing_ok=True)
        except OSError as exc:
            raise DNSError(f"Cannot remove {entry.path}: {exc}") from exc
        del self._files[name]
        log.info("Removed blocklist '%s'", name)

    def whitelist_add(self, domain: str) -> bool:
        """Append *domain* to the whitelist and unblock it immediately."""
        clean = domain.strip().strip(".").lower()
        if not is_domain(clean):
            raise DNSError(f"'{domain}' is not a valid domain name")
        if clean in self._whitelist:
            return False

        path = paths.whitelist_file()
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"{clean}\n")
        except OSError as exc:
            raise DNSError(f"Cannot update the whitelist: {exc}") from exc

        self._whitelist.add(clean)
        self._blocked.discard(clean)
        log.info("Whitelisted %s", clean)
        return True

    def whitelist_remove(self, domain: str) -> bool:
        clean = domain.strip().strip(".").lower()
        if clean not in self._whitelist:
            return False
        path = paths.whitelist_file()
        try:
            kept = [
                line
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip().strip(".").lower() != clean
            ]
            path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        except OSError as exc:
            raise DNSError(f"Cannot update the whitelist: {exc}") from exc
        self._whitelist.discard(clean)
        return True

    def whitelist(self) -> tuple[str, ...]:
        return tuple(sorted(self._whitelist))

    def _require(self, name: str) -> BlocklistFile:
        entry = self._files.get(name)
        if entry is None:
            raise DNSError(f"Unknown blocklist: {name}")
        return entry
