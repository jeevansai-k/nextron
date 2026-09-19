"""The exclusive NEXTRON colour hierarchy, expressed as a Textual theme.

Only the five brand colours are used as *hues*::

    Primary    #4B0082
    Secondary  #5D3D94
    Accent     #9B59B6
    Surface    #C8A2C8
    Text       #E6E6FA

A terminal still needs something to paint behind the text, so the background
and panel colours are **darkened derivations of Primary and Secondary** -- the
same hue, a lower luminance -- rather than new colours introduced into the
palette.
"""

from __future__ import annotations

from textual.theme import Theme

from nextron.core import constants

__all__ = ["DERIVED", "NEXTRON_THEME"]


def _darken(hex_color: str, factor: float) -> str:
    """Scale a hex colour towards black, preserving its hue."""
    raw = hex_color.lstrip("#")
    channels = (int(raw[index : index + 2], 16) for index in (0, 2, 4))
    scaled = (max(0, min(255, int(channel * factor))) for channel in channels)
    return "#{:02x}{:02x}{:02x}".format(*scaled)


#: Derivations used for large surfaces, documented so they are auditable.
DERIVED: dict[str, str] = {
    # Background: Primary at 22% luminance.
    "background": _darken(constants.COLOR_PRIMARY, 0.22),
    # Panels: Secondary at 34% luminance.
    "panel": _darken(constants.COLOR_SECONDARY, 0.34),
    # Raised surfaces: Secondary at 52% luminance.
    "surface": _darken(constants.COLOR_SECONDARY, 0.52),
    # Borders and muted text: Surface at 62% luminance.
    "muted": _darken(constants.COLOR_SURFACE, 0.62),
}


NEXTRON_THEME = Theme(
    name="nextron",
    primary=constants.COLOR_PRIMARY,
    secondary=constants.COLOR_SECONDARY,
    accent=constants.COLOR_ACCENT,
    foreground=constants.COLOR_TEXT,
    background=DERIVED["background"],
    surface=DERIVED["surface"],
    panel=DERIVED["panel"],
    # The palette is deliberately monochromatic: state is communicated through
    # the accent/surface/primary hierarchy rather than by adding new hues.
    success=constants.COLOR_ACCENT,
    warning=constants.COLOR_SURFACE,
    error=constants.COLOR_PRIMARY,
    dark=True,
    variables={
        "nextron-primary": constants.COLOR_PRIMARY,
        "nextron-secondary": constants.COLOR_SECONDARY,
        "nextron-accent": constants.COLOR_ACCENT,
        "nextron-surface": constants.COLOR_SURFACE,
        "nextron-text": constants.COLOR_TEXT,
        "nextron-muted": DERIVED["muted"],
        "nextron-panel": DERIVED["panel"],
        "border": DERIVED["muted"],
        "border-blurred": DERIVED["panel"],
        # Selection highlights are dark: the row reads as recessed rather
        # than flood-filled, which keeps long lists calm to scroll through.
        "block-cursor-background": constants.COLOR_PRIMARY,
        "block-cursor-foreground": constants.COLOR_TEXT,
        "block-cursor-text-style": "bold",
        "block-cursor-blurred-background": DERIVED["surface"],
        "block-cursor-blurred-foreground": constants.COLOR_TEXT,
        "block-cursor-blurred-text-style": "none",
        "block-hover-background": DERIVED["surface"],
        "footer-key-foreground": constants.COLOR_ACCENT,
        "footer-description-foreground": constants.COLOR_TEXT,
        "input-selection-background": f"{constants.COLOR_ACCENT} 35%",
        "scrollbar": DERIVED["panel"],
        "scrollbar-hover": constants.COLOR_SECONDARY,
        "scrollbar-active": constants.COLOR_ACCENT,
    },
)
