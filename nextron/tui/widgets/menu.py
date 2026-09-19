"""The keyboard-driven navigation menu.

Grouped into sections, each entry showing a glyph, its key, its label and a
live state value on the right. Section headers are disabled list items, which
Textual skips during navigation, so ``^``/``v`` still moves only between real
actions.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.table import Table
from rich.text import Text
from textual.message import Message
from textual.widgets import ListItem, ListView, Static

from nextron.core import constants

__all__ = ["MenuEntry", "MenuGroup", "NavMenu"]


@dataclass(frozen=True, slots=True)
class MenuGroup:
    """A labelled section of the menu."""

    name: str


@dataclass(frozen=True, slots=True)
class MenuEntry:
    """One selectable action."""

    key: str
    label: str
    action: str
    glyph: str = "."
    group: str = ""
    #: Placeholder shown on the right until the dashboard supplies a value.
    state: str = ""


class NavMenu(ListView):
    """A compact, grouped menu; every entry also has a global key binding."""

    class Activated(Message):
        """Posted when the user activates an entry, by keyboard or by mouse.

        Deliberately *not* called ``Selected``: ``ListView`` posts its own
        ``self.Selected(list_view, item, index)`` from both ``action_select_cursor``
        and its click handler, so a nested class of that name would shadow it and
        be constructed with the wrong arguments.
        """

        def __init__(self, entry: MenuEntry) -> None:
            super().__init__()
            self.entry = entry

    def __init__(self, entries: list[MenuEntry], **kwargs) -> None:
        self._entries = entries
        self._cells: dict[str, Static] = {}
        self._states: dict[str, str] = {}
        super().__init__(*self._build_items(entries), **kwargs)

    def on_mount(self) -> None:
        self.border_title = "Menu"
        # Start on the first real action, never on a section header.
        self.index = next(
            (
                position
                for position, item in enumerate(self.children)
                if not item.disabled
            ),
            0,
        )

    # -- construction ------------------------------------------------------- #

    def _build_items(self, entries: list[MenuEntry]) -> list[ListItem]:
        items: list[ListItem] = []
        group: str | None = None
        for entry in entries:
            if entry.group and entry.group != group:
                group = entry.group
                items.append(self._header(group))
            cell = Static(self._render_entry(entry), markup=False)
            self._cells[entry.action] = cell
            item = ListItem(cell)
            item.data_entry = entry  # type: ignore[attr-defined]
            items.append(item)
        return items

    @staticmethod
    def _header(name: str) -> ListItem:
        text = Text(name.upper(), style=f"bold {constants.COLOR_SECONDARY}")
        header = ListItem(Static(text, markup=False), classes="menu-section")
        header.disabled = True  # ListView skips disabled items when navigating
        return header

    def _render_entry(self, entry: MenuEntry) -> Table:
        state = self._states.get(entry.action, entry.state)

        row = Table.grid(expand=True, padding=(0, 1))
        row.add_column(width=1, no_wrap=True)                   # glyph
        row.add_column(width=3, no_wrap=True)                   # key badge
        row.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        row.add_column(justify="right", no_wrap=True)           # live state

        row.add_row(
            Text(entry.glyph, style=constants.COLOR_ACCENT),
            Text(entry.key, style=f"bold {constants.COLOR_ACCENT}"),
            Text(entry.label, style=constants.COLOR_TEXT),
            Text(state, style=constants.COLOR_SURFACE),
        )
        return row

    # -- live state --------------------------------------------------------- #

    def set_states(self, states: dict[str, str]) -> None:
        """Update the right-hand value of each entry (cheap, no relayout)."""
        if states == self._states:
            return
        self._states = dict(states)
        for entry in self._entries:
            cell = self._cells.get(entry.action)
            if cell is not None:
                cell.update(self._render_entry(entry))

    # -- selection ---------------------------------------------------------- #

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Translate a ListView selection into a NEXTRON menu message.

        Fires for ``enter`` on the highlighted entry and for a mouse click on
        any entry, so both routes reach the dashboard identically.
        """
        event.stop()
        entry = self.entry_of(event.item)
        if entry is not None:
            self.post_message(self.Activated(entry))

    @staticmethod
    def entry_of(item: ListItem | None) -> MenuEntry | None:
        """The entry behind *item*, or ``None`` for a section header."""
        if item is None or item.disabled:
            return None
        entry = getattr(item, "data_entry", None)
        return entry if isinstance(entry, MenuEntry) else None
