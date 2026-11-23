"""Small render helpers shared by every NEXTRON screen.

They exist so that "connected", "off", a key hint or a routing chain look the
same everywhere, instead of each screen picking its own colours.
"""

from __future__ import annotations

from rich.text import Text

from nextron.core import constants as c
from nextron.core.config import RoutingMode
from nextron.state.manager import AppState, ServiceStatus

__all__ = [
    "bar",
    "chain",
    "key_hint",
    "protection",
    "switch",
]


def switch(on: bool, *, on_label: str = "on", off_label: str = "off") -> Text:
    """A boolean drawn as a switch: ``◉ on`` in green, ``◌ off`` faint."""
    if on:
        return Text(f"◉ {on_label}", style=f"bold {c.SIGNAL_OK}")
    return Text(f"◌ {off_label}", style=c.INK_FAINT)


def key_hint(*pairs: tuple[str, str]) -> Text:
    """``C connect ∙ M mode`` -- keys in the accent, words muted."""
    text = Text(no_wrap=True, overflow="ellipsis")
    for index, (key, label) in enumerate(pairs):
        if index:
            text.append("  ∙  ", style=c.INK_FAINT)
        text.append(key, style=f"bold {c.ACCENT}")
        text.append(f" {label}", style=c.INK_MUTED)
    return text


def bar(percent: int, width: int = 24) -> Text:
    """A thin progress bar, e.g. Tor's bootstrap."""
    percent = max(0, min(100, int(percent)))
    filled = round(width * percent / 100)
    text = Text(no_wrap=True)
    text.append("▰" * filled, style=c.SIGNAL_BUSY)
    text.append("▱" * (width - filled), style=c.INK_LINE)
    text.append(f" {percent:>3}%", style=f"bold {c.INK_TEXT}")
    return text


def _hop_status(hop: str, state: AppState) -> ServiceStatus | None:
    if hop == "tor":
        return state.tor.status
    if hop == "vpn":
        return state.vpn.status
    return None


def chain(mode: RoutingMode, state: AppState | None = None) -> Text:
    """``you ⟶ VPN ⟶ Tor ⟶ internet``, each hop coloured by its engine.

    With no state the chain is drawn neutrally (for the mode picker).
    """
    hops = [part.strip() for part in mode.chain.split("->")]
    live = state is not None and state.route_status is ServiceStatus.ACTIVE
    text = Text(no_wrap=True, overflow="ellipsis")
    for index, hop in enumerate(hops):
        key = hop.lower()
        if index:
            text.append(" ⟶ ", style=c.SIGNAL_OK if live else c.INK_FAINT)
        if key in {"you", "internet"}:
            style = c.INK_MUTED
            if key == "internet" and live:
                style = c.SIGNAL_OK
            text.append(key, style=style)
            continue
        status = _hop_status(key, state) if state is not None else None
        colour = status.color if status is not None else c.ACCENT
        if status in (ServiceStatus.IDLE, ServiceStatus.DISABLED):
            colour = c.INK_TEXT
        text.append(hop, style=f"bold {colour}")
    return text


def protection(
    state: AppState, *, socks_only: bool = False
) -> tuple[str, str, str]:
    """``(glyph, word, colour)`` -- the one-glance answer to "am I protected?".

    *socks_only* is true when Tor is up but nothing redirects the system into
    it: only applications pointed at the proxy are covered, so the honest word
    is "proxy only", never "protected".
    """
    status = state.route_status
    if status is ServiceStatus.ACTIVE:
        if not state.verification_passed:
            return "◉", "UNVERIFIED", c.SIGNAL_WARN
        if socks_only:
            return "◉", "PROXY ONLY", c.SIGNAL_WARN
        return "◉", "PROTECTED", c.SIGNAL_OK
    if status.is_busy:
        word = "DISCONNECTING" if status is ServiceStatus.STOPPING else "CONNECTING"
        return "◌", word, c.SIGNAL_BUSY
    if status is ServiceStatus.ERROR:
        return "✗", "FAILED", c.SIGNAL_ERR
    return "◌", "EXPOSED", c.SIGNAL_WARN
