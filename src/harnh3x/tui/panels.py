"""Transcript widgets: thinking, debug, and tool panels, the answer, the composer."""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.events import Click, Key
from textual.widgets import Markdown, Static, TextArea
from textual.widgets.markdown import MarkdownStream

from harnh3x.tui.export import copy_response
from harnh3x.tui.text import (
    _panel_inner_width,
    thinking_hint,
    thinking_overflows,
    thinking_shown,
)


class CollapsiblePanel(Static):
    """A bordered panel that keeps its last rows until a click expands it.

    Subclasses set ``title`` and return the full text from ``body()``; the
    panel wraps it, decides whether it overflows, and keeps the hint in the
    border subtitle current.
    """

    title = ""

    def __init__(self, *, classes: str = "panel trace", hold: bool = False) -> None:
        super().__init__("", markup=False, classes=classes)
        self.expanded = False
        self.hold = hold
        self.plain = ""
        self._expandable = False

    def body(self) -> str:
        raise NotImplementedError

    def on_click(self, event: Click) -> None:
        event.stop()
        if self.text_selection is not None:
            return
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
        inner = _panel_inner_width(self, width)
        self._expandable = thinking_overflows(text, width=inner)
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
        self.plain = "\n".join(
            thinking_shown(text, expanded=self.expanded, width=inner)
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
    def __init__(self, source: str, *, stream: bool = False) -> None:
        # Set before super(), which composes children from this source.
        self.source = source
        self._live = stream
        self._pending = ""
        self._pumping = False
        self._closed = False
        self._stream: MarkdownStream | None = None
        super().__init__()

    def compose(self) -> ComposeResult:
        yield Static("Agent", markup=False)
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


class ToolBox(CollapsiblePanel):
    title = "Tools"

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []
        self._sync(80)

    def body(self) -> str:
        return "\n".join(self.lines)

    def add(self, name: str, args: object) -> None:
        self.lines.append(f"🔧 {name}({args})")
        self.refresh_body()


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
