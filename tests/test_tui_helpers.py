"""Pure TUI helpers: wrapping, transcript items, and export."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers import _export_messages

from harnh3x import is_failure_answer, matching_commands
from harnh3x.tui import (
    ModelApp,
    activity_text,
    context_level,
    context_line,
    conversation_turns,
    elapsed_text,
    export_base,
    export_filename,
    exportable_answers,
    format_call,
    render_export,
    slash_hint,
    transcript_items,
    translate_legacy_key,
    write_export,
)


def test_translate_legacy_key_maps_terminal_app_sequences():
    apple = "Apple_Terminal"
    assert translate_legacy_key("backspace", "\x08", apple) == "ctrl+h"
    assert translate_legacy_key("ctrl+w", None, apple) == "alt+backspace"
    assert translate_legacy_key("backspace", "\x7f", apple) == "backspace"
    assert translate_legacy_key("ctrl+w", "\x17", apple) == "ctrl+w"
    assert translate_legacy_key("enter", "\r", apple) == "enter"
    for other in ("iTerm.app", "vscode", None):
        assert translate_legacy_key("backspace", "\x08", other) == "backspace"
        assert translate_legacy_key("ctrl+w", None, other) == "ctrl+w"


def test_format_call_shows_readable_arguments():
    assert format_call("read_file", {"path": "a.txt"}) == "read_file  path=a.txt"
    assert (
        format_call("run_shell", {"command": "ls -la", "timeout": 5})
        == 'run_shell  command="ls -la"  timeout=5'
    )
    assert format_call("write_file", {"content": "a\nb"}) == (
        'write_file  content="a\\nb"'
    )
    long = format_call("write_file", {"content": "x" * 200})
    assert len(long) < 120
    assert long.endswith("…")
    whole = format_call("write_file", {"content": "x" * 200}, limit=None)
    assert whole == "write_file  content=" + "x" * 200
    assert format_call("unload_skill", {}) == "unload_skill"
    assert format_call("broken", "{not json") == "broken  {not json"


def test_context_level_warns_before_compaction():
    assert context_level(None, 1000, 0.5) is None
    assert context_level(100, None, 0.5) is None
    assert context_level(399, 1000, 0.5) is None
    assert context_level(400, 1000, 0.5) == "warn"
    assert context_level(499, 1000, 0.5) == "warn"
    assert context_level(500, 1000, 0.5) == "over"


def test_activity_text_shows_phase_time_and_stop_hint():
    assert elapsed_text(3.9) == "3s"
    assert elapsed_text(65) == "1m 05s"
    text = activity_text(0, "Thinking…", 12.4, stoppable=True)
    assert text.endswith("Thinking… · 12s · Esc to stop")
    assert "Esc" not in activity_text(1, "Stopping…", 1, stoppable=False)


def test_slash_hint_lists_matching_commands():
    assert [command.name for command in matching_commands("/e")] == [
        "export",
        "exit",
    ]
    assert matching_commands("/export path") == []
    assert matching_commands("hello") == []
    assert matching_commands("/a\nb") == []
    assert slash_hint(matching_commands("/e")) == "/export  /exit · Tab completes"
    assert slash_hint(matching_commands("/det")).startswith("/details  hide or show")


def test_failure_answers_are_recognized():
    assert is_failure_answer("Could not reach the API.")
    assert is_failure_answer("The API request timed out.")
    assert is_failure_answer("The API rejected the request: bad model")
    assert is_failure_answer("Max turns reached.")
    assert is_failure_answer("The model returned an empty answer.")
    assert not is_failure_answer("All done.")
    assert not is_failure_answer(None)


def test_export_filename_uses_the_time():
    from datetime import datetime

    assert export_filename(datetime(2026, 10, 16, 23, 5)) == "answer-20261016-2305.md"


def _run(app, keys: list[str]):
    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            if keys:
                await pilot.press(*keys)
            return app

    asyncio.run(drive())


def test_model_screen_accepts_arrows_or_a_number():
    app = ModelApp(["alpha", "beta", "gamma"])
    _run(app, ["down", "down", "enter"])
    assert app.return_value == "gamma"

    app = ModelApp(["alpha", "beta"])

    async def invalid_then_one():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("9", "enter")
            status = str(app.screen.query_one("#status").content)
            assert "Enter a number from 1 to 2." in status
            await pilot.press("1", "enter")

    asyncio.run(invalid_then_one())
    assert app.return_value == "alpha"


def test_context_line_formats_used_and_limit():
    assert context_line(1234, 8192) == "Context: 1,234 / 8,192 (15%)"
    assert context_line(0, 8192) == "Context: 0 / 8,192 (0%)"
    assert context_line(None, 8192) == "Context: — / 8,192"
    assert context_line(50, None) == "Context: 50"
    assert context_line(None, None) is None


def test_transcript_items_keep_compaction_notes_in_order():
    messages = [
        {"role": "system", "content": "s"},
        {
            "role": "user",
            "content": "Context compacted:\nkept the greeting",
            "context_before": 1526,
            "context_after": 412,
        },
        {"role": "user", "content": "What tools do you have"},
        {"role": "assistant", "content": "file tools"},
    ]
    items = transcript_items(messages)
    assert [kind for kind, _item in items] == ["compact", "turn"]
    assert items[0][1].summary == "kept the greeting"
    assert items[0][1].before == 1526
    assert items[0][1].after == 412
    assert items[1][1].user == "What tools do you have"
    assert [turn.user for turn in conversation_turns(messages)] == [
        "What tools do you have"
    ]
    past = transcript_items(
        [
            {"role": "user", "content": "fix the notes"},
            {
                "role": "assistant",
                "content": "",
                "reasoning": "alpha\nbeta\ngamma\ndelta",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "load_skill",
                            "arguments": '{"name": "file-ops"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "loaded"},
            {"role": "assistant", "content": "### **Done**"},
        ]
    )
    assert [step[0] for step in past[0][1].steps] == ["thinking", "tools", "answer"]


def test_exportable_answers_keep_final_replies_only():
    messages = _export_messages()
    assert exportable_answers(messages) == ["final alpha", "beta answer"]
    text = render_export(exportable_answers(messages))
    assert text == "final alpha\n\n---\n\nbeta answer\n"
    assert "draft" not in text
    assert "hidden thought" not in text
    assert "tool body" not in text
    assert "first question" not in text
    assert "secret thinking" not in text
    reasoning_only = [
        {"role": "user", "content": "think"},
        {"role": "assistant", "content": "", "reasoning": "the reply"},
    ]
    assert exportable_answers(reasoning_only) == ["the reply"]
    assert render_export(["already\n"]) == "already\n"
    assert exportable_answers([]) == []


def test_write_export_resolves_and_overwrites(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(tmp_path)
    path = write_export("notes/out.md", "alpha\n", base=project)
    assert path == (project / "notes" / "out.md").resolve()
    assert path.read_text(encoding="utf-8") == "alpha\n"
    write_export("notes/out.md", "beta\n", base=project)
    assert path.read_text(encoding="utf-8") == "beta\n"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    tilde = write_export("~/saved.md", "home\n", base=project)
    assert tilde == (home / "saved.md").resolve()
    assert tilde.read_text(encoding="utf-8") == "home\n"
    assert export_base(SimpleNamespace(project=project)) == project
    assert export_base(SimpleNamespace(project=None)) == Path.cwd()
    with pytest.raises(ValueError, match="is a directory"):
        write_export(str(project), "nope\n", base=project)
    with pytest.raises(ValueError, match="Usage"):
        write_export("  ", "nope\n", base=project)
    blocked = project / "file"
    blocked.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="Could not write"):
        write_export("file/nested.md", "nope\n", base=project)
