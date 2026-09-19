"""Transparent system-wide routing through Tor.

A SOCKS proxy only protects applications that were told about it. To make
``You -> Tor -> Internet`` true for the *whole machine*, NEXTRON installs an
nftables NAT ruleset that redirects outbound TCP to Tor's ``TransPort`` and
outbound DNS to Tor's ``DNSPort``.

There is one hard requirement: the Tor daemon's own traffic must be exempt,
otherwise its connections to guard relays are redirected back into itself. The
exemption is made on the daemon's uid, so transparent routing needs a Tor
daemon running under a *different* user than NEXTRON -- which is exactly how
the distribution packages ship it (``debian-tor`` / ``tor``). When that is not
the case NEXTRON reports transparent routing as unavailable and keeps working
in SOCKS mode instead of pretending the whole system is covered.
"""

from __future__ import annotations

import logging
import os
import pwd
from dataclasses import dataclass

from nextron.core import process

log = logging.getLogger(__name__)

__all__ = ["TransparentRouter", "TransparentStatus", "tor_daemon_uid"]

_TABLE = "nextron_transparent"
_FILTER_TABLE = "nextron_leakguard"
_TOR_USERS = ("debian-tor", "tor", "toranon", "_tor")
#: Never redirect traffic destined for these -- it would break the local network.
_EXCLUDED_NETS = (
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "100.64.0.0/10",
    "224.0.0.0/4",
    "255.255.255.255/32",
)


def tor_daemon_uid() -> tuple[int | None, str | None]:
    """Return ``(uid, username)`` of the system Tor account, if it exists."""
    for username in _TOR_USERS:
        try:
            entry = pwd.getpwnam(username)
        except KeyError:
            continue
        return entry.pw_uid, username
    return None, None


@dataclass(frozen=True, slots=True)
class TransparentStatus:
    """Whether system-wide redirection is in place."""

    active: bool
    reason: str | None = None
    tor_user: str | None = None
    trans_port: int | None = None
    dns_port: int | None = None

    @property
    def label(self) -> str:
        if self.active:
            return f"System-wide (TransPort {self.trans_port})"
        return f"SOCKS only -- {self.reason}" if self.reason else "SOCKS only"


class TransparentRouter:
    """Install and remove the Tor transparent-proxy NAT rules."""

    def __init__(self) -> None:
        self._active = False
        self._reason: str | None = None
        self._tor_user: str | None = None
        self._trans_port: int | None = None
        self._dns_port: int | None = None

    # -- properties --------------------------------------------------------- #

    @property
    def active(self) -> bool:
        return self._active

    def status(self) -> TransparentStatus:
        return TransparentStatus(
            active=self._active,
            reason=self._reason,
            tor_user=self._tor_user,
            trans_port=self._trans_port,
            dns_port=self._dns_port,
        )

    # -- capability --------------------------------------------------------- #

    def available(
        self, *, daemon_is_ours: bool, tor_uid: int | None = None
    ) -> tuple[bool, str | None]:
        """``(usable, reason_if_not)`` for the current system."""
        if not process.which("nft"):
            return False, "nftables is not installed"
        if not process.is_root() and not process.sudo_available():
            return False, "root privileges unavailable"

        if tor_uid is None:
            tor_uid, username = tor_daemon_uid()
            if tor_uid is None:
                return False, "no system Tor account to exempt from redirection"
            if daemon_is_ours:
                # The daemon still runs as us, so it cannot be exempted from a
                # redirect that applies to us. Running NEXTRON with sudo moves
                # it to the Tor account and lifts this.
                return False, (
                    "Tor runs as the current user; start NEXTRON with sudo for "
                    "system-wide routing"
                )
            self._tor_user = username
        elif tor_uid == os.geteuid():
            return False, "the Tor daemon shares this process's user"

        return True, None

    # -- ruleset ------------------------------------------------------------ #

    def build_ruleset(self, *, trans_port: int, dns_port: int, tor_uid: int) -> str:
        """Render the nftables ruleset (pure function -- unit testable).

        Two tables, because a redirect alone does not make traffic private:

        *nat* sends outbound TCP to Tor's ``TransPort`` and outbound DNS to its
        ``DNSPort``.

        *filter* closes what a redirect cannot reach. Tor carries TCP over
        IPv4; anything else would leave the machine untouched and still carry
        the real address:

        * **IPv6** -- a dual-stack host prefers it, so every IPv6-capable site
          would see the real address.
        * **UDP** -- browsers speak QUIC/HTTP3 on UDP 443 and would bypass Tor
          entirely. Dropping it makes them fall back to TCP, which is
          redirected. DNS is exempt (already redirected), as are DHCP and the
          local network.
        """
        excluded = ", ".join(_EXCLUDED_NETS)
        return "\n".join(
            [
                f"table ip {_TABLE} {{",
                "  chain output {",
                "    type nat hook output priority -100; policy accept;",
                f"    meta skuid {tor_uid} return",
                '    oifname "lo" return',
                f"    ip daddr {{ {excluded} }} return",
                f"    udp dport 53 redirect to :{dns_port}",
                f"    tcp dport 53 redirect to :{dns_port}",
                f"    meta l4proto tcp redirect to :{trans_port}",
                "  }",
                "}",
                f"table inet {_FILTER_TABLE} {{",
                "  chain output {",
                "    type filter hook output priority 0; policy accept;",
                f"    meta skuid {tor_uid} accept",
                '    oifname "lo" accept',
                f"    ip daddr {{ {excluded} }} accept",
                "    ip6 daddr { ::1/128, fe80::/10, ff00::/8 } accept",
                "    udp dport { 67, 68 } accept",
                "    meta nfproto ipv6 drop",
                "    meta l4proto udp drop",
                "  }",
                "}",
                "",
            ]
        )

    # -- engage / release --------------------------------------------------- #

    async def engage(
        self,
        *,
        trans_port: int,
        dns_port: int,
        daemon_is_ours: bool = True,
        tor_uid: int | None = None,
    ) -> TransparentStatus:
        """Install the redirection rules.

        *tor_uid* is the account the daemon actually runs under; NEXTRON drops
        its own daemon to the system Tor account when it has root, precisely so
        this exemption is possible.
        """
        usable, reason = self.available(
            daemon_is_ours=daemon_is_ours, tor_uid=tor_uid
        )
        if not usable:
            self._active = False
            self._reason = reason
            log.info("Transparent routing unavailable: %s", reason)
            return self.status()

        if tor_uid is None:
            tor_uid, username = tor_daemon_uid()
        else:
            username = self._tor_user or _username_for(tor_uid)
        assert tor_uid is not None  # guaranteed by available()

        await process.run_privileged(
            "nft", "delete", "table", "ip", _TABLE, timeout=10, quiet=True
        )
        await process.run_privileged(
            "nft", "delete", "table", "inet", _FILTER_TABLE, timeout=10, quiet=True
        )
        ruleset = self.build_ruleset(
            trans_port=trans_port, dns_port=dns_port, tor_uid=tor_uid
        )
        outcome = await process.run_privileged(
            "nft", "-f", "-", stdin=ruleset, timeout=20
        )
        if not outcome.ok:
            self._active = False
            self._reason = outcome.stderr.strip()[:160] or "nft rejected the ruleset"
            log.error("Transparent routing failed: %s", self._reason)
            return self.status()

        self._active = True
        self._reason = None
        self._tor_user = username
        self._trans_port = trans_port
        self._dns_port = dns_port
        log.info(
            "Transparent routing engaged (TransPort %s, DNSPort %s, exempt uid %s/%s)",
            trans_port,
            dns_port,
            tor_uid,
            username,
        )
        return self.status()

    async def release(self, *, force: bool = False) -> TransparentStatus:
        """Remove the redirection rules.

        ``force`` removes a table this process did not install -- used at
        startup to clean up after a crashed session.
        """
        if not self._active and not force:
            return self.status()
        for family, table in (("ip", _TABLE), ("inet", _FILTER_TABLE)):
            await process.run_privileged(
                "nft", "delete", "table", family, table, timeout=15, quiet=True
            )
        self._active = False
        self._trans_port = None
        self._dns_port = None
        log.info("Transparent routing released")
        return self.status()

    async def is_installed(self) -> bool:
        """True when a NEXTRON redirection table is present on the system."""
        if not process.which("nft"):
            return False
        outcome = await process.run_privileged(
            "nft", "list", "tables", timeout=10, quiet=True
        )
        return outcome.ok and (
            _TABLE in outcome.stdout or _FILTER_TABLE in outcome.stdout
        )


def _username_for(uid: int) -> str | None:
    """Best-effort account name for *uid*, for logging only."""
    import pwd

    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return None
