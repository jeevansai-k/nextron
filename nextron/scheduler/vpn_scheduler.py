"""The VPN shuffle scheduler -- completely independent of the Tor one."""

from __future__ import annotations

import logging

from nextron.core.context import EngineContext
from nextron.scheduler.base import IntervalScheduler
from nextron.vpn.manager import VPNEngine

log = logging.getLogger(__name__)

__all__ = ["VPNScheduler"]


class VPNScheduler(IntervalScheduler):
    """Switch VPN profiles on a 5s--5m timer using the shuffle algorithm."""

    name = "vpn_scheduler"

    def __init__(self, context: EngineContext, engine: VPNEngine) -> None:
        super().__init__(context, context.config.vpn.shuffle_interval)
        self._engine = engine

    @property
    def label(self) -> str:
        return "VPN scheduler"

    async def fire(self) -> bool:
        """Shuffle to the next profile in the pool."""
        if self._engine.shuffle.pool_size < 2:
            self._ctx.activity(
                "VPN shuffle needs at least two profiles in the pool",
                level="warning",
            )
            return False
        return await self._engine.switch(reason="scheduled") is not None

    def set_interval(self, seconds: int) -> int:
        clamped = super().set_interval(seconds)
        self._ctx.config.vpn.shuffle_interval = clamped
        self._ctx.state.update_vpn(shuffle_interval=clamped)
        self._ctx.save_config()
        return clamped

    def _publish_countdown(self) -> None:
        self._ctx.state.update_vpn(seconds_to_shuffle=self.remaining)

    def _on_state_change(self) -> None:
        self._ctx.config.vpn.shuffle_enabled = self.running
        self._ctx.state.update_vpn(
            shuffle_enabled=self.running,
            shuffle_algorithm=self._ctx.config.vpn.shuffle_algorithm,
        )
        self._ctx.save_config()
