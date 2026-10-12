from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.events import Click, Key
from textual.screen import ModalScreen, Screen
from textual.content import Content
from textual.widgets import Markdown, OptionList, Static, TextArea

from harnh3x import __version__
from harnh3x.agent import (
    _is_compact_note,
    _visible_answer,
    compact_session,
    list_sessions,
    model_context_limit,
    parse_command,
    session_label,
)

THINKING_ROWS = 3

YOLO_TITLE = "YOLO — sandbox off"
YOLO_PLAIN = "YOLO — sandbox off. This session can read any file and run any command, including sudo."

_TUI_HELP = """\
Commands
  /help     show commands and hotkeys
  /resume   resume a past session
  /compact  compact context
  /exit     quit

Hotkeys
  Shift+Enter  new line
  Ctrl+N       new session
  Ctrl+H       hide or show thinking, tool calls, debug, and compaction boxes
  Ctrl+Q       quit"""


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


def thinking_hint(*, expanded: bool, hold: bool, click: bool = False) -> str:
    parts = []
    if click:
        action = "collapse" if expanded else "expand"
        parts.append(f"click to {action}")
    if hold:
        parts.append("enter to close")
    return " · ".join(parts)


def _thinking_wrapped(reasoning: str, *, width: int) -> tuple[list[str], str]:
    limit = line_limit(width)
    indent = "  " if limit > 2 else ""
    body_limit = max(limit - len(indent), 1)
    return wrap_lines(reasoning, body_limit), indent


def thinking_overflows(reasoning: str, *, width: int) -> bool:
    wrapped, _indent = _thinking_wrapped(reasoning, width=width)
    return len(wrapped) > THINKING_ROWS


def thinking_shown(reasoning: str, *, expanded: bool, width: int) -> list[str]:
    wrapped, indent = _thinking_wrapped(reasoning, width=width)
    shown = wrapped if expanded else wrapped[-THINKING_ROWS:]
    return [f"{indent}{line}" for line in shown]


def thinking_text(reasoning: str, *, expanded: bool, width: int, hold: bool) -> str:
    hint = thinking_hint(expanded=expanded, hold=hold)
    title = f"Thinking · {hint}" if hint else "Thinking"
    return debug_text(title, reasoning, expanded=expanded, width=width)


def debug_text(title: str, body: str, *, expanded: bool, width: int) -> str:
    limit = line_limit(width)
    header = "\n".join(wrap_lines(title, limit))
    shown = "\n".join(thinking_shown(body, expanded=expanded, width=width))
    return f"{header}\n{shown}"


def context_line(used: int | None, limit: int | None) -> str | None:
    if used is None and limit is None:
        return None
    used_text = f"{used:,}" if used is not None else "—"
    if limit is None:
        return f"Context: {used_text}"
    return f"Context: {used_text} / {limit:,}"


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
    steps: tuple = ()


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


@dataclass(frozen=True)
class Compaction:
    summary: str
    before: int | None = None
    after: int | None = None


def _token_count(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def compact_summary(message: object) -> str | None:
    if not _is_compact_note(message):
        return None
    content = message.get("content") if isinstance(message, dict) else ""
    if not isinstance(content, str):
        return ""
    rest = content.split("\n", 1)[1] if "\n" in content else ""
    return rest.strip()


def compact_change(before: int | None, after: int | None) -> str | None:
    if before is None or after is None:
        return None
    return f"{before:,} → {after:,}"


def _static_text(widget: Static) -> str:
    body = str(widget.content)
    title = str(widget.border_title or "").strip()
    subtitle = str(widget.border_subtitle or "").strip()
    if not title:
        return body
    header = f"{title}  {subtitle}" if subtitle else title
    if body:
        return f"{header}\n{body}"
    return header


def compact_label(compaction: Compaction) -> str:
    header = "Context compacted"
    change = compact_change(compaction.before, compaction.after)
    if change is not None:
        header = f"{header}  {change}"
    if compaction.summary:
        return f"{header}\n{compaction.summary}"
    return header


def _call_steps(message: dict) -> tuple:
    calls = []
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str):
            name = ""
        calls.append((name, displayed_args(function.get("arguments"))))
    return tuple(calls)


def _assistant_steps(message: dict, answer: str | None, *, is_final: bool) -> list:
    reasoning = message.get("reasoning")
    if not isinstance(reasoning, str):
        reasoning = ""
    content = message.get("content")
    if not isinstance(content, str):
        content = ""
    calls = _call_steps(message)
    steps = []
    if reasoning and (content or calls or reasoning != (answer or "")):
        steps.append(("thinking", reasoning))
    if content:
        steps.append(("answer", content))
    elif not calls and reasoning and reasoning == (answer or ""):
        steps.append(("answer", reasoning))
    elif is_final and not calls and not reasoning and not content and answer:
        steps.append(("answer", answer))
    if calls:
        steps.append(("tools", calls))
    return steps


def _span_steps(span: list, answer: str | None) -> list:
    final = None
    for message in span:
        if (
            isinstance(message, dict)
            and message.get("role") == "assistant"
            and not _call_steps(message)
        ):
            final = message
    steps = []
    for message in span:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        steps.extend(_assistant_steps(message, answer, is_final=message is final))
    return steps


def transcript_items(messages: list) -> list[tuple]:
    items: list[tuple] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        summary = compact_summary(message)
        if summary is not None and isinstance(message, dict):
            items.append(
                (
                    "compact",
                    Compaction(
                        summary,
                        _token_count(message.get("context_before")),
                        _token_count(message.get("context_after")),
                    ),
                )
            )
            index += 1
            continue
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
        items.append(
            (
                "turn",
                Turn(
                    user,
                    _turn_tools(span),
                    reasoning,
                    answer,
                    tuple(_span_steps(span, answer)),
                ),
            )
        )
    return items


def conversation_turns(messages: list) -> list[Turn]:
    return [item for kind, item in transcript_items(messages) if kind == "turn"]


def _panel_inner_width(widget: Static, outer: int) -> int:
    gutter = widget.styles.gutter
    horizontal = gutter.left + gutter.right
    if horizontal == 0:
        horizontal = 4
    return max(outer - horizontal, 1)


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
        super().__init__("", markup=False, classes="panel trace")
        self.reasoning = reasoning
        self.expanded = False
        self.hold = hold
        self.plain = ""
        self._sync(80)

    def on_mount(self) -> None:
        self._sync(self.size.width or 80)

    def on_resize(self, event) -> None:
        self._sync(event.size.width or 80)

    def on_click(self, event: Click) -> None:
        event.stop()
        self.toggle()

    def toggle(self) -> None:
        if not self._expandable:
            return
        self.expanded = not self.expanded
        self._sync(self.size.width or 80)

    def append(self, text: str) -> None:
        self.reasoning += text
        self._sync(self.size.width or 80)

    def _sync(self, width: int) -> None:
        inner = _panel_inner_width(self, width)
        self._expandable = thinking_overflows(self.reasoning, width=inner)
        if not self._expandable:
            self.expanded = False
        self.border_title = "Thinking"
        if self._expandable:
            self.border_subtitle = thinking_hint(
                expanded=self.expanded, hold=self.hold, click=True
            )
        elif self.hold:
            self.border_subtitle = "enter to close"
        else:
            self.border_subtitle = ""
        self.plain = "\n".join(
            thinking_shown(
                self.reasoning,
                expanded=self.expanded,
                width=inner,
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

    def on_mount(self) -> None:
        self._refresh()

    def append(self, text: str) -> None:
        self.source += text
        self._refresh()

    def _refresh(self) -> None:
        found = self.query(Markdown)
        if found:
            found.first().update(self.source)


class DebugBox(Static):
    def __init__(self, title: str, body: str) -> None:
        super().__init__("", markup=False, classes="panel trace debug")
        self.title = title
        self.body = body
        self.expanded = False
        self._sync(80)

    def on_mount(self) -> None:
        self._sync(self.size.width or 80)

    def on_resize(self, event) -> None:
        self._sync(event.size.width or 80)

    def on_click(self, event: Click) -> None:
        event.stop()
        self.toggle()

    def toggle(self) -> None:
        if not self._expandable:
            return
        self.expanded = not self.expanded
        self._sync(self.size.width or 80)

    def _sync(self, width: int) -> None:
        inner = _panel_inner_width(self, width)
        self._expandable = thinking_overflows(self.body, width=inner)
        if not self._expandable:
            self.expanded = False
        self.border_title = self.title
        if self._expandable:
            self.border_subtitle = thinking_hint(
                expanded=self.expanded, hold=False, click=True
            )
        else:
            self.border_subtitle = ""
        self.update(
            "\n".join(thinking_shown(self.body, expanded=self.expanded, width=inner))
        )


class ToolBox(Static):
    def __init__(self) -> None:
        super().__init__("", markup=False, classes="panel trace")
        self.lines: list[str] = []
        self.expanded = False
        self._sync(80)

    def on_mount(self) -> None:
        self._sync(self.size.width or 80)

    def on_resize(self, event) -> None:
        self._sync(event.size.width or 80)

    def on_click(self, event: Click) -> None:
        event.stop()
        self.toggle()

    def toggle(self) -> None:
        if not self._expandable:
            return
        self.expanded = not self.expanded
        self._sync(self.size.width or 80)

    def add(self, name: str, args: object) -> None:
        self.lines.append(f"🔧 {name}({args})")
        self._sync(self.size.width or 80)

    def _sync(self, width: int) -> None:
        text = "\n".join(self.lines)
        inner = _panel_inner_width(self, width)
        self._expandable = thinking_overflows(text, width=inner)
        if not self._expandable:
            self.expanded = False
        self.border_title = "Tools"
        if self._expandable:
            self.border_subtitle = thinking_hint(
                expanded=self.expanded, hold=False, click=True
            )
        else:
            self.border_subtitle = ""
        self.update(
            "\n".join(thinking_shown(text, expanded=self.expanded, width=inner))
        )


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


class HelpScreen(ModalScreen):
    BINDINGS = [
        Binding("escape", "close", "Close", priority=True),
    ]
    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    #help-box {
        width: auto;
        height: auto;
        border: round ansi_bright_black;
        padding: 1 2;
        background: $surface;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(_TUI_HELP, id="help-box", markup=False)

    def on_mount(self) -> None:
        self.query_one("#help-box", Static).border_title = "Help"

    def action_close(self) -> None:
        self.dismiss()


class HarnessApp(App):
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("ctrl+h", "toggle_trace", "Details", priority=True),
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
    #transcript.details-hidden .trace {
        display: none;
    }
    .panel.debug {
        color: ansi_cyan;
        border: round ansi_cyan;
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
        context_limit: Callable[[str], int | None] | None = None,
        initial: str | None = None,
    ) -> None:
        super().__init__()
        self.session = session
        self._run = run
        self.show_tools = show_tools
        self.review = review
        self.initial = initial
        self._context_limit = context_limit
        self._shown_compactions: list[Compaction] = []
        self._compacting: Static | None = None
        self._rendering = False
        self.tools: ToolBox | None = None
        self._hold_close = False
        self._reset_live()

    def _yolo(self) -> bool:
        return bool(getattr(self.session, "yolo", False))

    def _banner(self) -> Content:
        lines = []
        if self.review is None:
            lines.append(f"🤖 Harnh3x {__version__}")
        lines.append(f"Workspace: {self.session.workspace}")
        project = getattr(self.session, "project", None)
        if project:
            lines.append(f"Project: {project}")
        if getattr(self.session, "coding", False):
            lines.append("Coding")
        if getattr(self.session, "debug", False):
            lines.append("Debug")
        lines.append(f"Model: {self.session.model}")
        context = context_line(
            getattr(self.session, "context_tokens", None),
            getattr(self.session, "context_limit", None),
        )
        if context is not None:
            lines.append(context)
        body = Content("\n".join(lines))
        if not self._yolo():
            return body
        title = Content.styled(YOLO_TITLE, "bold ansi_bright_yellow")
        return title + "\n" + body

    def _refresh_banner(self) -> None:
        self.query_one("#banner", Static).update(self._banner())

    def _load_context_limit(self) -> None:
        if self._context_limit is None or self.review is not None:
            return
        model = self.session.model

        def fetch() -> None:
            limit = self._context_limit(model)
            self.call_from_thread(self._apply_limit, model, limit)

        self.run_worker(fetch, thread=True, group="context-limit", exclusive=True)

    def _apply_limit(self, model: str, limit: int | None) -> None:
        if (
            self.session.model != model
            or isinstance(limit, bool)
            or not isinstance(limit, int)
        ):
            return
        self.session.context_limit = limit
        self._refresh_banner()

    def compose(self) -> ComposeResult:
        yield Static(self._banner(), id="banner", markup=False)
        yield VerticalScroll(id="transcript")
        if self.review is None and self.initial is None:
            yield Composer()

    def on_mount(self) -> None:
        self._load_context_limit()
        if self.review is not None:
            user, tools, reasoning, answer = self.review
            self._append_user(user)
            for name, args in tools:
                self._append_tool(name, args)
            self._finish(answer, reasoning, refocus=False)
            return
        self.session.on_delta = self._queue_delta
        self.session.on_compact = self._on_compact
        self.session.on_debug = self._queue_debug
        if self.initial:
            self._append_user(self.initial)
            self.run_worker(
                lambda: self._turn(self.initial), thread=True, exclusive=True
            )
            return
        self.query_one(Composer).focus()

    def action_toggle_trace(self) -> None:
        transcript = self.query_one("#transcript")
        transcript.set_class(
            "details-hidden" not in transcript.classes, "details-hidden"
        )

    async def action_new_session(self) -> None:
        if (
            self.review is not None
            or self.initial is not None
            or self._run is None
            or len(self.screen_stack) > 1
        ):
            return
        composer = self.query_one(Composer)
        if composer.disabled:
            return
        self.session.reset()
        self.query_one("#banner", Static).update(self._banner())
        self.tools = None
        self._shown_compactions = []
        self._compacting = None
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
        if name == "help" and rest == "":
            self.push_screen(HelpScreen())
            return
        if name == "help":
            self._note("Usage: /help")
            return
        if name == "resume" and rest == "":
            self.push_screen(ResumeScreen(list_sessions()), self._on_resume)
            return
        if name == "resume":
            self._note("Usage: /resume")
            return
        if name == "compact" and rest == "":
            composer = self.query_one(Composer)
            composer.disabled = True
            self.run_worker(self._compact_now, thread=True, exclusive=True)
            return
        if name == "compact":
            self._note("Usage: /compact")
            return
        if name == "exit" and rest == "":
            self.exit()
            return
        if name == "exit":
            self._note("Usage: /exit")
            return
        shown = f"/{name}" if name else "/"
        self._note(f"Unknown command: {shown}")

    def _on_compact(self, phase: str) -> None:
        if phase == "start":
            self.call_from_thread(self._show_compacting)
        else:
            self.call_from_thread(self._hide_compacting)

    def _show_compacting(self) -> None:
        if self._compacting is not None:
            return
        block = Static("Compacting context…", markup=False, classes="panel trace")
        block.border_title = "Context"
        self._compacting = block
        transcript = self.query_one("#transcript")
        transcript.mount(block)
        transcript.scroll_end(animate=False)

    def _hide_compacting(self) -> None:
        block = self._compacting
        self._compacting = None
        if block is not None and block.parent is not None:
            block.remove()

    def _compact_now(self) -> None:
        status = compact_session(self.session)
        self.call_from_thread(self._after_compact, status)

    def _after_compact(self, status: str) -> None:
        self._mount_new_compactions()
        self._refresh_banner()
        if status != "Context compacted.":
            self._note(status)
        composer = self.query_one(Composer)
        composer.disabled = False
        composer.focus()

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
        self.tools = None
        self.query_one("#banner", Static).update(self._banner())
        await self.query_one("#transcript").remove_children()
        self._render_history()
        self._load_context_limit()
        composer.disabled = False
        composer.focus()

    def _render_history(self) -> None:
        messages = getattr(self.session, "messages", None) or []
        self._rendering = True
        notes: list[Compaction] = []
        try:
            for kind, item in transcript_items(messages):
                if kind == "compact":
                    notes.append(item)
                    self._mount_compact(item)
                    continue
                self._append_user(item.user)
                self._render_steps(item.steps)
        finally:
            self._shown_compactions = notes
            self._rendering = False

    def _mount_compact(self, compaction: Compaction) -> None:
        block = Static(compaction.summary, markup=False, classes="panel trace")
        block.border_title = "Context compacted"
        change = compact_change(compaction.before, compaction.after)
        if change is not None:
            block.border_subtitle = change
        transcript = self.query_one("#transcript")
        transcript.mount(block)
        transcript.scroll_end(animate=False)

    def _mount_new_compactions(self) -> None:
        if self._rendering:
            return
        messages = getattr(self.session, "messages", None) or []
        notes = [item for kind, item in transcript_items(messages) if kind == "compact"]
        if notes == self._shown_compactions:
            return
        for compaction in notes:
            if compaction not in self._shown_compactions:
                self._mount_compact(compaction)
        self._shown_compactions = notes

    def action_close(self) -> None:
        if self._hold_close or self.review is not None:
            self.exit()

    def transcript_plain(self) -> str:
        parts = []
        for child in self.query_one("#transcript").children:
            if isinstance(child, AgentAnswer):
                parts.append(f"Agent: {child.source}")
            elif isinstance(child, Thinking):
                parts.append(child.plain)
            elif isinstance(child, Static):
                parts.append(_static_text(child))
        return "\n".join(parts)

    def _reset_live(self) -> None:
        self._streamed = False
        self._tools_open = False
        self._reasoning_open = False
        self._content_open = False
        self._msg_reasoning = ""
        self._msg_content = ""
        self._live_thinking: Thinking | None = None
        self._live_answer: AgentAnswer | None = None

    def _queue_debug(self, system: str | None, request: str) -> None:
        self.call_from_thread(self._show_debug, system, request)

    def _show_debug(self, system: str | None, request: str) -> None:
        transcript = self.query_one("#transcript")
        if system is not None:
            transcript.mount(DebugBox("System", system))
        transcript.mount(DebugBox("Request", request))

    def _queue_delta(self, kind: str, text: str) -> None:
        self.call_from_thread(self._apply_delta, kind, text)

    def _apply_delta(self, kind: str, text: str) -> None:
        if not text:
            return
        self._streamed = True
        self._tools_open = False
        transcript = self.query_one("#transcript")
        if kind == "reasoning":
            if not self._reasoning_open:
                self._reasoning_open = True
                if not self._content_open:
                    self._msg_reasoning = ""
                    self._msg_content = ""
                block = Thinking("", hold=False)
                self._live_thinking = block
                transcript.mount(block)
            assert self._live_thinking is not None
            self._msg_reasoning += text
            self._live_thinking.append(text)
        elif kind == "content":
            if not self._content_open:
                self._content_open = True
                self._msg_content = ""
                block = AgentAnswer("")
                self._live_answer = block
                transcript.mount(block)
            assert self._live_answer is not None
            self._msg_content += text
            self._live_answer.append(text)
        transcript.scroll_end(animate=False)

    def _render_steps(self, steps: tuple) -> None:
        transcript = self.query_one("#transcript")
        for kind, payload in steps:
            if kind == "thinking":
                transcript.mount(Thinking(payload, hold=False))
            elif kind == "answer":
                transcript.mount(AgentAnswer(payload))
            elif kind == "tools":
                self.tools = None
                for name, args in payload:
                    self._append_tool(name, args)
        transcript.scroll_end(animate=False)

    def _turn(self, text: str) -> None:
        start = len(getattr(self.session, "messages", []) or [])
        self._reset_live()

        def on_tool(name: str, args: object) -> None:
            self.call_from_thread(self._append_tool, name, args)

        assert self._run is not None
        answer = self._run(
            text,
            self.session,
            show_tools=self.show_tools,
            on_tool=on_tool if self.show_tools else None,
        )
        self.call_from_thread(self._after_turn, answer, start)

    def _append_user(self, text: str) -> None:
        self.tools = None
        self.query_one("#transcript").mount(Static(f"You: {text}", markup=False))

    def _append_tool(self, name: str, args: object) -> None:
        self._mount_new_compactions()
        if not self._rendering and not self._tools_open:
            self.tools = None
            self._tools_open = True
            self._reasoning_open = False
            self._content_open = False
            self._msg_reasoning = ""
            self._msg_content = ""
            self._live_thinking = None
            self._live_answer = None
        transcript = self.query_one("#transcript")
        if self.tools is None:
            self.tools = ToolBox()
            transcript.mount(self.tools)
        self.tools.add(name, args)
        transcript.scroll_end(animate=False)
        self._refresh_banner()

    def _finish(self, answer: str | None, reasoning: str, refocus: bool = True) -> None:
        self._mount_new_compactions()
        transcript = self.query_one("#transcript")
        if reasoning:
            transcript.mount(Thinking(reasoning, hold=self.review is not None))
        if answer is not None:
            transcript.mount(AgentAnswer(answer))
        transcript.scroll_end(animate=False)
        self._refresh_banner()
        if refocus and self.review is None and self.initial is None:
            composer = self.query_one(Composer)
            composer.disabled = False
            composer.focus()

    def _after_turn(self, answer: str, start: int) -> None:
        if not self._streamed:
            reasoning = ""
            if self.show_tools:
                reasoning = reply_reasoning(self.session, start, answer)
            self._finish(answer, reasoning, refocus=self.initial is None)
            self._maybe_hold(reasoning)
            return
        transcript = self.query_one("#transcript")
        self._mount_new_compactions()
        promoted = (
            not self._msg_content
            and bool(self._msg_reasoning)
            and answer == self._msg_reasoning
            and self._live_thinking is not None
            and self._live_thinking.parent is not None
        )
        if promoted:
            self._live_thinking.remove()
            self._live_thinking = None
            transcript.mount(AgentAnswer(answer))
        elif answer and answer != self._msg_content:
            transcript.mount(AgentAnswer(answer))
        transcript.scroll_end(animate=False)
        self._refresh_banner()
        if self.initial is None and self.review is None:
            composer = self.query_one(Composer)
            composer.disabled = False
            composer.focus()
        held = bool(list(self.query(Thinking)))
        self._maybe_hold("shown" if held else "")

    def _maybe_hold(self, reasoning: str) -> None:
        if self.initial is None:
            return
        if reasoning:
            self._hold_close = True
            for block in self.query(Thinking):
                block.hold = True
                block._sync(block.size.width or 80)
            return
        self.exit()


class OneShotApp(HarnessApp):
    BINDINGS = [
        *HarnessApp.BINDINGS,
        Binding("enter", "close", "Close", priority=True),
    ]


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


def run_interactive(
    session, run: Callable, *, show_tools: bool, initial: str | None = None
) -> None:
    app_cls = OneShotApp if initial else HarnessApp
    app_cls(
        session,
        run,
        show_tools=show_tools,
        context_limit=model_context_limit,
        initial=initial,
    ).run()


def run_review(
    session,
    user: str,
    tools: list[tuple[str, object]],
    reasoning: str,
    answer: str,
) -> None:
    ReviewApp(session, user, tools, reasoning, answer).run()
