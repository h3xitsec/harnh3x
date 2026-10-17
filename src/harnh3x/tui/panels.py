"""Transcript widgets: thinking, debug, and tool panels, the answer, the composer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.events import Click, Key
from textual.widgets import Markdown, Static, TextArea
from textual.widgets.markdown import MarkdownStream

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
        event.stop()
        if self.text_selection is not None:
            return
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


class UserMessage(Static):
    def __init__(self, text: str) -> None:
        self.source = text
        super().__init__(Content.assemble(("You", "bold"), "\n", text), classes="user")


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


class Composer(TextArea):
    """The message box: Enter sends, Shift+Enter or Ctrl+J adds a newline.

    Up on the first row and Down on the last row walk ``prompts`` (TextArea
    already uses ``history`` for undo); Tab completes a slash command while
    one matches.
    """

    def __init__(self) -> None:
        super().__init__(
            id="composer",
            placeholder="Message · Enter to send · Shift+Enter for newline · /help",
        )
        self.prompts: list[str] = []
        self._prompt_index: int | None = None
        self._prompt_draft = ""

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

    async def _on_key(self, event: Key) -> None:
        handled = False
        if event.key == "enter":
            self.app.action_submit()
            handled = True
        elif event.key in ("shift+enter", "ctrl+j"):
            self.insert("\n")
            handled = True
        elif event.key == "up" and self.cursor_location[0] == 0:
            handled = self._recall(-1)
        elif (
            event.key == "down"
            and self.cursor_location[0] == self.document.line_count - 1
        ):
            handled = self._recall(1)
        elif event.key == "tab":
            handled = self._complete_command()
        if handled:
            event.stop()
            event.prevent_default()
            return
        if event.key not in ("up", "down"):
            self._prompt_index = None
        await super()._on_key(event)
