"""The interactive application, its one-shot and review variants, and the runners."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalGroup, VerticalScroll
from textual.content import Content
from textual.geometry import Offset
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Static

from harnh3x import __version__
from harnh3x.agent import (
    compact_session,
    list_sessions,
    model_context_limit,
    parse_command,
)
from harnh3x.tools.process import kill_children
from harnh3x.tui.export import (
    EXPORT_USAGE,
    NO_EXPORT,
    export_base,
    exportable_answers,
    render_export,
    write_export,
)
from harnh3x.tui.modals import ExportPathScreen, HelpScreen, WarningScreen
from harnh3x.tui.panels import AgentAnswer, Composer, DebugBox, Thinking, ToolBox
from harnh3x.tui.pickers import ExportScreen, ResumeScreen
from harnh3x.tui.text import context_line, reply_reasoning
from harnh3x.tui.transcript import (
    Compaction,
    _static_text,
    compact_change,
    transcript_items,
)


class HarnessScreen(Screen):
    def get_widget_and_offset_at(
        self, x: int, y: int
    ) -> tuple[Widget | None, Offset | None]:
        # Markdown.update detaches blocks while the compositor can still
        # return them. Selection then reads parent.region and crashes.
        widget, offset = super().get_widget_and_offset_at(x, y)
        if widget is not None and not widget.is_attached:
            return None, None
        return widget, offset


class HarnessApp(App):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("ctrl+h", "toggle_trace", "Details", priority=True),
        Binding("ctrl+w", "warnings", "Warnings", priority=True),
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
        padding: 1;
    }
    #bottom {
        dock: bottom;
        height: auto;
    }
    #status-line {
        height: 1;
        width: 1fr;
    }
    #status {
        height: 1;
        padding: 0 1;
        overflow: hidden;
    }
    #status.hidden {
        display: none;
    }
    #version {
        dock: right;
        width: auto;
        height: 1;
        padding: 0 1;
    }
    #composer {
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
        margin-bottom: 1;
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
        self._quitting = False
        self._reset_live()

    def _yolo(self) -> bool:
        return bool(getattr(self.session, "yolo", False))

    def _banner(self) -> Content:
        lines = []
        if self.review is None:
            lines.append("🤖 Harnh3x")
        lines.append(f"Workspace: {self.session.workspace}")
        project = getattr(self.session, "project", None)
        if project:
            lines.append(f"Project: {project}")
        if getattr(self.session, "coding", False):
            lines.append("Coding")
        return Content("\n".join(lines))

    def _available_counts(self) -> tuple[int, int] | None:
        skills = getattr(self.session, "skills", None)
        counts = getattr(skills, "available_counts", None)
        if not callable(counts):
            return None
        found = counts()
        if (
            isinstance(found, tuple)
            and len(found) == 2
            and all(
                isinstance(item, int) and not isinstance(item, bool) for item in found
            )
        ):
            return found
        return None

    def _warnings(self) -> list[tuple[str, str]]:
        skills = getattr(self.session, "skills", None)
        warnings = getattr(skills, "warnings", None)
        if not isinstance(warnings, list):
            return []
        found = []
        for item in warnings:
            if (
                isinstance(item, tuple)
                and len(item) == 2
                and isinstance(item[0], str)
                and isinstance(item[1], str)
            ):
                found.append(item)
        return found

    def _status(self) -> Content:
        chunks: list[Content] = []

        def add(text: str, style: str | None = None) -> None:
            if chunks:
                chunks.append(Content(" · "))
            piece = Content.styled(text, style) if style else Content(text)
            chunks.append(piece)

        model = getattr(self.session, "model", None)
        if isinstance(model, str) and model:
            add(f"Model: {model}")
        context = context_line(
            getattr(self.session, "context_tokens", None),
            getattr(self.session, "context_limit", None),
        )
        if context is not None:
            add(context)
        counts = self._available_counts()
        if counts is not None:
            skills, tools = counts
            add(f"{skills} skill" if skills == 1 else f"{skills} skills")
            add(f"{tools} tool" if tools == 1 else f"{tools} tools")
        if self._yolo():
            add("YOLO", "bold ansi_bright_yellow")
        if getattr(self.session, "debug", False):
            add("Debug")
        warnings = self._warnings()
        if warnings:
            names = ", ".join(name for name, _reason in warnings)
            add(
                f"⚠ {names} unavailable · Ctrl+W for warning details",
                "bold ansi_bright_red",
            )
        if not chunks:
            return Content("")
        line = chunks[0]
        for chunk in chunks[1:]:
            line += chunk
        return line

    def _refresh_banner(self) -> None:
        self.query_one("#banner", Static).update(self._banner())
        self._refresh_status()

    def _refresh_status(self) -> None:
        bar = self.query_one("#status", Static)
        content = self._status()
        bar.update(content)
        bar.set_class(not str(content), "hidden")

    def _load_context_limit(self) -> None:
        if self._context_limit is None or self.review is not None:
            return
        model = self.session.model

        def fetch() -> None:
            limit = self._context_limit(model)
            self._from_thread(self._apply_limit, model, limit)

        self._spawn(fetch)

    def _apply_limit(self, model: str, limit: int | None) -> None:
        if (
            self.session.model != model
            or isinstance(limit, bool)
            or not isinstance(limit, int)
        ):
            return
        self.session.context_limit = limit
        self._refresh_banner()

    def get_default_screen(self) -> Screen:
        return HarnessScreen(id="_default")

    def compose(self) -> ComposeResult:
        yield Static(self._banner(), id="banner", markup=False)
        yield VerticalScroll(id="transcript")
        with VerticalGroup(id="bottom"):
            if self.review is None and self.initial is None:
                yield Composer()
            with Horizontal(id="status-line"):
                yield Static(self._status(), id="status", markup=False)
                yield Static(__version__, id="version", markup=False)

    def _anchor_transcript(self) -> None:
        self.query_one("#transcript").anchor()

    def on_mount(self) -> None:
        self._refresh_status()
        self._load_context_limit()
        self._anchor_transcript()
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
            self._spawn(lambda: self._turn(self.initial))
            return
        self.query_one(Composer).focus()

    def action_warnings(self) -> None:
        warnings = self._warnings()
        if not warnings or len(self.screen_stack) > 1:
            return
        self.push_screen(WarningScreen(warnings))

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
        self._refresh_banner()
        self.tools = None
        self._shown_compactions = []
        self._compacting = None
        await self.query_one("#transcript").remove_children()
        self._anchor_transcript()
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
        self._spawn(lambda: self._turn(text))

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
            self._spawn(self._compact_now)
            return
        if name == "compact":
            self._note("Usage: /compact")
            return
        if name == "export" and rest == "":
            self._note(EXPORT_USAGE)
            return
        if name == "export":
            self._start_export(rest)
            return
        if name == "exit" and rest == "":
            self.action_quit()
            return
        if name == "exit":
            self._note("Usage: /exit")
            return
        shown = f"/{name}" if name else "/"
        self._note(f"Unknown command: {shown}")

    def _focus_composer(self) -> None:
        composers = list(self.query(Composer))
        if not composers or composers[0].disabled:
            return
        composers[0].focus()

    def _write_export(self, raw: str, answers: list[str]) -> None:
        try:
            path = write_export(
                raw, render_export(answers), base=export_base(self.session)
            )
        except ValueError as exc:
            self._note(str(exc))
            return
        self._note(f"Exported to {path}")

    def _start_export(self, raw: str) -> None:
        messages = getattr(self.session, "messages", None) or []
        answers = exportable_answers(messages)
        if not answers:
            self._note(NO_EXPORT)
            return
        if len(answers) == 1:
            self._write_export(raw, answers)
            return

        def chosen(picked: list[str] | None) -> None:
            if picked:
                self._write_export(raw, picked)
            self._focus_composer()

        self.push_screen(ExportScreen(answers), chosen)

    def _prompt_export_path(self, source: str) -> None:
        if len(self.screen_stack) > 1:
            return

        def chosen(path: str | None) -> None:
            if path:
                self._write_export(path, [source])
            self._focus_composer()

        self.push_screen(ExportPathScreen(), chosen)

    def _stopping(self) -> bool:
        if self._quitting:
            return True
        abort = getattr(self.session, "abort", None)
        return abort is not None and abort.is_set()

    def action_quit(self) -> None:
        if not self._quitting:
            self._quitting = True
            abort = getattr(self.session, "abort", None)
            if abort is not None:
                abort.set()
            kill_children()
            skills = getattr(self.session, "skills", None)
            kill = getattr(skills, "kill", None)
            if callable(kill):
                kill()
        self.exit()

    def _spawn(self, work: Callable[[], None]) -> None:
        def target() -> None:
            try:
                work()
            except Exception as exc:
                self._from_thread(self._worker_error, exc)

        threading.Thread(target=target, daemon=True).start()

    def _worker_error(self, exc: Exception) -> None:
        if self._stopping():
            return
        self._handle_exception(exc)

    def _from_thread(self, callback: Callable, *args) -> None:
        if self._stopping() or self._loop is None or not self._running:
            return
        try:
            self.call_from_thread(callback, *args)
        except RuntimeError:
            return

    def _on_compact(self, phase: str) -> None:
        if phase == "start":
            self._from_thread(self._show_compacting)
        else:
            self._from_thread(self._hide_compacting)

    def _show_compacting(self) -> None:
        if self._compacting is not None:
            return
        block = Static("Compacting context…", markup=False, classes="panel trace")
        block.border_title = "Context"
        self._compacting = block
        self.query_one("#transcript").mount(block)

    def _hide_compacting(self) -> None:
        block = self._compacting
        self._compacting = None
        if block is not None and block.parent is not None:
            block.remove()

    def _compact_now(self) -> None:
        status = compact_session(self.session)
        self._from_thread(self._after_compact, status)

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
        transcript.anchor()

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
        self._refresh_banner()
        await self.query_one("#transcript").remove_children()
        self._anchor_transcript()
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
        self.query_one("#transcript").mount(block)

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
        self._from_thread(self._show_debug, system, request)

    def _show_debug(self, system: str | None, request: str) -> None:
        transcript = self.query_one("#transcript")
        if system is not None:
            transcript.mount(DebugBox("System", system))
        transcript.mount(DebugBox("Request", request))

    def _queue_delta(self, kind: str, text: str) -> None:
        self._from_thread(self._apply_delta, kind, text)

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
                block = AgentAnswer("", stream=True)
                self._live_answer = block
                transcript.mount(block)
            assert self._live_answer is not None
            self._msg_content += text
            self._live_answer.append(text)

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

    def _turn(self, text: str) -> None:
        start = len(getattr(self.session, "messages", []) or [])
        self._reset_live()

        def on_tool(name: str, args: object) -> None:
            self._from_thread(self._append_tool, name, args)

        assert self._run is not None
        answer = self._run(
            text,
            self.session,
            show_tools=self.show_tools,
            on_tool=on_tool if self.show_tools else None,
        )
        if self._stopping():
            return
        self._from_thread(self._after_turn, answer, start)

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
        self._refresh_banner()

    def _finish(self, answer: str | None, reasoning: str, refocus: bool = True) -> None:
        self._mount_new_compactions()
        transcript = self.query_one("#transcript")
        if reasoning:
            transcript.mount(Thinking(reasoning, hold=self.review is not None))
        if answer is not None:
            transcript.mount(AgentAnswer(answer))
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
    BINDINGS: ClassVar[list[Binding]] = [
        *HarnessApp.BINDINGS,
        Binding("enter", "close", "Close", priority=True),
    ]


class ReviewApp(HarnessApp):
    BINDINGS: ClassVar[list[Binding]] = [
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
