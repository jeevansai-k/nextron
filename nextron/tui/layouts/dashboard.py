"""The dashboard layout, expressed as data.

Keeping the panel registry here means the widget ids used when composing the
screen and the ids used when refreshing it come from one place, so a renamed
panel cannot silently stop updating.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = ["MENU_COLUMN", "PANELS", "Column", "PanelSpec", "panels_in"]


class Column(str, Enum):
    """The three dashboard columns."""

    LEFT = "dashboard-left"
    MIDDLE = "dashboard-mid"
    RIGHT = "dashboard-right"


@dataclass(frozen=True, slots=True)
class PanelSpec:
    """One dashboard panel: where it lives, what it is called, what draws it."""

    key: str
    title: str
    column: Column
    #: Name of the ``DashboardScreen`` method that redraws this panel.
    renderer: str

    @property
    def id(self) -> str:
        return f"panel-{self.key}"

    @property
    def selector(self) -> str:
        return f"#{self.id}"


#: The navigation menu sits above the panels in the left column.
MENU_COLUMN = Column.LEFT

PANELS: tuple[PanelSpec, ...] = (
    PanelSpec("routing", "Routing", Column.LEFT, "_render_routing"),
    PanelSpec("session", "Session", Column.LEFT, "_render_session"),
    PanelSpec("countdown", "Rotation Schedulers", Column.MIDDLE, "_render_countdowns"),
    PanelSpec("tor", "Tor Engine", Column.MIDDLE, "_render_tor"),
    PanelSpec("vpn", "VPN Engine", Column.MIDDLE, "_render_vpn"),
    PanelSpec("dns", "DNS Shield", Column.MIDDLE, "_render_dns"),
    PanelSpec("activity", "Activity", Column.RIGHT, "_render_activity"),
)


def panels_in(column: Column) -> tuple[PanelSpec, ...]:
    """Every panel assigned to *column*, in display order."""
    return tuple(panel for panel in PANELS if panel.column is column)
