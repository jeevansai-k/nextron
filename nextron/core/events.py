"""Asynchronous event bus.

Every engine is decoupled from the interface: engines publish events, the TUI
(or the CLI, or the storage layer) subscribes. Subscribers may be plain
callables or coroutine functions; coroutine subscribers are scheduled on the
running loop and never block the publisher.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["Event", "EventBus", "EventType"]


class EventType(str, Enum):
    """Everything an engine can announce."""

    # lifecycle
    APP_READY = "app.ready"
    APP_SHUTDOWN = "app.shutdown"

    # generic
    STATE_CHANGED = "state.changed"
    ACTIVITY = "activity"
    ERROR = "error"

    # tor
    TOR_STARTING = "tor.starting"
    TOR_BOOTSTRAP = "tor.bootstrap"
    TOR_READY = "tor.ready"
    TOR_ROTATED = "tor.rotated"
    TOR_CIRCUIT = "tor.circuit"
    TOR_STOPPED = "tor.stopped"

    # vpn
    VPN_CONNECTING = "vpn.connecting"
    VPN_CONNECTED = "vpn.connected"
    VPN_SWITCHED = "vpn.switched"
    VPN_DISCONNECTED = "vpn.disconnected"
    VPN_KILLSWITCH = "vpn.killswitch"

    # dns shield
    DNS_ENABLED = "dns.enabled"
    DNS_DISABLED = "dns.disabled"
    DNS_BLOCKED = "dns.blocked"
    DNS_RELOADED = "dns.reloaded"

    # routing / verification
    ROUTE_ESTABLISHING = "route.establishing"
    ROUTE_UP = "route.up"
    ROUTE_DOWN = "route.down"
    VERIFICATION_STARTED = "verification.started"
    VERIFICATION_STEP = "verification.step"
    VERIFICATION_PASSED = "verification.passed"
    VERIFICATION_FAILED = "verification.failed"

    # schedulers
    SCHEDULER_TICK = "scheduler.tick"
    SCHEDULER_STARTED = "scheduler.started"
    SCHEDULER_STOPPED = "scheduler.stopped"


@dataclass(frozen=True, slots=True)
class Event:
    """An immutable announcement flowing through the bus."""

    type: EventType
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    level: str = "info"

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


Subscriber = Callable[[Event], Awaitable[None] | None]


class EventBus:
    """Fan-out dispatcher with per-type and wildcard subscriptions."""

    def __init__(self) -> None:
        self._subscribers: dict[EventType | None, list[Subscriber]] = defaultdict(list)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._history: list[Event] = []
        self._history_limit = 500

    # -- subscription ------------------------------------------------------- #

    def subscribe(self, event_type: EventType | None, subscriber: Subscriber) -> None:
        """Subscribe to *event_type*, or to everything when it is ``None``."""
        self._subscribers[event_type].append(subscriber)

    def subscribe_many(
        self, event_types: list[EventType], subscriber: Subscriber
    ) -> None:
        for event_type in event_types:
            self.subscribe(event_type, subscriber)

    def unsubscribe(self, event_type: EventType | None, subscriber: Subscriber) -> None:
        if subscriber in self._subscribers[event_type]:
            self._subscribers[event_type].remove(subscriber)

    # -- publication -------------------------------------------------------- #

    def publish(self, event: Event) -> None:
        """Deliver *event* to every matching subscriber."""
        self._history.append(event)
        if len(self._history) > self._history_limit:
            del self._history[: len(self._history) - self._history_limit]

        for key in (event.type, None):
            for subscriber in tuple(self._subscribers.get(key, ())):
                self._dispatch(subscriber, event)

    def emit(
        self,
        event_type: EventType,
        message: str = "",
        *,
        level: str = "info",
        **data: Any,
    ) -> Event:
        """Build and publish an event in one call."""
        event = Event(type=event_type, message=message, level=level, data=data)
        self.publish(event)
        return event

    def _dispatch(self, subscriber: Subscriber, event: Event) -> None:
        try:
            result = subscriber(event)
        except Exception:  # pragma: no cover - a broken UI must not kill engines
            log.exception("Event subscriber raised for %s", event.type.value)
            return

        if inspect.isawaitable(result):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:  # no loop: drop the coroutine cleanly
                result.close()  # type: ignore[union-attr]
                return
            task = loop.create_task(self._await_safely(result))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _await_safely(awaitable: Awaitable[None]) -> None:
        try:
            await awaitable
        except asyncio.CancelledError:  # pragma: no cover
            raise
        except Exception:  # pragma: no cover
            log.exception("Async event subscriber failed")

    # -- introspection ------------------------------------------------------ #

    def history(self, limit: int = 100) -> tuple[Event, ...]:
        return tuple(self._history[-limit:])

    async def drain(self) -> None:
        """Await every in-flight async subscriber (used during shutdown)."""
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
