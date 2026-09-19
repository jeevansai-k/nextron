"""The VPN engine.

Sits on top of the profile library, the two protocol backends, the kill switch
and the shuffle engine, and exposes one coherent surface to the routing manager
and the VPN scheduler:

* ``connect`` / ``disconnect`` / ``switch`` with a guarded transition window;
* automatic protocol detection per profile;
* tunnel verification (interface, address, public IP change);
* graceful reconnect with bounded retries.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from nextron.core.context import EngineContext
from nextron.core.events import EventType
from nextron.core.exceptions import (
    VPNConnectionError,
    VPNCredentialsRequired,
    VPNError,
)
from nextron.state.manager import ServiceStatus
from nextron.utils import net
from nextron.vpn.killswitch import KillSwitch
from nextron.vpn.openvpn import OpenVPNBackend
from nextron.vpn.profiles import ProfileLibrary, VPNProfile, VPNProtocol
from nextron.vpn.shuffle import ShuffleEngine
from nextron.vpn.wireguard import WireGuardBackend

log = logging.getLogger(__name__)

__all__ = ["VPNEngine"]


class VPNEngine:
    """Own every VPN tunnel NEXTRON establishes."""

    def __init__(self, context: EngineContext) -> None:
        self._ctx = context
        settings = context.config.vpn

        self.library = ProfileLibrary()
        self.killswitch = KillSwitch(enabled=settings.killswitch_enabled)
        self.shuffle = ShuffleEngine(settings.shuffle_algorithm)

        self._openvpn = OpenVPNBackend(settings.openvpn_binary)
        self._wireguard = WireGuardBackend(
            settings.wireguard_binary, settings.wg_tool_binary
        )
        self._current: VPNProfile | None = None
        self._lock = asyncio.Lock()
        self._switches = 0
        self._connected_at: datetime | None = None
        #: SOCKS hop for VPN-over-Tor; ``None`` for every other mode.
        self._socks_proxy: tuple[str, int] | None = None

    # -- properties --------------------------------------------------------- #

    @property
    def settings(self):
        return self._ctx.config.vpn

    @property
    def current(self) -> VPNProfile | None:
        return self._current

    @property
    def connected(self) -> bool:
        if self._current is None:
            return False
        if self._current.protocol is VPNProtocol.WIREGUARD:
            return self._wireguard.connected
        return self._openvpn.connected

    @property
    def interface(self) -> str | None:
        if self._current is None:
            return None
        if self._current.protocol is VPNProtocol.WIREGUARD:
            return self._wireguard.interface
        return self._openvpn.result.interface

    @property
    def tunnel_ip(self) -> str | None:
        if self._current is None:
            return None
        if self._current.protocol is VPNProtocol.WIREGUARD:
            return self._wireguard.result.tunnel_ip
        return self._openvpn.result.tunnel_ip

    @property
    def uptime_seconds(self) -> int | None:
        if self._connected_at is None:
            return None
        return int((datetime.now() - self._connected_at).total_seconds())

    # -- setup -------------------------------------------------------------- #

    def load_library(self) -> list[VPNProfile]:
        """Load profiles from disk and seed the shuffle pool."""
        profiles = self.library.load()
        self.refresh_pool()
        self._ctx.state.update_vpn(
            pool_size=self.shuffle.pool_size,
            shuffle_enabled=self.settings.shuffle_enabled,
            shuffle_algorithm=self.settings.shuffle_algorithm,
            shuffle_interval=self.settings.shuffle_interval,
        )
        log.info("Profile library loaded (%d profiles)", len(profiles))
        return profiles

    def refresh_pool(self) -> None:
        """Re-resolve the rotation pool from configuration."""
        self.shuffle.set_algorithm(self.settings.shuffle_algorithm)
        self.shuffle.set_pool(self.library.pool(self.settings.shuffle_pool))
        self._ctx.state.update_vpn(pool_size=self.shuffle.pool_size)

    def use_socks_proxy(self, proxy: tuple[str, int] | None) -> None:
        """Route subsequent OpenVPN connections through a SOCKS hop (VPN over Tor)."""
        self._socks_proxy = proxy

    def resolve_target(self, needle: str | None = None) -> VPNProfile:
        """Pick the profile to connect: explicit, configured, favourite, or first."""
        if needle:
            profile = self.library.resolve(needle)
            if profile is None:
                raise VPNError(f"No VPN profile matches '{needle}'")
            return profile

        configured = self.library.get(self.settings.active_profile)
        if configured is not None:
            return configured

        candidates = self.library.favorites() or self.library.all()
        if not candidates:
            raise VPNError(
                "No VPN profiles have been imported yet. Add one from the "
                "VPN Library screen (V) or with: nextron vpn import <file>"
            )
        return candidates[0]

    # -- connection --------------------------------------------------------- #

    async def connect(
        self,
        profile: VPNProfile | str | None = None,
        *,
        reason: str = "manual",
    ) -> VPNProfile:
        """Establish a tunnel, guarding the transition with the kill switch."""
        async with self._lock:
            target = (
                profile
                if isinstance(profile, VPNProfile)
                else self.resolve_target(profile)
            )
            return await self._connect_locked(target, reason=reason)

    async def _connect_locked(self, target: VPNProfile, *, reason: str) -> VPNProfile:
        state = self._ctx.state
        state.update_vpn(
            status=ServiceStatus.STARTING,
            profile_id=target.id,
            profile_name=target.name,
            protocol=target.protocol.label,
            endpoint=target.endpoint,
            error=None,
        )
        self._ctx.bus.emit(
            EventType.VPN_CONNECTING,
            f"Connecting VPN '{target.name}' ({target.protocol.label})",
            profile=target.name,
            reason=reason,
        )

        await self._guard_transition(target)
        if self.connected:
            await self._teardown_current()

        began = time.monotonic()
        attempts = max(1, self.settings.reconnect_retries + 1)
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                await self._bring_up(target)
                break
            except VPNCredentialsRequired as exc:
                # Deterministic: the answer will not appear between attempts.
                last_error = exc
                log.warning("VPN '%s' needs credentials: %s", target.name, exc)
                break
            except VPNConnectionError as exc:
                last_error = exc
                log.warning(
                    "VPN attempt %d/%d for '%s' failed: %s",
                    attempt,
                    attempts,
                    target.name,
                    exc,
                )
                if attempt < attempts:
                    await asyncio.sleep(min(5.0, 1.5 * attempt))

        if not self.connected:
            state.update_vpn(
                status=ServiceStatus.ERROR,
                error=str(last_error) if last_error else "tunnel not established",
            )
            self._ctx.bus.emit(
                EventType.VPN_DISCONNECTED,
                f"VPN '{target.name}' failed to connect",
                level="error",
            )
            raise VPNConnectionError(
                str(last_error) if last_error else f"Could not connect '{target.name}'"
            )

        self._current = target
        self._connected_at = datetime.now()
        self.library.mark_used(target.id)

        state.update_vpn(
            status=ServiceStatus.VERIFYING,
            interface=self.interface,
            tunnel_ip=self.tunnel_ip,
            connected_at=self._connected_at,
        )

        # Re-arm the kill switch now that the tunnel interface exists so a
        # tunnel drop cannot leak traffic to the default route.
        await self._arm_steady_state(target)

        public_ip, country = await self._observe_exit()
        state.update_vpn(
            status=ServiceStatus.ACTIVE,
            public_ip=public_ip,
            country=country,
            error=None,
        )
        if public_ip and self._socks_proxy is None:
            state.update(public_ip=public_ip, exit_country=country)

        duration_ms = int((time.monotonic() - began) * 1000)
        self._ctx.bus.emit(
            EventType.VPN_CONNECTED,
            f"VPN '{target.name}' connected"
            + (f" via {public_ip} ({country})" if public_ip and country else ""),
            profile=target.name,
            interface=self.interface,
            public_ip=public_ip,
            duration_ms=duration_ms,
        )
        try:
            if public_ip:
                await self._ctx.database.record_ip("vpn", public_ip, country)
        except Exception:  # pragma: no cover
            pass
        return target

    async def _bring_up(self, target: VPNProfile) -> None:
        """Dispatch to the backend that matches the profile's protocol."""
        timeout = float(self.settings.connect_timeout)
        if target.protocol is VPNProtocol.WIREGUARD:
            if self._socks_proxy is not None:
                raise VPNConnectionError(
                    f"'{target.name}' is WireGuard. WireGuard is UDP-only and "
                    "cannot traverse Tor's SOCKS proxy -- use a TCP OpenVPN "
                    "profile for VPN over Tor."
                )
            await self._wireguard.connect(target, timeout=timeout)
        else:
            await self._openvpn.connect(
                target, timeout=timeout, socks_proxy=self._socks_proxy
            )

    async def _teardown_current(self) -> None:
        if self._current is None:
            return
        if self._current.protocol is VPNProtocol.WIREGUARD:
            await self._wireguard.disconnect()
        else:
            await self._openvpn.disconnect()

    async def disconnect(self, *, release_killswitch: bool = True) -> None:
        """Tear the tunnel down and optionally disarm the kill switch."""
        async with self._lock:
            if self._current is not None:
                self._ctx.state.update_vpn(status=ServiceStatus.STOPPING)
                await self._teardown_current()
                self._ctx.bus.emit(
                    EventType.VPN_DISCONNECTED,
                    f"VPN '{self._current.name}' disconnected",
                    profile=self._current.name,
                )
            self._current = None
            self._connected_at = None

            if release_killswitch and self.killswitch.active:
                status = await self.killswitch.release()
                self._ctx.state.update_vpn(killswitch_active=status.active)

            self._ctx.state.update_vpn(
                status=ServiceStatus.IDLE,
                interface=None,
                tunnel_ip=None,
                public_ip=None,
                country=None,
                connected_at=None,
                seconds_to_shuffle=None,
            )

    async def switch(
        self, profile: VPNProfile | str | None = None, *, reason: str = "shuffle"
    ) -> VPNProfile | None:
        """Graceful reconnect used by the shuffle scheduler."""
        async with self._lock:
            previous = self._current
            target = (
                profile
                if isinstance(profile, VPNProfile)
                else (
                    self.library.resolve(profile)
                    if isinstance(profile, str)
                    else self.shuffle.next(previous.id if previous else None)
                )
            )
            if target is None:
                self._ctx.activity(
                    "VPN shuffle skipped: the rotation pool is empty", level="warning"
                )
                return None
            repeat = previous is not None and target.id == previous.id
            if repeat and self.shuffle.pool_size > 1:
                target = self.shuffle.next(previous.id) or target

            began = time.monotonic()
            try:
                await self._connect_locked(target, reason=reason)
            except VPNError as exc:
                self._ctx.failure(f"VPN switch to '{target.name}' failed: {exc}")
                if previous is not None:
                    self._ctx.activity(
                        f"Falling back to '{previous.name}'", level="warning"
                    )
                    try:
                        await self._connect_locked(previous, reason="fallback")
                    except VPNError:
                        self._ctx.failure("Fallback profile also failed; VPN is down")
                return None

            self._switches += 1
            duration_ms = int((time.monotonic() - began) * 1000)
            self._ctx.state.update_vpn(
                switches=self._switches, last_switch=datetime.now()
            )
            self._ctx.bus.emit(
                EventType.VPN_SWITCHED,
                f"VPN switched {previous.name if previous else 'none'} -> {target.name}",
                previous=previous.name if previous else None,
                profile=target.name,
                duration_ms=duration_ms,
            )
            try:
                await self._ctx.database.record_rotation(
                    "vpn",
                    detail=target.name,
                    exit_ip=self._ctx.state.state.vpn.public_ip,
                    exit_country=self._ctx.state.state.vpn.country,
                    duration_ms=duration_ms,
                )
            except Exception:  # pragma: no cover
                pass
            return target

    # -- kill switch -------------------------------------------------------- #

    async def _guard_transition(self, target: VPNProfile) -> None:
        """Arm the kill switch for the window where no tunnel exists."""
        if not self.settings.killswitch_enabled:
            return
        if self._socks_proxy is not None:
            # VPN over Tor: Tor itself must reach arbitrary guard relays, so a
            # default-drop egress filter would break the very path we need.
            self._ctx.activity(
                "Kill switch stays off for VPN over Tor (Tor needs direct egress)",
                level="warning",
            )
            return

        self.killswitch.enabled = True
        hosts = [target.endpoint] if target.endpoint else []
        status = await self.killswitch.engage(allow_hosts=hosts)
        self._ctx.state.update_vpn(killswitch_active=status.active)
        self._ctx.bus.emit(
            EventType.VPN_KILLSWITCH,
            f"Kill switch {status.label}",
            active=status.active,
            phase="transition",
        )

    async def _arm_steady_state(self, target: VPNProfile) -> None:
        """Re-arm the kill switch with the live tunnel interface permitted."""
        if not self.settings.killswitch_enabled or self._socks_proxy is not None:
            return
        interfaces = [self.interface] if self.interface else []
        hosts = [target.endpoint] if target.endpoint else []
        status = await self.killswitch.engage(
            allow_hosts=hosts, allow_interfaces=[i for i in interfaces if i]
        )
        self._ctx.state.update_vpn(killswitch_active=status.active)

    # -- verification ------------------------------------------------------- #

    async def _observe_exit(self) -> tuple[str | None, str | None]:
        """Read the public IP as seen from behind the tunnel."""
        if not self.settings.verify_tunnel:
            return None, None
        ip = await net.public_ip(timeout=15)
        if not ip:
            return None, None
        info = await net.ip_info(ip, timeout=10)
        return ip, info.country if info else None

    async def verify(self) -> tuple[bool, str]:
        """Confirm the tunnel exists and carries traffic."""
        if self._current is None:
            return False, "no VPN profile is active"
        interface = self.interface
        if not interface:
            return False, "no tunnel interface was created"
        if not net.interface_exists(interface):
            return False, f"tunnel interface {interface} has disappeared"
        if not net.interface_addresses(interface):
            return False, f"tunnel interface {interface} has no address"

        if self._current.protocol is VPNProtocol.WIREGUARD:
            age = await self._wireguard.handshake_age()
            if age is not None and age > 180:
                return False, f"last WireGuard handshake was {age}s ago"
        elif not self._openvpn.connected:
            return False, "the OpenVPN process is no longer running"

        return True, f"{interface} up ({self.tunnel_ip or 'no address'})"

    async def watchdog(self) -> bool:
        """Re-verify the tunnel and reconnect once if it has dropped."""
        if self._current is None:
            return False
        healthy, detail = await self.verify()
        if healthy:
            return True

        self._ctx.activity(f"VPN tunnel unhealthy: {detail}", level="warning")
        target = self._current
        try:
            await self.connect(target, reason="watchdog")
            return True
        except VPNError as exc:
            self._ctx.failure(f"VPN reconnect failed: {exc}")
            return False
