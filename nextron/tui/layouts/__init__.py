"""Screen layouts.

Textual expresses layout in containers and the stylesheet; these modules hold
the *structure* that both the composition and the refresh path need to agree
on -- panel ids, titles and which column each panel belongs to.
"""

from nextron.tui.layouts.dashboard import (
    MENU_COLUMN,
    PANELS,
    Column,
    PanelSpec,
    panels_in,
)

__all__ = ["MENU_COLUMN", "PANELS", "Column", "PanelSpec", "panels_in"]
