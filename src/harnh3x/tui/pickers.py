"""Numbered list screens: pick a model, a past session, or answers to export."""

from __future__ import annotations

from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static

from harnh3x.agent import session_label
from harnh3x.tui.export import answer_preview
from harnh3x.tui.modals import BackdropCloses
from harnh3x.tui.theme import ANSI_THEME, THEME

_PROMPT = "Use up/down and Enter, or type a number."
_FILTER_PROMPT = "Use up/down and Enter, type a number, or type to filter."
_BACK = "Press Enter or Escape to go back."
_CANCEL = Binding("escape", "cancel", "Cancel", priority=True)


class NumberedPicker(ModalScreen):
    """A bordered box: a numbered option list and a status line.

    Arrow keys move the highlight; typed digits follow it and Enter picks.
    Subclasses set the class attributes, build the labels, and implement
    ``choose``. ``extra_label`` adds one trailing option outside the
    numbered range that ``choose_extra`` handles. ``empty_text`` replaces
    the list when there is nothing to pick. When ``filterable`` is set,
    typed text that is not a number narrows the list to labels containing
    it; the labels keep their original numbers.
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("enter", "choose", "Choose", priority=True),
        Binding("up", "move_up", "Up", priority=True),
        Binding("down", "move_down", "Down", priority=True),
        Binding("backspace", "backspace", "Backspace", priority=True),
    ]
    DEFAULT_CSS = """
    NumberedPicker {
        align: center middle;
    }
    #picker-box {
        width: 90%;
        max-width: 120;
        height: auto;
        max-height: 90%;
        border: round $accent;
        padding: 0 1;
        background: $surface;
    }
    #picker-box NumberedOptions {
        height: auto;
        max-height: 70vh;
        border: none;
    }
    #picker-box #note {
        margin-bottom: 1;
    }
    #picker-box #status {
        color: $text-muted;
        margin-top: 1;
    }
    """

    heading: ClassVar[str] = ""
    option_id: ClassVar[str] = ""
    cancellable: ClassVar[bool] = False
    filterable: ClassVar[bool] = False
    empty_text: ClassVar[str | None] = None
    extra_label: ClassVar[str | None] = None

    def __init__(self, labels: list[str]) -> None:
        super().__init__()
        self.labels = labels
        self.digits = ""
        self.error = ""
        self.note = ""
        self.shown = list(range(len(labels)))

    def choose(self, index: int) -> None:
        raise NotImplementedError

    def choose_extra(self) -> None:
        self.dismiss(None)

    @property
    def _empty(self) -> bool:
        return not self.labels and self.empty_text is not None

    @property
    def _filtering(self) -> bool:
        return self.filterable and bool(self.digits) and not self.digits.isdigit()

    @property
    def _prompt(self) -> str:
        prompt = _FILTER_PROMPT if self.filterable else _PROMPT
        return f"{prompt} Escape cancels." if self.cancellable else prompt

    def _options(self) -> list[str]:
        options = [self.labels[index] for index in self.shown]
        if self.extra_label is not None:
            options.append(self.extra_label)
        return options

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-box"):
            if self.note:
                yield Static(self.note, id="note", markup=False)
            if self._empty:
                yield Static(self.empty_text or "", markup=False)
                yield Static(_BACK, id="status", markup=False)
                return
            yield NumberedOptions(*self._options(), id=self.option_id)
            yield Static(self._prompt, id="status", markup=False)

    def on_mount(self) -> None:
        self.query_one("#picker-box", Vertical).border_title = self.heading
        if not self._empty:
            self.query_one(NumberedOptions).focus()

    def add_digit(self, digit: str) -> None:
        self.add_text(digit)

    def add_text(self, text: str) -> None:
        self.error = ""
        self.digits += text
        self._apply_typed()

    def action_backspace(self) -> None:
        self.error = ""
        self.digits = self.digits[:-1]
        self._apply_typed()

    def _move(self, up: bool) -> None:
        if not self._filtering:
            self.digits = ""
        self.error = ""
        self._refresh_status()
        if self._empty:
            return
        options = self.query_one(NumberedOptions)
        if up:
            options.action_cursor_up()
        else:
            options.action_cursor_down()

    def action_move_up(self) -> None:
        self._move(up=True)

    def action_move_down(self) -> None:
        self._move(up=False)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_choose(self) -> None:
        if self._empty:
            self.dismiss(None)
            return
        count = len(self.labels)
        if self.digits == "" or self._filtering:
            highlighted = self.query_one(NumberedOptions).highlighted
            if highlighted is None:
                return
            if highlighted >= len(self.shown):
                self.choose_extra()
                return
            self.choose(self.shown[highlighted])
            return
        number = int(self.digits)
        if str(number) == self.digits and 1 <= number <= count:
            self.choose(number - 1)
            return
        self.error = self._range_error(count)
        self.digits = ""
        self._apply_typed()

    def _range_error(self, count: int) -> str:
        if self.extra_label is not None:
            return f"Enter a number from 1 to {count}, or choose {self.extra_label}."
        return f"Enter a number from 1 to {count}."

    def _apply_typed(self) -> None:
        if self._empty:
            self._refresh_status()
            return
        if self._filtering:
            needle = self.digits.lower()
            shown = [
                index
                for index, label in enumerate(self.labels)
                if needle in label.lower()
            ]
        else:
            shown = list(range(len(self.labels)))
        options = self.query_one(NumberedOptions)
        if shown != self.shown:
            self.shown = shown
            options.clear_options()
            options.add_options(self._options())
            options.highlighted = 0 if self._options() else None
        self._follow_digits()
        self._refresh_status()

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
        elif self._filtering:
            found = len(self.shown)
            matches = "1 match" if found == 1 else f"{found} matches"
            text = f"Filter: {self.digits} · {matches} · Backspace edits"
        elif self.digits:
            text = f"Number: {self.digits}"
        else:
            text = self._prompt
        self.query_one("#status", Static).update(text)


class NumberedOptions(OptionList):
    def on_key(self, event: Key) -> None:
        character = event.character
        screen = self.screen
        if not character or not isinstance(screen, NumberedPicker):
            return
        if character.isdigit():
            event.stop()
            event.prevent_default()
            screen.add_digit(character)
        elif screen.filterable and character.isprintable():
            event.stop()
            event.prevent_default()
            screen.add_text(character)


ModelOptions = NumberedOptions
ResumeOptions = NumberedOptions
ExportOptions = NumberedOptions


class ModelScreen(NumberedPicker):
    BINDINGS: ClassVar[list[Binding]] = [_CANCEL]

    heading = "Available models"
    option_id = "models"
    cancellable = True

    def __init__(self, models: list[str], reason: str | None = None) -> None:
        super().__init__([f"{index + 1}. {name}" for index, name in enumerate(models)])
        self.models = models
        self.note = reason or ""

    def choose(self, index: int) -> None:
        self.dismiss(self.models[index])


class SwitchModelScreen(BackdropCloses, ModelScreen):
    """The model list opened from the status bar: cancellable, filterable, current marked."""

    BINDINGS: ClassVar[list[Binding]] = [_CANCEL]

    heading = "Choose a model"
    cancellable = True
    filterable = True

    def __init__(self, models: list[str], current: str | None) -> None:
        super().__init__(models)
        self.current = current
        self.labels = [
            f"{label} (current)" if name == current else label
            for label, name in zip(self.labels, models, strict=True)
        ]

    def on_mount(self) -> None:
        super().on_mount()
        if self.current in self.models:
            options = self.query_one(NumberedOptions)
            options.highlighted = self.models.index(self.current)


class ModelApp(App[str]):
    def __init__(self, models: list[str], reason: str | None = None) -> None:
        super().__init__()
        self.theme = THEME
        self.ansi_theme_dark = ANSI_THEME
        self.models = models
        self.reason = reason

    def on_mount(self) -> None:
        self.push_screen(ModelScreen(self.models, self.reason), self.exit)


def pick_model(models: list[str], reason: str | None = None) -> str:
    chosen = ModelApp(models, reason).run()
    if not isinstance(chosen, str):
        raise SystemExit(1)
    return chosen


class ResumeScreen(NumberedPicker):
    BINDINGS: ClassVar[list[Binding]] = [_CANCEL]

    heading = "Past sessions"
    option_id = "sessions"
    cancellable = True
    filterable = True
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

    heading = "Responses"
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
