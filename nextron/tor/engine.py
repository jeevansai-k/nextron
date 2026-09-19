"""The Tor engine: daemon + ControlPort + identity rotation.

Responsibilities:

* own the daemon lifecycle and surface bootstrap progress into the state;
* perform manual and scheduled identity rotations (NEWNYM);
* verify that the exit IP actually changed after a rotation;
* keep circuit, exit IP, exit country and uptime fresh for the dashboard.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from nextron.core.context import EngineContext
from nextron.core.events import EventType
from nextron.state.manager import ServiceStatus
from nextron.tor.controller import TorController
from nextron.tor.daemon import TorDaemon
from nextron.utils import net

log = logging.getLogger(__name__)

__all__ = ["TorEngine"]


class TorEngine:
    """High level Tor control surface used by the routing manager."""

    def __init__(self, context: EngineContext) -> None:
        self._ctx = context
        self._daemon = TorDaemon(context.config.tor, context.bus)
        self._controller = TorController(context.config.tor, context.bus)
        self._rotation_lock = asyncio.Lock()
        self._started_at: datetime | None = None
        self._rotations = 0

        context.bus.subscribe(EventType.TOR_BOOTSTRAP, self._on_bootstrap)

    # -- properties --------------------------------------------------------- #

    @property
    def settings(self):
        return self._ctx.config.tor

    @property
    def controller(self) -> TorController:
        return self._controller

    @property
    def daemon(self) -> TorDaemon:
        return self._daemon

    @property
    def running(self) -> bool:
        return self._daemon.running

    @property
    def socks_port(self) -> int:
        """The port Tor is *actually* listening on, not merely the preference."""
        return self._daemon.socks_port

    @property
    def control_port(self) -> int:
        return self._daemon.control_port

    @property
    def dns_port(self) -> int:
        return self._daemon.dns_port

    @property
    def trans_port(self) -> int:
        return self._daemon.trans_port

    @property
    def proxy_url(self) -> str:
        """SOCKS5 URL for routing HTTP probes through Tor."""
        return net.socks_proxy_url(self.socks_port)

    @property
    def uptime_seconds(self) -> int | None:
        if self._started_at is None:
            return None
        return int((datetime.now() - self._started_at).total_seconds())

    # -- lifecycle ---------------------------------------------------------- #

    async def start(self) -> None:
        """Bring Tor up and connect the ControlPort."""
        if self.running and self._controller.connected:
            return

        state = self._ctx.state
        state.update_tor(
            status=ServiceStatus.STARTING,
            error=None,
            socks_port=self._daemon.socks_port,
            control_port=self._daemon.control_port,
            rotation_enabled=self.settings.rotation_enabled,
            rotation_interval=self.settings.rotation_interval,
        )
        self._ctx.activity("Starting Tor engine")

        try:
            await self._daemon.start()
            state.update_tor(status=ServiceStatus.VERIFYING, bootstrap_percent=100)
            await self._controller.connect(
                self._daemon.resolve_cookie(), port=self._daemon.control_port
            )
        except Exception as exc:
            state.update_tor(status=ServiceStatus.ERROR, error=str(exc))
            raise

        self._started_at = datetime.now()
        version = await self._controller.version()
        state.update_tor(
            status=ServiceStatus.ACTIVE,
            socks_port=self._daemon.socks_port,
            control_port=self._daemon.control_port,
            started_at=self._started_at,
            version=version,
            bootstrap_percent=100,
            bootstrap_phase="Done",
        )
        self._ctx.bus.emit(
            EventType.TOR_READY,
            f"Tor ready (v{version})" if version else "Tor ready",
            socks_port=self._daemon.socks_port,
        )
        await self.refresh(fetch_ip=True)

    async def stop(self) -> None:
        """Disconnect the ControlPort and stop the daemon."""
        self._ctx.state.update_tor(status=ServiceStatus.STOPPING)
        await self._controller.close()
        await self._daemon.stop()
        self._started_at = None
        self._ctx.state.update_tor(
            status=ServiceStatus.IDLE,
            bootstrap_percent=0,
            bootstrap_phase="",
            circuit_id=None,
            circuit_path=(),
            exit_ip=None,
            exit_country=None,
            exit_fingerprint=None,
            seconds_to_rotation=None,
            started_at=None,
        )
        self._ctx.activity("Tor engine stopped")

    # -- rotation ----------------------------------------------------------- #

    async def rotate(self, *, reason: str = "manual") -> bool:
        """Request a new identity and verify the exit address changed."""
        if not self._controller.connected:
            self._ctx.failure("Cannot rotate: Tor ControlPort is not connected")
            return False

        if self._rotation_lock.locked():
            # Silently doing nothing looks like a broken key press, so say it.
            self._ctx.activity(
                "A Tor rotation is already in progress", level="warning"
            )
            log.debug("Rotation already in progress; ignoring %s request", reason)
            return False

        async with self._rotation_lock:
            state = self._ctx.state
            previous_ip = state.state.tor.exit_ip
            state.update_tor(status=ServiceStatus.ROTATING)
            self._ctx.activity(f"Rotating Tor identity ({reason})")
            began = time.monotonic()

            if not await self._controller.newnym():
                state.update_tor(status=ServiceStatus.ACTIVE, error="NEWNYM refused")
                self._ctx.failure("Tor refused the NEWNYM signal")
                return False

            new_ip = previous_ip
            country = state.state.tor.exit_country
            if self.settings.verify_exit_after_rotation:
                new_ip, country = await self._await_new_exit(previous_ip)
            else:
                await asyncio.sleep(1.0)

            duration_ms = int((time.monotonic() - began) * 1000)
            self._rotations += 1
            changed = bool(new_ip) and new_ip != previous_ip

            await self.refresh(fetch_ip=False)
            state.update_tor(
                status=ServiceStatus.ACTIVE,
                exit_ip=new_ip or previous_ip,
                exit_country=country,
                rotations=self._rotations,
                last_rotation=datetime.now(),
                error=None,
            )
            if new_ip:
                state.update(public_ip=new_ip, exit_country=country)

            self._ctx.bus.emit(
                EventType.TOR_ROTATED,
                f"New identity: {new_ip or 'unverified'}"
                + (f" ({country})" if country else ""),
                exit_ip=new_ip,
                exit_country=country,
                changed=changed,
                duration_ms=duration_ms,
            )
            try:
                await self._ctx.database.record_rotation(
                    "tor",
                    detail=state.state.tor.circuit_id,
                    exit_ip=new_ip,
                    exit_country=country,
                    duration_ms=duration_ms,
                    success=bool(new_ip),
                )
                if new_ip:
                    await self._ctx.database.record_ip("tor", new_ip, country)
            except Exception as exc:  # pragma: no cover
                log.debug("Rotation not persisted: %s", exc)

            if not changed and self.settings.verify_exit_after_rotation:
                self._ctx.activity(
                    "Exit address unchanged after rotation "
                    "(Tor may have reused a clean circuit)",
                    level="warning",
                )
            return True

    async def _await_new_exit(
        self, previous_ip: str | None
    ) -> tuple[str | None, str | None]:
        """Poll the exit address until it differs from *previous_ip*."""
        attempts = max(1, self.settings.rotation_retries + 1)
        ip: str | None = None
        for attempt in range(attempts):
            await asyncio.sleep(1.5 if attempt == 0 else 2.5)
            ip = await net.public_ip(proxy=self.proxy_url, timeout=12)
            if ip and ip != previous_ip:
                break
        if not ip:
            return None, None

        country = await self._controller.country_of(ip)
        if not country:
            info = await net.ip_info(ip, proxy=self.proxy_url, timeout=10)
            country = info.country if info else None
        return ip, country

    # -- refresh ------------------------------------------------------------ #

    async def refresh(self, *, fetch_ip: bool = True) -> None:
        """Update circuit and exit facts on the dashboard."""
        if not self._controller.connected:
            return

        circuit = await self._controller.active_circuit()
        updates: dict[str, object] = {}
        if circuit is not None:
            updates["circuit_id"] = circuit.id
            updates["circuit_path"] = circuit.path

        if fetch_ip:
            ip = await net.public_ip(proxy=self.proxy_url, timeout=12)
            if ip:
                country = await self._controller.country_of(ip)
                if not country:
                    info = await net.ip_info(ip, proxy=self.proxy_url, timeout=10)
                    country = info.country if info else None
                updates["exit_ip"] = ip
                updates["exit_country"] = country
                self._ctx.state.update(public_ip=ip, exit_country=country)
                try:
                    await self._ctx.database.record_ip("tor", ip, country)
                except Exception:  # pragma: no cover
                    pass

        fingerprint = await self._controller.exit_fingerprint()
        if fingerprint:
            updates["exit_fingerprint"] = fingerprint

        if updates:
            self._ctx.state.update_tor(**updates)

    async def verify_socks(self) -> bool:
        """True when the SOCKS port accepts connections."""
        return await net.is_port_open("127.0.0.1", self.socks_port)

    async def confirm_tor_routing(self) -> tuple[bool, str | None]:
        """Ask check.torproject.org whether traffic really exits via Tor."""
        return await net.tor_check(proxy=self.proxy_url, timeout=20)

    # -- events ------------------------------------------------------------- #

    def _on_bootstrap(self, event) -> None:
        percent = int(event.get("percent", 0) or 0)
        phase = str(event.get("phase", "") or "")
        status = (
            ServiceStatus.BOOTSTRAPPING
            if percent < 100
            else self._ctx.state.state.tor.status
        )
        self._ctx.state.update_tor(
            status=status, bootstrap_percent=percent, bootstrap_phase=phase
        )
