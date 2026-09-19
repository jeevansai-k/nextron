"""Shared machinery for NEXTRON's independent rotation schedulers.

Both schedulers are ordinary asyncio tasks that tick once per second so the
dashboard countdown stays accurate, fire their action when the interval
elapses, and survive a failing action with bounded retries. Intervals are
always clamped to the specification's 5s--5m window.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from datetime import datetime

from nextron.core import constants
from nextron.core.context import EngineContext
from nextron.core.events import EventType

log = logging.getLogger(__name__)

__all__ = ["IntervalScheduler"]

_TICK = 1.0


class IntervalScheduler(ABC):
    """A pausable, retrying interval runner with a live countdown."""

    #: Human readable name used in logs and events.
    name: str = "scheduler"

    def __init__(self, context: EngineContext, interval: int) -> None:
        self._ctx = context
        self._interval = self._clamp(interval)
        self._task: asyncio.Task[None] | None = None
        self._remaining = self._interval
        self._paused = False
        self._trigger = asyncio.Event()
        self._fires = 0
        self._failures = 0
        self._last_fire: datetime | None = None

    # -- interval ----------------------------------------------------------- #

    @staticmethod
    def _clamp(seconds: int) -> int:
        return max(
            constants.ROTATION_MIN_SECONDS,
            min(constants.ROTATION_MAX_SECONDS, int(seconds)),
        )

    @property
    def interval(self) -> int:
        return self._interval

    @property
    def remaining(self) -> int | None:
        if not self.running or self._paused:
            return None
        return self._remaining

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def fires(self) -> int:
        return self._fires

    @property
    def last_fire(self) -> datetime | None:
        return self._last_fire

    def set_interval(self, seconds: int) -> int:
        """Change the interval; the current countdown is restarted."""
        clamped = self._clamp(seconds)
        if clamped != seconds:
            log.debug(
                "%s interval %ss clamped to %ss (allowed range %s-%ss)",
                self.name,
                seconds,
                clamped,
                constants.ROTATION_MIN_SECONDS,
                constants.ROTATION_MAX_SECONDS,
            )
        self._interval = clamped
        self._remaining = clamped
        self._publish_countdown()
        return clamped

    # -- lifecycle ---------------------------------------------------------- #

    async def start(self) -> None:
        """Start ticking (idempotent)."""
        if self.running:
            return
        self._remaining = self._interval
        self._paused = False
        self._task = asyncio.create_task(self._loop(), name=f"{self.name}-loop")
        # Publish immediately so the dashboard shows a full countdown right
        # away instead of "--:--" until the first tick.
        self._publish_countdown()
        self._ctx.bus.emit(
            EventType.SCHEDULER_STARTED,
            f"{self.label} started ({self._interval}s interval)",
            scheduler=self.name,
            interval=self._interval,
        )
        self._on_state_change()

    async def stop(self) -> None:
        """Stop ticking and clear the countdown."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._remaining = self._interval
        self._publish_countdown()
        self._on_state_change()
        self._ctx.bus.emit(
            EventType.SCHEDULER_STOPPED, f"{self.label} stopped", scheduler=self.name
        )

    async def toggle(self) -> bool:
        """Start or stop. Returns the new running state."""
        if self.running:
            await self.stop()
        else:
            await self.start()
        return self.running

    def pause(self) -> None:
        """Freeze the countdown without tearing the task down."""
        if self.running and not self._paused:
            self._paused = True
            self._publish_countdown()

    def resume(self) -> None:
        if self._paused:
            self._paused = False
            self._publish_countdown()

    def trigger(self) -> None:
        """Fire immediately, then restart the countdown."""
        self._trigger.set()

    # -- loop --------------------------------------------------------------- #

    async def _loop(self) -> None:
        try:
            while True:
                fired = await self._countdown()
                if not fired:
                    continue
                await self._run_action()
                self._remaining = self._interval
                self._publish_countdown()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - a scheduler must never die quietly
            log.exception("%s crashed", self.name)
            self._ctx.failure(f"{self.label} stopped unexpectedly")

    async def _countdown(self) -> bool:
        """Tick down one interval. Returns ``True`` when it is time to fire."""
        while self._remaining > 0:
            try:
                await asyncio.wait_for(self._trigger.wait(), timeout=_TICK)
            except TimeoutError:
                if not self._paused:
                    self._remaining -= 1
                    self._publish_countdown()
                continue
            # Manual trigger
            self._trigger.clear()
            return True
        return True

    async def _run_action(self) -> None:
        self._fires += 1
        self._last_fire = datetime.now()
        self._ctx.bus.emit(
            EventType.SCHEDULER_TICK,
            f"{self.label} firing (#{self._fires})",
            scheduler=self.name,
            count=self._fires,
        )
        try:
            success = await self.fire()
        except Exception as exc:
            log.exception("%s action failed", self.name)
            success = False
            self._ctx.failure(f"{self.label} action failed: {exc}")

        if success:
            self._failures = 0
            return

        self._failures += 1
        backoff = min(self._interval, 5 * self._failures)
        log.warning(
            "%s action failed (%d consecutive); next attempt in %ss",
            self.name,
            self._failures,
            backoff,
        )
        await asyncio.sleep(backoff)

    # -- subclass API ------------------------------------------------------- #

    @property
    def label(self) -> str:
        return self.name.replace("_", " ").title()

    @abstractmethod
    async def fire(self) -> bool:
        """Perform the scheduled action. Return ``True`` on success."""

    @abstractmethod
    def _publish_countdown(self) -> None:
        """Push the remaining seconds into the application state."""

    def _on_state_change(self) -> None:  # noqa: B027 - deliberately concrete
        """Mirror running/paused flags into the application state.

        Concrete and empty on purpose: a scheduler with no flags to publish
        should not have to implement this, so it must not be abstract.
        """
