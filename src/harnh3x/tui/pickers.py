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

_PROMPT = "Use up/down and Enter, or type a number."
_BACK = "Press Enter or Escape to go back."
_CANCEL = Binding("escape", "cancel", "Cancel", priority=True)


class NumberedPicker(Screen):
    """A heading, a numbered option list, and a status line.

    Arrow keys move the highlight; typed digits follow it and Enter picks.
    Subclasses set the class attributes, build the labels, and implement
    ``choose``. ``extra_label`` adds one trailing option outside the
    numbered range that ``choose_extra`` handles. ``empty_text`` replaces
    the list when there is nothing to pick.
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("enter", "choose", "Choose", priority=True),
        Binding("up", "move_up", "Up", priority=True),
        Binding("down", "move_down", "Down", priority=True),
        Binding("backspace", "backspace", "Backspace", priority=True),
    ]

    heading: ClassVar[str] = ""
    option_id: ClassVar[str] = ""
    cancellable: ClassVar[bool] = False
    empty_text: ClassVar[str | None] = None
    extra_label: ClassVar[str | None] = None

    def __init__(self, labels: list[str]) -> None:
        super().__init__()
        self.labels = labels
        self.digits = ""
        self.error = ""

    def choose(self, index: int) -> None:
        raise NotImplementedError

    def choose_extra(self) -> None:
        self.dismiss(None)

    @property
    def _empty(self) -> bool:
        return not self.labels and self.empty_text is not None

    @property
    def _prompt(self) -> str:
        return f"{_PROMPT} Escape cancels." if self.cancellable else _PROMPT

    def compose(self) -> ComposeResult:
        if self._empty:
            yield Static(self.empty_text or "", markup=False)
            yield Static(_BACK, id="status", markup=False)
            return
        yield Static(self.heading, markup=False)
        options = list(self.labels)
        if self.extra_label is not None:
            options.append(self.extra_label)
        yield NumberedOptions(*options, id=self.option_id)
        yield Static(self._prompt, id="status", markup=False)

    def on_mount(self) -> None:
        if not self._empty:
            self.query_one(NumberedOptions).focus()

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
        if not self._empty:
            self.query_one(NumberedOptions).action_cursor_up()

    def action_move_down(self) -> None:
        self.digits = ""
        self.error = ""
        self._refresh_status()
        if not self._empty:
            self.query_one(NumberedOptions).action_cursor_down()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_choose(self) -> None:
        if self._empty:
            self.dismiss(None)
            return
        count = len(self.labels)
        if self.digits == "":
            highlighted = self.query_one(NumberedOptions).highlighted
            if highlighted is None:
                return
            if highlighted >= count:
                self.choose_extra()
                return
            self.choose(highlighted)
            return
        number = int(self.digits)
        if str(number) == self.digits and 1 <= number <= count:
            self.choose(number - 1)
            return
        self.error = self._range_error(count)
        self.digits = ""
        self._refresh_status()

    def _range_error(self, count: int) -> str:
        if self.extra_label is not None:
            return f"Enter a number from 1 to {count}, or choose {self.extra_label}."
        return f"Enter a number from 1 to {count}."

    def _follow_digits(self) -> None:
        if self.digits.isdigit() and str(int(self.digits)) == self.digits:
            number = int(self.digits)
            if 1 <= number <= len(self.labels):
                self.query_one(NumberedOptions).highlighted = number - 1

    def _refresh_status(self) -> None:
        if self._empty:
            text = _BACK
        elif self.error:
            text = self.error
        elif self.digits:
            text = f"Number: {self.digits}"
        else:
            text = self._prompt
        self.query_one("#status", Static).update(text)


class NumberedOptions(OptionList):
    def on_key(self, event: Key) -> None:
        if event.character and event.character.isdigit():
            event.stop()
            event.prevent_default()
            screen = self.screen
            if isinstance(screen, NumberedPicker):
                screen.add_digit(event.character)


ModelOptions = NumberedOptions
ResumeOptions = NumberedOptions
ExportOptions = NumberedOptions


class ModelScreen(NumberedPicker):
    heading = "Available models:"
    option_id = "models"

    def __init__(self, models: list[str]) -> None:
        super().__init__([f"{index + 1}. {name}" for index, name in enumerate(models)])
        self.models = models

    def choose(self, index: int) -> None:
        self.dismiss(self.models[index])


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


class ResumeScreen(NumberedPicker):
    BINDINGS: ClassVar[list[Binding]] = [_CANCEL]

    heading = "Past sessions:"
    option_id = "sessions"
    cancellable = True
    empty_text = "No past sessions."

    def __init__(self, sessions: list) -> None:
        super().__init__(
            [session_label(info, index) for index, info in enumerate(sessions)]
        )
        self.sessions = sessions

    def choose(self, index: int) -> None:
        self.dismiss(self.sessions[index])


class ExportScreen(NumberedPicker):
    BINDINGS: ClassVar[list[Binding]] = [_CANCEL]

    heading = "Responses:"
    option_id = "exports"
    cancellable = True
    extra_label = "All"

    def __init__(self, answers: list[str]) -> None:
        super().__init__(
            [
                f"{index + 1}. {answer_preview(answer)}"
                for index, answer in enumerate(answers)
            ]
        )
        self.answers = answers

    def choose(self, index: int) -> None:
        self.dismiss([self.answers[index]])

    def choose_extra(self) -> None:
        self.dismiss(list(self.answers))
