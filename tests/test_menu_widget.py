"""Unit tests for the dashboard navigation menu.

The menu is a ``ListView`` subclass, so it inherits framework messages. A
nested class that reuses one of those names silently replaces it and is then
constructed with the framework's arguments -- which is how ``enter`` and a
mouse click both used to raise ``TypeError``. These tests pin that down.
"""

from __future__ import annotations

import inspect

from textual.message import Message
from textual.widgets import ListItem, ListView, Static

from nextron.tui.screens.dashboard import MENU_ENTRIES
from nextron.tui.widgets.menu import MenuEntry, NavMenu


def _message_names(widget_class: type) -> set[str]:
    return {
        name
        for name, member in vars(widget_class).items()
        if inspect.isclass(member) and issubclass(member, Message)
    }


def test_nav_menu_never_shadows_a_listview_message():
    """Its own messages must not collide with the ones ListView posts."""
    assert not _message_names(NavMenu) & _message_names(ListView)


def test_listview_selected_still_reaches_nav_menu_unchanged():
    """ListView.Selected must resolve through NavMenu with its own signature."""
    assert NavMenu.Selected is ListView.Selected
    parameters = list(
        inspect.signature(NavMenu.Selected.__init__).parameters
    )
    assert parameters == ["self", "list_view", "item", "index"]


def test_activated_carries_the_entry():
    entry = MenuEntry("Z", "Zero", "zero")
    assert NavMenu.Activated(entry).entry is entry


def test_entry_of_ignores_section_headers_and_foreign_items():
    header = ListItem(Static("SESSION"))
    header.disabled = True
    assert NavMenu.entry_of(header) is None
    assert NavMenu.entry_of(None) is None
    assert NavMenu.entry_of(ListItem(Static("stray"))) is None

    entry = MenuEntry("Z", "Zero", "zero")
    item = ListItem(Static("Zero"))
    item.data_entry = entry
    assert NavMenu.entry_of(item) is entry


def test_every_menu_entry_has_a_unique_key_and_action():
    keys = [entry.key for entry in MENU_ENTRIES]
    actions = [entry.action for entry in MENU_ENTRIES]
    assert len(set(keys)) == len(keys)
    assert len(set(actions)) == len(actions)


def test_clicking_an_entry_runs_the_same_action_as_its_letter():
    """Every entry maps to a dashboard binding on its own key, and to a method."""
    from nextron.tui.screens.dashboard import DashboardScreen

    bindings = {}
    for binding in DashboardScreen.BINDINGS:
        for key in binding.key.split(","):
            bindings[key.strip()] = binding.action

    for entry in MENU_ENTRIES:
        key = entry.key.lower()
        assert key in bindings, f"{entry.label}: no '{entry.key}' binding"
        assert bindings[key] == entry.action, (
            f"{entry.label}: '{entry.key}' runs {bindings[key]!r}, "
            f"the menu runs {entry.action!r}"
        )
        assert callable(getattr(DashboardScreen, f"action_{entry.action}", None))
