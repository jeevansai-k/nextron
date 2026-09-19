"""The shared runtime context handed to every engine.

Instead of threading four collaborators through a dozen constructors, each
engine receives one :class:`EngineContext`: configuration, the event bus, the
state manager and the statistics database.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from nextron.core.config import ConfigManager, NextronConfig
from nextron.core.events import Event, EventBus, EventType
from nextron.state.manager import StateManager
from nextron.storage.database import Database

log = logging.getLogger(__name__)

__all__ = ["EngineContext"]


@dataclass(slots=True)
class EngineContext:
    """Collaborators every engine needs."""

    config_manager: ConfigManager
    bus: EventBus
    state: StateManager
    database: Database

    @property
    def config(self) -> NextronConfig:
        return self.config_manager.config

    def save_config(self) -> None:
        self.config_manager.save()

    # -- convenience announcements ------------------------------------------ #

    def activity(self, message: str, *, level: str = "info", **data) -> Event:
        """Log *message* and mirror it into the activity feed."""
        getattr(log, level if level != "success" else "info")("%s", message)
        return self.bus.emit(EventType.ACTIVITY, message, level=level, **data)

    def failure(self, message: str, **data) -> Event:
        """Record a failure in the log, the state and the activity feed."""
        log.error("%s", message)
        self.state.set_error(message)
        return self.bus.emit(EventType.ERROR, message, level="error", **data)

    async def persist(
        self, event_type: str, message: str = "", *, level: str = "info", **payload
    ) -> None:
        """Best-effort write into the statistics database."""
        if not self.database.connected:
            return
        try:
            await self.database.record_event(
                event_type, message, level=level, payload=payload or None
            )
        except Exception as exc:  # pragma: no cover - storage must never block
            log.debug("Event persistence skipped: %s", exc)
