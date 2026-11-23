"""The NEXTRON "Signal" theme for Textual.

Design rules, in order of importance:

1. **Colour carries meaning.** Green means carrying traffic, amber means look
   at this, red means failed, blue means working on it. Nothing decorative is
   ever painted in a signal colour, so the eye can trust them.
2. **One accent.** The brand violet marks focus, keys and selection -- the
   places your attention should go next -- and nothing else.
3. **Quiet surfaces.** Backgrounds and borders are near-neutral with a faint
   violet tint, so a panel's border lights up only when it has focus.

The values live in :mod:`nextron.core.constants` (``INK_*``, ``ACCENT``,
``SIGNAL_*``) so the CLI, the doctor report and the TUI share one source.
"""

from __future__ import annotations

from textual.theme import Theme

from nextron.core import constants as c

__all__ = ["DERIVED", "NEXTRON_THEME"]


#: Kept for compatibility with code that inspects the old derived surfaces.
DERIVED: dict[str, str] = {
    "background": c.INK_BG,
    "panel": c.INK_PANEL,
    "surface": c.INK_RAISED,
    "muted": c.INK_MUTED,
}


NEXTRON_THEME = Theme(
    name="nextron",
    primary=c.ACCENT_DEEP,
    secondary=c.COLOR_SECONDARY,
    accent=c.ACCENT,
    foreground=c.INK_TEXT,
    background=c.INK_BG,
    surface=c.INK_RAISED,
    panel=c.INK_PANEL,
    success=c.SIGNAL_OK,
    warning=c.SIGNAL_WARN,
    error=c.SIGNAL_ERR,
    boost=c.INK_RAISED,
    dark=True,
    variables={
        "border": c.ACCENT_DEEP,
        "border-blurred": c.INK_LINE,
        # Selection: a violet-tinted recess plus the accent bar, never a
        # flood of saturated colour.
        "block-cursor-background": c.INK_SELECT,
        "block-cursor-foreground": c.INK_TEXT,
        "block-cursor-text-style": "bold",
        "block-cursor-blurred-background": c.INK_RAISED,
        "block-cursor-blurred-foreground": c.INK_TEXT,
        "block-cursor-blurred-text-style": "none",
        "block-hover-background": c.INK_RAISED,
        "footer-background": c.INK_PANEL,
        "footer-key-foreground": c.ACCENT,
        "footer-key-background": c.INK_PANEL,
        "footer-description-foreground": c.INK_MUTED,
        "footer-item-background": c.INK_PANEL,
        "input-cursor-background": c.ACCENT,
        "input-selection-background": f"{c.ACCENT_DEEP} 35%",
        "scrollbar": c.INK_LINE,
        "scrollbar-hover": c.INK_FAINT,
        "scrollbar-active": c.ACCENT_DEEP,
        "scrollbar-background": c.INK_PANEL,
        "scrollbar-corner-color": c.INK_PANEL,
    },
)
