"""The independent rotation schedulers."""

from __future__ import annotations

import asyncio

import pytest

from nextron.core import constants
from nextron.scheduler.base import IntervalScheduler


class RecordingScheduler(IntervalScheduler):
    """A scheduler whose action just records when it fired."""

    name = "recording"

    def __init__(self, context, interval: int, *, succeed: bool = True) -> None:
        super().__init__(context, interval)
        self.fired = 0
        self.countdowns: list[int | None] = []
        self._succeed = succeed

    async def fire(self) -> bool:
        self.fired += 1
        return self._succeed

    def _publish_countdown(self) -> None:
        self.countdowns.append(self.remaining)


@pytest.mark.parametrize(
    "given,expected",
    [(1, constants.ROTATION_MIN_SECONDS), (42, 42), (9999, constants.ROTATION_MAX_SECONDS)],
)
def test_interval_is_clamped_to_the_specified_window(context, given, expected):
    assert RecordingScheduler(context, given).interval == expected


async def test_manual_trigger_fires_immediately(context):
    scheduler = RecordingScheduler(context, 300)
    await scheduler.start()
    try:
        scheduler.trigger()
        await asyncio.sleep(0.3)
        assert scheduler.fired == 1
        assert scheduler.last_fire is not None
    finally:
        await scheduler.stop()


async def test_countdown_decreases_while_running(context):
    scheduler = RecordingScheduler(context, 10)
    await scheduler.start()
    try:
        await asyncio.sleep(2.2)
        assert scheduler.remaining is not None
        assert scheduler.remaining < 10
    finally:
        await scheduler.stop()


async def test_pause_freezes_the_countdown(context):
    scheduler = RecordingScheduler(context, 30)
    await scheduler.start()
    try:
        await asyncio.sleep(1.1)
        frozen = scheduler.remaining
        scheduler.pause()
        assert scheduler.remaining is None       # nothing is counting down
        await asyncio.sleep(1.1)
        scheduler.resume()
        assert scheduler.remaining == frozen
    finally:
        await scheduler.stop()


async def test_fires_on_the_interval(context):
    scheduler = RecordingScheduler(context, 5)
    await scheduler.start()
    try:
        await asyncio.sleep(5.6)
        assert scheduler.fired >= 1
    finally:
        await scheduler.stop()


async def test_stop_clears_the_countdown(context):
    scheduler = RecordingScheduler(context, 20)
    await scheduler.start()
    await scheduler.stop()
    assert not scheduler.running
    assert scheduler.remaining is None


async def test_toggle_starts_and_stops(context):
    scheduler = RecordingScheduler(context, 20)
    assert await scheduler.toggle() is True
    assert await scheduler.toggle() is False


async def test_a_failing_action_does_not_kill_the_scheduler(context):
    scheduler = RecordingScheduler(context, 5, succeed=False)
    await scheduler.start()
    try:
        scheduler.trigger()
        await asyncio.sleep(0.4)
        assert scheduler.fired == 1
        assert scheduler.running
    finally:
        await scheduler.stop()


async def test_tor_and_vpn_schedulers_are_independent(context, profiles):
    """Two schedulers, two intervals, two countdowns -- as specified."""
    from nextron.scheduler.tor_scheduler import TorScheduler
    from nextron.scheduler.vpn_scheduler import VPNScheduler
    from nextron.tor.engine import TorEngine
    from nextron.vpn.manager import VPNEngine

    tor = TorScheduler(context, TorEngine(context))
    vpn = VPNScheduler(context, VPNEngine(context))

    tor.set_interval(30)
    vpn.set_interval(120)
    assert tor.interval == 30
    assert vpn.interval == 120
    assert context.config.tor.rotation_interval == 30
    assert context.config.vpn.shuffle_interval == 120

    await tor.start()
    try:
        assert tor.running and not vpn.running
        assert context.state.state.tor.seconds_to_rotation is not None
        assert context.state.state.vpn.seconds_to_shuffle is None
    finally:
        await tor.stop()


async def test_scheduler_interval_changes_persist(context):
    from nextron.core.config import ConfigManager
    from nextron.scheduler.tor_scheduler import TorScheduler
    from nextron.tor.engine import TorEngine

    scheduler = TorScheduler(context, TorEngine(context))
    scheduler.set_interval(77)
    assert ConfigManager().load().tor.rotation_interval == 77
