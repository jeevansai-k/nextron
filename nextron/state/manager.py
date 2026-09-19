"""Central application state.

The dashboard is a pure function of :class:`AppState`. Engines never touch the
interface -- they mutate state through :class:`StateManager`, which publishes a
single ``STATE_CHANGED`` event so the TUI refreshes once per change.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Any

from nextron.core import constants
from nextron.core.config import RoutingMode, ShuffleAlgorithm
from nextron.core.events import Event, EventBus, EventType

log = logging.getLogger(__name__)

__all__ = [
    "AppState",
    "DNSState",
    "ServiceStatus",
    "StateManager",
    "TorState",
    "VPNState",
]


class ServiceStatus(str, Enum):
    """Lifecycle state shared by every engine."""

    DISABLED = "disabled"
    IDLE = "idle"
    STARTING = "starting"
    BOOTSTRAPPING = "bootstrapping"
    VERIFYING = "verifying"
    ACTIVE = "active"
    ROTATING = "rotating"
    STOPPING = "stopping"
    ERROR = "error"

    @property
    def label(self) -> str:
        return {
            ServiceStatus.DISABLED: "Disabled",
            ServiceStatus.IDLE: "Idle",
            ServiceStatus.STARTING: "Starting",
            ServiceStatus.BOOTSTRAPPING: "Bootstrapping",
            ServiceStatus.VERIFYING: "Verifying",
            ServiceStatus.ACTIVE: "Connected",
            ServiceStatus.ROTATING: "Rotating",
            ServiceStatus.STOPPING: "Stopping",
            ServiceStatus.ERROR: "Error",
        }[self]

    @property
    def color(self) -> str:
        """A brand-palette colour for this status (no colours outside the set)."""
        if self is ServiceStatus.ACTIVE:
            return constants.COLOR_ACCENT
        if self is ServiceStatus.ERROR:
            return constants.COLOR_PRIMARY
        if self in {
            ServiceStatus.STARTING,
            ServiceStatus.BOOTSTRAPPING,
            ServiceStatus.VERIFYING,
            ServiceStatus.ROTATING,
            ServiceStatus.STOPPING,
        }:
            return constants.COLOR_SURFACE
        return constants.COLOR_SECONDARY

    @property
    def marker(self) -> str:
        return {
            ServiceStatus.ACTIVE: "◉",
            ServiceStatus.ERROR: "✗",
            ServiceStatus.DISABLED: "◌",
            ServiceStatus.IDLE: "◌",
        }.get(self, "◔")

    @property
    def is_busy(self) -> bool:
        return self in {
            ServiceStatus.STARTING,
            ServiceStatus.BOOTSTRAPPING,
            ServiceStatus.VERIFYING,
            ServiceStatus.ROTATING,
            ServiceStatus.STOPPING,
        }


@dataclass(frozen=True, slots=True)
class TorState:
    """Live Tor engine facts."""

    status: ServiceStatus = ServiceStatus.IDLE
    bootstrap_percent: int = 0
    bootstrap_phase: str = ""
    circuit_id: str | None = None
    circuit_path: tuple[str, ...] = ()
    exit_ip: str | None = None
    exit_country: str | None = None
    exit_fingerprint: str | None = None
    socks_port: int = constants.TOR_SOCKS_PORT
    control_port: int = constants.TOR_CONTROL_PORT
    rotation_enabled: bool = False
    rotation_interval: int = 60
    seconds_to_rotation: int | None = None
    rotations: int = 0
    last_rotation: datetime | None = None
    started_at: datetime | None = None
    version: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class VPNState:
    """Live VPN engine facts."""

    status: ServiceStatus = ServiceStatus.IDLE
    protocol: str | None = None
    profile_id: str | None = None
    profile_name: str | None = None
    interface: str | None = None
    tunnel_ip: str | None = None
    public_ip: str | None = None
    country: str | None = None
    endpoint: str | None = None
    shuffle_enabled: bool = False
    shuffle_algorithm: ShuffleAlgorithm = ShuffleAlgorithm.RANDOM
    shuffle_interval: int = 120
    seconds_to_shuffle: int | None = None
    switches: int = 0
    last_switch: datetime | None = None
    killswitch_active: bool = False
    connected_at: datetime | None = None
    pool_size: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class DNSState:
    """Live DNS Shield facts."""

    status: ServiceStatus = ServiceStatus.DISABLED
    listen: str | None = None
    upstream: tuple[str, ...] = ()
    using_tor_dns: bool = False
    blocked_domains: int = 0
    whitelisted_domains: int = 0
    active_blocklists: tuple[str, ...] = ()
    queries_total: int = 0
    queries_blocked: int = 0
    resolv_conf_managed: bool = False
    error: str | None = None

    @property
    def block_rate(self) -> float:
        if not self.queries_total:
            return 0.0
        return (self.queries_blocked / self.queries_total) * 100.0


@dataclass(frozen=True, slots=True)
class AppState:
    """The complete, immutable snapshot rendered by the dashboard."""

    routing_mode: RoutingMode = RoutingMode.TOR_ONLY
    route_status: ServiceStatus = ServiceStatus.IDLE
    tor: TorState = field(default_factory=TorState)
    vpn: VPNState = field(default_factory=VPNState)
    dns: DNSState = field(default_factory=DNSState)

    public_ip: str | None = None
    exit_country: str | None = None
    session_started: datetime = field(default_factory=datetime.now)
    verification_passed: bool = False
    verification_summary: str = ""
    last_error: str | None = None
    privileged: bool = False

    @property
    def uptime_seconds(self) -> int:
        return max(0, int((datetime.now() - self.session_started).total_seconds()))

    @property
    def connected(self) -> bool:
        return self.route_status is ServiceStatus.ACTIVE


class StateManager:
    """Owns the single :class:`AppState` instance and announces every change."""

    def __init__(self, bus: EventBus, *, routing_mode: RoutingMode | None = None) -> None:
        self._bus = bus
        self._state = AppState(
            routing_mode=routing_mode or RoutingMode.TOR_ONLY,
        )
        self._watchers: list[Callable[[AppState], None]] = []

    # -- access ------------------------------------------------------------- #

    @property
    def state(self) -> AppState:
        return self._state

    def watch(self, callback: Callable[[AppState], None]) -> None:
        """Register a direct state watcher (used by Textual widgets)."""
        self._watchers.append(callback)

    def unwatch(self, callback: Callable[[AppState], None]) -> None:
        if callback in self._watchers:
            self._watchers.remove(callback)

    # -- mutation ----------------------------------------------------------- #

    def update(self, **changes: Any) -> AppState:
        """Replace top-level fields of the state."""
        self._state = replace(self._state, **changes)
        self._announce()
        return self._state

    def update_tor(self, **changes: Any) -> AppState:
        return self.update(tor=replace(self._state.tor, **changes))

    def update_vpn(self, **changes: Any) -> AppState:
        return self.update(vpn=replace(self._state.vpn, **changes))

    def update_dns(self, **changes: Any) -> AppState:
        return self.update(dns=replace(self._state.dns, **changes))

    def set_error(self, message: str) -> AppState:
        log.error("%s", message)
        return self.update(last_error=message)

    def clear_error(self) -> AppState:
        return self.update(last_error=None)

    def reset_session(self) -> AppState:
        self._state = replace(self._state, session_started=datetime.now())
        self._announce()
        return self._state

    # -- internals ---------------------------------------------------------- #

    def _announce(self) -> None:
        for watcher in tuple(self._watchers):
            try:
                watcher(self._state)
            except Exception:  # pragma: no cover - UI errors stay in the UI
                log.exception("State watcher failed")
        self._bus.publish(
            Event(type=EventType.STATE_CHANGED, data={"state": self._state})
        )
