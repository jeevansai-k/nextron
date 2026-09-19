"""The Tor rotation scheduler -- completely independent of the VPN one."""

from __future__ import annotations

import logging

from nextron.core.context import EngineContext
from nextron.scheduler.base import IntervalScheduler
from nextron.tor.engine import TorEngine

log = logging.getLogger(__name__)

__all__ = ["TorScheduler"]


class TorScheduler(IntervalScheduler):
    """Fire ``NEWNYM`` on a 5s--5m timer, verifying the exit each time."""

    name = "tor_scheduler"

    def __init__(self, context: EngineContext, engine: TorEngine) -> None:
        super().__init__(context, context.config.tor.rotation_interval)
        self._engine = engine

    @property
    def label(self) -> str:
        return "Tor scheduler"

    async def fire(self) -> bool:
        """Rotate the Tor identity."""
        if not self._engine.running:
            log.debug("Tor is not running; skipping scheduled rotation")
            return False
        return await self._engine.rotate(reason="scheduled")

    def set_interval(self, seconds: int) -> int:
        clamped = super().set_interval(seconds)
        self._ctx.config.tor.rotation_interval = clamped
        self._ctx.state.update_tor(rotation_interval=clamped)
        self._ctx.save_config()
        return clamped

    def _publish_countdown(self) -> None:
        self._ctx.state.update_tor(seconds_to_rotation=self.remaining)

    def _on_state_change(self) -> None:
        self._ctx.config.tor.rotation_enabled = self.running
        self._ctx.state.update_tor(rotation_enabled=self.running)
        self._ctx.save_config()
