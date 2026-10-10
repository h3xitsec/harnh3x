from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.events import Key
from textual.screen import Screen
from textual.widgets import Markdown, OptionList, Static, TextArea

from harness.agent import (
    _visible_answer,
    list_sessions,
    parse_command,
    session_label,
)

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


def displayed_args(arguments: object) -> object:
    if isinstance(arguments, dict):
        return arguments
    if not isinstance(arguments, str):
        return arguments
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return arguments
    if isinstance(parsed, dict):
        return parsed
    return arguments


@dataclass(frozen=True)
class Turn:
    user: str
    tools: list[tuple[str, object]]
    reasoning: str
    answer: str | None


def _turn_tools(span: list) -> list[tuple[str, object]]:
    tools: list[tuple[str, object]] = []
    for message in span:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            if not isinstance(name, str):
                name = ""
            tools.append((name, displayed_args(function.get("arguments"))))
    return tools


def _turn_answer(span: list) -> str | None:
    last = None
    for message in span:
        if isinstance(message, dict) and message.get("role") == "assistant":
            last = message
    if last is None or last.get("tool_calls"):
        return None
    return _visible_answer(last)


def conversation_turns(messages: list) -> list[Turn]:
    turns: list[Turn] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") != "user":
            index += 1
            continue
        content = message.get("content")
        user = content if isinstance(content, str) else ""
        index += 1
        start = index
        while index < len(messages):
            nxt = messages[index]
            if isinstance(nxt, dict) and nxt.get("role") == "user":
                break
            index += 1
        span = messages[start:index]
        answer = _turn_answer(span)
        reasoning = reply_reasoning(
            SimpleNamespace(messages=span),
            0,
            answer if answer is not None else "",
        )
        turns.append(Turn(user, _turn_tools(span), reasoning, answer))
    return turns


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


class ResumeScreen(Screen):
    BINDINGS = [
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


class HarnessApp(App):
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("ctrl+t", "toggle_thinking", "Thinking", priority=True),
        Binding("ctrl+n", "new_session", "New session", priority=True),
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

    def _banner(self) -> str:
        lines = []
        if self.review is None:
            lines.append(
                "🤖 File Agent (Shift+Enter for a new line, ctrl+n for a new session, "
                "/resume to resume a session, type 'exit' or press 'ctrl+q' to exit)"
            )
        lines.append(f"Workspace: {self.session.workspace}")
        lines.append(f"Model: {self.session.model}")
        return "\n".join(lines)

    def compose(self) -> ComposeResult:
        yield Static(self._banner(), id="banner", markup=False)
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

    async def action_new_session(self) -> None:
        if self.review is not None or self._run is None or len(self.screen_stack) > 1:
            return
        composer = self.query_one(Composer)
        if composer.disabled:
            return
        self.session.reset()
        self.query_one("#banner", Static).update(self._banner())
        self.latest = None
        self.tools = None
        await self.query_one("#transcript").remove_children()
        composer.load_text("")
        composer.focus()

    def action_submit(self) -> None:
        if self.review is not None or self._run is None or len(self.screen_stack) > 1:
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
        command = parse_command(text)
        if command is not None:
            composer.load_text("")
            self._command(*command)
            return
        composer.load_text("")
        composer.disabled = True
        self._append_user(text)
        self.run_worker(lambda: self._turn(text), thread=True, exclusive=True)

    def _command(self, name: str, rest: str) -> None:
        if name == "resume" and rest == "":
            self.push_screen(ResumeScreen(list_sessions()), self._on_resume)
            return
        if name == "resume":
            self._note("Usage: /resume")
            return
        shown = f"/{name}" if name else "/"
        self._note(f"Unknown command: {shown}")

    def _note(self, text: str) -> None:
        transcript = self.query_one("#transcript")
        transcript.mount(Static(text, markup=False))
        transcript.scroll_end(animate=False)

    async def _on_resume(self, chosen) -> None:
        composer = self.query_one(Composer)
        if chosen is None:
            composer.focus()
            return
        try:
            self.session.resume(chosen.workspace)
        except ValueError as exc:
            self._note(str(exc))
            composer.focus()
            return
        self.latest = None
        self.tools = None
        self.query_one("#banner", Static).update(self._banner())
        await self.query_one("#transcript").remove_children()
        self._render_history()
        composer.disabled = False
        composer.focus()

    def _render_history(self) -> None:
        messages = getattr(self.session, "messages", None) or []
        for turn in conversation_turns(messages):
            self._append_user(turn.user)
            for name, args in turn.tools:
                self._append_tool(name, args)
            self._finish(turn.answer, turn.reasoning, refocus=False)

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

    def _finish(self, answer: str | None, reasoning: str, refocus: bool = True) -> None:
        transcript = self.query_one("#transcript")
        if reasoning:
            block = Thinking(reasoning, hold=self.review is not None)
            self.latest = block
            transcript.mount(block)
        if answer is not None:
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
