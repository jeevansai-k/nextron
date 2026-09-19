"""Small presentation helpers shared by the TUI and the CLI."""

from __future__ import annotations

from datetime import datetime

__all__ = [
    "boolean",
    "clock_duration",
    "countdown",
    "duration",
    "percentage",
    "relative",
    "thousands",
    "truncate",
]


def duration(seconds: int | float | None) -> str:
    """``3725`` -> ``"1h 2m 5s"``; ``None`` -> ``"--"``."""
    if seconds is None:
        return "--"
    total = int(max(0, seconds))
    days, rem = divmod(total, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes, secs = divmod(rem, 60)

    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def clock_duration(seconds: int | float | None) -> str:
    """``3725`` -> ``"01:02:05"`` -- fixed width, good for a dashboard cell."""
    if seconds is None:
        return "--:--:--"
    total = int(max(0, seconds))
    hours, rem = divmod(total, 3_600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def countdown(seconds: int | None) -> str:
    """``95`` -> ``"01:35"``; ``None`` -> ``"--:--"`` (rotation disabled)."""
    if seconds is None:
        return "--:--"
    total = int(max(0, seconds))
    minutes, secs = divmod(total, 60)
    return f"{minutes:02d}:{secs:02d}"


def relative(moment: datetime | None) -> str:
    """``datetime`` -> ``"12s ago"``; ``None`` -> ``"never"``."""
    if moment is None:
        return "never"
    delta = (datetime.now() - moment).total_seconds()
    if delta < 1:
        return "just now"
    return f"{duration(delta)} ago"


def truncate(text: str | None, width: int, *, placeholder: str = "...") -> str:
    """Shorten *text* to *width* characters, appending *placeholder*."""
    if not text:
        return "--"
    if len(text) <= width:
        return text
    if width <= len(placeholder):
        return text[:width]
    return text[: width - len(placeholder)] + placeholder


def boolean(value: bool, *, yes: str = "Yes", no: str = "No") -> str:
    return yes if value else no


def thousands(value: int | None) -> str:
    """``1234567`` -> ``"1,234,567"``."""
    if value is None:
        return "--"
    return f"{value:,}"


def percentage(value: float | None, *, digits: int = 1) -> str:
    if value is None:
        return "--"
    return f"{value:.{digits}f}%"
