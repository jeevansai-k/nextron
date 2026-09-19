"""Modal dialogs: confirmation, single-line prompt and report viewer."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, RichLog, Static

from nextron.core import constants
from nextron.core.verification import VerificationReport

__all__ = ["ConfirmScreen", "PromptScreen", "ReportScreen"]


class ConfirmScreen(ModalScreen[bool]):
    """A yes/no dialog. Returns ``True`` when confirmed.

    The dialog owns its own focus: the arrow and tab keys move between its two
    buttons and never reach the screen underneath, and one of the buttons is
    always focused so ``Enter`` has an unambiguous meaning.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("y", "confirm", "Yes", show=False),
        Binding("n", "cancel", "No", show=False),
        # Keep navigation inside the dialog. Without these the keys fall
        # through and move focus on the screen below, which makes the dialog
        # look unresponsive.
        Binding("left", "previous_button", "Choose", show=False, priority=True),
        Binding("right", "next_button", "Choose", show=False, priority=True),
        Binding("up", "previous_button", "Choose", show=False, priority=True),
        Binding("down", "next_button", "Choose", show=False, priority=True),
        Binding("tab", "next_button", "Choose", show=False, priority=True),
        Binding("shift+tab", "previous_button", "Choose", show=False, priority=True),
    ]

    def __init__(
        self,
        question: str,
        *,
        title: str = "Confirm",
        confirm_label: str = "Yes",
        cancel_label: str = "No",
        focus_confirm: bool = True,
    ) -> None:
        super().__init__()
        self._question = question
        self._title = title
        self._confirm_label = confirm_label
        self._cancel_label = cancel_label
        #: Destructive dialogs focus "cancel" so a stray Enter cannot delete.
        self._focus_confirm = focus_confirm

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box") as box:
            box.border_title = self._title
            yield Static(
                Text(self._question, style=constants.COLOR_TEXT),
                classes="modal-question",
            )
            yield Static(self._hint(), classes="modal-hint")
            with Horizontal(classes="modal-actions"):
                yield Button(self._cancel_label, id="cancel")
                yield Button(self._confirm_label, id="confirm", variant="primary")

    def _hint(self) -> Text:
        """Spell the keys out: a dialog nobody can dismiss is a trap."""
        hint = Text(no_wrap=True)
        hint.append("Y", style=f"bold {constants.COLOR_ACCENT}")
        hint.append(f" {self._confirm_label.lower()}   ", style=constants.COLOR_SURFACE)
        hint.append("N", style=f"bold {constants.COLOR_ACCENT}")
        hint.append(
            f" {self._cancel_label.lower()}   ", style=constants.COLOR_SURFACE
        )
        hint.append("<- ->", style=f"bold {constants.COLOR_ACCENT}")
        hint.append(" choose   ", style=constants.COLOR_SURFACE)
        hint.append("Enter", style=f"bold {constants.COLOR_ACCENT}")
        hint.append(" select   ", style=constants.COLOR_SURFACE)
        hint.append("Esc", style=f"bold {constants.COLOR_ACCENT}")
        hint.append(f" {self._cancel_label.lower()}", style=constants.COLOR_SURFACE)
        return hint

    def on_mount(self) -> None:
        """Always start with a focused button so Enter is never a no-op."""
        target = "#confirm" if self._focus_confirm else "#cancel"
        self.query_one(target, Button).focus()

    # -- navigation --------------------------------------------------------- #

    def _buttons(self) -> list[Button]:
        return list(self.query(Button))

    def _move_focus(self, step: int) -> None:
        buttons = self._buttons()
        if not buttons:
            return
        focused = next(
            (index for index, button in enumerate(buttons) if button.has_focus), 0
        )
        buttons[(focused + step) % len(buttons)].focus()

    def action_next_button(self) -> None:
        self._move_focus(1)

    def action_previous_button(self) -> None:
        self._move_focus(-1)

    # -- outcome ------------------------------------------------------------ #

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class PromptScreen(ModalScreen[str | None]):
    """A single-line text prompt. Returns the text, or ``None`` if cancelled."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self,
        question: str,
        *,
        title: str = "Input",
        value: str = "",
        placeholder: str = "",
    ) -> None:
        super().__init__()
        self._question = question
        self._title = title
        self._value = value
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box") as box:
            box.border_title = self._title
            yield Static(
                Text(self._question, style=constants.COLOR_TEXT),
                classes="modal-question",
            )
            yield Input(
                value=self._value, placeholder=self._placeholder, id="prompt-input"
            )
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="cancel")
                yield Button("OK", id="ok", variant="primary")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self.dismiss(self.query_one(Input).value.strip() or None)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ReportScreen(ModalScreen[None]):
    """Show a verification report: every check, its verdict and its detail."""

    BINDINGS = [Binding("escape", "dismiss_report", "Close")]

    def __init__(self, report: VerificationReport) -> None:
        super().__init__()
        self._report = report

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box") as box:
            box.border_title = f"Verification -- {self._report.mode.label}"
            yield RichLog(id="report-log", markup=False, wrap=True, highlight=False)
            yield Static("", classes="report-summary", id="report-summary")
            with Horizontal(classes="modal-actions"):
                yield Button("Close", id="close", variant="primary")

    def on_mount(self) -> None:
        log = self.query_one("#report-log", RichLog)
        for check in self._report.checks:
            line = Text(no_wrap=False)
            style = (
                constants.COLOR_ACCENT
                if check.passed and not check.skipped
                else constants.COLOR_SECONDARY
                if check.skipped
                else constants.COLOR_SURFACE
                if not check.required
                else constants.COLOR_PRIMARY
            )
            line.append(f"{check.marker} ", style=f"bold {style}")
            line.append(f"{check.name}: ", style=f"bold {constants.COLOR_TEXT}")
            line.append(check.detail or check.verdict, style=constants.COLOR_TEXT)
            log.write(line)

        summary = self.query_one("#report-summary", Static)
        verdict = "PASSED" if self._report.passed else "FAILED"
        verdict_color = (
            constants.COLOR_ACCENT if self._report.passed else constants.COLOR_SURFACE
        )
        summary.update(
            Text(
                f"{verdict} -- {self._report.summary} "
                f"({self._report.duration_seconds:.1f}s)",
                style=f"bold {verdict_color}",
            )
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_dismiss_report(self) -> None:
        self.dismiss(None)
