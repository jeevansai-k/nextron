"""Kill switch.

While a VPN transition is in flight there is a window in which the old tunnel
is gone and the new one is not up yet. The kill switch closes that window by
installing a default-drop output filter that permits only:

* loopback traffic (the local DNS Shield and Tor's SOCKS/Trans ports),
* already established connections,
* the VPN server endpoints needed to build the next tunnel,
* traffic leaving through the tunnel interfaces themselves,
* optionally the local LAN, so an SSH session or printer keeps working.

``nftables`` is preferred, with an ``iptables`` fallback. Both require root: if
NEXTRON cannot get root it reports the kill switch as *unavailable* rather than
pretending the machine is protected.
"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass

from nextron.core import process
from nextron.core.exceptions import PrivilegeError

log = logging.getLogger(__name__)

__all__ = ["KillSwitch", "KillSwitchStatus"]

_TABLE = "nextron_killswitch"
_CHAIN = "output"
_PRIVATE_NETS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")


@dataclass(frozen=True, slots=True)
class KillSwitchStatus:
    """What the kill switch is currently doing."""

    active: bool
    backend: str | None = None
    reason: str | None = None
    allowed_hosts: tuple[str, ...] = ()
    allowed_interfaces: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        if self.active:
            return f"Armed ({self.backend})"
        return f"Off -- {self.reason}" if self.reason else "Off"


class KillSwitch:
    """Default-drop egress filter, engaged around every VPN transition."""

    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled
        self._active = False
        self._backend: str | None = None
        self._reason: str | None = None
        self._hosts: tuple[str, ...] = ()
        self._interfaces: tuple[str, ...] = ()

    # -- properties --------------------------------------------------------- #

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    @property
    def active(self) -> bool:
        return self._active

    def status(self) -> KillSwitchStatus:
        return KillSwitchStatus(
            active=self._active,
            backend=self._backend,
            reason=self._reason,
            allowed_hosts=self._hosts,
            allowed_interfaces=self._interfaces,
        )

    # -- capability --------------------------------------------------------- #

    def backend_name(self) -> str | None:
        """Return the filtering backend available on this system."""
        if process.which("nft"):
            return "nftables"
        if process.which("iptables"):
            return "iptables"
        return None

    def available(self) -> tuple[bool, str | None]:
        """``(usable, reason_if_not)``."""
        if not self._enabled:
            return False, "disabled in settings"
        if self.backend_name() is None:
            return False, "neither nft nor iptables is installed"
        if not process.is_root() and not process.sudo_available():
            return False, "root privileges unavailable"
        return True, None

    # -- engage / release --------------------------------------------------- #

    async def engage(
        self,
        *,
        allow_hosts: list[str] | None = None,
        allow_interfaces: list[str] | None = None,
        allow_lan: bool = True,
    ) -> KillSwitchStatus:
        """Install the default-drop ruleset."""
        usable, reason = self.available()
        if not usable:
            self._active = False
            self._reason = reason
            log.warning("Kill switch not engaged: %s", reason)
            return self.status()

        hosts = tuple(_resolve_hosts(allow_hosts or []))
        interfaces = tuple(allow_interfaces or [])
        backend = self.backend_name()

        try:
            if backend == "nftables":
                await self._engage_nft(hosts, interfaces, allow_lan)
            else:
                await self._engage_iptables(hosts, interfaces, allow_lan)
        except PrivilegeError as exc:
            self._active = False
            self._reason = str(exc)
            log.error("Kill switch failed: %s", exc)
            return self.status()

        self._active = True
        self._backend = backend
        self._reason = None
        self._hosts = hosts
        self._interfaces = interfaces
        log.info(
            "Kill switch armed via %s (hosts=%s interfaces=%s)",
            backend,
            ", ".join(hosts) or "none",
            ", ".join(interfaces) or "none",
        )
        return self.status()

    async def release(self, *, force: bool = False) -> KillSwitchStatus:
        """Remove every rule NEXTRON installed.

        ``force`` removes a ruleset this process did not install -- used at
        startup to clean up after a crashed session.
        """
        if not self._active and not force:
            self._reason = None
            return self.status()

        backend = self._backend or self.backend_name()
        if backend == "nftables":
            await process.run_privileged(
                "nft", "delete", "table", "inet", _TABLE, timeout=15, quiet=True
            )
        else:
            await self._release_iptables()

        log.info("Kill switch released")
        self._active = False
        self._hosts = ()
        self._interfaces = ()
        return self.status()

    async def is_installed(self) -> bool:
        """Check the system for a NEXTRON ruleset (survives a crash/restart)."""
        if process.which("nft"):
            outcome = await process.run_privileged(
                "nft", "list", "tables", timeout=10, quiet=True
            )
            if outcome.ok and _TABLE in outcome.stdout:
                return True
        if process.which("iptables"):
            outcome = await process.run_privileged(
                "iptables", "-S", "OUTPUT", timeout=10, quiet=True
            )
            if outcome.ok and "NEXTRON-KILLSWITCH" in outcome.stdout:
                return True
        return False

    # -- backends ----------------------------------------------------------- #

    def build_nft_ruleset(
        self,
        hosts: tuple[str, ...],
        interfaces: tuple[str, ...],
        allow_lan: bool,
    ) -> str:
        """Render the nftables ruleset (pure function -- unit testable)."""
        rules = [
            f"table inet {_TABLE} {{",
            f"  chain {_CHAIN} {{",
            "    type filter hook output priority 0; policy drop;",
            "    oifname \"lo\" accept",
            "    ct state established,related accept",
        ]
        for interface in interfaces:
            rules.append(f'    oifname "{interface}" accept')
        for host in hosts:
            family = "ip6" if ":" in host else "ip"
            rules.append(f"    {family} daddr {host} accept")
        if allow_lan:
            rules.append(f"    ip daddr {{ {', '.join(_PRIVATE_NETS)} }} accept")
            rules.append("    ip6 daddr fe80::/10 accept")
        # DHCP renewal must survive, or the machine loses its lease mid-session.
        rules.append("    udp dport { 67, 68 } accept")
        rules.extend(["  }", "}"])
        return "\n".join(rules) + "\n"

    async def _engage_nft(
        self,
        hosts: tuple[str, ...],
        interfaces: tuple[str, ...],
        allow_lan: bool,
    ) -> None:
        # Replace any previous incarnation atomically.
        await process.run_privileged(
            "nft", "delete", "table", "inet", _TABLE, timeout=10, quiet=True
        )
        ruleset = self.build_nft_ruleset(hosts, interfaces, allow_lan)
        outcome = await process.run_privileged(
            "nft", "-f", "-", stdin=ruleset, timeout=20
        )
        if not outcome.ok:
            raise PrivilegeError(
                f"nft refused the ruleset: {outcome.stderr.strip()[:200]}"
            )

    async def _engage_iptables(
        self,
        hosts: tuple[str, ...],
        interfaces: tuple[str, ...],
        allow_lan: bool,
    ) -> None:
        await self._release_iptables()
        base = ["iptables", "-w", "5"]
        rules: list[list[str]] = [
            ["-o", "lo", "-j", "ACCEPT"],
            ["-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
        ]
        for interface in interfaces:
            rules.append(["-o", interface, "-j", "ACCEPT"])
        for host in hosts:
            if ":" in host:
                continue
            rules.append(["-d", host, "-j", "ACCEPT"])
        if allow_lan:
            rules.extend([["-d", net, "-j", "ACCEPT"] for net in _PRIVATE_NETS])
        rules.append(["-p", "udp", "--dport", "67:68", "-j", "ACCEPT"])

        for rule in rules:
            outcome = await process.run_privileged(
                *base,
                "-A",
                "OUTPUT",
                *rule,
                "-m",
                "comment",
                "--comment",
                "NEXTRON-KILLSWITCH",
                timeout=15,
            )
            if not outcome.ok:
                raise PrivilegeError(
                    f"iptables rejected a rule: {outcome.stderr.strip()[:200]}"
                )

        outcome = await process.run_privileged(
            *base,
            "-A",
            "OUTPUT",
            "-m",
            "comment",
            "--comment",
            "NEXTRON-KILLSWITCH",
            "-j",
            "DROP",
            timeout=15,
        )
        if not outcome.ok:
            raise PrivilegeError("iptables could not install the final DROP rule")

    async def _release_iptables(self) -> None:
        """Delete every OUTPUT rule carrying the NEXTRON comment."""
        for _ in range(64):  # bounded: rules are removed one at a time
            listing = await process.run_privileged(
                "iptables", "-w", "5", "-S", "OUTPUT", timeout=15, quiet=True
            )
            if not listing.ok:
                return
            target = next(
                (
                    line
                    for line in listing.stdout.splitlines()
                    if "NEXTRON-KILLSWITCH" in line
                ),
                None,
            )
            if target is None:
                return
            arguments = target.split()[1:]  # drop the leading "-A"
            await process.run_privileged(
                "iptables", "-w", "5", "-D", *arguments, timeout=15, quiet=True
            )


def _resolve_hosts(entries: list[str]) -> list[str]:
    """Resolve ``host`` / ``host:port`` entries to bare IP addresses."""
    resolved: list[str] = []
    for entry in entries:
        if not entry:
            continue
        host = entry.rsplit(":", 1)[0] if entry.count(":") == 1 else entry
        host = host.strip("[]")
        try:
            socket.inet_aton(host)
            resolved.append(host)
            continue
        except OSError:
            pass
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError:
            log.warning("Kill switch cannot resolve '%s'; it will be blocked", entry)
            continue
        for info in infos:
            address = info[4][0]
            if address not in resolved:
                resolved.append(address)
    return resolved
