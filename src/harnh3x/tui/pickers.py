"""Numbered list screens: pick a model, a past session, or answers to export."""

from __future__ import annotations

from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.events import Key
from textual.screen import Screen
from textual.widgets import OptionList, Static

from harnh3x.agent import session_label
from harnh3x.tui.export import answer_preview


class ModelScreen(Screen):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("enter", "choose", "Choose", priority=True),
        Binding("up", "move_up", "Up", priority=True),
        Binding("down", "move_down", "Down", priority=True),
        Binding("backspace", "backspace", "Backspace", priority=True),
    ]

    def __init__(self, models: list[str]) -> None:
        super().__init__()
        self.models = models
        self.digits = ""
        self.error = ""

    def compose(self) -> ComposeResult:
        yield Static("Available models:", markup=False)
        yield ModelOptions(
            *[f"{index + 1}. {name}" for index, name in enumerate(self.models)],
            id="models",
        )
        yield Static(
            "Use up/down and Enter, or type a number.",
            id="status",
            markup=False,
        )

    def on_mount(self) -> None:
        self.query_one(ModelOptions).focus()

    def add_digit(self, digit: str) -> None:
        self.error = ""
        self.digits += digit
        self._follow_digits()
        self._refresh_status()

    def action_backspace(self) -> None:
        self.error = ""
        self.digits = self.digits[:-1]
        self._follow_digits()
        self._refresh_status()

    def action_move_up(self) -> None:
        self.digits = ""
        self.error = ""
        self._refresh_status()
        self.query_one(ModelOptions).action_cursor_up()

    def action_move_down(self) -> None:
        self.digits = ""
        self.error = ""
        self._refresh_status()
        self.query_one(ModelOptions).action_cursor_down()

    def action_choose(self) -> None:
        if self.digits == "":
            highlighted = self.query_one(ModelOptions).highlighted
            if highlighted is None:
                return
            self.dismiss(self.models[highlighted])
            return
        number = int(self.digits)
        if str(number) == self.digits and 1 <= number <= len(self.models):
            self.dismiss(self.models[number - 1])
            return
        self.error = f"Enter a number from 1 to {len(self.models)}."
        self.digits = ""
        self._refresh_status()

    def _follow_digits(self) -> None:
        if self.digits.isdigit() and str(int(self.digits)) == self.digits:
            number = int(self.digits)
            if 1 <= number <= len(self.models):
                self.query_one(ModelOptions).highlighted = number - 1

    def _refresh_status(self) -> None:
        if self.error:
            text = self.error
        elif self.digits:
            text = f"Number: {self.digits}"
        else:
            text = "Use up/down and Enter, or type a number."
        self.query_one("#status", Static).update(text)


class ModelOptions(OptionList):
    def on_key(self, event: Key) -> None:
        if event.character and event.character.isdigit():
            event.stop()
            event.prevent_default()
            screen = self.screen
            if isinstance(screen, ModelScreen):
                screen.add_digit(event.character)


class ModelApp(App[str]):
    def __init__(self, models: list[str]) -> None:
        super().__init__()
        self.models = models

    def on_mount(self) -> None:
        self.push_screen(ModelScreen(self.models), self.exit)


def pick_model(models: list[str]) -> str:
    chosen = ModelApp(models).run()
    if not isinstance(chosen, str):
        raise KeyboardInterrupt
    return chosen


class ResumeScreen(Screen):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("enter", "choose", "Choose", priority=True),
        Binding("up", "move_up", "Up", priority=True),
        Binding("down", "move_down", "Down", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("backspace", "backspace", "Backspace", priority=True),
    ]

    def __init__(self, sessions: list) -> None:
        super().__init__()
        self.sessions = sessions
        self.digits = ""
        self.error = ""

    def compose(self) -> ComposeResult:
        if not self.sessions:
            yield Static("No past sessions.", markup=False)
            yield Static(
                "Press Enter or Escape to go back.",
                id="status",
                markup=False,
            )
            return
        yield Static("Past sessions:", markup=False)
        yield ResumeOptions(
            *[session_label(info, index) for index, info in enumerate(self.sessions)],
            id="sessions",
        )
        yield Static(
            "Use up/down and Enter, or type a number. Escape cancels.",
            id="status",
            markup=False,
        )

    def on_mount(self) -> None:
        if self.sessions:
            self.query_one(ResumeOptions).focus()

    def add_digit(self, digit: str) -> None:
        self.error = ""
        self.digits += digit
        self._follow_digits()
        self._refresh_status()

    def action_backspace(self) -> None:
        self.error = ""
        self.digits = self.digits[:-1]
        self._follow_digits()
        self._refresh_status()

    def action_move_up(self) -> None:
        self.digits = ""
        self.error = ""
        self._refresh_status()
        if self.sessions:
            self.query_one(ResumeOptions).action_cursor_up()

    def action_move_down(self) -> None:
        self.digits = ""
        self.error = ""
        self._refresh_status()
        if self.sessions:
            self.query_one(ResumeOptions).action_cursor_down()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_choose(self) -> None:
        if not self.sessions:
            self.dismiss(None)
            return
        if self.digits == "":
            highlighted = self.query_one(ResumeOptions).highlighted
            if highlighted is None:
                return
            self.dismiss(self.sessions[highlighted])
            return
        number = int(self.digits)
        if str(number) == self.digits and 1 <= number <= len(self.sessions):
            self.dismiss(self.sessions[number - 1])
            return
        self.error = f"Enter a number from 1 to {len(self.sessions)}."
        self.digits = ""
        self._refresh_status()

    def _follow_digits(self) -> None:
        if self.digits.isdigit() and str(int(self.digits)) == self.digits:
            number = int(self.digits)
            if 1 <= number <= len(self.sessions):
                self.query_one(ResumeOptions).highlighted = number - 1

    def _refresh_status(self) -> None:
        if not self.sessions:
            text = "Press Enter or Escape to go back."
        elif self.error:
            text = self.error
        elif self.digits:
            text = f"Number: {self.digits}"
        else:
            text = "Use up/down and Enter, or type a number. Escape cancels."
        self.query_one("#status", Static).update(text)


class ResumeOptions(OptionList):
    def on_key(self, event: Key) -> None:
        if event.character and event.character.isdigit():
            event.stop()
            event.prevent_default()
            screen = self.screen
            if isinstance(screen, ResumeScreen):
                screen.add_digit(event.character)


class ExportScreen(Screen):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("enter", "choose", "Choose", priority=True),
        Binding("up", "move_up", "Up", priority=True),
        Binding("down", "move_down", "Down", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("backspace", "backspace", "Backspace", priority=True),
    ]

    def __init__(self, answers: list[str]) -> None:
        super().__init__()
        self.answers = answers
        self.digits = ""
        self.error = ""

    def compose(self) -> ComposeResult:
        yield Static("Responses:", markup=False)
        labels = [
            f"{index + 1}. {answer_preview(answer)}"
            for index, answer in enumerate(self.answers)
        ]
        labels.append("All")
        yield ExportOptions(*labels, id="exports")
        yield Static(
            "Use up/down and Enter, or type a number. Escape cancels.",
            id="status",
            markup=False,
        )

    def on_mount(self) -> None:
        self.query_one(ExportOptions).focus()

    def add_digit(self, digit: str) -> None:
        self.error = ""
        self.digits += digit
        self._follow_digits()
        self._refresh_status()

    def action_backspace(self) -> None:
        self.error = ""
        self.digits = self.digits[:-1]
        self._follow_digits()
        self._refresh_status()

    def action_move_up(self) -> None:
        self.digits = ""
        self.error = ""
        self._refresh_status()
        self.query_one(ExportOptions).action_cursor_up()

    def action_move_down(self) -> None:
        self.digits = ""
        self.error = ""
        self._refresh_status()
        self.query_one(ExportOptions).action_cursor_down()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_choose(self) -> None:
        count = len(self.answers)
        if self.digits == "":
            highlighted = self.query_one(ExportOptions).highlighted
            if highlighted is None:
                return
            if highlighted == count:
                self.dismiss(list(self.answers))
                return
            self.dismiss([self.answers[highlighted]])
            return
        number = int(self.digits)
        if str(number) == self.digits and 1 <= number <= count:
            self.dismiss([self.answers[number - 1]])
            return
        self.error = f"Enter a number from 1 to {count}, or choose All."
        self.digits = ""
        self._refresh_status()

    def _follow_digits(self) -> None:
        if self.digits.isdigit() and str(int(self.digits)) == self.digits:
            number = int(self.digits)
            if 1 <= number <= len(self.answers):
                self.query_one(ExportOptions).highlighted = number - 1

    def _refresh_status(self) -> None:
        if self.error:
            text = self.error
        elif self.digits:
            text = f"Number: {self.digits}"
        else:
            text = "Use up/down and Enter, or type a number. Escape cancels."
        self.query_one("#status", Static).update(text)


class ExportOptions(OptionList):
    def on_key(self, event: Key) -> None:
        if event.character and event.character.isdigit():
            event.stop()
            event.prevent_default()
            screen = self.screen
            if isinstance(screen, ExportScreen):
                screen.add_digit(event.character)
