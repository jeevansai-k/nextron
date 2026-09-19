"""The Doctor screen: the automatic system health report."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import RichLog, Static

from nextron.core import constants
from nextron.diagnostics.doctor import Doctor, DoctorReport
from nextron.tui.screens.base import NextronScreen

__all__ = ["DoctorScreen"]


class DoctorScreen(NextronScreen):
    """Run the diagnostics and show every finding with its remedy."""

    subtitle = "Doctor"

    BINDINGS = [
        Binding("r", "rerun", "Re-run"),
        Binding("o", "rerun_offline", "Offline run", show=False),
        Binding("escape", "close", "Return"),
    ]

    def compose_body(self) -> ComposeResult:
        yield Static("", id="doctor-summary", classes="screen-hint")
        with Vertical(classes="screen-body") as body:
            body.border_title = "System health"
            yield RichLog(id="doctor-log", markup=False, wrap=True, highlight=False)

    def on_mount(self) -> None:
        super().on_mount()
        self.action_rerun()

    # -- running ------------------------------------------------------------ #

    def action_rerun(self) -> None:
        self._run(network=True)

    def action_rerun_offline(self) -> None:
        self._run(network=False)

    def _run(self, *, network: bool) -> None:
        log = self.query_one("#doctor-log", RichLog)
        log.clear()
        log.write(
            Text(
                "Running diagnostics..."
                + ("" if network else " (offline: no network probes)"),
                style=constants.COLOR_ACCENT,
            )
        )
        self.query_one("#doctor-summary", Static).update(
            Text("Working...", style=constants.COLOR_SURFACE)
        )
        self.run_worker(self._execute(network), exclusive=True)

    async def _execute(self, network: bool) -> None:
        doctor = Doctor(self.app.runtime.config)
        report = await doctor.run(network=network)
        self._render_report(report)

    # -- rendering ---------------------------------------------------------- #

    def _render_report(self, report: DoctorReport) -> None:
        log = self.query_one("#doctor-log", RichLog)
        log.clear()
        for section, findings in report.sections().items():
            log.write(Text(f"\n{section}", style=f"bold {constants.COLOR_ACCENT}"))
            for finding in findings:
                line = Text(no_wrap=False)
                line.append(
                    f"  {finding.status.marker} ",
                    style=f"bold {finding.status.color}",
                )
                line.append(f"{finding.name}: ", style=f"bold {constants.COLOR_TEXT}")
                line.append(finding.detail, style=constants.COLOR_TEXT)
                log.write(line)
                if finding.hint:
                    hint = Text(no_wrap=False)
                    hint.append("      -> ", style=constants.COLOR_SURFACE)
                    hint.append(finding.hint, style=constants.COLOR_SURFACE)
                    log.write(hint)

        summary = Text(no_wrap=True)
        verdict_color = (
            constants.COLOR_ACCENT if report.healthy else constants.COLOR_SURFACE
        )
        summary.append(
            "HEALTHY" if report.healthy else "NEEDS ATTENTION",
            style=f"bold {verdict_color}",
        )
        summary.append(f" -- {report.summary}", style=constants.COLOR_TEXT)
        self.query_one("#doctor-summary", Static).update(summary)
