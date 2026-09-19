"""Filter-list to domain conversion."""

from __future__ import annotations

import pytest

from nextron.dns.adblock import convert, convert_text, is_domain


def _one(line: str):
    return convert([line])


# -- domain validation ------------------------------------------------------ #


@pytest.mark.parametrize(
    "candidate,valid",
    [
        ("ads.example.com", True),
        ("a.b.c.example.co.uk", True),
        ("xn--80ak6aa92e.com", True),
        ("under_score.example.com", True),
        ("example", False),            # single label
        ("192.0.2.1", False),          # IP literal
        ("2001:db8::1", False),
        ("-bad.example.com", False),
        ("", False),
    ],
)
def test_domain_validation(candidate, valid):
    assert is_domain(candidate) is valid


# -- hosts and plain formats ------------------------------------------------ #


def test_hosts_entries():
    assert _one("0.0.0.0 ads.example.com").domains == {"ads.example.com"}
    assert _one("127.0.0.1 tracker.example.net").domains == {"tracker.example.net"}


def test_plain_domain():
    assert _one("metrics.example.com").domains == {"metrics.example.com"}


def test_inline_comment_is_stripped():
    assert _one("ads.example.com # an ad server").domains == {"ads.example.com"}


def test_single_label_hosts_entries_are_skipped_not_invalid():
    """"0.0.0.0 localhost" is understood perfectly -- there is just no name."""
    result = _one("0.0.0.0 localhost")
    assert result.domains == set()
    assert result.skipped == 1
    assert result.invalid == 0


def test_localhost_is_never_blocked():
    assert convert(["localhost", "localhost.localdomain"]).domains == set()


# -- adblock network rules -------------------------------------------------- #


def test_plain_network_rule():
    assert _one("||ads.example.com^").domains == {"ads.example.com"}


def test_rule_without_trailing_separator():
    assert _one("||ads.example.com").domains == {"ads.example.com"}


@pytest.mark.parametrize(
    "modifier", ["third-party", "3p", "all", "document", "popup", "important"]
)
def test_modifiers_that_keep_the_rule_dns_expressible(modifier):
    assert _one(f"||ads.example.com^${modifier}").domains == {"ads.example.com"}


@pytest.mark.parametrize(
    "rule",
    [
        "||example.com^$domain=other.tv",   # scoped to one site
        "||example.com^$script",            # one request type only
        "||example.com^$~third-party",      # negated
        "||example.com^$image,domain=x.io",
    ],
)
def test_context_scoped_rules_are_skipped(rule):
    """A resolver cannot see the page or the request type, so it must not guess."""
    result = _one(rule)
    assert result.domains == set()
    assert result.skipped == 1


@pytest.mark.parametrize(
    "rule",
    [
        "||adservice.google.",        # any-TLD wildcard
        "||google.*/url?$ping",       # wildcard + path
        "||example.com/ads.js",       # path
        "||23.83.114.131^",           # IP literal
        "example.com##.ad-banner",    # cosmetic
        "example.com#@#.ad",          # cosmetic exception
        "/banner[0-9]+/",             # regular expression
        "|http://example.com/ad",     # anchored URL
    ],
)
def test_rules_dns_cannot_express_are_skipped(rule):
    result = _one(rule)
    assert result.domains == set()
    assert result.skipped == 1
    assert result.invalid == 0


# -- exceptions ------------------------------------------------------------- #


def test_global_exception_is_recorded():
    result = _one("@@||good.example.com^")
    assert result.exceptions == {"good.example.com"}
    assert result.domains == set()


def test_scoped_exception_is_ignored():
    """An exception that only applies on one site cannot be honoured globally."""
    result = _one("@@||tracker.example.com^$domain=news.example")
    assert result.exceptions == set()
    assert result.skipped == 1


def test_an_exception_beats_a_block_in_the_same_list():
    result = convert(["||example.com^", "@@||example.com^"])
    assert result.domains == set()
    assert result.exceptions == {"example.com"}


# -- counters --------------------------------------------------------------- #


def test_comments_are_counted_separately():
    result = convert(["! a comment", "# another", "[Adblock Plus 2.0]", "; third"])
    assert result.comments == 4
    assert result.invalid == 0


def test_unparseable_lines_are_counted():
    assert _one("&&sub19=undefined&sub20=undefined").invalid == 1


def test_summary_mentions_every_bucket():
    result = convert(["||a.example.com^", "b.example.com##.ad", "!c", "&&junk"])
    assert "1 domains" in result.summary
    assert "not DNS-expressible" in result.summary
    assert "unparseable" in result.summary


# -- realistic mixed list --------------------------------------------------- #


def test_mixed_list_round_trip():
    text = """\
[Adblock Plus 2.0]
! Title: Example list
||ads.example.com^
||tracker.example.net^$third-party
||scoped.example.org^$domain=host.tv
@@||allowed.example.com^
0.0.0.0 hosts-style.example
plain.example
example.com##.banner
/regex[0-9]/
"""
    result = convert_text(text)
    assert result.domains == {
        "ads.example.com",
        "tracker.example.net",
        "hosts-style.example",
        "plain.example",
    }
    assert result.exceptions == {"allowed.example.com"}
    assert result.skipped == 3        # scoped rule, cosmetic, regex
    assert result.comments == 2
