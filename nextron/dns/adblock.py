"""Convert filter-list syntax into the domain set a DNS resolver can enforce.

Ad-blocker filter lists are written for a browser extension that sees full
URLs, request types and the page a request came from. A DNS resolver sees a
name and nothing else, so only a subset of any list is expressible here:

============================  ==========================================
Rule                          Treatment
============================  ==========================================
``ads.example.com``           blocked (plain domain / hosts-file entry)
``0.0.0.0 ads.example.com``   blocked
``||ads.example.com^``        blocked
``||ads.example.com^$third-party``  blocked -- the domain is a tracker either way
``@@||good.example.com^``     allowed -- a global exception
``||example.com^$domain=x.tv``      skipped -- scoped to one site, DNS cannot express it
``||ads.example.*``           skipped -- wildcard
``||example.com/ads.js``      skipped -- path
``example.com##.ad``          skipped -- cosmetic, nothing to resolve
``/banner\\d+/``               skipped -- regular expression
============================  ==========================================

*Skipped* and *invalid* are counted separately on purpose: a list that is 90%
cosmetic rules is working exactly as intended, while a list that is 90%
unparseable is probably a failed download.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

__all__ = ["ConversionResult", "convert", "convert_text", "is_domain"]

_COMMENT_PREFIXES = ("!", "#", ";")
_HOSTS_SINKS = frozenset(
    {"0.0.0.0", "127.0.0.1", "::", "::1", "0000::", "fe80::1", "255.255.255.255"}
)
#: Cosmetic / scriptlet separators. Anything containing one is not a DNS rule.
_COSMETIC_MARKERS = ("##", "#@#", "#?#", "#$#", "#%#", "$$", "$@$")
#: Modifiers that do not change *which name* is contacted, so the domain can
#: still be blocked at the DNS layer.
_SAFE_MODIFIERS = frozenset(
    {"third-party", "3p", "all", "document", "doc", "popup", "important"}
)
#: Never block these, whatever a list says.
_NEVER_BLOCK = frozenset({"localhost", "localhost.localdomain", "local", "ip6-localhost"})

_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[a-z0-9_-]{1,63}(?<!-)(\.(?!-)[a-z0-9_-]{1,63}(?<!-))+$"
)
_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_NETWORK_RULE_RE = re.compile(r"^(?P<exception>@@)?\|\|(?P<body>[^\s]+)$")


def is_domain(candidate: str) -> bool:
    """True when *candidate* is a plausible DNS name (and not an IP literal)."""
    if not candidate or _IPV4_RE.match(candidate) or ":" in candidate:
        return False
    return bool(_DOMAIN_RE.match(candidate))


@dataclass(slots=True)
class ConversionResult:
    """What one list yielded."""

    domains: set[str] = field(default_factory=set)
    exceptions: set[str] = field(default_factory=set)
    #: Valid filter rules that a DNS resolver simply cannot express.
    skipped: int = 0
    #: Lines that could not be understood at all.
    invalid: int = 0
    comments: int = 0
    total: int = 0

    @property
    def usable(self) -> int:
        return len(self.domains)

    @property
    def summary(self) -> str:
        return (
            f"{len(self.domains):,} domains, {len(self.exceptions):,} exceptions, "
            f"{self.skipped:,} not DNS-expressible, {self.invalid:,} unparseable"
        )


def convert_text(text: str) -> ConversionResult:
    """Convert a whole list."""
    return convert(text.splitlines())


def convert(lines: Iterable[str]) -> ConversionResult:
    """Convert an iterable of raw list lines."""
    result = ConversionResult()

    for raw in lines:
        line = raw.strip()
        result.total += 1

        if not line:
            continue
        if line.startswith(_COMMENT_PREFIXES) or line.startswith("["):
            result.comments += 1
            continue
        if any(marker in line for marker in _COSMETIC_MARKERS):
            result.skipped += 1
            continue

        if line.startswith("||") or line.startswith("@@"):
            _network_rule(line, result)
            continue
        if line.startswith(("/", "|", "-", ".", "@", "*")):
            # Regular expressions, anchored URL rules and fragment matches.
            result.skipped += 1
            continue

        _plain_or_hosts(line, result)

    # A name can appear as both a block and a global exception; the exception
    # wins, exactly as it does in a browser.
    result.domains -= result.exceptions
    result.domains -= _NEVER_BLOCK
    return result


def _network_rule(line: str, result: ConversionResult) -> None:
    """Handle ``||domain^`` and ``@@||domain^`` rules."""
    match = _NETWORK_RULE_RE.match(line)
    if match is None:
        result.skipped += 1
        return

    body = match.group("body")
    is_exception = bool(match.group("exception"))

    # Split the pattern from its modifiers.
    pattern, _, modifiers = body.partition("$")
    if modifiers and not _modifiers_are_safe(modifiers):
        result.skipped += 1
        return

    # A DNS rule is a bare host anchored by "^" (or nothing at all). Anything
    # with a path, a wildcard or a query is a URL rule.
    if any(char in pattern for char in "*/?=~|"):
        result.skipped += 1
        return

    host = pattern.rstrip("^")
    if host.endswith("."):
        # "||adservice.google." matches any TLD -- not a name we can resolve.
        result.skipped += 1
        return
    if not host or "^" in host:
        result.skipped += 1
        return

    host = host.lower()
    if not is_domain(host):
        result.skipped += 1
        return

    if is_exception:
        result.exceptions.add(host)
    else:
        result.domains.add(host)


def _modifiers_are_safe(modifiers: str) -> bool:
    """True when every modifier leaves the rule meaningful at the DNS layer."""
    for modifier in modifiers.split(","):
        name = modifier.strip().lower()
        if not name:
            continue
        # "$domain=", "$script", "$~third-party" and friends narrow the rule to
        # a context a resolver cannot see.
        if "=" in name or name.startswith("~"):
            return False
        if name not in _SAFE_MODIFIERS:
            return False
    return True


def _plain_or_hosts(line: str, result: ConversionResult) -> None:
    """Handle hosts-file entries and bare domains."""
    # Strip an inline comment: "ads.example.com # tracker"
    for prefix in _COMMENT_PREFIXES:
        position = line.find(f" {prefix}")
        if position != -1:
            line = line[:position].strip()
            break
    if not line:
        return

    parts = line.split()
    if len(parts) >= 2 and parts[0] in _HOSTS_SINKS:
        candidates = parts[1:]
    elif len(parts) == 1:
        candidates = parts
    else:
        # A hosts line pointing at a real address, or something unrecognised.
        candidates = parts[1:] if _IPV4_RE.match(parts[0]) else []
        if not candidates:
            result.invalid += 1
            return

    accepted = False
    understood = False
    for candidate in candidates:
        name = candidate.strip().strip(".").lower()
        if is_domain(name):
            result.domains.add(name)
            accepted = True
        elif name in _NEVER_BLOCK or ("." not in name and name.isalnum()):
            # "0.0.0.0 localhost" and other single-label hosts entries are
            # understood perfectly well -- there is just nothing to block.
            understood = True
    if not accepted:
        result.skipped += 1 if understood else 0
        result.invalid += 0 if understood else 1
