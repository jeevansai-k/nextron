"""WireGuard backend, driven through the official ``wg-quick``/``wg`` tools."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from nextron.core import process
from nextron.core.exceptions import VPNConnectionError
from nextron.storage import paths
from nextron.utils import net
from nextron.vpn.profiles import VPNProfile

log = logging.getLogger(__name__)

__all__ = ["WireGuardBackend", "WireGuardResult"]

#: Linux caps interface names at 15 characters.
_MAX_IFNAME = 15
_ADDRESS_RE = re.compile(
    r"^\s*Address\s*=\s*(?P<ip>[0-9a-fA-F:.]+)", re.MULTILINE | re.IGNORECASE
)


@dataclass(slots=True)
class WireGuardResult:
    """Facts about an established WireGuard tunnel."""

    interface: str | None = None
    tunnel_ip: str | None = None
    endpoint: str | None = None
    ready: bool = False
    error: str | None = None


class WireGuardBackend:
    """Bring ``wg-quick`` interfaces up and down."""

    def __init__(self, wg_quick: str = "wg-quick", wg: str = "wg") -> None:
        self._wg_quick = wg_quick
        self._wg = wg
        self._interface: str | None = None
        self._result = WireGuardResult()

    # -- properties --------------------------------------------------------- #

    @property
    def connected(self) -> bool:
        return self._interface is not None and net.interface_exists(self._interface)

    @property
    def interface(self) -> str | None:
        return self._interface

    @property
    def result(self) -> WireGuardResult:
        return self._result

    # -- configuration ------------------------------------------------------ #

    @staticmethod
    def interface_name(profile: VPNProfile) -> str:
        """Derive a stable, legal interface name for *profile*."""
        base = re.sub(r"[^a-z0-9]+", "", profile.name.lower())[:8] or "wg"
        name = f"nx{base}{profile.id[:4]}"
        return name[:_MAX_IFNAME]

    def runtime_config(self, profile: VPNProfile) -> Path:
        """Write the config under the name ``wg-quick`` will use as the interface."""
        paths.ensure_layout()
        directory = paths.runtime_dir() / "wireguard"
        directory.mkdir(parents=True, exist_ok=True)
        try:
            directory.chmod(0o700)
        except OSError:  # pragma: no cover
            pass

        target = directory / f"{self.interface_name(profile)}.conf"
        target.write_text(profile.read_config(), encoding="utf-8")
        target.chmod(0o600)
        return target

    # -- lifecycle ---------------------------------------------------------- #

    async def connect(
        self, profile: VPNProfile, *, timeout: float = 45.0
    ) -> WireGuardResult:
        """Run ``wg-quick up`` and confirm the interface really exists."""
        if not process.which(self._wg_quick):
            raise VPNConnectionError(
                "WireGuard tools are not installed. Install them with: "
                "sudo apt install wireguard-tools"
            )
        if self._interface:
            await self.disconnect()

        config = self.runtime_config(profile)
        interface = config.stem
        self._result = WireGuardResult(interface=interface)

        log.info("Bringing up WireGuard interface %s", interface)
        outcome = await process.run_privileged(
            self._wg_quick, "up", str(config), timeout=timeout
        )
        if not outcome.ok:
            detail = outcome.stderr.strip().splitlines()[-1:] or ["unknown error"]
            self._result.error = detail[0][:200]
            raise VPNConnectionError(f"{profile.name}: {self._result.error}")

        # wg-quick returns before the kernel has finished; confirm the device.
        for _ in range(20):
            if net.interface_exists(interface):
                break
            await asyncio.sleep(0.25)
        else:
            raise VPNConnectionError(
                f"{profile.name}: wg-quick reported success but {interface} is absent"
            )

        self._interface = interface
        addresses = net.interface_addresses(interface)
        self._result.tunnel_ip = next(
            (a for a in addresses if "." in a),
            next(iter(addresses), None),
        )
        if not self._result.tunnel_ip:
            match = _ADDRESS_RE.search(profile.read_config())
            self._result.tunnel_ip = match.group("ip").split("/")[0] if match else None

        self._result.endpoint = await self.current_endpoint()
        self._result.ready = True
        log.info(
            "WireGuard connected: %s (%s)",
            interface,
            self._result.tunnel_ip or "no address",
        )
        return self._result

    async def disconnect(self) -> None:
        """Run ``wg-quick down`` for the interface we brought up."""
        if not self._interface:
            return
        config = paths.runtime_dir() / "wireguard" / f"{self._interface}.conf"
        target = str(config) if config.is_file() else self._interface

        outcome = await process.run_privileged(self._wg_quick, "down", target, timeout=30)
        if not outcome.ok:
            log.warning(
                "wg-quick down failed for %s: %s",
                self._interface,
                outcome.stderr.strip()[:200],
            )
        else:
            log.info("WireGuard interface %s removed", self._interface)

        self._interface = None
        self._result = WireGuardResult()

    # -- queries ------------------------------------------------------------ #

    async def current_endpoint(self) -> str | None:
        """Read the negotiated peer endpoint from ``wg show``."""
        if not self._interface and not self._result.interface:
            return None
        interface = self._interface or self._result.interface
        if not process.which(self._wg):
            return None
        outcome = await process.run_privileged(
            self._wg, "show", str(interface), "endpoints", timeout=10, quiet=True
        )
        if not outcome.ok or not outcome.stdout.strip():
            return None
        parts = outcome.stdout.split()
        return parts[-1] if len(parts) >= 2 else None

    async def handshake_age(self) -> int | None:
        """Seconds since the last handshake, or ``None`` when unavailable."""
        interface = self._interface
        if not interface or not process.which(self._wg):
            return None
        outcome = await process.run_privileged(
            self._wg, "show", interface, "latest-handshakes", timeout=10, quiet=True
        )
        if not outcome.ok:
            return None
        import time

        newest = 0
        for line in outcome.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[-1].isdigit():
                newest = max(newest, int(fields[-1]))
        if newest == 0:
            return None
        return max(0, int(time.time()) - newest)
