"""The connection animation, and the layout arithmetic behind the panels."""

from __future__ import annotations

import pytest
from rich.console import Console
from rich.text import Text

from nextron.state.manager import ServiceStatus
from nextron.tui.widgets import pulse
from nextron.tui.widgets.panels import InfoPanel

# -- what moves, and what does not ------------------------------------------- #


@pytest.mark.parametrize(
    "status",
    [
        ServiceStatus.STARTING,
        ServiceStatus.BOOTSTRAPPING,
        ServiceStatus.VERIFYING,
        ServiceStatus.ROTATING,
        ServiceStatus.STOPPING,
        ServiceStatus.ACTIVE,
    ],
)
def test_a_service_in_use_animates(status):
    assert pulse.animated(status)
    frames = {pulse.marker_for(status, frame) for frame in range(40)}
    assert len(frames) > 1, f"{status} never changed"


@pytest.mark.parametrize(
    "status", [ServiceStatus.IDLE, ServiceStatus.DISABLED, ServiceStatus.ERROR]
)
def test_a_settled_service_stays_still(status):
    """Movement must mean something is happening, so nothing else may move."""
    assert not pulse.animated(status)
    frames = {pulse.marker_for(status, frame) for frame in range(40)}
    assert frames == {status.marker}


def test_connecting_spins_and_connected_breathes():
    spin = [pulse.marker_for(ServiceStatus.BOOTSTRAPPING, f) for f in range(10)]
    assert len(set(spin)) == 10, "the spinner must not repeat within one turn"

    # The pulse holds each frame, so it reads as a slow breath, not a flicker.
    breath = [pulse.marker_for(ServiceStatus.ACTIVE, f) for f in range(4)]
    assert len(set(breath)) == 1


def test_the_pulse_dims_at_the_bottom_of_a_breath():
    styles = {pulse.pulse_style(ServiceStatus.ACTIVE, f) for f in range(40)}
    assert len(styles) == 2


def test_every_frame_is_one_cell_wide():
    """A two-cell glyph would shift the text beside it on every frame."""
    console = Console(width=20, no_color=True)
    for status in ServiceStatus:
        for frame in range(40):
            marker = pulse.marker_for(status, frame)
            assert console.measure(Text(marker)).maximum == 1, (
                f"{status} frame {frame} ({marker!r}) is not one cell"
            )


def test_the_frame_rate_is_sane():
    assert 0.05 <= pulse.FRAME_SECONDS <= 0.25


# -- panel alignment --------------------------------------------------------- #


def _value_columns(*row_sets) -> set[int]:
    """Where the value starts in each rendered panel."""
    columns = set()
    console = Console(width=64, no_color=True, legacy_windows=False)
    for rows in row_sets:
        panel = InfoPanel("t")
        panel._rows = rows
        table = panel.build_table()
        with console.capture() as capture:
            console.print(table)
        first = capture.get().splitlines()[0]
        label = rows[0][0]
        columns.add(first.index(str(rows[0][1]), len(label)))
    return columns


def test_panels_put_their_values_in_the_same_column():
    """Panels sit above one another; their values have to share an edge.

    Without a ratio on the value column, Rich shares an expanding grid's slack
    out by measured content, so a panel of short values indents them much
    further than a panel of long ones and the dashboard reads as ragged.
    """
    columns = _value_columns(
        [("Mode", "Tor Only"), ("Chain", "You -> Tor -> Internet")],
        [("Status", "Idle"), ("Bootstrap", "0%")],
        [("Uptime", "00:00:01"), ("Public IP", "--")],
        [("Upstream", "1.1.1.1"), ("Blocked", "0 domains")],
    )
    assert len(columns) == 1, f"values landed in different columns: {sorted(columns)}"


# -- the trap this animation fell into --------------------------------------- #


def test_no_screen_method_is_shadowed_by_a_textual_attribute():
    """``Widget.__init__`` sets ``self._animate = None``.

    A method of that name on a subclass is silently replaced by the instance
    attribute, so ``set_interval(..., self._animate)`` handed the timer None
    and the animation never ran once. Catch the next such collision here.
    """
    from textual.screen import Screen
    from textual.widget import Widget

    from nextron.tui.screens.dashboard import DashboardScreen
    from nextron.tui.widgets.activity import ActivityLog
    from nextron.tui.widgets.menu import NavMenu
    from nextron.tui.widgets.panels import CountdownPanel, InfoPanel

    probe = Widget()
    instance_attributes = set(vars(probe)) | set(vars(Screen.__new__(Screen)))

    for cls in (DashboardScreen, NavMenu, InfoPanel, CountdownPanel, ActivityLog):
        ours = {
            name
            for name, value in vars(cls).items()
            if callable(value) and not name.startswith("__")
        }
        clash = ours & instance_attributes
        assert not clash, f"{cls.__name__} defines {clash}, which Textual overwrites"


# -- glyph width: the other half of "the borders are jumbled" ---------------- #

#: East-Asian "Ambiguous" characters are drawn one cell by Rich and two by many
#: terminals. Emoji-capable ones get a colour glyph from a fallback font, which
#: is also two cells. Either way the rest of the line shifts and the panel
#: border lands a column late -- on that row only, so the border staggers.
_NARROW = {"N", "Na", "H"}
_EMOJI_CAPABLE = {
    0x2699, 0x2714, 0x2716, 0x25AA, 0x25AB, 0x25B6, 0x25C0, 0x26A1, 0x2B50,
    0x2705, 0x274C, 0x2753, 0x2757, 0x2764, 0x231A, 0x231B, 0x23F0,
}


def _unsafe(ch: str) -> str | None:
    import unicodedata

    from rich.cells import cell_len

    if unicodedata.east_asian_width(ch) not in _NARROW:
        return "ambiguous or wide"
    # Rich lays the screen out with its own width table, which disagrees with
    # Unicode for a handful of glyphs. U+2630 measured 2 there, so Rich
    # truncated it inside a one-cell column and drew an ellipsis instead.
    if cell_len(ch) != 1:
        return f"Rich measures {cell_len(ch)} cells"
    if ord(ch) in _EMOJI_CAPABLE:
        return "emoji-capable"
    return None


def test_the_interface_draws_only_one_cell_glyphs():
    """Every symbol NEXTRON prints must be one cell in any terminal.

    ``banner.py`` is excluded: it paints a picture out of half blocks, which
    are uniform there and scale together rather than shifting a line.
    """
    import pathlib
    import unicodedata

    offenders: dict[str, list[str]] = {}
    root = pathlib.Path(__file__).resolve().parent.parent / "nextron"
    for path in root.rglob("*.py"):
        if "__pycache__" in str(path) or path.name == "banner.py":
            continue
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if line.lstrip().startswith("#"):
                continue
            for ch in line:
                if ord(ch) <= 127:
                    continue
                if not unicodedata.category(ch).startswith(("S", "P")):
                    continue
                reason = _unsafe(ch)
                if reason:
                    offenders.setdefault(
                        f"{ch} U+{ord(ch):04X} ({reason})", []
                    ).append(f"{path.name}:{number}")

    assert not offenders, "\n".join(
        f"{glyph} at {', '.join(places[:3])}" for glyph, places in offenders.items()
    )


def test_every_status_marker_and_frame_is_narrow():
    from nextron.state.manager import ServiceStatus

    for status in ServiceStatus:
        assert not _unsafe(status.marker), f"{status} marker {status.marker!r}"
        for frame in range(40):
            marker = pulse.marker_for(status, frame)
            assert not _unsafe(marker), f"{status} frame {frame}: {marker!r}"


def test_every_menu_glyph_is_narrow():
    from nextron.tui.screens.dashboard import MENU_ENTRIES

    for entry in MENU_ENTRIES:
        assert not _unsafe(entry.glyph), f"{entry.label}: {entry.glyph!r}"


def test_menu_glyphs_come_from_long_established_unicode():
    """A terminal font without the glyph substitutes a wider one from elsewhere.

    U+23FB and U+25EB passed every width check and still rendered too wide on a
    real terminal, because the font had no glyph for them. Staying inside the
    ranges that have been in fonts since Unicode 1.1 avoids the substitution.
    """
    from nextron.tui.screens.dashboard import MENU_ENTRIES

    # Unicode 1.1 blocks that terminal fonts have carried for decades.
    established = [
        range(0x2190, 0x2200),   # Arrows
        range(0x2300, 0x2400),   # Miscellaneous Technical
        range(0x2700, 0x27C0),   # Dingbats
        range(0x0020, 0x007F),   # plain ASCII
    ]
    for entry in MENU_ENTRIES:
        code = ord(entry.glyph)
        assert any(code in block for block in established), (
            f"{entry.label}: {entry.glyph!r} (U+{code:04X}) is outside the "
            "blocks terminal fonts reliably carry"
        )
