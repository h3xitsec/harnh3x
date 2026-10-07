from __future__ import annotations

from collections.abc import Callable

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.events import Key
from textual.screen import Screen
from textual.widgets import Markdown, OptionList, Static, TextArea

THINKING_ROWS = 3


def line_limit(width: int) -> int:
    # A line as wide as the terminal wraps before the following newline.
    if width <= 1:
        return 1
    return width - 1


def wrap_lines(text: str, width: int) -> list[str]:
    width = max(width, 1)
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if paragraph == "":
            lines.append("")
            continue
        start = 0
        while start < len(paragraph):
            lines.append(paragraph[start : start + width])
            start += width
    return lines


def thinking_hint(*, expanded: bool, hold: bool) -> str:
    action = "collapse" if expanded else "expand"
    hint = f"ctrl+t to {action}"
    if hold:
        hint += " · enter to close"
    return hint


def thinking_shown(reasoning: str, *, expanded: bool, width: int) -> list[str]:
    limit = line_limit(width)
    indent = "  " if limit > 2 else ""
    body_limit = max(limit - len(indent), 1)
    wrapped = wrap_lines(reasoning, body_limit)
    shown = wrapped if expanded else wrapped[-THINKING_ROWS:]
    return [f"{indent}{line}" for line in shown]


def thinking_text(reasoning: str, *, expanded: bool, width: int, hold: bool) -> str:
    limit = line_limit(width)
    hint = thinking_hint(expanded=expanded, hold=hold)
    header = "\n".join(wrap_lines(f"Thinking · {hint}", limit))
    body = "\n".join(thinking_shown(reasoning, expanded=expanded, width=width))
    return f"{header}\n{body}"


def reply_reasoning(session, start: int, answer: str) -> str:
    messages = getattr(session, "messages", None)
    if not messages:
        return ""
    parts = []
    for msg in messages[start:]:
        if not isinstance(msg, dict):
            continue
        reasoning = msg.get("reasoning")
        if msg.get("role") == "assistant" and reasoning:
            parts.append(reasoning)
    if parts and parts[-1] == answer:
        parts.pop()
    return "\n\n".join(parts)


class Thinking(Static):
    def __init__(self, reasoning: str, *, hold: bool) -> None:
        super().__init__("", markup=False, classes="panel")
        self.reasoning = reasoning
        self.expanded = False
        self.hold = hold
        self.plain = ""
        self._sync(80)

    def on_mount(self) -> None:
        self._sync(self.size.width or 80)

    def on_resize(self, event) -> None:
        self._sync(event.size.width or 80)

    def toggle(self) -> None:
        self.expanded = not self.expanded
        self._sync(self.size.width or 80)

    def _inner_width(self, outer: int) -> int:
        gutter = self.styles.gutter
        horizontal = gutter.left + gutter.right
        if horizontal == 0:
            horizontal = 4
        return max(outer - horizontal, 1)

    def _sync(self, width: int) -> None:
        self.border_title = "Thinking"
        self.border_subtitle = thinking_hint(expanded=self.expanded, hold=self.hold)
        self.plain = "\n".join(
            thinking_shown(
                self.reasoning,
                expanded=self.expanded,
                width=self._inner_width(width),
            )
        )
        self.update(self.plain)


class AgentAnswer(Vertical):
    def __init__(self, source: str) -> None:
        super().__init__()
        self.source = source

    def compose(self) -> ComposeResult:
        yield Static("Agent", markup=False)
        yield Markdown(self.source, open_links=False)


class ToolBox(Static):
    def __init__(self) -> None:
        super().__init__("", markup=False, classes="panel")
        self.lines: list[str] = []
        self.border_title = "Tools"

    def add(self, name: str, args: object) -> None:
        self.lines.append(f"🔧 {name}({args})")
        self.update("\n".join(self.lines))


class Composer(TextArea):
    def __init__(self) -> None:
        super().__init__(id="composer", placeholder="You")

    async def _on_key(self, event: Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.app.action_submit()
            return
        if event.key == "shift+enter":
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        await super()._on_key(event)


class ModelScreen(Screen):
    BINDINGS = [
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


class HarnessApp(App):
    BINDINGS = [
        Binding("ctrl+t", "toggle_thinking", "Thinking", priority=True),
    ]
    CSS = """
    #banner {
        dock: top;
        height: auto;
        padding: 0 1;
        border-bottom: solid ansi_bright_black;
    }
    #transcript {
        height: 1fr;
        padding: 1 1 0 1;
    }
    #composer {
        dock: bottom;
        height: 5;
    }
    .panel {
        border: round ansi_bright_black;
        padding: 0 1;
        margin: 1 0;
        color: $text-muted;
        height: auto;
    }
    AgentAnswer {
        height: auto;
    }
    """

    def __init__(
        self,
        session,
        run: Callable | None = None,
        *,
        show_tools: bool = True,
        review: tuple | None = None,
    ) -> None:
        super().__init__()
        self.session = session
        self._run = run
        self.show_tools = show_tools
        self.review = review
        self.latest: Thinking | None = None
        self.tools: ToolBox | None = None

    def compose(self) -> ComposeResult:
        if self.review is None:
            banner = (
                "🤖 File Agent (Shift+Enter for a new line, type 'exit' or press 'ctrl+q' to exit)\n"
                f"Workspace: {self.session.workspace}\n"
                f"Model: {self.session.model}"
            )
        else:
            banner = f"Workspace: {self.session.workspace}\nModel: {self.session.model}"
        yield Static(banner, id="banner", markup=False)
        yield VerticalScroll(id="transcript")
        if self.review is None:
            yield Composer()

    def on_mount(self) -> None:
        if self.review is not None:
            user, tools, reasoning, answer = self.review
            self._append_user(user)
            for name, args in tools:
                self._append_tool(name, args)
            self._finish(answer, reasoning, refocus=False)
            return
        self.query_one(Composer).focus()

    def action_toggle_thinking(self) -> None:
        if self.latest is not None:
            self.latest.toggle()

    def action_submit(self) -> None:
        if self.review is not None or self._run is None:
            return
        composer = self.query_one(Composer)
        if composer.disabled:
            return
        text = composer.text.strip()
        if not text:
            return
        if text.lower() == "exit":
            self.exit()
            return
        composer.load_text("")
        composer.disabled = True
        self._append_user(text)
        self.run_worker(lambda: self._turn(text), thread=True, exclusive=True)

    def action_close(self) -> None:
        if self.review is not None:
            self.exit()

    def transcript_plain(self) -> str:
        parts = []
        for child in self.query_one("#transcript").children:
            if isinstance(child, AgentAnswer):
                parts.append(f"Agent: {child.source}")
            elif isinstance(child, Thinking):
                parts.append(child.plain)
            elif isinstance(child, Static):
                parts.append(str(child.content))
        return "\n".join(parts)

    def _turn(self, text: str) -> None:
        start = len(getattr(self.session, "messages", []) or [])

        def on_tool(name: str, args: object) -> None:
            self.call_from_thread(self._append_tool, name, args)

        assert self._run is not None
        answer = self._run(
            text,
            self.session,
            show_tools=self.show_tools,
            on_tool=on_tool if self.show_tools else None,
        )
        reasoning = ""
        if self.show_tools:
            reasoning = reply_reasoning(self.session, start, answer)
        self.call_from_thread(self._finish, answer, reasoning, True)

    def _append_user(self, text: str) -> None:
        self.tools = None
        self.query_one("#transcript").mount(Static(f"You: {text}", markup=False))

    def _append_tool(self, name: str, args: object) -> None:
        transcript = self.query_one("#transcript")
        if self.tools is None:
            self.tools = ToolBox()
            transcript.mount(self.tools)
        self.tools.add(name, args)
        transcript.scroll_end(animate=False)

    def _finish(self, answer: str, reasoning: str, refocus: bool = True) -> None:
        transcript = self.query_one("#transcript")
        if reasoning:
            block = Thinking(reasoning, hold=self.review is not None)
            self.latest = block
            transcript.mount(block)
        transcript.mount(AgentAnswer(answer))
        transcript.scroll_end(animate=False)
        if refocus and self.review is None:
            composer = self.query_one(Composer)
            composer.disabled = False
            composer.focus()


class ReviewApp(HarnessApp):
    BINDINGS = [
        Binding("enter", "close", "Close", priority=True),
    ]

    def __init__(
        self,
        session,
        user: str,
        tools: list[tuple[str, object]],
        reasoning: str,
        answer: str,
    ) -> None:
        super().__init__(
            session,
            review=(user, tools, reasoning, answer),
        )


def run_interactive(session, run: Callable, *, show_tools: bool) -> None:
    HarnessApp(session, run, show_tools=show_tools).run()


def run_review(
    session,
    user: str,
    tools: list[tuple[str, object]],
    reasoning: str,
    answer: str,
) -> None:
    ReviewApp(session, user, tools, reasoning, answer).run()
