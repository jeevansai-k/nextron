"""The rotation pool screen: which profiles the VPN shuffle engine cycles."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import DataTable, Static

from nextron.core import constants
from nextron.core.config import ShuffleAlgorithm
from nextron.tui.screens.base import NextronScreen
from nextron.tui.screens.modals import PromptScreen
from nextron.utils import format as fmt
from nextron.utils import intervals
from nextron.vpn.profiles import VPNProfile

__all__ = ["RotationPoolScreen"]


class RotationPoolScreen(NextronScreen):
    """Build the shuffle pool and tune the VPN scheduler."""

    subtitle = "Rotation Pool"

    BINDINGS = [
        Binding("space", "toggle_member", "Add / remove"),
        Binding("a", "select_all", "All"),
        Binding("n", "select_none", "None"),
        Binding("g", "cycle_algorithm", "Algorithm"),
        Binding("left_square_bracket", "interval_down", "Interval −"),
        Binding("right_square_bracket", "interval_up", "Interval +"),
        Binding("t", "toggle_scheduler", "Scheduler"),
        Binding("i", "set_interval", "Set interval", show=False),
        Binding("escape", "close", "Return"),
    ]

    def compose_body(self) -> ComposeResult:
        yield Static("", id="pool-summary", classes="screen-hint")
        with Vertical(classes="screen-body") as body:
            body.border_title = "Shuffle Pool"
            yield DataTable(id="pool-table", cursor_type="row")
            yield Static("", id="pool-next")

    def on_mount(self) -> None:
        super().on_mount()
        table = self.query_one(DataTable)
        table.add_columns("In pool", "Name", "Protocol", "Endpoint", "Used", "Last used")
        self.reload()
        table.focus()

    # -- rendering ---------------------------------------------------------- #

    def reload(self) -> None:
        runtime = self.app.runtime
        pool = set(runtime.config.vpn.shuffle_pool)
        table = self.query_one(DataTable)
        table.clear()

        for profile in runtime.vpn.library.all():
            member = profile.id in pool or not pool
            table.add_row(
                Text("◉" if member else "◌", style=constants.COLOR_ACCENT),
                profile.display,
                profile.protocol.label,
                fmt.truncate(profile.endpoint, 28),
                str(profile.use_count),
                fmt.relative(profile.last_used),
                key=profile.id,
            )

        config = runtime.config.vpn
        summary = Text(no_wrap=True)
        summary.append("Algorithm ", style=constants.COLOR_SURFACE)
        summary.append(
            f"{config.shuffle_algorithm.label}", style=f"bold {constants.COLOR_TEXT}"
        )
        summary.append("   Interval ", style=constants.COLOR_SURFACE)
        summary.append(
            intervals.strip(config.shuffle_interval),
            style=f"bold {constants.COLOR_TEXT}",
        )
        summary.append("   Scheduler ", style=constants.COLOR_SURFACE)
        summary.append(
            "running" if runtime.vpn_scheduler.running else "stopped",
            style=f"bold {constants.COLOR_TEXT}",
        )
        summary.append(
            f"   [ ] steps:  {intervals.ladder(config.shuffle_interval)}",
            style=constants.COLOR_SURFACE,
        )
        self.query_one("#pool-summary", Static).update(summary)

        shuffle = runtime.vpn.shuffle
        upcoming = shuffle.peek(runtime.vpn.current.id if runtime.vpn.current else None)
        note = Text(no_wrap=True)
        note.append(
            f"{shuffle.pool_size} profile(s) in the pool -- an empty selection "
            "means every profile.   ",
            style=constants.COLOR_SURFACE,
        )
        if upcoming is not None:
            note.append("Next: ", style=constants.COLOR_SURFACE)
            note.append(upcoming.name, style=f"bold {constants.COLOR_ACCENT}")
        self.query_one("#pool-next", Static).update(note)

    def _selected(self) -> VPNProfile | None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except Exception:
            return None
        return self.app.runtime.vpn.library.get(str(row_key.value))

    # -- actions ------------------------------------------------------------ #

    def action_toggle_member(self) -> None:
        profile = self._selected()
        if profile is None:
            return
        runtime = self.app.runtime
        pool = list(runtime.config.vpn.shuffle_pool)
        if not pool:
            # An empty pool means "everything"; materialise it before removing.
            pool = [p.id for p in runtime.vpn.library.all()]
        if profile.id in pool:
            pool.remove(profile.id)
        else:
            pool.append(profile.id)
        runtime.set_shuffle_pool(pool)
        self.reload()

    def action_select_all(self) -> None:
        runtime = self.app.runtime
        runtime.set_shuffle_pool([p.id for p in runtime.vpn.library.all()])
        self.reload()

    def action_select_none(self) -> None:
        self.app.runtime.set_shuffle_pool([])
        self.reload()

    def action_cycle_algorithm(self) -> None:
        algorithms = list(ShuffleAlgorithm)
        current = self.app.runtime.config.vpn.shuffle_algorithm
        nxt = algorithms[(algorithms.index(current) + 1) % len(algorithms)]
        self.app.runtime.set_shuffle_algorithm(nxt)
        self.reload()

    def _step_interval(self, direction: int) -> None:
        """Walk the preset ladder: 15s, 30s, then every 30s up to 5m."""
        current = self.app.runtime.config.vpn.shuffle_interval
        applied = self.app.runtime.set_vpn_interval(
            intervals.step(current, direction)
        )
        self.reload()
        self.app.notify(
            f"VPN shuffle every {intervals.label(applied)}", title="Rotation Pool"
        )

    def action_interval_up(self) -> None:
        self._step_interval(+1)

    def action_interval_down(self) -> None:
        self._step_interval(-1)

    def action_set_interval(self) -> None:
        def handle(value: str | None) -> None:
            if not value or not value.strip().isdigit():
                return
            applied = self.app.runtime.set_vpn_interval(int(value.strip()))
            self.reload()
            self.app.notify(f"VPN shuffle interval: {applied}s")

        self.app.push_screen(
            PromptScreen(
                f"Shuffle interval in seconds "
                f"({constants.ROTATION_MIN_SECONDS}-{constants.ROTATION_MAX_SECONDS}).\n"
                "The [ ] steps are: "
                + ", ".join(intervals.label(preset) for preset in intervals.PRESETS),
                title="VPN interval",
                value=str(self.app.runtime.config.vpn.shuffle_interval),
            ),
            handle,
        )

    def action_toggle_scheduler(self) -> None:
        self.app.run_action_task(
            self.app.runtime.toggle_vpn_shuffle(),
            "Toggling VPN shuffle",
            on_done=self.reload,
        )
