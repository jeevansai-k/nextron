"""Structured logging for long running NEXTRON sessions.

Two sinks are configured:

* a rotating file handler at ``~/.config/nextron/logs/nextron.log`` which keeps
  a structured, timestamped record suitable for multi-day sessions;
* an in-memory ring buffer that the TUI activity log and the Logs screen read
  from, so the interface never has to touch the filesystem to redraw.
"""

from __future__ import annotations

import logging
import logging.handlers
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime

from nextron.storage import paths

__all__ = ["LogRecordEntry", "RingBufferHandler", "get_ring_buffer", "setup_logging"]

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-28s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_MAX_RING_ENTRIES = 2000


@dataclass(frozen=True, slots=True)
class LogRecordEntry:
    """A single rendered log line held in the ring buffer."""

    timestamp: datetime
    level: str
    source: str
    message: str

    @property
    def clock(self) -> str:
        return self.timestamp.strftime("%H:%M:%S")

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"{self.clock} [{self.level}] {self.source}: {self.message}"


class RingBufferHandler(logging.Handler):
    """Keep the most recent records in memory and notify subscribers."""

    def __init__(self, capacity: int = _MAX_RING_ENTRIES) -> None:
        super().__init__()
        self._entries: deque[LogRecordEntry] = deque(maxlen=capacity)
        self._subscribers: list[Callable[[LogRecordEntry], None]] = []

    # -- logging.Handler ---------------------------------------------------- #

    def emit(self, record: logging.LogRecord) -> None:
        entry = LogRecordEntry(
            timestamp=datetime.fromtimestamp(record.created),
            level=record.levelname,
            source=record.name.replace("nextron.", ""),
            message=record.getMessage(),
        )
        self._entries.append(entry)
        for callback in tuple(self._subscribers):
            try:
                callback(entry)
            except Exception:  # pragma: no cover - a bad subscriber must not
                continue      # take the logging pipeline down with it

    # -- public API --------------------------------------------------------- #

    def entries(self) -> tuple[LogRecordEntry, ...]:
        return tuple(self._entries)

    def tail(self, count: int) -> tuple[LogRecordEntry, ...]:
        if count <= 0:
            return ()
        return tuple(self._entries)[-count:]

    def filtered(self, levels: Iterable[str]) -> tuple[LogRecordEntry, ...]:
        wanted = {level.upper() for level in levels}
        return tuple(e for e in self._entries if e.level in wanted)

    def subscribe(self, callback: Callable[[LogRecordEntry], None]) -> None:
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[LogRecordEntry], None]) -> None:
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def clear(self) -> None:
        self._entries.clear()


_ring_buffer = RingBufferHandler()
_configured = False


def get_ring_buffer() -> RingBufferHandler:
    """Return the process wide in-memory log buffer."""
    return _ring_buffer


def setup_logging(level: str = "INFO", *, console: bool = False) -> RingBufferHandler:
    """Configure the ``nextron`` logger tree exactly once.

    ``console`` is only enabled for CLI sub-commands -- the TUI must never let
    a stray handler write to stdout because that corrupts the Textual screen.
    """
    global _configured

    paths.ensure_layout()
    root = logging.getLogger("nextron")
    numeric = getattr(logging, level.upper(), logging.INFO)
    root.setLevel(numeric)

    if _configured:
        for handler in root.handlers:
            handler.setLevel(numeric)
        return _ring_buffer

    root.propagate = False
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    file_handler = logging.handlers.RotatingFileHandler(
        paths.logs_dir() / "nextron.log",
        maxBytes=4 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    _ring_buffer.setFormatter(formatter)
    root.addHandler(_ring_buffer)

    if console:
        from rich.logging import RichHandler

        rich_handler = RichHandler(rich_tracebacks=True, show_path=False)
        rich_handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(rich_handler)

    # Third party libraries are noisy; keep them out of the activity log.
    for noisy in ("stem", "httpx", "httpcore", "asyncio", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True
    root.debug("Logging initialised at level %s", level.upper())
    return _ring_buffer
