"""The Textual app driven through a pilot."""

import asyncio
import threading
from types import SimpleNamespace

from helpers import _export_messages, _past_workspace

from harnh3x import __version__, agent
from harnh3x.tui import (
    EXPORT_USAGE,
    AgentAnswer,
    CopyAnswer,
    DebugBox,
    ExportAnswer,
    ExportPathScreen,
    ExportScreen,
    HarnessApp,
    HarnessScreen,
    HelpScreen,
    ResumeOptions,
    ResumeScreen,
    ReviewApp,
    Thinking,
    ToolBox,
)


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
            version = app.query_one("#version")
            assert str(version.content) == __version__
            assert version.region.right == app.size.width
            assert version.region.y == app.query_one("#status").region.y
            assert "Context:" not in banner
            assert "Model:" not in banner
            status = str(app.query_one("#status").content)
            assert status == "Model: m · Context: 1,234 / 8,192"
            await pilot.press(*"hi", "enter")
            await _wait_for(pilot, lambda: "Agent: done" in app.transcript_plain())
            banner = str(app.query_one("#banner").content)
            assert "Context:" not in banner
            status = str(app.query_one("#status").content)
            assert status == "Model: m · Context: 5,000 / 8,192"
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
            assert "Exported to" in app.transcript_plain()
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
            copy.scroll_visible()
            await pilot.pause()
            await pilot.click(copy)
            await pilot.pause()
            assert app.clipboard == "draft"
            button.scroll_visible()
            await pilot.pause()
            await pilot.click(button)
            await pilot.pause()
            assert isinstance(app.screen, ExportPathScreen)
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
            assert str(app.query_one("#status").content) == "Model: m · Debug"
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
            skills, tools = session.skills.available_counts()
            expected = f"Model: custom-model · {skills} skills · {tools} tools · Debug"
            assert "\nDebug\n" not in str(app.query_one("#banner").content)
            assert "Model:" not in str(app.query_one("#banner").content)
            assert str(app.query_one("#status").content) == expected
            await pilot.press("ctrl+n")
            skills, tools = session.skills.available_counts()
            expected = f"Model: custom-model · {skills} skills · {tools} tools · Debug"
            assert "\nDebug\n" not in str(app.query_one("#banner").content)
            assert str(app.query_one("#status").content) == expected
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
            skills, tools = plain.skills.available_counts()
            assert (
                str(plain_app.query_one("#status").content)
                == f"Model: custom-model · {skills} skills · {tools} tools"
            )
            composer = plain_app.query_one("#composer")
            status_bar = plain_app.query_one("#status")
            assert composer.region.bottom == status_bar.region.y
            assert status_bar.region.bottom == plain_app.size.height
        async with app.run_test(size=(80, 24)) as pilot:
            assert "YOLO" not in str(app.query_one("#banner").content)
            content = app.query_one("#status").content
            skills, tools = session.skills.available_counts()
            assert (
                str(content)
                == f"Model: custom-model · {skills} skills · {tools} tools · YOLO"
            )
            text = str(content)
            yellow = [
                text[span.start : span.end]
                for span in content.spans
                if span.style == "bold ansi_bright_yellow"
            ]
            assert yellow == ["YOLO"]
            composer = app.query_one("#composer")
            status_bar = app.query_one("#status")
            assert composer.region.bottom == status_bar.region.y
            assert status_bar.region.bottom == app.size.height
            await pilot.press("ctrl+n")
            assert "YOLO" not in str(app.query_one("#banner").content)
            content = app.query_one("#status").content
            skills, tools = session.skills.available_counts()
            assert (
                str(content)
                == f"Model: custom-model · {skills} skills · {tools} tools · YOLO"
            )
            text = str(content)
            yellow = [
                text[span.start : span.end]
                for span in content.spans
                if span.style == "bold ansi_bright_yellow"
            ]
            assert yellow == ["YOLO"]
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
            assert "Model: saved-model" in str(app.query_one("#status").content)
            captured["plain"] = app.transcript_plain()
            await pilot.press(*"next", "enter")
            await _wait_for(pilot, lambda: bool(seen))

    asyncio.run(drive())
    assert "🔧 load_skill({'name': 'file-ops'})" in captured["plain"]
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
    assert "🔧 load_skill({'name': 'file-ops'})" in captured["plain"]


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
