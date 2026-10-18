"""The Textual app driven through a pilot."""

import asyncio
import threading
from types import SimpleNamespace

from helpers import _export_messages, _past_workspace, _write_transcript
from textual import events
from textual.widgets import Rule

from harnh3x import __version__, agent
from harnh3x.tui import (
    EXPORT_USAGE,
    AgentAnswer,
    Composer,
    ContinueScreen,
    CopyAnswer,
    DebugBox,
    ExportAnswer,
    ExportPathScreen,
    ExportScreen,
    HarnessApp,
    HarnessScreen,
    HelpScreen,
    OneShotApp,
    ResumeOptions,
    ResumeScreen,
    ReviewApp,
    SkillsScreen,
    StatusBar,
    SwitchModelScreen,
    Thinking,
    ToolBox,
    TurnHeading,
    UserMessage,
    WarningScreen,
)


def _status(app):
    return [segment.text for segment in app.query_one("#status", StatusBar).segments]


def _status_styles(app):
    bar = app.query_one("#status", StatusBar)
    return {segment.key: segment.style for segment in bar.segments}


def test_compacting_indicator_shows_and_hides():
    session = SimpleNamespace(
        workspace="ws",
        model="m",
        messages=[],
        context_tokens=None,
        context_limit=None,
    )
    app = HarnessApp(session, lambda *args, **kwargs: "", show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            app._show_compacting()
            await pilot.pause()
            assert "Compacting context…" in app.transcript_plain()
            app._hide_compacting()
            await pilot.pause()
            assert "Compacting context…" not in app.transcript_plain()

    asyncio.run(drive())


def test_compact_command_shows_the_note(monkeypatch):
    def fake_compact(session, client=None):
        session.messages.append(
            {"role": "user", "content": "Context compacted:\nforced summary"}
        )
        return "Context compacted."

    monkeypatch.setattr("harnh3x.tui.app.compact_session", fake_compact)
    session = SimpleNamespace(
        workspace="ws",
        model="m",
        messages=[],
        context_tokens=None,
        context_limit=None,
    )
    app = HarnessApp(session, lambda *args, **kwargs: "", show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press(*"/compact", "enter")
            await _wait_for(pilot, lambda: "forced summary" in app.transcript_plain())
            plain = app.transcript_plain()
            assert "Context compacted" in plain
            assert "You: Context compacted" not in plain
            await pilot.press(*"/compact extra", "enter")
            assert "Usage: /compact" in app.transcript_plain()

    asyncio.run(drive())


def test_turn_shows_the_compaction_note():
    session = SimpleNamespace(workspace="ws", model="m", messages=[])

    def fake_run(message, active, show_tools=True, on_tool=None):
        active.messages.append(
            {
                "role": "user",
                "content": "Context compacted:\nkept the greeting",
                "context_before": 1526,
                "context_after": 412,
            }
        )
        return "done"

    app = HarnessApp(session, fake_run, show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press(*"hi", "enter")
            await _wait_for(pilot, lambda: "Agent: done" in app.transcript_plain())
            plain = app.transcript_plain()
            assert "Context compacted" in plain
            assert "1,526 → 412" in plain
            assert "kept the greeting" in plain
            assert "You: Context compacted" not in plain

    asyncio.run(drive())


def test_banner_shows_context_and_updates_after_a_turn():
    session = SimpleNamespace(
        workspace="ws",
        model="m",
        messages=[],
        context_tokens=1234,
        context_limit=8192,
    )

    def fake_run(message, active, show_tools=True, on_tool=None):
        active.context_tokens = 5000
        if on_tool is not None:
            on_tool("read_file", {"path": "a.txt"})
        return "done"

    app = HarnessApp(session, fake_run, show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            banner = str(app.query_one("#banner").content)
            assert "🤖 Harnh3x" in banner
            assert __version__ not in banner
            bar = app.query_one("#status", StatusBar)
            assert bar.region.right == app.size.width
            assert bar.region.bottom == app.size.height
            _start, end, last = bar.hits[-1]
            assert last.text == __version__
            assert end == bar.size.width
            assert "Context:" not in banner
            assert "Model:" not in banner
            assert _status(app) == ["m", "ctx 1.2k/8.2k 15%", __version__]
            await pilot.press(*"hi", "enter")
            await _wait_for(pilot, lambda: "Agent: done" in app.transcript_plain())
            banner = str(app.query_one("#banner").content)
            assert "Context:" not in banner
            assert _status(app) == ["m", "ctx 5.0k/8.2k 61%", __version__]
            assert "Shift+Enter" not in banner
            assert "/resume" not in banner

    asyncio.run(drive())


def test_help_command_lists_commands_and_hotkeys():
    session = SimpleNamespace(workspace="ws", model="m", messages=[])
    app = HarnessApp(session, lambda *args, **kwargs: "", show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press(*"/help", "enter")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)
            shown = str(app.screen.query_one("#help-box").content)
            assert "/resume" in shown
            assert "/compact" in shown
            assert "/export" in shown
            assert "/exit" in shown
            assert "Shift+Enter" in shown
            assert "Ctrl+N" in shown
            assert "Ctrl+T" not in shown
            assert "Ctrl+H" in shown
            assert "Ctrl+W" in shown
            assert "open warning details" in shown
            assert "Ctrl+Q" in shown
            assert "Ctrl+J" in shown
            assert "Esc          close the @ list, or stop the running request" in shown
            assert (
                "@            in a coding session, pick a project file or folder"
                in shown
            )
            assert "/details" in shown
            assert "/new" in shown
            assert "Use Option as Meta key" in shown
            frame = app.screen.query_one("#help-frame")
            assert frame.border_subtitle == "Escape to close"
            assert frame.region.width > 60
            assert frame.region.bottom <= app.size.height
            assert frame.max_scroll_y > 0
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, HelpScreen)
            assert app.transcript_plain() == ""
            await pilot.press(*"/help extra", "enter")
            assert "Usage: /help" in app.transcript_plain()

    asyncio.run(drive())


def test_export_command_writes_the_chosen_response(tmp_path):
    session = SimpleNamespace(
        workspace=tmp_path,
        model="m",
        messages=_export_messages(),
        project=None,
    )
    app = HarnessApp(session, lambda *args, **kwargs: "", show_tools=True)
    toasts = _record_toasts(app)
    dest = tmp_path / "chosen.md"

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press(*f"/export {dest}", "enter")
            await pilot.pause()
            assert isinstance(app.screen, ExportScreen)
            assert app.screen.answers == ["final alpha", "beta answer"]
            await pilot.press("down", "enter")
            await pilot.pause()
            assert dest.read_text(encoding="utf-8") == "beta answer\n"
            assert toasts[-1] == (f"Exported to {dest.resolve()}", "information")
            assert "Exported to" not in app.transcript_plain()
            assert "secret thinking" not in dest.read_text(encoding="utf-8")
            await pilot.press(*f"/export {dest}", "enter")
            await pilot.pause()
            assert isinstance(app.screen, ExportScreen)
            await pilot.press("down", "down", "enter")
            await pilot.pause()
            assert dest.read_text(encoding="utf-8") == (
                "final alpha\n\n---\n\nbeta answer\n"
            )
            await pilot.press(*f"/export {dest}", "enter")
            await pilot.pause()
            assert isinstance(app.screen, ExportScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, ExportScreen)
            await pilot.press(*"/export", "enter")
            assert EXPORT_USAGE in app.transcript_plain()

    asyncio.run(drive())


def test_export_icon_writes_the_clicked_response(tmp_path):
    session = SimpleNamespace(
        workspace="ws",
        model="m",
        messages=_export_messages(),
        project=None,
    )
    app = HarnessApp(session, lambda *args, **kwargs: "", show_tools=True)
    toasts = _record_toasts(app)
    dest = tmp_path / "clicked.md"

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            app.query_one("#transcript").anchor(False)
            app._render_history()
            await pilot.pause()
            answers = list(app.query(AgentAnswer))
            assert [block.source for block in answers] == [
                "draft",
                "final alpha",
                "beta answer",
            ]
            button = answers[0].query_one(ExportAnswer)
            copy = answers[0].query_one(CopyAnswer)
            assert str(button.content) == "⤓"
            assert str(copy.content) == "⧉"
            assert copy.region.x < button.region.x
            assert button.region.x > answers[0].region.x
            copy.scroll_visible(animate=False)
            await pilot.pause()
            await pilot.click(copy)
            await pilot.pause()
            assert app.clipboard == "draft"
            assert toasts == [("Copied to the clipboard.", "information")]
            button.scroll_visible(animate=False)
            await pilot.pause()
            await pilot.click(button)
            await pilot.pause()
            assert isinstance(app.screen, ExportPathScreen)
            path_input = app.screen.query_one("#export-path")
            assert path_input.value.startswith("answer-")
            assert path_input.value.endswith(".md")
            base = str(app.screen.query_one("#export-base").content)
            assert base.startswith("Relative paths go to ")
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, ExportPathScreen)
            assert not dest.exists()
            await pilot.click(button)
            await pilot.pause()
            assert isinstance(app.screen, ExportPathScreen)
            await pilot.press(*str(dest), "enter")
            await pilot.pause()
            assert dest.read_text(encoding="utf-8") == "draft\n"
            assert "final alpha" not in dest.read_text(encoding="utf-8")
            assert "hidden thought" not in dest.read_text(encoding="utf-8")

    asyncio.run(drive())


def test_export_icon_stays_above_the_composer():
    body = "\n".join(f"answer line {index}" for index in range(30))
    session = SimpleNamespace(
        workspace="ws",
        model="m",
        messages=[
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": body},
        ],
        project=None,
    )
    app = HarnessApp(session, lambda *args, **kwargs: "", show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            app._render_history()
            await pilot.pause()
            await pilot.pause()
            composer = app.query_one("#composer")
            icon = app.query_one(ExportAnswer)
            assert icon.region.bottom <= composer.region.y

    asyncio.run(drive())


def test_submit_shows_the_request_and_the_answer():
    session = SimpleNamespace(workspace="ws", model="m", messages=[])
    seen = []

    def fake_run(message, session, show_tools=True, on_tool=None):
        seen.append(message)
        return "final answer"

    app = HarnessApp(session, fake_run, show_tools=True)
    captured = {}

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("a", "shift+enter", "b")
            assert app.query_one("#composer").text == "a\nb"
            await pilot.press("enter")
            for _ in range(40):
                if "Agent: final answer" in app.transcript_plain():
                    break
                await pilot.pause(0.05)
            else:
                raise AssertionError(app.transcript_plain())
            captured["plain"] = app.transcript_plain()

    asyncio.run(drive())
    assert seen == ["a\nb"]
    assert "You: a\nb" in captured["plain"]
    assert "Agent: final answer" in captured["plain"]


def test_exit_command_quits_the_app():
    seen = []

    def fake_run(message, active, show_tools=True, on_tool=None):
        seen.append(message)
        return "still here"

    session = SimpleNamespace(workspace="ws", model="m", messages=[])
    app = HarnessApp(session, fake_run, show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press(*"exit", "enter")
            await _wait_for(
                pilot, lambda: "Agent: still here" in app.transcript_plain()
            )
            assert not app._exit
            await pilot.press(*"/exit extra", "enter")
            assert "Usage: /exit" in app.transcript_plain()
            assert not app._exit
            await pilot.press(*"/exit", "enter")
            assert app._exit

    asyncio.run(drive())
    assert seen == ["exit"]


def test_ctrl_q_quits_the_app():
    session = SimpleNamespace(workspace="ws", model="m", messages=[])
    app = HarnessApp(session, lambda *args, **kwargs: "", show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            assert not app._exit
            await pilot.press("ctrl+q")
            assert app._exit

    asyncio.run(drive())


def test_ctrl_q_closes_the_skill_registry():
    closed = []
    skills = SimpleNamespace(close=lambda: closed.append(True))
    session = SimpleNamespace(workspace="ws", model="m", messages=[], skills=skills)
    app = HarnessApp(session, lambda *args, **kwargs: "", show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+q")
            assert app._exit

    asyncio.run(drive())
    assert closed == [True]


def test_ctrl_q_aborts_a_running_request():
    started = threading.Event()
    released = threading.Event()
    session = SimpleNamespace(
        workspace="ws", model="m", messages=[], abort=threading.Event()
    )

    def fake_run(message, active, show_tools=True, on_tool=None):
        started.set()
        assert active.abort.wait(timeout=2)
        released.set()
        return "should not show"

    app = HarnessApp(session, fake_run, show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press(*"hi", "enter")
            for _ in range(40):
                if started.is_set():
                    break
                await pilot.pause(0.05)
            else:
                raise AssertionError("request did not start")
            await pilot.press("ctrl+q")
            assert app._exit
            assert session.abort.is_set()
            assert released.wait(timeout=2)

    asyncio.run(drive())


def test_debug_panels_collapse_and_hide():
    system = "\n".join(f"system-{index}" for index in range(6))
    request = "\n".join(f"request-{index}" for index in range(6))
    session = SimpleNamespace(workspace="ws", model="m", messages=[], debug=True)

    def fake_run(message, active, show_tools=True, on_tool=None):
        active.on_debug(system, request)
        active.on_debug(None, "user: later")
        return "done"

    app = HarnessApp(session, fake_run, show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            banner = str(app.query_one("#banner").content)
            assert "\nDebug\n" not in banner
            assert _status(app) == ["m", "DEBUG", __version__]
            assert _status_styles(app)["debug"] == "ansi_black on ansi_cyan"
            await pilot.press(*"hi", "enter")
            await _wait_for(pilot, lambda: "Agent: done" in app.transcript_plain())
            boxes = list(app.query(".debug"))
            assert [box.border_title for box in boxes] == [
                "System",
                "Request",
                "Request",
            ]
            assert all("debug" in box.classes for box in boxes)
            assert all(box.styles.color.ansi == 6 for box in boxes)
            assert all(box.styles.border.top[1].ansi == 6 for box in boxes)
            plain = app.transcript_plain()
            assert plain.index("You: hi") < plain.index("System")
            assert plain.index("System") < plain.index("Request")
            assert plain.index("Request") < plain.index("Agent: done")
            assert "system-0" not in plain
            assert "system-5" in plain
            assert "request-0" not in plain
            assert "request-5" in plain
            await pilot.press("ctrl+h")
            await pilot.pause()
            assert all(not box.display for box in boxes)

    asyncio.run(drive())


def test_debug_banner_stays_after_a_new_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WORKSPACE", raising=False)
    session = agent.Session(
        workspace=tmp_path / "first", model="custom-model", debug=True
    )
    app = HarnessApp(session, lambda *args, **kwargs: "done", show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            skills, tools, _custom, _servers, _mcp_tools = (
                session.skills.available_counts()
            )
            expected = [
                "custom-model",
                "DEBUG",
                f"skills {skills}",
                f"tools {tools}",
                __version__,
            ]
            assert "\nDebug\n" not in str(app.query_one("#banner").content)
            assert "Model:" not in str(app.query_one("#banner").content)
            assert _status(app) == expected
            await pilot.press("ctrl+n")
            skills, tools, _custom, _servers, _mcp_tools = (
                session.skills.available_counts()
            )
            expected = [
                "custom-model",
                "DEBUG",
                f"skills {skills}",
                f"tools {tools}",
                __version__,
            ]
            assert "\nDebug\n" not in str(app.query_one("#banner").content)
            assert _status(app) == expected
            assert session.debug is True

    asyncio.run(drive())


def test_yolo_banner_stays_after_a_new_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WORKSPACE", raising=False)
    session = agent.Session(
        workspace=tmp_path / "first", model="custom-model", yolo=True
    )
    app = HarnessApp(session, lambda *args, **kwargs: "done", show_tools=True)
    plain = agent.Session(workspace=tmp_path / "plain", model="custom-model")
    plain_app = HarnessApp(plain, lambda *args, **kwargs: "done", show_tools=True)

    async def drive():
        async with plain_app.run_test(size=(80, 24)) as pilot:
            assert "YOLO" not in str(plain_app.query_one("#banner").content)
            skills, tools, _custom, _servers, _mcp_tools = (
                plain.skills.available_counts()
            )
            assert _status(plain_app) == [
                "custom-model",
                f"skills {skills}",
                f"tools {tools}",
                __version__,
            ]
            composer = plain_app.query_one("#composer")
            status_bar = plain_app.query_one("#status")
            assert status_bar.region.y == composer.region.bottom + 1
            assert status_bar.region.bottom == plain_app.size.height
        async with app.run_test(size=(80, 24)) as pilot:
            assert "YOLO" not in str(app.query_one("#banner").content)
            skills, tools, _custom, _servers, _mcp_tools = (
                session.skills.available_counts()
            )
            assert _status(app) == [
                "custom-model",
                "YOLO",
                f"skills {skills}",
                f"tools {tools}",
                __version__,
            ]
            yellow = [
                key
                for key, style in _status_styles(app).items()
                if style == "bold ansi_black on ansi_bright_yellow"
            ]
            assert yellow == ["yolo"]
            composer = app.query_one("#composer")
            status_bar = app.query_one("#status")
            assert status_bar.region.y == composer.region.bottom + 1
            assert status_bar.region.bottom == app.size.height
            await pilot.press("ctrl+n")
            assert "YOLO" not in str(app.query_one("#banner").content)
            skills, tools, _custom, _servers, _mcp_tools = (
                session.skills.available_counts()
            )
            assert _status(app) == [
                "custom-model",
                "YOLO",
                f"skills {skills}",
                f"tools {tools}",
                __version__,
            ]
            yellow = [
                key
                for key, style in _status_styles(app).items()
                if style == "bold ansi_black on ansi_bright_yellow"
            ]
            assert yellow == ["yolo"]
            assert session.yolo is True

    asyncio.run(drive())


def test_coding_banner_stays_after_a_new_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WORKSPACE", raising=False)
    project = tmp_path / "proj"
    project.mkdir()
    session = agent.Session(workspace=tmp_path / "first", project=project, coding=True)
    app = HarnessApp(session, lambda *args, **kwargs: "done", show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            banner = str(app.query_one("#banner").content)
            project_line = f"Project: {project.resolve()}"
            assert project_line in banner
            assert banner.index(project_line) < banner.index("Coding")
            await pilot.press("ctrl+n")
            banner = str(app.query_one("#banner").content)
            assert "Coding" in banner
            assert session.coding is True

    asyncio.run(drive())


def test_ctrl_n_starts_a_new_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WORKSPACE", raising=False)
    session = agent.Session(workspace=tmp_path / "first", model="custom-model")
    previous = session.workspace
    seen = []

    def fake_run(message, active, show_tools=True, on_tool=None):
        seen.append((message, [item["role"] for item in active.messages]))
        active.messages.append({"role": "user", "content": message})
        return "done"

    app = HarnessApp(session, fake_run, show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press(*"hello", "enter")
            for _ in range(40):
                if "Agent: done" in app.transcript_plain():
                    break
                await pilot.pause(0.05)
            else:
                raise AssertionError(app.transcript_plain())
            app.query_one("#composer").disabled = True
            await pilot.press("ctrl+n")
            assert "You: hello" in app.transcript_plain()
            assert session.messages[-1]["content"] == "hello"
            assert session.workspace == previous
            assert f"Workspace: {previous}" in str(app.query_one("#banner").content)
            app.query_one("#composer").disabled = False
            app.query_one("#composer").load_text("draft")
            await pilot.press("ctrl+n")
            assert app.transcript_plain() == ""
            assert app.query_one("#composer").text == ""
            assert session.workspace != previous
            assert [message["role"] for message in session.messages] == ["system"]
            banner = str(app.query_one("#banner").content)
            assert f"Workspace: {session.workspace}" in banner
            assert f"Workspace: {previous}" not in banner
            await pilot.press(*"next", "enter")
            for _ in range(40):
                if any(message == "next" for message, _roles in seen):
                    break
                await pilot.pause(0.05)
            else:
                raise AssertionError(seen)

    asyncio.run(drive())
    assert session.workspace.parent == (tmp_path / "workspaces").resolve()
    assert session.model == "custom-model"
    assert seen[0] == ("hello", ["system"])
    assert seen[-1] == ("next", ["system"])
    assert [message["role"] for message in session.messages] == ["system", "user"]
    assert session.messages[-1]["content"] == "next"


def _record_toasts(app):
    toasts = []

    def notify(message, *, severity="information", **_kwargs):
        toasts.append((str(message), severity))

    app.notify = notify
    return toasts


def _wait_for(pilot, predicate):
    async def wait():
        for _ in range(40):
            if predicate():
                return
            await pilot.pause(0.05)
        raise AssertionError("timed out")

    return wait()


def test_resume_restores_the_transcript(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WORKSPACE", raising=False)
    past = _past_workspace(tmp_path)
    session = agent.Session(workspace=tmp_path / "current", model="custom-model")
    seen = []

    def fake_run(message, active, show_tools=True, on_tool=None):
        seen.append(
            (
                message,
                [
                    item["content"]
                    for item in active.messages
                    if item.get("role") == "user"
                ],
            )
        )
        return "continued"

    app = HarnessApp(session, fake_run, show_tools=True)
    captured = {}

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press(*"/resume", "enter")
            assert isinstance(app.screen, ResumeScreen)
            prompt = str(
                app.screen.query_one(ResumeOptions).get_option_at_index(0).prompt
            )
            assert "fix the notes" in prompt
            assert "saved-model" in prompt
            await pilot.press("enter")
            await _wait_for(
                pilot,
                lambda: (
                    "### **Done**" in app.transcript_plain()
                    and "fix the notes" in app.transcript_plain()
                ),
            )
            thinking = app.query_one("Thinking")
            assert "alpha" not in thinking.plain
            assert "delta" in thinking.plain
            assert app.query_one("MarkdownH3")
            banner = str(app.query_one("#banner").content)
            assert f"Workspace: {past.resolve()}" in banner
            assert "Model:" not in banner
            assert _status(app)[0] == "saved-model"
            captured["plain"] = app.transcript_plain()
            await pilot.press(*"next", "enter")
            await _wait_for(pilot, lambda: bool(seen))

    asyncio.run(drive())
    assert "load_skill  name=file-ops" in captured["plain"]
    assert "✓" not in captured["plain"]
    assert seen == [("next", ["fix the notes"])]


def test_escape_leaves_the_current_transcript(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WORKSPACE", raising=False)
    _past_workspace(tmp_path)
    current = tmp_path / "current"
    session = agent.Session(workspace=current, model="custom-model")
    app = HarnessApp(session, lambda *args, **kwargs: "done", show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press(*"hello", "enter")
            await _wait_for(pilot, lambda: "Agent: done" in app.transcript_plain())
            await pilot.press(*"/resume", "enter")
            assert isinstance(app.screen, ResumeScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, ResumeScreen)
            assert "You: hello" in app.transcript_plain()
            assert "fix the notes" not in app.transcript_plain()

    asyncio.run(drive())
    assert session.workspace == current.resolve()


def test_unknown_slash_command_is_not_sent():
    seen = []

    def fake_run(message, active, show_tools=True, on_tool=None):
        seen.append(message)
        return "nope"

    session = SimpleNamespace(workspace="ws", model="m", messages=[])
    app = HarnessApp(session, fake_run, show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press(*"/nope", "enter")
            await pilot.pause()
            assert "Unknown command: /nope" in app.transcript_plain()
            await pilot.press(*"/resume extra", "enter")
            await pilot.pause()
            assert "Usage: /resume" in app.transcript_plain()
            assert not isinstance(app.screen, ResumeScreen)

    asyncio.run(drive())
    assert seen == []


def test_resume_says_when_there_are_no_sessions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WORKSPACE", raising=False)
    session = agent.Session(workspace=tmp_path / "current")
    app = HarnessApp(session, lambda *args, **kwargs: "", show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press(*"/resume", "enter")
            assert isinstance(app.screen, ResumeScreen)
            texts = [str(widget.content) for widget in app.screen.query("Static")]
            assert "No past sessions." in texts
            await pilot.press("escape")
            await pilot.pause()
            assert app.transcript_plain() == ""

    asyncio.run(drive())


def test_click_toggles_the_thinking_box_that_was_hit():
    session = SimpleNamespace(workspace="ws", model="m", messages=[])
    app = HarnessApp(session, lambda *args, **kwargs: "", show_tools=True)
    first = Thinking("alpha\nbeta\ngamma\ndelta", hold=False)
    second = Thinking("one\ntwo\nthree\nfour", hold=False)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            app.query_one("#transcript").mount(first, second)
            await pilot.pause()
            assert "click to expand" in (first.border_subtitle or "")
            assert "alpha" not in first.plain
            assert "one" not in second.plain
            # Click inside the body: the anchored transcript scrolls when a
            # panel grows, which can push a panel's top border out of view.
            await pilot.click(first, offset=(2, 2))
            assert "alpha" in first.plain
            assert "click to collapse" in (first.border_subtitle or "")
            assert "one" not in second.plain
            await pilot.click(second, offset=(2, 2))
            assert "four" in second.plain
            assert "one" in second.plain
            assert first.expanded
            await pilot.click(first, offset=(2, 2))
            assert "alpha" not in first.plain
            assert second.expanded

    asyncio.run(drive())


def test_drag_selection_keeps_the_panel_expanded():
    from textual.events import MouseDown, MouseMove, MouseUp

    session = SimpleNamespace(workspace="ws", model="m", messages=[])
    app = HarnessApp(session, lambda *args, **kwargs: "", show_tools=True)
    thinking = Thinking("alpha\nbeta\ngamma\ndelta", hold=False)
    debug = DebugBox("System", "alpha\nbeta\ngamma\ndelta")
    tools = ToolBox()
    for name in ("one", "two", "three", "four"):
        tools.add(name, {})

    async def drive():
        async with app.run_test(size=(80, 40)) as pilot:
            app.query_one("#transcript").mount(thinking, debug, tools)
            await pilot.pause()
            for widget in (thinking, debug, tools):
                await pilot.click(widget)
                assert widget.expanded
                region = widget.content_region
                start_x = region.x + 1
                end_x = region.x + 6
                y = region.y
                for event in (
                    MouseDown(widget, start_x, y, 0, 0, 1, False, False, False),
                    MouseMove(
                        widget, end_x, y, end_x - start_x, 0, 1, False, False, False
                    ),
                    MouseUp(widget, end_x, y, 0, 0, 1, False, False, False),
                ):
                    await app.on_event(event)
                await pilot.pause()
                assert widget.expanded
                assert widget.text_selection is not None

    asyncio.run(drive())


def test_ctrl_h_hides_thinking_tools_and_compaction():
    from textual.widgets import Static

    session = SimpleNamespace(workspace="ws", model="m", messages=[])
    app = HarnessApp(session, lambda *args, **kwargs: "", show_tools=True)
    thinking = Thinking("alpha\nbeta\ngamma\ndelta", hold=False)
    tools = ToolBox()
    tools.add("read_file", {"path": "a.txt"})
    compact = Static("kept the plan", markup=False, classes="panel trace")
    compact.border_title = "Context compacted"
    note = Static("You: hello", markup=False)
    answer = AgentAnswer("done")

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            transcript = app.query_one("#transcript")
            transcript.mount(note, thinking, tools, compact, answer)
            await pilot.pause()
            assert thinking.display
            await pilot.press("ctrl+h")
            assert not thinking.display
            assert not tools.display
            assert not compact.display
            assert note.display
            assert answer.display
            later = Thinking("one\ntwo\nthree\nfour", hold=False)
            transcript.mount(later)
            await pilot.pause()
            assert not later.display
            await pilot.press("ctrl+h")
            assert thinking.display
            assert tools.display
            assert compact.display
            assert later.display
            assert note.display
            assert answer.display

    asyncio.run(drive())


def test_short_thinking_hides_the_expand_hint():
    session = SimpleNamespace(workspace="ws", model="m", messages=[])
    app = HarnessApp(session, lambda *args, **kwargs: "", show_tools=True)
    short = Thinking("alpha\nbeta\ngamma", hold=False)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            app.query_one("#transcript").mount(short)
            await pilot.pause()
            assert "click" not in str(short.border_subtitle or "")
            assert "alpha" in short.plain
            await pilot.click(short)
            assert not short.expanded
            assert "click" not in str(short.border_subtitle or "")

    asyncio.run(drive())


def test_click_toggles_the_tool_box():
    session = SimpleNamespace(workspace="ws", model="m", messages=[])
    app = HarnessApp(session, lambda *args, **kwargs: "", show_tools=True)
    tools = ToolBox()
    for name in ("one", "two", "three", "four"):
        tools.add(name, {})

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            app.query_one("#transcript").mount(tools)
            await pilot.pause()
            shown = str(tools.content)
            assert "one" not in shown
            assert "four" in shown
            assert "click to expand" in (tools.border_subtitle or "")
            await pilot.click(tools)
            shown = str(tools.content)
            assert "one" in shown
            assert "click to collapse" in (tools.border_subtitle or "")
            await pilot.click(tools)
            assert "one" not in str(tools.content)
            tools.add("five", {})
            assert not tools.expanded
            assert "one" not in str(tools.content)
            assert "five" in str(tools.content)

    asyncio.run(drive())


def test_short_tool_box_hides_the_expand_hint():
    session = SimpleNamespace(workspace="ws", model="m", messages=[])
    app = HarnessApp(session, lambda *args, **kwargs: "", show_tools=True)
    tools = ToolBox()
    tools.add("read_file", {"path": "a.txt"})

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            app.query_one("#transcript").mount(tools)
            await pilot.pause()
            assert "click" not in str(tools.border_subtitle or "")
            assert "read_file" in str(tools.content)
            await pilot.click(tools)
            assert not tools.expanded
            assert "click" not in str(tools.border_subtitle or "")

    asyncio.run(drive())


def test_clipped_tool_call_expands_to_its_full_arguments():
    session = SimpleNamespace(workspace="ws", model="m", messages=[])
    app = HarnessApp(session, lambda *args, **kwargs: "", show_tools=True)
    tools = ToolBox()
    body = "line one\n" * 40 + "the very end"
    tools.add("write_file", {"path": "notes.md", "content": body})

    async def drive():
        async with app.run_test(size=(100, 30)) as pilot:
            app.query_one("#transcript").mount(tools)
            await pilot.pause()
            assert "…" in tools.plain
            assert "the very end" not in tools.plain
            assert "click to expand" in (tools.border_subtitle or "")
            await pilot.click(tools, offset=(3, 1))
            assert tools.expanded
            assert "theveryend" in "".join(tools.plain.split())
            assert "click to collapse" in (tools.border_subtitle or "")
            await pilot.click(tools, offset=(3, 1))
            assert not tools.expanded
            assert "the very end" not in tools.plain

    asyncio.run(drive())


def test_expanded_tool_box_keeps_a_new_call():
    session = SimpleNamespace(workspace="ws", model="m", messages=[])
    app = HarnessApp(session, lambda *args, **kwargs: "", show_tools=True)
    tools = ToolBox()
    for name in ("one", "two", "three", "four"):
        tools.add(name, {})

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            app.query_one("#transcript").mount(tools)
            await pilot.pause()
            await pilot.click(tools)
            tools.add("five", {})
            shown = str(tools.content)
            assert tools.expanded
            assert "one" in shown
            assert "five" in shown

    asyncio.run(drive())


def test_tool_callback_line_shows_in_the_transcript():
    session = SimpleNamespace(workspace="ws", model="m", messages=[])

    def fake_run(message, session, show_tools=True, on_tool=None):
        on_tool("load_skill", {"name": "file-ops"})
        return "loaded"

    app = HarnessApp(session, fake_run, show_tools=True)
    captured = {}

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press(*"hello", "enter")
            for _ in range(40):
                if "Agent: loaded" in app.transcript_plain():
                    break
                await pilot.pause(0.05)
            else:
                raise AssertionError(app.transcript_plain())
            captured["plain"] = app.transcript_plain()

    asyncio.run(drive())
    assert "load_skill  name=file-ops" in captured["plain"]


def test_deltas_show_before_the_turn_returns():
    import time

    session = SimpleNamespace(workspace="ws", model="m", messages=[])
    holder = {}
    errors = []

    def fake_run(message, active, show_tools=True, on_tool=None):
        app = holder["app"]
        try:
            active.on_delta("reasoning", "alpha\nbeta\ngamma\n")
            active.on_delta("reasoning", "delta")
            plain = ""
            for _ in range(40):
                plain = app.transcript_plain()
                if "delta" in plain and "alpha" not in plain:
                    break
                time.sleep(0.05)
            else:
                raise AssertionError(plain)
            on_tool("read_file", {"path": "a.txt"})
            active.on_delta("content", "Do")
            active.on_delta("content", "ne")
            for _ in range(40):
                plain = app.transcript_plain()
                if "Agent: Done" in plain and "read_file" in plain:
                    break
                time.sleep(0.05)
            else:
                raise AssertionError(plain)
            assert plain.index("delta") < plain.index("read_file")
            assert plain.index("read_file") < plain.index("Agent: Done")
        except Exception as exc:
            errors.append(exc)
            return "Done"
        errors.append(None)
        return "Done"

    app = HarnessApp(session, fake_run, show_tools=True)
    holder["app"] = app

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press(*"hello", "enter")
            await _wait_for(pilot, lambda: "Agent: Done" in app.transcript_plain())

    asyncio.run(drive())
    assert errors == [None]


def test_scrolled_up_transcript_stays_put_while_output_grows():
    from textual.widgets import Static

    session = SimpleNamespace(workspace="ws", model="m", messages=[])
    app = HarnessApp(session, lambda *args, **kwargs: "", show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            transcript = app.query_one("#transcript")
            for n in range(40):
                transcript.mount(Static(f"line {n}", markup=False))
            await pilot.pause()
            assert transcript.is_vertical_scroll_end
            transcript.scroll_to(y=0, animate=False)
            await pilot.pause()
            assert transcript.scroll_y == 0
            app._apply_delta("content", "more\n" * 10)
            await pilot.pause()
            assert transcript.scroll_y == 0
            transcript.scroll_to(y=transcript.max_scroll_y, animate=False)
            await pilot.pause()
            app._apply_delta("content", "tail\n" * 10)
            await pilot.pause()
            assert transcript.is_vertical_scroll_end

    asyncio.run(drive())


def test_streaming_answer_keeps_its_place_and_its_text():
    from textual.widgets import Markdown, Static

    session = SimpleNamespace(workspace="ws", model="m", messages=[])
    app = HarnessApp(session, lambda *args, **kwargs: "", show_tools=True)
    updates = []
    original = Markdown.update

    def record(self, markdown: str):
        updates.append(markdown)
        return original(self, markdown)

    Markdown.update = record

    async def drive():
        try:
            async with app.run_test(size=(80, 24)) as pilot:
                transcript = app.query_one("#transcript")
                for n in range(40):
                    transcript.mount(Static(f"line {n}", markup=False))
                await pilot.pause()
                transcript.scroll_to(y=0, animate=False)
                await pilot.pause()
                words = [f"w{n}" for n in range(40)]
                for word in words:
                    app._apply_delta("content", word + " ")
                await pilot.pause()
                assert transcript.scroll_y == 0
                doc = app.query_one(Markdown)
                for _ in range(30):
                    if all(word in doc.source for word in words):
                        break
                    await pilot.pause(0.05)
                else:
                    raise AssertionError(doc.source)
                shown = doc.query_one("MarkdownParagraph")._content.plain
                assert shown.count("w0") == 1
                assert shown.count("w39") == 1
                assert updates == [""]
        finally:
            Markdown.update = original

    asyncio.run(drive())


def test_mousedown_during_markdown_update_does_not_crash():
    from textual.events import MouseDown

    session = SimpleNamespace(workspace="ws", model="m", messages=[])
    app = HarnessApp(session, lambda *args, **kwargs: "", show_tools=True)
    answer = AgentAnswer("# Before\n\nClick target paragraph.")

    async def drive():
        async with app.run_test(size=(100, 30)) as pilot:
            assert isinstance(app.screen, HarnessScreen)
            app.query_one("#transcript").mount(answer)
            await pilot.pause()
            document = app.query_one("Markdown")
            old = document.query("MarkdownParagraph").first()
            x = old.region.x + 1
            y = old.region.y
            hit, offset = app.screen.get_widget_and_offset_at(x, y)
            assert hit is old
            assert offset is not None
            update = document.update(
                "\n\n".join(f"replacement paragraph {index}" for index in range(201))
            )
            try:
                for _ in range(1000):
                    await asyncio.sleep(0)
                    raw, _raw_offset = app.screen._compositor.get_widget_and_offset_at(
                        x, y
                    )
                    if old.parent is None and raw is old:
                        guarded, guarded_offset = app.screen.get_widget_and_offset_at(
                            x, y
                        )
                        assert guarded is None
                        assert guarded_offset is None
                        await app.on_event(
                            MouseDown(None, x, y, 0, 0, 1, False, False, False)
                        )
                        return
                raise AssertionError("detached markdown paragraph was not observed")
            finally:
                await update

    asyncio.run(drive())


def test_agent_answer_renders_markdown():
    answer = """\
### **Ransomware**
- **Australia**: 18%

| Date | Event |
|------|-------|
| Sep 2019 | First ABCD |"""
    session = SimpleNamespace(workspace="ws", model="m", messages=[])
    app = ReviewApp(session, "summarize", [], "", answer)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert app.query_one("MarkdownH3")
            assert app.query_one("MarkdownBulletList")
            assert app.query_one("MarkdownTable")
            assert answer in app.transcript_plain()

    asyncio.run(drive())


def _warned_session(**fields):
    skills = SimpleNamespace(warnings=[("caido", "connection refused")])
    return SimpleNamespace(
        workspace="ws", model="m", messages=[], skills=skills, **fields
    )


def test_terminal_app_ctrl_h_and_option_backspace(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")
    app = HarnessApp(_warned_session(), lambda *args, **kwargs: "", show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            composer = app.query_one(Composer)
            transcript = app.query_one("#transcript")
            composer.load_text("hello world")
            composer.move_cursor(composer.document.end)
            app.post_message(events.Key("ctrl+w", None))
            await pilot.pause()
            assert composer.text == "hello "
            assert not isinstance(app.screen, WarningScreen)
            app.post_message(events.Key("backspace", "\x08"))
            await pilot.pause()
            assert "details-hidden" in transcript.classes
            assert composer.text == "hello "
            app.post_message(events.Key("backspace", "\x7f"))
            await pilot.pause()
            assert composer.text == "hello"
            assert "details-hidden" in transcript.classes
            app.post_message(events.Key("ctrl+w", "\x17"))
            await pilot.pause()
            assert isinstance(app.screen, WarningScreen)

    asyncio.run(drive())


def test_other_terminals_keep_ctrl_w_for_warnings(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    app = HarnessApp(_warned_session(), lambda *args, **kwargs: "", show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            composer = app.query_one(Composer)
            composer.load_text("hello world")
            composer.move_cursor(composer.document.end)
            app.post_message(events.Key("ctrl+w", None))
            await pilot.pause()
            assert isinstance(app.screen, WarningScreen)
            assert composer.text == "hello world"
            frame = app.screen.query_one("#warning-frame")
            assert frame.border_subtitle == "Escape to close"
            assert "connection refused" in str(
                app.screen.query_one("#warning-box").content
            )

    asyncio.run(drive())


def test_clicking_skills_or_tools_opens_the_skill_list(tmp_path):
    session = agent.Session(workspace=tmp_path)
    session.skills.load_skill("shell")
    app = HarnessApp(session, lambda *args, **kwargs: "", show_tools=True)

    async def drive():
        async with app.run_test(size=(100, 24)) as pilot:
            bar = app.query_one("#status", StatusBar)
            for key in ("skills", "tools"):
                start, _end, _segment = next(
                    hit for hit in bar.hits if hit[2].key == key
                )
                await pilot.click("#status", offset=(start + 1, 0))
                await pilot.pause()
                assert isinstance(app.screen, SkillsScreen)
                shown = str(app.screen.query_one("#skills-box").content)
                assert shown.startswith("Loaded (1)\n  shell\n    run_shell\n")
                assert "Not loaded" in shown
                assert "  file-ops\n    read_file" in shown
                frame = app.screen.query_one("#skills-frame")
                assert frame.border_subtitle == "Escape to close"
                await pilot.press("escape")
                await pilot.pause()
                assert not isinstance(app.screen, SkillsScreen)
                assert app.focused is app.query_one(Composer)
            await _click_badge(pilot, app, "skills")
            assert isinstance(app.screen, SkillsScreen)
            await pilot.click("#skills-box")
            await pilot.pause()
            assert isinstance(app.screen, SkillsScreen)
            await pilot.click(offset=(0, 0))
            await pilot.pause()
            assert not isinstance(app.screen, SkillsScreen)
            assert app.focused is app.query_one(Composer)

    asyncio.run(drive())


async def _click_badge(pilot, app, key):
    bar = app.query_one("#status", StatusBar)
    start, _end, _segment = next(hit for hit in bar.hits if hit[2].key == key)
    await pilot.click("#status", offset=(start + 1, 0))
    await pilot.pause()


def test_clicking_the_model_switches_to_a_listed_model():
    session = SimpleNamespace(
        workspace="ws",
        model="beta",
        messages=[],
        context_tokens=500,
        context_limit=8192,
    )
    asked = []

    def context_limit(model):
        asked.append(model)
        return {"beta": 8192, "gamma": 32768}.get(model)

    app = HarnessApp(
        session,
        lambda *args, **kwargs: "",
        show_tools=True,
        context_limit=context_limit,
        models=lambda: ["alpha", "beta", "gamma"],
    )
    toasts = _record_toasts(app)

    async def drive():
        async with app.run_test(size=(100, 24)) as pilot:
            await _click_badge(pilot, app, "model")
            await _wait_for(pilot, lambda: isinstance(app.screen, SwitchModelScreen))
            options = app.screen.query_one("#models")
            assert options.highlighted == 1
            labels = [str(options.get_option_at_index(i).prompt) for i in range(3)]
            assert labels == ["1. alpha", "2. beta (current)", "3. gamma"]
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, SwitchModelScreen)
            assert session.model == "beta"
            assert app.focused is app.query_one(Composer)
            await _click_badge(pilot, app, "model")
            await _wait_for(pilot, lambda: isinstance(app.screen, SwitchModelScreen))
            await pilot.click("#picker-box")
            await pilot.pause()
            assert isinstance(app.screen, SwitchModelScreen)
            await pilot.click(offset=(0, 0))
            await pilot.pause()
            assert not isinstance(app.screen, SwitchModelScreen)
            assert session.model == "beta"
            assert app.focused is app.query_one(Composer)
            await _click_badge(pilot, app, "model")
            await _wait_for(pilot, lambda: isinstance(app.screen, SwitchModelScreen))
            await pilot.press("down", "enter")
            await _wait_for(pilot, lambda: session.context_limit == 32768)
            assert session.model == "gamma"
            assert asked[-1] == "gamma"
            assert _status(app)[:2] == ["gamma", "ctx 500/33k 2%"]
            assert toasts[-1] == ("Model: gamma", "information")
            assert app.focused is app.query_one(Composer)

    asyncio.run(drive())


def test_model_list_failures_show_a_toast():
    session = SimpleNamespace(workspace="ws", model="m", messages=[])

    def unreachable():
        raise SystemExit("Could not reach the API.")

    for models, expected in (
        (unreachable, ("Could not reach the API.", "error")),
        (list, ("No models available from the API.", "warning")),
    ):
        app = HarnessApp(
            session, lambda *args, **kwargs: "", show_tools=True, models=models
        )
        toasts = _record_toasts(app)

        async def drive(app=app, toasts=toasts, expected=expected):
            async with app.run_test(size=(100, 24)) as pilot:
                await _click_badge(pilot, app, "model")
                await _wait_for(pilot, lambda: bool(toasts))
                assert toasts == [expected]
                assert not isinstance(app.screen, SwitchModelScreen)
                assert session.model == "m"

        asyncio.run(drive())


def test_clicking_the_status_warning_opens_details():
    app = HarnessApp(_warned_session(), lambda *args, **kwargs: "", show_tools=True)

    async def drive():
        async with app.run_test(size=(100, 24)) as pilot:
            bar = app.query_one("#status", StatusBar)
            assert "⚠ 1" in _status(app)
            await pilot.click("#status", offset=(1, 0))
            await pilot.pause()
            assert not isinstance(app.screen, WarningScreen)
            start, _end, _segment = next(
                hit for hit in bar.hits if hit[2].key == "warnings"
            )
            await pilot.click("#status", offset=(start + 1, 0))
            await pilot.pause()
            assert isinstance(app.screen, WarningScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, WarningScreen)
            await pilot.click("#status", offset=(start + 1, 0))
            await pilot.pause()
            assert isinstance(app.screen, WarningScreen)
            await pilot.click("#warning-box")
            await pilot.pause()
            assert isinstance(app.screen, WarningScreen)
            await pilot.click(offset=(0, 0))
            await pilot.pause()
            assert not isinstance(app.screen, WarningScreen)

    asyncio.run(drive())


def test_ctrl_j_adds_a_newline():
    session = SimpleNamespace(workspace="ws", model="m", messages=[])
    app = HarnessApp(session, lambda *args, **kwargs: "", show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("a", "ctrl+j", "b")
            assert app.query_one(Composer).text == "a\nb"

    asyncio.run(drive())


def test_escape_stops_a_turn_and_keeps_the_app():
    started = threading.Event()
    session = SimpleNamespace(
        workspace="ws", model="m", messages=[], abort=threading.Event()
    )

    def fake_run(message, active, show_tools=True, on_tool=None):
        if message == "slow":
            on_tool("run_shell", {"command": "sleep 100"})
            started.set()
            assert active.abort.wait(timeout=2)
            return ""
        return "quick answer"

    app = HarnessApp(session, fake_run, show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            activity = app.query_one("#activity")
            assert not activity.display
            await pilot.press(*"slow", "enter")
            await _wait_for(pilot, started.is_set)
            await pilot.pause()
            assert activity.display
            shown = str(activity.content)
            assert "Running run_shell…" in shown
            assert "Esc to stop" in shown
            await pilot.press("escape")
            await _wait_for(pilot, lambda: "Stopped." in app.transcript_plain())
            plain = app.transcript_plain()
            assert "✗ run_shell" in plain
            assert not app._exit
            assert not session.abort.is_set()
            assert not app.query_one(Composer).disabled
            assert not activity.display
            await pilot.press(*"next", "enter")
            await _wait_for(
                pilot, lambda: "Agent: quick answer" in app.transcript_plain()
            )

    asyncio.run(drive())


def test_enter_continues_past_max_turns():
    session = SimpleNamespace(workspace="ws", model="m", messages=[])

    def fake_run(message, active, show_tools=True, on_tool=None):
        assert active.confirm_continue() is True
        return "kept going"

    app = HarnessApp(session, fake_run, show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press(*"go", "enter")
            await _wait_for(pilot, lambda: isinstance(app.screen, ContinueScreen))
            shown = str(app.screen.query_one("#continue-prompt").content)
            assert "Continue for another" in shown
            assert "turns?" in shown
            activity = str(app.query_one("#activity").content)
            assert "Max turns reached" in activity
            assert "Esc to stop" not in activity
            await pilot.press("enter")
            await _wait_for(
                pilot, lambda: "Agent: kept going" in app.transcript_plain()
            )
            assert not isinstance(app.screen, ContinueScreen)

    asyncio.run(drive())


def test_escape_stops_at_max_turns():
    session = SimpleNamespace(workspace="ws", model="m", messages=[])

    def fake_run(message, active, show_tools=True, on_tool=None):
        assert active.confirm_continue() is False
        return "Max turns reached."

    app = HarnessApp(session, fake_run, show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press(*"go", "enter")
            await _wait_for(pilot, lambda: isinstance(app.screen, ContinueScreen))
            await pilot.press("escape")
            await _wait_for(
                pilot, lambda: "Agent: Max turns reached." in app.transcript_plain()
            )
            assert not isinstance(app.screen, ContinueScreen)
            assert app.query_one(AgentAnswer).has_class("error")

    asyncio.run(drive())


def test_one_shot_enter_continues_past_max_turns():
    session = SimpleNamespace(workspace="ws", model="m", messages=[])

    def fake_run(message, active, show_tools=True, on_tool=None):
        assert active.confirm_continue() is True
        return "kept going"

    app = OneShotApp(session, fake_run, show_tools=True, initial="go")

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await _wait_for(pilot, lambda: isinstance(app.screen, ContinueScreen))
            await pilot.press("enter")
            await _wait_for(
                pilot, lambda: "Agent: kept going" in app.transcript_plain()
            )

    asyncio.run(drive())


def test_model_turns_head_each_round():
    session = SimpleNamespace(workspace="ws", model="m", messages=[])

    def fake_run(message, active, show_tools=True, on_tool=None):
        active.on_model_turn(1)
        on_tool("read_file", {"path": "a.txt"})
        active.on_model_turn(2)
        return "done"

    app = HarnessApp(session, fake_run, show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press(*"go", "enter")
            await _wait_for(pilot, lambda: "Agent: done" in app.transcript_plain())
            plain = app.transcript_plain()
            assert plain.index("Turn 1") < plain.index("read_file")
            assert plain.index("read_file") < plain.index("Turn 2")
            assert plain.index("Turn 2") < plain.index("Agent: done")
            headings = list(app.query(TurnHeading))
            assert [heading.number for heading in headings] == [1, 2]
            assert all(list(heading.query(Rule)) for heading in headings)

    asyncio.run(drive())


def test_tool_results_mark_each_call():
    session = SimpleNamespace(workspace="ws", model="m", messages=[])

    def fake_run(message, active, show_tools=True, on_tool=None):
        on_tool("read_file", {"path": "a.txt"})
        active.on_tool_done("read_file", "contents")
        on_tool("run_shell", {"command": "false"})
        active.on_tool_done("run_shell", "Error: exit 1\nmore detail")
        return "done"

    app = HarnessApp(session, fake_run, show_tools=True)

    async def drive():
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press(*"go", "enter")
            await _wait_for(pilot, lambda: "Agent: done" in app.transcript_plain())
            box = app.query_one(ToolBox)
            box.toggle()
            plain = app.transcript_plain()
            assert "✓ read_file  path=a.txt" in plain
            assert "✗ run_shell  command=false" in plain
            assert "Error: exit 1" in plain
            assert "more detail" not in plain

    asyncio.run(drive())


def test_failure_answers_and_user_messages_are_styled():
    session = SimpleNamespace(workspace="ws", model="m", messages=[])
    answers = iter(["Could not reach the API.", "fine"])
    app = HarnessApp(session, lambda *args, **kwargs: next(answers), show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press(*"one", "enter")
            await _wait_for(pilot, lambda: len(app.query(AgentAnswer)) == 1)
            await pilot.press(*"two", "enter")
            await _wait_for(pilot, lambda: len(app.query(AgentAnswer)) == 2)
            failed, ok = list(app.query(AgentAnswer))
            assert failed.has_class("error")
            assert not ok.has_class("error")
            users = list(app.query(UserMessage))
            assert [user.source for user in users] == ["one", "two"]
            assert all(user.has_class("user") for user in users)
            await pilot.press(*"/nope", "enter")
            notes = list(app.query(".note.error"))
            assert [str(note.content) for note in notes] == ["Unknown command: /nope"]

    asyncio.run(drive())


def test_up_and_down_recall_prompts():
    session = SimpleNamespace(
        workspace="ws",
        model="m",
        messages=[
            {"role": "user", "content": "older"},
            {"role": "assistant", "content": "x"},
        ],
    )
    app = HarnessApp(session, lambda *args, **kwargs: "ok", show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press(*"newer", "enter")
            await _wait_for(pilot, lambda: "Agent: ok" in app.transcript_plain())
            composer = app.query_one(Composer)
            await pilot.press("d", "r")
            await pilot.press("up")
            assert composer.text == "newer"
            await pilot.press("up")
            assert composer.text == "older"
            await pilot.press("up")
            assert composer.text == "older"
            await pilot.press("down")
            assert composer.text == "newer"
            await pilot.press("down")
            assert composer.text == "dr"

    asyncio.run(drive())


def test_slash_hint_and_tab_completion():
    session = SimpleNamespace(workspace="ws", model="m", messages=[])
    app = HarnessApp(session, lambda *args, **kwargs: "", show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            hint = app.query_one("#slash-hint")
            composer = app.query_one(Composer)
            assert not hint.display
            await pilot.press("/", "e")
            assert hint.display
            assert str(hint.content) == "/export  /exit · Tab completes"
            await pilot.press("tab")
            assert composer.text == "/export "
            assert not hint.display
            composer.load_text("")
            await pilot.press("/", "d", "e", "tab")
            assert composer.text == "/details"
            await pilot.press("enter")
            assert "details-hidden" in app.query_one("#transcript").classes
            assert not hint.display

    asyncio.run(drive())


def test_at_mention_inserts_a_project_path(tmp_path):
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "app.py").write_text("print(1)\n")
    (project / "readme.md").write_text("hi\n")
    calls = []

    def run(*args, **kwargs):
        calls.append(args[0])
        return "ok"

    session = SimpleNamespace(
        workspace="ws",
        model="m",
        messages=[],
        coding=True,
        project=project,
        abort=threading.Event(),
    )
    app = HarnessApp(session, run, show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            menu = app.query_one("#mention-menu")
            composer = app.query_one(Composer)
            await pilot.press(*"older", "enter")
            await _wait_for(pilot, lambda: "Agent: ok" in app.transcript_plain())
            await pilot.press("@")
            assert menu.display
            assert "> readme.md" in str(menu.content)
            await pilot.press("up")
            assert composer.text == "@"
            app._busy = True
            await pilot.press("escape")
            assert not session.abort.is_set()
            app._busy = False
            assert not menu.display
            assert composer.text == "@"
            await pilot.press(*"src/app")
            assert "> src/app.py" in str(menu.content)
            await pilot.press("enter")
            assert composer.text == "@src/app.py "
            assert not menu.display
            assert calls == ["older"]
            composer.load_text("")
            await pilot.pause()
            await pilot.press("@", "down", "tab")
            assert composer.text == "@src/ "
            composer.load_text("")
            await pilot.pause()
            await pilot.press(*"@zzz", "enter")
            await _wait_for(pilot, lambda: "@zzz" in app.transcript_plain())
            assert calls[-1] == "@zzz"
            (project / "new.py").write_text("x\n")
            await pilot.press(*"@new")
            assert "new.py" in str(menu.content)

    asyncio.run(drive())


def test_at_mention_scrolls_past_the_visible_rows(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    for index in range(10):
        (project / f"f{index:02}.txt").write_text("x\n")
    session = SimpleNamespace(
        workspace="ws",
        model="m",
        messages=[],
        coding=True,
        project=project,
    )
    app = HarnessApp(session, lambda *args, **kwargs: "", show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            menu = app.query_one("#mention-menu")
            await pilot.press("@")
            lines = str(menu.content).splitlines()
            assert lines[0] == "> f00.txt"
            assert lines[-1] == "  f07.txt"
            assert "f08.txt" not in lines
            for _step in range(8):
                await pilot.press("down")
            lines = str(menu.content).splitlines()
            assert lines[0] == "  f01.txt"
            assert lines[-1] == "> f08.txt"
            assert "f00.txt" not in lines
            await pilot.press("down")
            lines = str(menu.content).splitlines()
            assert lines[-1] == "> f09.txt"
            await pilot.press("up")
            lines = str(menu.content).splitlines()
            assert "> f08.txt" in lines
            assert lines[-1] == "  f09.txt"
            for _step in range(8):
                await pilot.press("up")
            assert str(menu.content).splitlines()[0] == "> f00.txt"

    asyncio.run(drive())


def test_at_mention_enters_a_directory_and_returns_to_its_parent(tmp_path):
    project = tmp_path / "proj"
    (project / "src" / "pkg").mkdir(parents=True)
    (project / "app.py").write_text("x\n")
    (project / "src" / "app.py").write_text("x\n")
    (project / "src" / "pkg" / "code.py").write_text("x\n")
    session = SimpleNamespace(
        workspace="ws",
        model="m",
        messages=[],
        coding=True,
        project=project,
    )
    app = HarnessApp(session, lambda *args, **kwargs: "", show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            menu = app.query_one("#mention-menu")
            composer = app.query_one(Composer)
            await pilot.press("@")
            assert str(menu.content) == "> app.py\n  src/"
            await pilot.press("right")
            assert composer.text == "@"
            assert str(menu.content) == "> app.py\n  src/"
            await pilot.press("down", "right")
            assert composer.text == "@src/"
            assert str(menu.content) == "> src/app.py\n  src/pkg/"
            await pilot.press("right")
            assert composer.text == "@src/"
            await pilot.press("down", "right")
            assert composer.text == "@src/pkg/"
            assert str(menu.content) == "> src/pkg/code.py"
            await pilot.press("right")
            assert composer.text == "@src/pkg/"
            await pilot.press("left")
            assert composer.text == "@src/"
            assert str(menu.content) == "  src/app.py\n> src/pkg/"
            await pilot.press("left")
            assert composer.text == "@"
            assert str(menu.content) == "  app.py\n> src/"
            await pilot.press("left")
            assert composer.text == "@"
            assert str(menu.content) == "  app.py\n> src/"

    asyncio.run(drive())


def test_at_mention_stays_closed_outside_a_coding_session(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "app.py").write_text("x")
    session = SimpleNamespace(
        workspace="ws",
        model="m",
        messages=[],
        coding=False,
        project=project,
    )
    app = HarnessApp(session, lambda *args, **kwargs: "", show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("@")
            assert not app.query_one("#mention-menu").display
            assert app.query_one(Composer).text == "@"

    asyncio.run(drive())


def test_new_command_starts_a_new_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WORKSPACE", raising=False)
    session = agent.Session(workspace=tmp_path / "first", model="custom-model")
    previous = session.workspace
    app = HarnessApp(session, lambda *args, **kwargs: "done", show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press(*"hello", "enter")
            await _wait_for(pilot, lambda: "Agent: done" in app.transcript_plain())
            await pilot.press(*"/new", "enter")
            await _wait_for(pilot, lambda: session.workspace != previous)
            await pilot.pause()
            assert app.transcript_plain() == ""
            assert f"Workspace: {session.workspace}" in str(
                app.query_one("#banner").content
            )

    asyncio.run(drive())


def test_resume_filter_narrows_the_list(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WORKSPACE", raising=False)
    _past_workspace(tmp_path)
    trip = tmp_path / "workspaces" / "abcdef02-0000-0000-0000-000000000002"
    _write_transcript(
        trip,
        "saved-model",
        [
            {"role": "system", "content": "stale"},
            {"role": "user", "content": "plan the trip"},
            {"role": "assistant", "content": "trip planned"},
        ],
    )
    session = agent.Session(workspace=tmp_path / "current", model="custom-model")
    app = HarnessApp(session, lambda *args, **kwargs: "", show_tools=True)

    async def drive():
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press(*"/resume", "enter")
            assert isinstance(app.screen, ResumeScreen)
            options = app.screen.query_one(ResumeOptions)
            assert options.option_count == 2
            await pilot.press(*"trip")
            assert options.option_count == 1
            assert "plan the trip" in str(options.get_option_at_index(0).prompt)
            status = str(app.screen.query_one("#status").content)
            assert status.startswith("Filter: trip · 1 match")
            await pilot.press("backspace", "backspace", "backspace", "backspace")
            assert options.option_count == 2
            await pilot.press(*"trip", "enter")
            await _wait_for(pilot, lambda: "trip planned" in app.transcript_plain())

    asyncio.run(drive())
    assert session.workspace == trip.resolve()


def test_context_warning_colors(monkeypatch):
    monkeypatch.setattr("harnh3x.tui.app.COMPACT_AT", 0.5)
    session = SimpleNamespace(
        workspace="ws",
        model="m",
        messages=[],
        context_tokens=450,
        context_limit=1000,
    )
    app = HarnessApp(session, lambda *args, **kwargs: "", show_tools=True)

    async def drive():
        async with app.run_test(size=(100, 24)) as pilot:
            assert _status(app)[1] == "ctx 450/1.0k 45%"
            assert _status_styles(app)["context"] == "ansi_black on ansi_yellow"
            session.context_tokens = 600
            app._refresh_status()
            await pilot.pause()
            assert _status(app)[1] == "ctx 600/1.0k 60%"
            assert _status_styles(app)["context"] == "bold ansi_white on ansi_red"
            session.context_tokens = 100
            app._refresh_status()
            await pilot.pause()
            assert _status_styles(app)["context"] == "ansi_white on ansi_bright_black"

    asyncio.run(drive())


def test_panels_toggle_from_the_keyboard_and_clicks_keep_focus():
    session = SimpleNamespace(workspace="ws", model="m", messages=[])
    app = HarnessApp(session, lambda *args, **kwargs: "", show_tools=True)
    thinking = Thinking("alpha\nbeta\ngamma\ndelta", hold=False)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            app.query_one("#transcript").mount(thinking)
            await pilot.pause()
            composer = app.query_one(Composer)
            await pilot.click(thinking, offset=(2, 2))
            assert thinking.expanded
            assert app.focused is composer
            thinking.focus()
            await pilot.press("enter")
            assert not thinking.expanded
            await pilot.press("space")
            assert thinking.expanded

    asyncio.run(drive())
