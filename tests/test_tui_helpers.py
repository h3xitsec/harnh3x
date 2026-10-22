"""Pure TUI helpers: wrapping, transcript items, and export."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers import _export_messages

from harnh3x import CacheRound, is_failure_answer, matching_commands
from harnh3x.tui import (
    MENTION_ROWS,
    Mention,
    ModelApp,
    Segment,
    StatusState,
    activity_text,
    build_segments,
    cache_details,
    cache_text,
    compact_count,
    context_level,
    context_text,
    conversation_turns,
    elapsed_text,
    export_base,
    export_filename,
    exportable_answers,
    filter_mentions,
    format_call,
    index_project,
    insert_mention,
    layout_segments,
    mention_at,
    mention_browse,
    mention_children,
    mention_menu,
    mention_window,
    pick_model,
    render_export,
    replace_mention_query,
    skills_text,
    slash_hint,
    transcript_items,
    translate_legacy_key,
    write_export,
)
from harnh3x.tui.status import BADGE_STYLE, CONTEXT_STYLES


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


def test_mention_token_stops_at_whitespace_and_finished_quotes():
    assert mention_at("look @src/fo", len("look @src/fo")) == (5, 12, "src/fo")
    assert mention_at("user@host", len("user@host")) is None
    assert mention_at("@", 1) == (0, 1, "")
    assert mention_at("@src/foo bar", len("@src/foo bar")) is None
    assert mention_at("@src/foo bar", len("@src/foo")) == (0, 8, "src/foo")
    assert mention_at("hello\n@src", len("hello\n@src")) == (6, 10, "src")
    quoted = '@"my file.py" '
    assert mention_at(quoted, len(quoted)) is None
    assert mention_at('@"my file', len('@"my file')) == (0, 9, "my file")


def test_insert_mention_quotes_spaces_and_marks_folders():
    text = "see @src and more"
    start, end, _query = mention_at(text, len("see @src"))
    updated, cursor = insert_mention(text, start, end, Mention("src/app.py", False))
    assert updated == "see @src/app.py  and more"
    assert cursor == len("see @src/app.py ")
    folder, _cursor = insert_mention("@tu", 0, 3, Mention("src/tui", True))
    assert folder == "@src/tui/ "
    spaced, _cursor = insert_mention("@", 0, 1, Mention("my file.py", False))
    assert spaced == '@"my file.py" '
    assert mention_at(spaced, len(spaced)) is None
    quoted, _cursor = insert_mention("@", 0, 1, Mention('say "hi".py', False))
    assert quoted == '@"say \\"hi\\".py" '
    assert mention_at(quoted, len(quoted)) is None
    assert mention_menu([Mention("readme.md", False), Mention("src", True)], 1) == (
        "  readme.md\n> src/"
    )


def test_mention_browse_enters_directories_and_keeps_a_filter():
    entries = [
        Mention("readme.md", False),
        Mention("src", True),
        Mention("src/app.py", False),
        Mention("src/pkg", True),
        Mention("src/pkg/code.py", False),
    ]
    assert mention_browse(entries, "") == ("", "")
    assert mention_browse(entries, "src") == ("", "src")
    assert mention_browse(entries, "src/") == ("src", "")
    assert mention_browse(entries, "src/app") == ("src", "app")
    assert mention_browse(entries, "src/pkg/code") == ("src/pkg", "code")
    assert mention_browse(entries, "src/nope/x") == ("src", "nope/x")
    assert {item.path for item in mention_children(entries, "")} == {
        "readme.md",
        "src",
    }
    assert {item.path for item in mention_children(entries, "src")} == {
        "src/app.py",
        "src/pkg",
    }
    text, cursor = replace_mention_query("@src", 0, len("@src"), "src/pkg/")
    assert (text, cursor) == ("@src/pkg/", len("@src/pkg/"))
    assert mention_at(text, cursor)[2] == "src/pkg/"
    quoted, quoted_cursor = replace_mention_query("@", 0, 1, 'say "hi"/')
    assert quoted == '@"say \\"hi\\"/'
    assert mention_at(quoted, quoted_cursor)[2] == 'say "hi"/'


def test_filter_mentions_ranks_subsequence_matches():
    entries = [
        Mention("tui/app.py", False),
        Mention("app.py", False),
        Mention("src/app_helper.py", False),
        Mention("other/tapp.py", False),
        Mention("readme.md", False),
    ]
    assert [item.path for item in filter_mentions(entries, "tapp")] == [
        "other/tapp.py",
        "tui/app.py",
    ]
    assert filter_mentions(entries, "app", limit=0) == []
    assert [item.path for item in filter_mentions(entries, "app")] == [
        "app.py",
        "tui/app.py",
        "src/app_helper.py",
        "other/tapp.py",
    ]
    shallow = filter_mentions(
        [
            Mention("b/c.py", False),
            Mention("a.py", False),
            Mention("b", True),
        ],
        "",
    )
    assert [item.path for item in shallow] == ["a.py", "b", "b/c.py"]
    many = [Mention(f"a{index}.txt", False) for index in range(9)]
    shown = filter_mentions(many, "")
    assert [item.path for item in shown] == [f"a{index}.txt" for index in range(9)]
    selected, offset, menu = mention_window(shown, 0, 0)
    assert (selected, offset) == (0, 0)
    assert menu.splitlines()[0] == "> a0.txt"
    assert menu.splitlines()[-1] == "  a7.txt"
    assert len(menu.splitlines()) == MENTION_ROWS
    for _step in range(7):
        selected += 1
        selected, offset, menu = mention_window(shown, selected, offset)
        assert offset == 0
    selected, offset, menu = mention_window(shown, selected + 1, offset)
    assert (selected, offset) == (8, 1)
    assert menu.splitlines()[0] == "  a1.txt"
    assert menu.splitlines()[-1] == "> a8.txt"
    selected, offset, menu = mention_window(shown, 0, offset)
    assert (selected, offset) == (0, 0)
    assert menu.splitlines()[0] == "> a0.txt"


def test_index_project_skips_ignored_dirs_and_symlinks(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "keep.py").write_text("x")
    nested = root / "src"
    nested.mkdir()
    (nested / "app.py").write_text("x")
    skipped = root / "node_modules"
    skipped.mkdir()
    (skipped / "secret.py").write_text("x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "hidden.py").write_text("x")
    (root / "link").symlink_to(outside, target_is_directory=True)
    found = {entry.path: entry.is_dir for entry in index_project(root)}
    assert found["keep.py"] is False
    assert found["src"] is True
    assert found["src/app.py"] is False
    assert "node_modules" not in found
    assert "node_modules/secret.py" not in found
    assert found["link"] is True
    assert "link/hidden.py" not in found
    assert index_project(root / "keep.py") == []
    assert index_project(root, limit=0) == []
    assert len(index_project(root, limit=2)) == 2


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


def test_model_screen_explains_a_required_choice():
    reason = "qwen3.5-128k is missing from this provider. Choose a model to continue."
    app = ModelApp(["alpha", "beta"], reason)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            assert str(app.screen.query_one("#note").content) == reason
            await pilot.press("enter")

    asyncio.run(drive())
    assert app.return_value == "alpha"

    plain = ModelApp(["alpha"])

    async def no_note():
        async with plain.run_test(size=(80, 24)) as pilot:
            assert len(plain.screen.query("#note")) == 0
            await pilot.press("enter")

    asyncio.run(no_note())


def test_escape_closes_the_model_screen():
    app = ModelApp(["alpha", "beta"])

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            status = str(app.screen.query_one("#status").content)
            assert "Escape cancels." in status
            await pilot.press("escape")

    asyncio.run(drive())
    assert app.return_value is None


def test_pick_model_exits_when_the_list_is_closed(monkeypatch):
    monkeypatch.setattr("harnh3x.tui.pickers.ModelApp.run", lambda self: None)

    with pytest.raises(SystemExit) as exc:
        pick_model(["alpha"])

    assert exc.value.code == 1


def test_compact_count_and_context_text():
    assert compact_count(0) == "0"
    assert compact_count(999) == "999"
    assert compact_count(1234) == "1.2k"
    assert compact_count(8192) == "8.2k"
    assert compact_count(262_144) == "262k"
    assert compact_count(1_048_576) == "1.0M"
    assert context_text(1234, 8192) == "ctx 1.2k/8.2k 15%"
    assert context_text(0, 8192) == "ctx 0/8.2k 0%"
    assert context_text(None, 262_144) == "ctx —/262k"
    assert context_text(50, None) == "ctx 50"
    assert context_text(None, None) is None


def _texts(segments):
    return [segment.text for segment in segments]


def test_build_segments_shows_each_badge_only_when_it_applies():
    assert build_segments(StatusState()) == []
    full = StatusState(
        model="qwen3.5:9b",
        used=None,
        limit=262_144,
        compact_at=0.75,
        counts=(12, 31, 1, 1, 67),
        warnings=(("caido", "refused"), ("web-triage", "server unavailable")),
        yolo=True,
        debug=True,
        version="0.6.0",
    )
    segments = build_segments(full)
    assert _texts(segments) == [
        "qwen3.5:9b",
        "ctx —/262k",
        "YOLO",
        "DEBUG",
        "skills 12+1",
        "tools 31",
        "mcp 1 · 67 tools",
        "⚠ 2",
        "0.6.0",
    ]
    sides = {segment.key: segment.side for segment in segments}
    assert [key for key, side in sides.items() if side == "left"] == [
        "model",
        "context",
        "yolo",
        "debug",
    ]
    actions = {segment.key: segment.action for segment in segments}
    assert actions["warnings"] == "warnings"
    assert actions["skills"] == actions["tools"] == "skills"
    assert actions["model"] == "model"
    clickable = {"warnings", "skills", "tools", "model"}
    assert all(
        action is None for key, action in actions.items() if key not in clickable
    )
    plain = build_segments(StatusState(model="m", counts=(12, 31, 0, 0, 0)))
    assert _texts(plain) == ["m", "skills 12", "tools 31"]
    one = build_segments(StatusState(counts=(1, 1, 0, 1, 1)))
    assert "mcp 1 · 1 tool" in _texts(one)


def test_skills_text_lists_loaded_skills_first():
    text = skills_text(
        [
            ("file-ops", "", True, ("read_file", "write_file")),
            ("shell", "", False, ("run_shell",)),
            ("caido", "mcp", False, ()),
        ]
    )
    assert text.splitlines() == [
        "Loaded (1)",
        "  file-ops",
        "    read_file, write_file",
        "",
        "Not loaded (2)",
        "  shell",
        "    run_shell",
        "  caido [mcp]",
        "    no tools",
    ]
    empty = skills_text([("shell", "", False, ("run_shell",))])
    assert empty.splitlines()[:2] == ["Loaded (0)", "  none"]


def test_context_badge_style_follows_the_compaction_level():
    def style(used):
        state = StatusState(used=used, limit=1000, compact_at=0.5)
        return build_segments(state)[0].style

    assert style(100) == BADGE_STYLE
    assert style(450) == CONTEXT_STYLES["warn"]
    assert style(600) == CONTEXT_STYLES["over"]


def test_cache_badge_compares_the_read_with_what_was_reusable():
    assert cache_text(None, 4000) is None
    assert cache_text(3600, 3900) == "cache 3.6k 92%"
    assert cache_text(4588, 4369) == "cache 4.6k 100%"
    assert cache_text(0, 3900) == "cache 0 0%"
    assert cache_text(512, None) == "cache 512"
    assert cache_text(0, 0) == "cache 0"

    def cache(read, reusable):
        state = StatusState(
            model="m", used=10, cache_read=read, cache_reusable=reusable
        )
        return {segment.key: segment for segment in build_segments(state)}

    assert "cache" not in cache(None, 4000)
    hit = cache(3600, 3900)
    assert list(hit) == ["model", "context", "cache"]
    assert hit["cache"].style == BADGE_STYLE
    assert hit["cache"].side == "left"
    assert hit["cache"].action == "cache"
    assert cache(1000, 3900)["cache"].style == CONTEXT_STYLES["warn"]
    assert cache(512, None)["cache"].style == BADGE_STYLE


def test_cache_details_show_the_last_completion_and_session_totals():
    rounds = [
        CacheRound(read=0, write=None, reusable=None, prompt=1386),
        CacheRound(read=0, write=None, reusable=1520, prompt=2881),
        CacheRound(read=3027, write=None, reusable=3027, prompt=4369),
        CacheRound(read=4588, write=None, reusable=4588, prompt=10505),
        CacheRound(read=2369, write=None, reusable=11260, prompt=11260),
        CacheRound(read=11366, write=None, reusable=11366, prompt=12172),
        CacheRound(read=12347, write=None, reusable=12347, prompt=16726),
    ]

    lines = [" ".join(line.split()) for line in cache_details(rounds).splitlines()]

    assert lines[:4] == [
        "Last completion",
        "Read from cache 12,347",
        "Reusable 12,347 100% reused",
        "Prompt 16,726 74% from cache",
    ]
    assert lines[5:10] == [
        "Session (7 completions)",
        "Read from cache 33,697",
        "Reusable 44,108 76% reused",
        "Prompt 59,299 57% from cache",
        "Misses 2 under 50% reused",
    ]
    assert "How to read this" in lines
    terms = [line.split()[0] for line in lines[lines.index("How to read this") + 5 :]]
    assert "Prompt" in terms and "Misses" in terms
    assert "Written" not in terms


def test_cache_details_cover_writes_and_missing_counts():
    rounds = [
        CacheRound(read=900, write=50, reusable=None, prompt=957),
        CacheRound(read=None, write=None, reusable=960, prompt=None),
    ]

    lines = [" ".join(line.split()) for line in cache_details(rounds).splitlines()]

    assert lines[:2] == ["Last completion", "no cached count reported"]
    assert lines[3:7] == [
        "Session (1 of 2 completions reported a count)",
        "Read from cache 900",
        "Written to cache 50",
        "Prompt 957 94% from cache",
    ]
    assert not any(
        line.startswith("Misses") and line.split()[1].isdigit() for line in lines
    )
    assert any(line.startswith("Written to cache tokens stored") for line in lines)
    assert cache_details([]) == "No completion has reported cached tokens yet."
    assert cache_details([CacheRound(None, None, None, None)]) == (
        "No completion has reported cached tokens yet."
    )


def test_layout_segments_right_aligns_and_reports_hit_ranges():
    segments = [
        Segment("model", "m", "s"),
        Segment("tools", "tools 3", "s", "right"),
        Segment("version", "1.0", "s", "right"),
    ]
    content, hits = layout_segments(segments, 30)
    text = str(content)
    assert len(text) == 30
    assert text.startswith(" m ")
    assert text.endswith(" tools 3   1.0 ")
    assert [(start, end, segment.key) for start, end, segment in hits] == [
        (0, 3, "model"),
        (15, 24, "tools"),
        (25, 30, "version"),
    ]
    for start, end, segment in hits:
        assert text[start:end] == f" {segment.text} "
    assert all(span.style == "s" for span in content.spans)


def test_layout_segments_drops_the_lowest_priority_first():
    state = StatusState(
        model="qwen3.5:9b",
        used=1234,
        limit=262_144,
        counts=(12, 31, 1, 1, 67),
        warnings=(("caido", "refused"),),
        yolo=True,
        version="0.6.0",
    )
    segments = build_segments(state)

    def shown(width):
        _content, hits = layout_segments(segments, width)
        return [segment.key for _start, _end, segment in hits]

    assert shown(200) == [
        "model",
        "context",
        "yolo",
        "skills",
        "tools",
        "mcp",
        "warnings",
        "version",
    ]
    assert shown(80) == ["model", "context", "yolo", "skills", "mcp", "warnings"]
    assert shown(70) == ["model", "context", "yolo", "mcp", "warnings"]
    assert shown(60) == ["model", "context", "yolo", "warnings"]
    assert shown(30) == ["model", "yolo", "warnings"]
    assert shown(12) == ["yolo", "warnings"]
    assert shown(0) == []
    content, _hits = layout_segments(segments, 40)
    assert content.cell_length <= 40


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
    assert [step[0] for step in past[0][1].steps] == [
        "round",
        "thinking",
        "tools",
        "round",
        "answer",
    ]
    assert [step[1] for step in past[0][1].steps if step[0] == "round"] == [1, 2]


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
