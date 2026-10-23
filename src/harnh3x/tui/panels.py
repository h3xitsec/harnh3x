"""Transcript widgets: thinking, debug, and tool panels, the answer, the composer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.events import Click, Key
from textual.widgets import Markdown, Rule, Static, TextArea
from textual.widgets.markdown import MarkdownStream
from textual.widgets.text_area import Edit, EditResult

from harnh3x.commands import matching_commands
from harnh3x.tui.export import copy_response
from harnh3x.tui.text import (
    _clip,
    _panel_inner_width,
    format_call,
    thinking_hint,
    thinking_overflows,
    thinking_shown,
)


class CollapsiblePanel(Static, can_focus=True):
    """A bordered panel that keeps its last rows until a click expands it.

    Subclasses set ``title`` and return the text from ``body()``; the panel
    wraps it, decides whether it overflows, and keeps the hint in the border
    subtitle current. A subclass whose collapsed text is shortened returns
    the whole text from ``full_body()``, and that difference alone makes the
    panel expandable. Enter or Space toggles a focused panel; a click toggles
    it without taking focus from the composer.
    """

    FOCUS_ON_CLICK = False
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("enter,space", "toggle_panel", "Toggle", show=False),
    ]

    title = ""

    def __init__(self, *, classes: str = "panel trace", hold: bool = False) -> None:
        super().__init__("", markup=False, classes=classes)
        self.expanded = False
        self.hold = hold
        self.plain = ""
        self._expandable = False

    def body(self) -> str:
        raise NotImplementedError

    def full_body(self) -> str:
        return self.body()

    def on_click(self, event: Click) -> None:
        if self.text_selection is not None:
            event.stop()
            return
        # A panel that already fits lets the click reach a parent subagent box.
        if not self._expandable:
            return
        event.stop()
        self.toggle()

    def action_toggle_panel(self) -> None:
        self.toggle()

    def on_mount(self) -> None:
        self._sync(self.size.width or 80)

    def on_resize(self, event) -> None:
        self._sync(event.size.width or 80)

    def toggle(self) -> None:
        if not self._expandable:
            return
        self.expanded = not self.expanded
        self._sync(self.size.width or 80)

    def refresh_body(self) -> None:
        self._sync(self.size.width or 80)

    def _sync(self, width: int) -> None:
        text = self.body()
        full = self.full_body()
        inner = _panel_inner_width(self, width)
        self._expandable = full != text or thinking_overflows(text, width=inner)
        if not self._expandable:
            self.expanded = False
        self.border_title = self.title
        if self._expandable:
            self.border_subtitle = thinking_hint(
                expanded=self.expanded, hold=self.hold, click=True
            )
        elif self.hold:
            self.border_subtitle = "enter to close"
        else:
            self.border_subtitle = ""
        shown = full if self.expanded else text
        self.plain = "\n".join(
            thinking_shown(shown, expanded=self.expanded, width=inner)
        )
        self.update(self.plain)


class Thinking(CollapsiblePanel):
    title = "Thinking"

    def __init__(self, reasoning: str, *, hold: bool) -> None:
        super().__init__(hold=hold)
        self.reasoning = reasoning
        self._sync(80)

    def body(self) -> str:
        return self.reasoning

    def append(self, text: str) -> None:
        self.reasoning += text
        self.refresh_body()


class TurnHeading(Vertical):
    """The model-round label and the rule drawn under it."""

    DEFAULT_CSS = """
    TurnHeading {
        height: auto;
        margin: 1 0 0 0;
    }
    TurnHeading .turn-label {
        color: $text-muted;
        height: 1;
    }
    TurnHeading Rule.-horizontal {
        margin: 0;
        height: 1;
        color: $text-muted;
    }
    """

    def __init__(self, number: int) -> None:
        super().__init__(classes="trace")
        self.number = number

    def compose(self) -> ComposeResult:
        yield Static(f"Turn {self.number}", classes="turn-label", markup=False)
        yield Rule(line_style="solid")


class UserMessage(Vertical):
    """A sent message. The text is Markdown, including code fences."""

    DEFAULT_CSS = """
    UserMessage .you-label {
        text-style: bold;
        height: 1;
    }
    UserMessage Markdown {
        padding: 0;
        margin: 0;
        background: transparent;
    }
    """

    def __init__(self, text: str) -> None:
        self.source = text
        super().__init__(classes="user")

    def compose(self) -> ComposeResult:
        yield Static("You", markup=False, classes="you-label")
        yield Markdown(self.source, open_links=False)


class AnswerIcon(Static, can_focus=True):
    DEFAULT_CSS = """
    AnswerIcon {
        width: 3;
        height: 1;
        content-align: center middle;
        color: $text-muted;
        background: transparent;
    }
    AnswerIcon:hover, AnswerIcon:focus {
        background: $boost;
        color: $text;
    }
    """

    def _answer(self) -> AgentAnswer | None:
        widget = self.parent
        while widget is not None and not isinstance(widget, AgentAnswer):
            widget = widget.parent
        if isinstance(widget, AgentAnswer):
            return widget
        return None


class CopyAnswer(AnswerIcon):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("enter", "copy", "Copy", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__("⧉", markup=False)
        self.tooltip = "Copy"

    def on_click(self, event: Click) -> None:
        event.stop()
        self._copy()

    def action_copy(self) -> None:
        self._copy()

    def _copy(self) -> None:
        answer = self._answer()
        if answer is None:
            return
        copy_response(self.app, answer.source)
        self.app.notify("Copied to the clipboard.", timeout=2)


class CopyWorkspace(AnswerIcon):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("enter", "copy", "Copy", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__("⧉", markup=False)
        self.tooltip = "Copy workspace path"

    def on_click(self, event: Click) -> None:
        event.stop()
        self._copy()

    def action_copy(self) -> None:
        self._copy()

    def _copy(self) -> None:
        copy_response(self.app, str(self.app.session.workspace))
        self.app.notify("Copied to the clipboard.", timeout=2)


class ExportAnswer(AnswerIcon):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("enter", "export", "Export", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__("⤓", markup=False)
        self.tooltip = "Export to markdown"

    def on_click(self, event: Click) -> None:
        event.stop()
        self._export()

    def action_export(self) -> None:
        self._export()

    def _export(self) -> None:
        answer = self._answer()
        if answer is None:
            return
        prompt = getattr(self.app, "_prompt_export_path", None)
        if callable(prompt):
            prompt(answer.source)


class ExportRow(Horizontal):
    DEFAULT_CSS = """
    ExportRow {
        height: 1;
        width: 1fr;
        align: right middle;
    }
    """

    def compose(self) -> ComposeResult:
        yield CopyAnswer()
        yield ExportAnswer()


class AgentAnswer(Vertical):
    def __init__(
        self, source: str, *, stream: bool = False, error: bool = False
    ) -> None:
        # Set before super(), which composes children from this source.
        self.source = source
        self._live = stream
        self._pending = ""
        self._pumping = False
        self._closed = False
        self._stream: MarkdownStream | None = None
        super().__init__(classes="error" if error else "")

    def compose(self) -> ComposeResult:
        yield Static("Agent", markup=False, classes="agent-label")
        # A live answer starts empty. Markdown mounts by replacing every
        # block, so the stream appends instead of rebuilding on each token.
        yield Markdown(None if self._live else self.source, open_links=False)
        yield ExportRow()

    def on_mount(self) -> None:
        if self._live:
            # After the empty document's own mount update, so the stream
            # does not append into a widget that is about to clear itself.
            self.call_after_refresh(self._open_stream)

    def _open_stream(self) -> None:
        if self._closed:
            return
        self._stream = Markdown.get_stream(self.query_one(Markdown))
        self._kick()

    def on_unmount(self) -> None:
        self._closed = True
        stream = self._stream
        self._stream = None
        if stream is not None:
            self.app.run_worker(stream.stop)

    def append(self, text: str) -> None:
        if not text or self._closed:
            return
        self.source += text
        self._pending += text
        if self._stream is not None:
            self._kick()

    def _kick(self) -> None:
        if self._pumping or self._closed or self._stream is None:
            return
        self._pumping = True
        self.run_worker(self._pump)

    async def _pump(self) -> None:
        try:
            while self._pending and not self._closed:
                chunk = self._pending
                self._pending = ""
                stream = self._stream
                if stream is None:
                    self._pending = chunk + self._pending
                    return
                try:
                    await stream.write(chunk)
                except RuntimeError:
                    self._pending = chunk + self._pending
                    return
        finally:
            self._pumping = False
            if self._pending and not self._closed and self._stream is not None:
                self._kick()


class DebugBox(CollapsiblePanel):
    def __init__(self, title: str, body: str) -> None:
        super().__init__(classes="panel trace debug")
        self.title = title
        self.source = body
        self._sync(80)

    def body(self) -> str:
        return self.source


TOOL_DETAIL_LIMIT = 120
_TOOL_MARKS = {"running": "…", "ok": "✓", "error": "✗", "stopped": "✗"}


@dataclass
class ToolEntry:
    name: str
    args: object
    state: str | None = None
    detail: str = ""


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


class ToolBox(CollapsiblePanel):
    """One model round's tool calls.

    A live call starts as ``running`` and becomes ``ok`` or ``error`` when its
    result arrives; a replayed call has no state and no mark.
    """

    title = "Tools"

    def __init__(self) -> None:
        super().__init__()
        self.entries: list[ToolEntry] = []
        self._sync(80)

    def body(self) -> str:
        return self._call_lines(clip=True)

    def full_body(self) -> str:
        return self._call_lines(clip=False)

    def _call_lines(self, *, clip: bool) -> str:
        lines = []
        for entry in self.entries:
            if clip:
                call = format_call(entry.name, entry.args)
                detail = _clip(entry.detail, TOOL_DETAIL_LIMIT)
            else:
                call = format_call(entry.name, entry.args, limit=None)
                detail = entry.detail
            mark = _TOOL_MARKS.get(entry.state or "")
            lines.append(f"{mark} {call}" if mark else call)
            if detail:
                lines.append(f"  {detail}")
        return "\n".join(lines)

    def add(self, name: str, args: object, *, state: str | None = None) -> None:
        self.entries.append(ToolEntry(name, args, state))
        self.refresh_body()

    def finish(self, name: str, result: object) -> None:
        for entry in reversed(self.entries):
            if entry.state == "running" and entry.name == name:
                text = result if isinstance(result, str) else str(result)
                if text.lstrip().startswith("Error"):
                    entry.state = "error"
                    entry.detail = _first_line(text)
                else:
                    entry.state = "ok"
                self.refresh_body()
                return

    def settle(self) -> None:
        """Drop the mark on calls whose result never arrived."""
        changed = False
        for entry in self.entries:
            if entry.state == "running":
                entry.state = None
                changed = True
        if changed:
            self.refresh_body()

    def stop_running(self) -> None:
        changed = False
        for entry in self.entries:
            if entry.state == "running":
                entry.state = "stopped"
                entry.detail = "Stopped."
                changed = True
        if changed:
            self.refresh_body()


class SubagentBox(Vertical, can_focus=True):
    """One subagent's turns, thinking, and tool calls, kept out of the parent.

    A turn heading is mounted only when that round has thinking or a tool
    call, so a final answer that the parent receives as the task result does
    not leave an empty heading. The box keeps the last three lines until a
    click expands it, and Enter or Space toggles it while it is focused.
    """

    FOCUS_ON_CLICK = False
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("enter,space", "toggle_panel", "Toggle", show=False),
    ]

    DEFAULT_CSS = """
    SubagentBox {
        border: round $primary;
        border-title-color: $primary;
        border-subtitle-color: $primary;
        padding: 0 1;
        margin: 1 0 1 2;
        height: auto;
        color: $text-muted;
    }
    SubagentBox:focus {
        border: round $accent;
    }
    SubagentBox .subagent-preview {
        height: auto;
    }
    """

    def __init__(self, kind: str, description: str) -> None:
        super().__init__(classes="panel trace subagent")
        self.kind = kind
        self.description = description
        self.border_title = _subagent_title(kind, description)
        self.expanded = False
        self._expandable = False
        self._syncing = False
        self._preview = Static("", markup=False, classes="subagent-preview")
        self._pending_turn: int | None = None
        self._open_turn: int | None = None
        self._thinking: Thinking | None = None
        self._tools: ToolBox | None = None
        self._reasoning_open = False
        self._tools_open = False

    def note_turn(self, number: int) -> None:
        self._pending_turn = number

    def append_reasoning(self, text: str) -> None:
        if not text:
            return
        self._open_round()
        if not self._reasoning_open:
            self._reasoning_open = True
            self._tools_open = False
            self._thinking = Thinking("", hold=False)
            self.mount(self._thinking)
        assert self._thinking is not None
        self._thinking.append(text)
        self._sync(self.size.width or 80)

    def add_tool(
        self, name: str, args: object, *, state: str | None = "running"
    ) -> None:
        self._open_round()
        if not self._tools_open or self._tools is None:
            self._tools_open = True
            self._reasoning_open = False
            self._tools = ToolBox()
            self.mount(self._tools)
        self._tools.add(name, args, state=state)
        self._sync(self.size.width or 80)

    def finish_tool(self, name: str, result: str) -> None:
        if self._tools is not None:
            self._tools.finish(name, result)
            self._sync(self.size.width or 80)

    def on_mount(self) -> None:
        self.mount(self._preview, before=0)
        self._sync(self.size.width or 80)

    def on_resize(self, event) -> None:
        self._sync(event.size.width or 80)

    def on_click(self, event: Click) -> None:
        event.stop()
        target = event.widget
        if self.text_selection is not None or (
            target is not None and target.text_selection is not None
        ):
            return
        self.toggle()

    def action_toggle_panel(self) -> None:
        self.toggle()

    def toggle(self) -> None:
        if not self._expandable:
            return
        self.expanded = not self.expanded
        self._sync(self.size.width or 80)

    def body_text(self) -> str:
        trace = self._trace()
        if not trace:
            return self.border_title
        return f"{self.border_title}\n{trace}"

    def _trace(self) -> str:
        parts: list[str] = []
        for child in self.children:
            if isinstance(child, TurnHeading):
                parts.append(f"Turn {child.number}")
            elif isinstance(child, (Thinking, ToolBox)) and child.plain:
                parts.append(child.plain)
        return "\n".join(parts)

    def _sync(self, width: int) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            text = self._trace()
            inner = _panel_inner_width(self, width)
            self._expandable = thinking_overflows(text, width=inner)
            if not self._expandable:
                self.expanded = False
            if self._expandable:
                self.border_subtitle = thinking_hint(
                    expanded=self.expanded, hold=False, click=True
                )
            else:
                self.border_subtitle = ""
            show_preview = self._expandable and not self.expanded
            if self._preview.parent is self:
                self._preview.display = show_preview
                if show_preview:
                    shown = thinking_shown(text, expanded=False, width=inner)
                    self._preview.update("\n".join(shown))
            for child in list(self.children):
                if child is self._preview:
                    continue
                child.display = not show_preview
        finally:
            self._syncing = False

    def _open_round(self) -> None:
        number = self._pending_turn
        if number is None or number == self._open_turn:
            return
        self._open_turn = number
        self._thinking = None
        self._tools = None
        self._reasoning_open = False
        self._tools_open = False
        self.mount(TurnHeading(number))


def _subagent_title(kind: str, description: str) -> str:
    text = " ".join(description.split())
    if not text:
        return kind
    title = f"{kind} · {text}"
    if len(title) <= 60:
        return title
    return title[:59] + "…"


class PromptBox(Vertical):
    """The message editor: a tall border and a prompt mark.

    The border is clear until the editor is focused, then it uses the
    secondary color. The mark is ``❯`` on one line and ``☰`` once the draft
    wraps to another.
    """

    DEFAULT_CSS = """
    PromptBox {
        height: auto;
        margin: 0 1;
    }
    #prompt-container {
        height: auto;
        border: tall transparent;
        background: black 10%;
        padding: 0 1 0 0;
    }
    #prompt-container:focus-within {
        border: tall $secondary;
    }
    #prompt-mark {
        width: auto;
        height: 1;
        padding: 0 1 0 1;
        color: $text-muted;
    }
    PromptBox #composer {
        width: 1fr;
        height: auto;
        min-height: 1;
        max-height: 10;
        margin: 0;
        padding: 0 1 0 0;
        background: transparent;
        border: none;
        text-opacity: 50%;
    }
    PromptBox #composer:focus {
        text-opacity: 100%;
    }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="prompt-container"):
            yield Static("❯", id="prompt-mark", markup=False)
            yield Composer()

    def set_multiline(self, multiline: bool) -> None:
        self.query_one("#prompt-mark", Static).update("☰" if multiline else "❯")

    def on_click(self, event: Click) -> None:
        composer = self.query_one(Composer)
        if event.widget is composer:
            return
        composer.focus()


class Composer(TextArea):
    """The message box: Enter sends, Shift+Enter or Ctrl+J adds a newline.

    The box grows with the draft until it reaches its height, then scrolls so
    the cursor stays on screen. Up on the first row and Down on the last row
    walk ``prompts`` (TextArea already uses ``history`` for undo); Tab
    completes a slash command while one matches. In a coding session, an open
    ``@`` list takes Up, Down, Tab, and Enter. Right enters the selected
    directory, Left returns to its parent, and Escape closes that list.
    """

    def __init__(self) -> None:
        super().__init__(
            id="composer",
            placeholder="How can I help?",
        )
        self.prompts: list[str] = []
        self._prompt_index: int | None = None
        self._prompt_draft = ""

    def edit(self, edit: Edit) -> EditResult:
        # The cursor moves before the new line is in the scrollable height,
        # so the first scroll stops one row short. Scroll again once the
        # height includes that line.
        result = super().edit(edit)
        self.scroll_cursor_visible()
        return result

    def set_prompts(self, items: list[str]) -> None:
        self.prompts = list(items)
        self._prompt_index = None

    def remember(self, text: str) -> None:
        if text and (not self.prompts or self.prompts[-1] != text):
            self.prompts.append(text)
        self._prompt_index = None

    def _load(self, text: str) -> None:
        self.load_text(text)
        self.move_cursor(self.document.end)

    def place(self, text: str, cursor: int) -> None:
        """Replace the draft and leave the cursor at a codepoint index."""
        self.load_text(text)
        self.move_cursor(self.document.get_location_from_index(cursor))
        self._prompt_index = None

    def _recall(self, step: int) -> bool:
        if not self.prompts:
            return False
        if self._prompt_index is None:
            if step > 0:
                return False
            self._prompt_draft = self.text
            index = len(self.prompts) - 1
        else:
            index = self._prompt_index + step
        if index < 0:
            return True
        if index >= len(self.prompts):
            self._prompt_index = None
            self._load(self._prompt_draft)
            return True
        self._prompt_index = index
        self._load(self.prompts[index])
        return True

    def _complete_command(self) -> bool:
        matches = matching_commands(self.text)
        if not matches:
            return False
        command = matches[0]
        text = f"/{command.name}" + (" " if command.takes_argument else "")
        self._load(text)
        return True

    def _mention_app(self):
        app = self.app
        if callable(getattr(app, "mention_open", None)):
            return app
        return None

    async def _on_key(self, event: Key) -> None:
        handled = False
        mentions = self._mention_app()
        if mentions is not None and mentions.mention_open():
            if event.key == "escape":
                handled = mentions.mention_dismiss()
            elif event.key in ("enter", "tab"):
                handled = mentions.mention_accept()
            elif event.key == "up":
                handled = mentions.mention_move(-1)
            elif event.key == "down":
                handled = mentions.mention_move(1)
            elif event.key == "right":
                handled = mentions.mention_into()
            elif event.key == "left":
                handled = mentions.mention_back()
        elif mentions is not None and event.key == "left":
            handled = mentions.mention_back()
        if not handled and event.key == "enter":
            self.app.action_submit()
            handled = True
        elif not handled and event.key in ("shift+enter", "ctrl+j"):
            self.insert("\n")
            handled = True
        elif not handled and event.key == "up" and self.cursor_location[0] == 0:
            handled = self._recall(-1)
        elif (
            not handled
            and event.key == "down"
            and self.cursor_location[0] == self.document.line_count - 1
        ):
            handled = self._recall(1)
        elif not handled and event.key == "tab":
            handled = self._complete_command()
        if handled:
            event.stop()
            event.prevent_default()
            return
        if event.key not in ("up", "down"):
            self._prompt_index = None
        await super()._on_key(event)
        if mentions is not None:
            mentions._refresh_mentions(self)
