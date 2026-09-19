"""State management and the event bus."""

from __future__ import annotations

import asyncio

from nextron.core import constants
from nextron.core.config import RoutingMode
from nextron.core.events import Event, EventType
from nextron.state.manager import AppState, ServiceStatus, StateManager
from nextron.utils import format as fmt


def test_sync_and_async_subscribers_both_receive_events(bus):
    async def main():
        received: list[tuple[str, str]] = []
        bus.subscribe(EventType.TOR_ROTATED, lambda e: received.append(("sync", e.message)))

        async def handler(event: Event) -> None:
            received.append(("async", event.get("exit_ip")))

        bus.subscribe(None, handler)
        bus.emit(EventType.TOR_ROTATED, "new identity", exit_ip="185.1.2.3")
        await bus.drain()
        return received

    assert asyncio.run(main()) == [("sync", "new identity"), ("async", "185.1.2.3")]


def test_a_broken_subscriber_does_not_stop_delivery(bus):
    delivered: list[str] = []

    def explode(event: Event) -> None:
        raise RuntimeError("subscriber is broken")

    bus.subscribe(EventType.ACTIVITY, explode)
    bus.subscribe(EventType.ACTIVITY, lambda e: delivered.append(e.message))
    bus.emit(EventType.ACTIVITY, "still delivered")
    assert delivered == ["still delivered"]


def test_unsubscribe_stops_delivery(bus):
    seen: list[str] = []

    def handler(event: Event) -> None:
        seen.append(event.message)

    bus.subscribe(EventType.ACTIVITY, handler)
    bus.emit(EventType.ACTIVITY, "first")
    bus.unsubscribe(EventType.ACTIVITY, handler)
    bus.emit(EventType.ACTIVITY, "second")
    assert seen == ["first"]


def test_state_updates_are_immutable_snapshots(bus):
    manager = StateManager(bus)
    first = manager.state
    manager.update_tor(status=ServiceStatus.ACTIVE, exit_ip="185.1.2.3")
    assert first.tor.exit_ip is None          # the old snapshot is untouched
    assert manager.state.tor.exit_ip == "185.1.2.3"


def test_state_watchers_are_notified(bus):
    manager = StateManager(bus)
    seen: list[AppState] = []
    manager.watch(seen.append)
    manager.update(routing_mode=RoutingMode.VPN_ONLY)
    assert seen and seen[-1].routing_mode is RoutingMode.VPN_ONLY

    manager.unwatch(seen.append)  # a different bound object: still fine
    manager.update_dns(blocked_domains=10)
    assert manager.state.dns.blocked_domains == 10


def test_state_change_publishes_one_event(bus):
    manager = StateManager(bus)
    events: list[Event] = []
    bus.subscribe(EventType.STATE_CHANGED, events.append)
    manager.update_vpn(status=ServiceStatus.ACTIVE)
    assert len(events) == 1
    assert events[0].data["state"] is manager.state


def test_service_status_presentation_uses_the_brand_palette():
    palette = set(constants.PALETTE.values())
    for status in ServiceStatus:
        assert status.color in palette
        assert status.label
        assert status.marker
    assert ServiceStatus.ACTIVE.label == "Connected"
    assert ServiceStatus.BOOTSTRAPPING.is_busy is True
    assert ServiceStatus.IDLE.is_busy is False


def test_dns_block_rate_handles_zero_queries(bus):
    manager = StateManager(bus)
    assert manager.state.dns.block_rate == 0.0
    manager.update_dns(queries_total=200, queries_blocked=50)
    assert manager.state.dns.block_rate == 25.0


def test_connected_requires_an_active_route(bus):
    manager = StateManager(bus)
    assert manager.state.connected is False
    manager.update(route_status=ServiceStatus.ACTIVE)
    assert manager.state.connected is True


def test_format_helpers():
    assert fmt.duration(0) == "0s"
    assert fmt.duration(65) == "1m 5s"
    assert fmt.duration(3725) == "1h 2m 5s"
    assert fmt.duration(90000) == "1d 1h 0m"
    assert fmt.duration(None) == "--"
    assert fmt.clock_duration(3725) == "01:02:05"
    assert fmt.clock_duration(None) == "--:--:--"
    assert fmt.countdown(95) == "01:35"
    assert fmt.countdown(None) == "--:--"
    assert fmt.truncate("abcdefghij", 6) == "abc..."
    assert fmt.truncate(None, 6) == "--"
    assert fmt.thousands(1234567) == "1,234,567"
    assert fmt.percentage(12.345) == "12.3%"
    assert fmt.relative(None) == "never"
