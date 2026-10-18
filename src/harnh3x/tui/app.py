"""The interactive application, its one-shot and review variants, and the runners."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalGroup, VerticalScroll
from textual.content import Content
from textual.geometry import Offset
from textual.screen import Screen
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static, TextArea

from harnh3x import __version__
from harnh3x.agent import (
    compact_session,
    list_sessions,
    model_context_limit,
    parse_command,
)
from harnh3x.commands import NO_EXPORT, dispatch, matching_commands
from harnh3x.config import COMPACT_AT
from harnh3x.failures import is_failure_answer
from harnh3x.tools.process import kill_children
from harnh3x.tui.export import (
    export_base,
    exportable_answers,
    render_export,
    write_export,
)
from harnh3x.tui.mentions import (
    Mention,
    filter_mentions,
    index_project,
    insert_mention,
    mention_at,
    mention_browse,
    mention_children,
    mention_window,
    replace_mention_query,
)
from harnh3x.tui.modals import (
    ContinueScreen,
    ExportPathScreen,
    HelpScreen,
    WarningScreen,
)
from harnh3x.tui.panels import (
    AgentAnswer,
    Composer,
    DebugBox,
    Thinking,
    ToolBox,
    TurnHeading,
    UserMessage,
)
from harnh3x.tui.pickers import ExportScreen, ResumeScreen
from harnh3x.tui.text import (
    activity_text,
    context_level,
    context_line,
    reply_reasoning,
    slash_hint,
    translate_legacy_key,
)
from harnh3x.tui.transcript import (
    Compaction,
    _static_text,
    compact_change,
    conversation_turns,
    transcript_items,
)

WAITING = "Waiting for the model…"
STOPPING = "Stopping…"
MAX_TURNS_PHASE = "Max turns reached"
_CONTEXT_STYLES = {"warn": "ansi_yellow", "over": "bold ansi_red"}


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


class StatusBar(Static):
    def on_click(self, event: events.Click) -> None:
        event.stop()
        action = getattr(self.app, "action_warnings", None)
        if callable(action):
            action()


class HarnessApp(App):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("ctrl+h", "toggle_trace", "Details", priority=True),
        Binding("ctrl+w", "warnings", "Warnings", priority=True),
        Binding("ctrl+n", "new_session", "New session", priority=True),
        Binding("escape", "stop_turn", "Stop", show=False),
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
        height: auto;
        min-height: 3;
        max-height: 10;
    }
    #activity {
        height: 1;
        padding: 0 1;
        color: $accent;
    }
    #mention-menu {
        height: auto;
        max-height: 8;
        padding: 0 1;
        color: $text-muted;
    }
    #slash-hint {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    #activity.hidden, #mention-menu.hidden, #slash-hint.hidden {
        display: none;
    }
    .panel {
        border: round ansi_bright_black;
        padding: 0 1;
        margin: 1 0;
        color: $text-muted;
        height: auto;
    }
    .panel:focus {
        border: round $accent;
    }
    #transcript.details-hidden .trace {
        display: none;
    }
    .panel.debug {
        color: ansi_cyan;
        border: round ansi_cyan;
    }
    .user {
        height: auto;
        margin: 1 0 0 0;
        padding: 0 1;
        border-left: thick $accent;
        background: $boost;
    }
    .agent-label {
        text-style: bold;
        color: $accent;
    }
    AgentAnswer {
        height: auto;
        margin: 1 0;
    }
    AgentAnswer.error {
        border-left: thick $error;
        padding-left: 1;
    }
    AgentAnswer.error .agent-label {
        color: $error;
    }
    .note {
        color: $text-muted;
        text-style: italic;
    }
    .note.error {
        color: $error;
        text-style: none;
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
        self._busy = False
        self._phase = WAITING
        self._busy_since = 0.0
        self._frame = 0
        self._activity_timer: Timer | None = None
        self._mentions: list[Mention] | None = None
        self._mention_matches: list[Mention] = []
        self._mention_selected = 0
        self._mention_offset = 0
        self._mention_prefer: str | None = None
        self._mention_token: tuple[int, int, str] | None = None
        self._mention_hold: str | None = None
        self._reset_live()

    async def on_event(self, event: events.Event) -> None:
        if isinstance(event, events.Key) and not event.is_forwarded:
            key = translate_legacy_key(
                event.key, event.character, os.environ.get("TERM_PROGRAM")
            )
            if key != event.key:
                event = events.Key(key, event.character if key == "ctrl+h" else None)
        await super().on_event(event)

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

    def _available_counts(self) -> tuple[int, int, int, int, int] | None:
        skills = getattr(self.session, "skills", None)
        counts = getattr(skills, "available_counts", None)
        if not callable(counts):
            return None
        found = counts()
        if (
            isinstance(found, tuple)
            and len(found) == 5
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
        used = getattr(self.session, "context_tokens", None)
        limit = getattr(self.session, "context_limit", None)
        context = context_line(used, limit)
        if context is not None:
            add(context, _CONTEXT_STYLES.get(context_level(used, limit, COMPACT_AT)))
        counts = self._available_counts()
        if counts is not None:
            skills, tools, custom, servers, mcp_tools = counts
            add(f"{skills} skill" if skills == 1 else f"{skills} skills")
            add(f"{tools} tool" if tools == 1 else f"{tools} tools")
            if custom:
                add(
                    f"{custom} custom skill"
                    if custom == 1
                    else f"{custom} custom skills"
                )
            if servers:
                add(
                    f"{servers} MCP server"
                    if servers == 1
                    else f"{servers} MCP servers"
                )
                add(
                    f"{mcp_tools} MCP tool"
                    if mcp_tools == 1
                    else f"{mcp_tools} MCP tools"
                )
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
            yield Static("", id="activity", classes="hidden", markup=False)
            if self.review is None and self.initial is None:
                yield Static("", id="mention-menu", classes="hidden", markup=False)
                yield Static("", id="slash-hint", classes="hidden", markup=False)
                yield Composer()
            with Horizontal(id="status-line"):
                yield StatusBar(self._status(), id="status", markup=False)
                yield Static(__version__, id="version", markup=False)

    def _anchor_transcript(self) -> None:
        self.query_one("#transcript").anchor()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # One-shot binds Enter to close. That binding is priority, so it is
        # checked before the modal; leave Enter for the continue prompt.
        return not (action == "close" and isinstance(self.screen, ContinueScreen))

    def on_mount(self) -> None:
        self.session.confirm_continue = self._confirm_continue
        self.session.on_model_turn = self._queue_model_turn
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
        if self.show_tools:
            self.session.on_tool_done = self._queue_tool_done
        if self.initial:
            self._append_user(self.initial)
            self._begin_busy()
            self._spawn(lambda: self._turn(self.initial))
            return
        self._seed_history()
        self.query_one(Composer).focus()

    def _seed_history(self) -> None:
        composers = list(self.query(Composer))
        if not composers:
            return
        messages = getattr(self.session, "messages", None) or []
        composers[0].set_prompts([turn.user for turn in conversation_turns(messages)])

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if isinstance(event.text_area, Composer):
            self._refresh_slash_hint(event.text_area.text)
            self._refresh_mentions(event.text_area)

    def _refresh_slash_hint(self, text: str) -> None:
        hints = list(self.query("#slash-hint"))
        if not hints:
            return
        matches = matching_commands(text)
        hint = hints[0]
        if matches:
            hint.update(slash_hint(matches))
        hint.set_class(not matches, "hidden")

    def _mention_root(self) -> Path | None:
        if not getattr(self.session, "coding", False):
            return None
        project = getattr(self.session, "project", None)
        if project is None:
            return None
        root = Path(project)
        if not root.is_dir():
            return None
        return root

    def mention_open(self) -> bool:
        return bool(self._mention_matches)

    def mention_move(self, step: int) -> bool:
        if not self._mention_matches:
            return False
        last = len(self._mention_matches) - 1
        self._mention_selected = min(max(self._mention_selected + step, 0), last)
        self._paint_mentions()
        return True

    def mention_into(self) -> bool:
        if not self.mention_open() or self._mention_token is None:
            return False
        entry = self._mention_matches[self._mention_selected]
        if not entry.is_dir:
            return True
        self._mention_prefer = None
        self._set_mention_query(entry.path + "/")
        return True

    def mention_back(self) -> bool:
        if self._mention_token is None or self._mentions is None:
            return False
        directory, _name = mention_browse(self._mentions, self._mention_token[2])
        if not directory:
            return self.mention_open()
        parent = directory.rpartition("/")[0]
        self._mention_prefer = directory
        self._set_mention_query(f"{parent}/" if parent else "")
        return True

    def _set_mention_query(self, query: str) -> None:
        if self._mention_token is None:
            return
        composer = self.query_one(Composer)
        start, end, _query = self._mention_token
        updated, cursor = replace_mention_query(composer.text, start, end, query)
        self._mention_hold = None
        self._mention_selected = 0
        self._mention_offset = 0
        composer.place(updated, cursor)

    def _paint_mentions(self) -> None:
        menus = list(self.query("#mention-menu"))
        if not menus:
            return
        self._mention_selected, self._mention_offset, text = mention_window(
            self._mention_matches, self._mention_selected, self._mention_offset
        )
        menus[0].update(text)
        menus[0].set_class(not text, "hidden")

    def mention_accept(self) -> bool:
        if not self._mention_matches or self._mention_token is None:
            return False
        composer = self.query_one(Composer)
        start, end, _query = self._mention_token
        entry = self._mention_matches[self._mention_selected]
        updated, cursor = insert_mention(composer.text, start, end, entry)
        self._mention_hold = None
        composer.place(updated, cursor)
        return True

    def mention_dismiss(self) -> bool:
        if not self._mention_matches:
            return False
        composers = list(self.query(Composer))
        self._mention_hold = composers[0].text if composers else ""
        self._mention_matches = []
        self._mention_token = None
        self._mention_selected = 0
        self._mention_offset = 0
        self._mention_prefer = None
        menus = list(self.query("#mention-menu"))
        if menus:
            menus[0].set_class(True, "hidden")
        return True

    def _drop_mentions(self) -> None:
        self._mentions = None
        self._mention_matches = []
        self._mention_selected = 0
        self._mention_offset = 0
        self._mention_prefer = None
        self._mention_token = None
        self._mention_hold = None
        menus = list(self.query("#mention-menu"))
        if menus:
            menus[0].set_class(True, "hidden")

    def _refresh_mentions(self, composer: Composer) -> None:
        menus = list(self.query("#mention-menu"))
        if not menus:
            return
        menu = menus[0]
        if self._mention_hold is not None and composer.text == self._mention_hold:
            self._mention_matches = []
            self._mention_token = None
            self._mention_offset = 0
            menu.set_class(True, "hidden")
            return
        self._mention_hold = None
        root = self._mention_root()
        token = None
        if root is not None:
            offset = composer.document.get_index_from_location(composer.cursor_location)
            token = mention_at(composer.text, offset)
        if token is None or root is None:
            self._mention_matches = []
            self._mention_token = None
            self._mention_selected = 0
            self._mention_offset = 0
            self._mention_prefer = None
            menu.set_class(True, "hidden")
            return
        if self._mentions is None:
            self._mentions = index_project(root)
        _start, _end, query = token
        if self._mention_token is None or self._mention_token[2] != query:
            self._mention_selected = 0
            self._mention_offset = 0
        self._mention_token = token
        directory, name = mention_browse(self._mentions, query)
        children = mention_children(self._mentions, directory)
        self._mention_matches = filter_mentions(children, name)
        if self._mention_prefer:
            for index, entry in enumerate(self._mention_matches):
                if entry.path == self._mention_prefer:
                    self._mention_selected = index
                    break
            self._mention_prefer = None
        if not self._mention_matches:
            menu.set_class(True, "hidden")
            return
        self._paint_mentions()

    def _begin_busy(self, phase: str = WAITING) -> None:
        self._busy = True
        self._phase = phase
        self._busy_since = time.monotonic()
        self._frame = 0
        if self._activity_timer is None:
            self._activity_timer = self.set_interval(0.1, self._tick_activity)
        else:
            self._activity_timer.resume()
        self._render_activity()

    def _end_busy(self) -> None:
        self._busy = False
        if self._activity_timer is not None:
            self._activity_timer.pause()
        self._render_activity()

    def _set_phase(self, phase: str) -> None:
        if not self._busy or self._phase == STOPPING or self._phase == phase:
            return
        self._phase = phase
        self._render_activity()

    def _tick_activity(self) -> None:
        if not self._busy:
            return
        self._frame += 1
        self._render_activity()

    def _render_activity(self) -> None:
        bars = list(self.query("#activity"))
        if not bars:
            return
        bar = bars[0]
        if not self._busy:
            bar.add_class("hidden")
            return
        stoppable = getattr(
            self.session, "abort", None
        ) is not None and self._phase not in {STOPPING, MAX_TURNS_PHASE}
        bar.update(
            activity_text(
                self._frame,
                self._phase,
                time.monotonic() - self._busy_since,
                stoppable=stoppable,
            )
        )
        bar.remove_class("hidden")

    def _aborted(self) -> bool:
        abort = getattr(self.session, "abort", None)
        return abort is not None and abort.is_set()

    def action_stop_turn(self) -> None:
        if not self._busy or self._quitting or len(self.screen_stack) > 1:
            return
        abort = getattr(self.session, "abort", None)
        if abort is None or abort.is_set():
            return
        abort.set()
        kill_children()
        self._phase = STOPPING
        self._render_activity()

    def _after_stop(self) -> None:
        abort = getattr(self.session, "abort", None)
        if abort is not None:
            abort.clear()
        if self.tools is not None:
            self.tools.stop_running()
        self._mount_new_compactions()
        self._end_busy()
        self._note("Stopped.")
        self._refresh_banner()
        if self.initial is not None:
            self.exit()
            return
        composer = self.query_one(Composer)
        composer.disabled = False
        composer.focus()
        self._drop_mentions()

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
        self._drop_mentions()
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
        composer.remember(text)
        command = parse_command(text)
        if command is not None:
            composer.load_text("")
            self._refresh_slash_hint("")
            self._command(*command)
            return
        composer.load_text("")
        self._refresh_slash_hint("")
        composer.disabled = True
        self._append_user(text)
        self._begin_busy()
        self._spawn(lambda: self._turn(text))

    def _command(self, name: str, rest: str) -> None:
        handlers = {
            "help": lambda _rest: self.push_screen(HelpScreen()),
            "new": lambda _rest: self.call_later(self.action_new_session),
            "resume": lambda _rest: self.push_screen(
                ResumeScreen(list_sessions()), self._on_resume
            ),
            "compact": lambda _rest: self._start_compact(),
            "export": self._start_export,
            "details": lambda _rest: self.action_toggle_trace(),
            "exit": lambda _rest: self.action_quit(),
        }
        dispatch(name, rest, handlers, self._error_note)

    def _start_compact(self) -> None:
        composer = self.query_one(Composer)
        composer.disabled = True
        self._begin_busy("Compacting context…")
        self._spawn(self._compact_now)

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
            self.notify(str(exc), severity="error")
            return
        self.notify(f"Exported to {path}")

    def _start_export(self, raw: str) -> None:
        messages = getattr(self.session, "messages", None) or []
        answers = exportable_answers(messages)
        if not answers:
            self.notify(NO_EXPORT, severity="warning")
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

        self.push_screen(ExportPathScreen(export_base(self.session)), chosen)

    def action_quit(self) -> None:
        if not self._quitting:
            self._quitting = True
            abort = getattr(self.session, "abort", None)
            if abort is not None:
                abort.set()
            kill_children()
            skills = getattr(self.session, "skills", None)
            close = getattr(skills, "close", None)
            if callable(close):
                close()
        self.exit()

    def _spawn(self, work: Callable[[], None]) -> None:
        def target() -> None:
            try:
                work()
            except Exception as exc:
                self._from_thread(self._worker_error, exc)

        threading.Thread(target=target, daemon=True).start()

    def _worker_error(self, exc: Exception) -> None:
        if self._quitting:
            return
        self._handle_exception(exc)

    def _from_thread(self, callback: Callable, *args) -> None:
        if self._quitting or self._loop is None or not self._running:
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
        self._set_phase("Compacting context…")
        if self._compacting is not None:
            return
        block = Static("Compacting context…", markup=False, classes="panel trace")
        block.border_title = "Context"
        self._compacting = block
        self.query_one("#transcript").mount(block)

    def _hide_compacting(self) -> None:
        self._set_phase(WAITING)
        block = self._compacting
        self._compacting = None
        if block is not None and block.parent is not None:
            block.remove()

    def _compact_now(self) -> None:
        status = compact_session(self.session)
        if self._quitting:
            return
        if self._aborted():
            self._from_thread(self._after_stop)
            return
        self._from_thread(self._after_compact, status)

    def _after_compact(self, status: str) -> None:
        self._mount_new_compactions()
        self._end_busy()
        self._refresh_banner()
        if status != "Context compacted.":
            self._note(status)
        composer = self.query_one(Composer)
        composer.disabled = False
        composer.focus()

    def _note(self, text: str, *, error: bool = False) -> None:
        transcript = self.query_one("#transcript")
        classes = "note error" if error else "note"
        transcript.mount(Static(text, markup=False, classes=classes))
        transcript.anchor()

    def _error_note(self, text: str) -> None:
        self._note(text, error=True)

    async def _on_resume(self, chosen) -> None:
        composer = self.query_one(Composer)
        if chosen is None:
            composer.focus()
            return
        try:
            self.session.resume(chosen.workspace)
        except ValueError as exc:
            self._error_note(str(exc))
            composer.focus()
            return
        self.tools = None
        self._refresh_banner()
        await self.query_one("#transcript").remove_children()
        self._anchor_transcript()
        self._render_history()
        self._load_context_limit()
        self._seed_history()
        self._drop_mentions()
        self._refresh_mentions(composer)
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
            elif isinstance(child, UserMessage):
                parts.append(f"You: {child.source}")
            elif isinstance(child, TurnHeading):
                parts.append(f"Turn {child.number}")
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

    def _queue_model_turn(self, number: int) -> None:
        self._from_thread(self._begin_model_turn, number)

    def _begin_model_turn(self, number: int) -> None:
        self._tools_open = False
        self._reasoning_open = False
        self._content_open = False
        self._msg_reasoning = ""
        self._msg_content = ""
        self._live_thinking = None
        self._live_answer = None
        self.tools = None
        self.query_one("#transcript").mount(TurnHeading(number))

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
        self._set_phase("Thinking…" if kind == "reasoning" else "Writing…")
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
            if kind == "round":
                transcript.mount(TurnHeading(payload))
            elif kind == "thinking":
                transcript.mount(Thinking(payload, hold=False))
            elif kind == "answer":
                transcript.mount(AgentAnswer(payload, error=is_failure_answer(payload)))
            elif kind == "tools":
                self.tools = None
                for name, args in payload:
                    self._append_tool(name, args)

    def _confirm_continue(self) -> bool:
        # Posted to the app thread. Waiting on the screen here would deadlock,
        # because call_from_thread holds that thread until this returns.
        if self._quitting or self._aborted():
            return False
        done = threading.Event()
        choice = {"yes": False}

        def ask() -> None:
            if self._quitting or self._aborted():
                done.set()
                return
            self._set_phase(MAX_TURNS_PHASE)

            def chosen(yes: bool | None) -> None:
                choice["yes"] = yes is True
                if self._phase == MAX_TURNS_PHASE and not self._quitting:
                    self._set_phase(WAITING)
                done.set()

            self.push_screen(ContinueScreen(), chosen)

        self._from_thread(ask)
        while not done.wait(0.1):
            if self._quitting or self._aborted():
                return False
        return choice["yes"] and not self._aborted() and not self._quitting

    def _turn(self, text: str) -> None:
        start = len(getattr(self.session, "messages", []) or [])
        self._reset_live()

        def on_tool(name: str, args: object) -> None:
            self._from_thread(self._append_tool, name, args, "running")

        assert self._run is not None
        answer = self._run(
            text,
            self.session,
            show_tools=self.show_tools,
            on_tool=on_tool if self.show_tools else None,
        )
        if self._quitting:
            return
        if self._aborted():
            self._from_thread(self._after_stop)
            return
        self._from_thread(self._after_turn, answer, start)

    def _queue_tool_done(self, name: str, result: str) -> None:
        self._from_thread(self._tool_done, name, result)

    def _tool_done(self, name: str, result: str) -> None:
        if self.tools is not None:
            self.tools.finish(name, result)
        self._set_phase(WAITING)

    def _append_user(self, text: str) -> None:
        self.tools = None
        self.query_one("#transcript").mount(UserMessage(text))

    def _append_tool(self, name: str, args: object, state: str | None = None) -> None:
        if state == "running":
            self._set_phase(f"Running {name}…")
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
        self.tools.add(name, args, state=state)
        self._refresh_banner()

    def _settle_tools(self) -> None:
        for box in self.query(ToolBox):
            box.settle()

    def _finish(self, answer: str | None, reasoning: str, refocus: bool = True) -> None:
        self._mount_new_compactions()
        self._settle_tools()
        self._end_busy()
        transcript = self.query_one("#transcript")
        if reasoning:
            transcript.mount(Thinking(reasoning, hold=self.review is not None))
        if answer is not None:
            transcript.mount(AgentAnswer(answer, error=is_failure_answer(answer)))
        self._refresh_banner()
        if refocus and self.review is None and self.initial is None:
            composer = self.query_one(Composer)
            composer.disabled = False
            composer.focus()

    def _after_turn(self, answer: str, start: int) -> None:
        self._drop_mentions()
        if not self._streamed:
            reasoning = ""
            if self.show_tools:
                reasoning = reply_reasoning(self.session, start, answer)
            self._finish(answer, reasoning, refocus=self.initial is None)
            self._maybe_hold(reasoning)
            return
        transcript = self.query_one("#transcript")
        self._mount_new_compactions()
        self._settle_tools()
        self._end_busy()
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
            transcript.mount(AgentAnswer(answer, error=is_failure_answer(answer)))
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
                block.refresh_body()
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
