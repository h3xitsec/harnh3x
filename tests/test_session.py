"""Session lifecycle: workspaces, transcripts, reset, and resume."""

import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers import FILE_OPS_LOADED, _delta, _write_transcript

from harnh3x import agent, config
from harnh3x.config import TRANSCRIPT_FILE
from harnh3x.skills import SkillRegistry
from harnh3x.tui import conversation_turns


def test_session_log_lives_in_the_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WORKSPACE", raising=False)

    session = agent.Session()
    agent.write_log({"event": "turn", "turn": 0}, session.workspace)

    log_file = session.workspace / "session.log"
    assert json.loads(log_file.read_text().strip()) == {"event": "turn", "turn": 0}
    assert not (tmp_path / "session.log").exists()
    assert session.workspace.parent == (tmp_path / "workspaces").resolve()


def test_session_defaults_to_a_unique_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WORKSPACE", raising=False)

    first = agent.Session()
    second = agent.Session()

    root = (tmp_path / "workspaces").resolve()
    assert first.workspace.is_dir()
    assert second.workspace.is_dir()
    assert first.workspace.parent == root
    assert second.workspace.parent == root
    assert first.workspace != second.workspace
    uuid.UUID(first.workspace.name)
    uuid.UUID(second.workspace.name)


def test_debug_hook_reports_each_completion_and_stays_a_launch_flag(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WORKSPACE", raising=False)
    seen = []
    calls = {"n": 0}

    def create(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            message = SimpleNamespace(
                content="",
                tool_calls=[
                    SimpleNamespace(
                        id="call_1",
                        function=SimpleNamespace(
                            name="load_skill",
                            arguments='{"name": "file-ops"}',
                        ),
                    )
                ],
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])
        message = SimpleNamespace(content="done", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    session = agent.Session(workspace=tmp_path / "ws", debug=True)
    session.on_debug = lambda system, request: seen.append((system, request))

    assert agent.run("read the notes", session, client=client) == "done"
    assert agent.run("again", session, client=client) == "done"

    assert len(seen) == 3
    assert seen[0][1] == "user: read the notes"
    assert "load_skill" not in seen[0][1]
    assert "[loaded:" not in seen[0][0]
    assert "[loaded:" in seen[1][0]
    assert seen[1][0] == session.messages[0]["content"]
    assert "user: read the notes" in seen[1][1]
    assert 'load_skill({"name": "file-ops"})' in seen[1][1]
    assert "tool call_1:" in seen[1][1]
    assert seen[2][0] is None
    assert "user: again" in seen[2][1]
    saved = json.loads((session.workspace / TRANSCRIPT_FILE).read_text())
    assert "debug" not in saved

    session.reset()
    assert session.debug is True
    assert agent.run("fresh", session, client=client) == "done"
    assert seen[-1][0] == session.messages[0]["content"]

    past = tmp_path / "past"
    _write_transcript(
        past,
        "saved-model",
        [
            {"role": "system", "content": "stale"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ],
    )
    session.resume(past)
    assert session.debug is True
    assert agent.run("resumed", session, client=client) == "done"
    assert seen[-1][0] == session.messages[0]["content"]

    quiet = []
    plain = agent.Session(workspace=tmp_path / "plain")

    def create_once(**kwargs):
        message = SimpleNamespace(content="ok", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    plain.on_debug = lambda system, request: quiet.append((system, request))
    agent.run(
        "hello",
        plain,
        client=SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create_once))
        ),
    )
    assert quiet == []


def test_session_reset_starts_a_new_conversation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WORKSPACE", str(tmp_path / "pinned"))
    session = agent.Session(workspace=tmp_path / "first", model="custom-model")
    previous = session.workspace
    session.messages.append({"role": "user", "content": "remember this"})
    session.skills.load_skill("file-ops")
    session.context_tokens = 40
    session.context_limit = 8192

    session.reset()

    assert session.workspace != previous
    assert session.workspace.parent == (tmp_path / "workspaces").resolve()
    assert session.workspace.is_dir()
    assert previous.is_dir()
    assert str(session.workspace) in session.messages[0]["content"]
    assert session.model == "custom-model"
    assert session.context_tokens is None
    assert session.context_limit == 8192
    assert [message["role"] for message in session.messages] == ["system"]
    assert "[loaded:" not in session.messages[0]["content"]
    assert [tool["function"]["name"] for tool in session.skills.schemas()] == [
        "load_skill",
        "unload_skill",
        "task",
    ]


def test_run_saves_the_full_transcript(tmp_path):
    thinking = "r" * 600

    def create(**kwargs):
        message = SimpleNamespace(content="ok", reasoning=thinking, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    session = agent.Session(workspace=tmp_path, model="custom-model")

    assert agent.run("think", session, client=client) == "ok"

    assert session.context_tokens is None
    saved = json.loads((tmp_path / TRANSCRIPT_FILE).read_text(encoding="utf-8"))
    assert saved["model"] == "custom-model"
    assert "context_tokens" not in saved
    assert saved["messages"][-1]["reasoning"] == thinking
    assert not (tmp_path / f".{TRANSCRIPT_FILE}.tmp").exists()
    events = [
        json.loads(line) for line in (tmp_path / "session.log").read_text().splitlines()
    ]
    logged = next(
        event["message"]["reasoning"] for event in events if event["event"] == "model"
    )
    assert logged == "r" * 500 + "...[truncated]"


def test_resume_restores_skills_without_load_messages(tmp_path):
    past = tmp_path / "past"
    past.mkdir()
    (past / TRANSCRIPT_FILE).write_text(
        json.dumps(
            {
                "model": "saved-model",
                "skills": ["file-ops"],
                "messages": [
                    {"role": "system", "content": "stale"},
                    {
                        "role": "user",
                        "content": "Context compacted:\nnotes were updated",
                    },
                    {"role": "user", "content": "continue"},
                    {"role": "assistant", "content": "ready"},
                ],
            }
        ),
        encoding="utf-8",
    )
    session = agent.Session(workspace=tmp_path / "current", model="custom-model")

    session.resume(past)

    assert "read_file" in [
        tool["function"]["name"] for tool in session.skills.schemas()
    ]
    assert [turn.user for turn in conversation_turns(session.messages)] == ["continue"]


def test_transcript_survives_a_failed_turn(tmp_path):
    def create(**kwargs):
        raise RuntimeError("offline")

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    session = agent.Session(workspace=tmp_path, model="custom-model")

    with pytest.raises(RuntimeError, match="offline"):
        agent.run("hello", session, client=client)

    saved = json.loads((tmp_path / TRANSCRIPT_FILE).read_text(encoding="utf-8"))
    assert saved["messages"][-1] == {"role": "user", "content": "hello"}
    assert not (tmp_path / f".{TRANSCRIPT_FILE}.tmp").exists()


def test_parse_command_reads_a_slash_name():
    assert agent.parse_command("hello") is None
    assert agent.parse_command("  /resume  ") == ("resume", "")
    assert agent.parse_command("/resume extra") == ("resume", "extra")
    assert agent.parse_command("/") == ("", "")


def test_list_sessions_is_newest_first_and_skips_incomplete(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "workspaces"
    older = root / "11111111-1111-1111-1111-111111111111"
    newer = root / "22222222-2222-2222-2222-222222222222"
    system_only = root / "33333333-3333-3333-3333-333333333333"
    empty = root / "empty"
    log_only = root / "44444444-4444-4444-4444-444444444444"
    broken = root / "55555555-5555-5555-5555-555555555555"
    empty.mkdir(parents=True)
    log_only.mkdir()
    (log_only / "session.log").write_text("{}\n", encoding="utf-8")
    broken.mkdir()
    (broken / TRANSCRIPT_FILE).write_text("{", encoding="utf-8")
    _write_transcript(system_only, "m", [{"role": "system", "content": "hi"}])
    _write_transcript(
        older,
        "old-model",
        [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "first\nquestion"},
        ],
    )
    _write_transcript(
        newer,
        "new-model",
        [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "later question " + ("x" * 80)},
        ],
    )
    os.utime(older / TRANSCRIPT_FILE, (1000, 1000))
    os.utime(newer / TRANSCRIPT_FILE, (2000, 2000))

    found = agent.list_sessions()

    assert [item.workspace for item in found] == [newer.resolve(), older.resolve()]
    assert found[0].model == "new-model"
    assert found[0].preview.startswith("later question")
    assert found[0].preview.endswith("...")
    assert len(found[0].preview) == 60
    assert found[1].preview == "first question"
    assert "old-model" in agent.session_label(found[1], 1)
    assert found[1].workspace.name[:8] in agent.session_label(found[1], 1)


def test_resume_restores_messages_model_and_skills(tmp_path, monkeypatch):
    def fail_execute(self, name, args, workspace):
        raise AssertionError(name)

    monkeypatch.setattr(SkillRegistry, "execute", fail_execute)
    past = tmp_path / "past"
    messages = [
        {"role": "system", "content": "stale"},
        {"role": "user", "content": "remember this"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {
                        "name": "load_skill",
                        "arguments": '{"name": "file-ops"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "loaded file-ops"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c2",
                    "type": "function",
                    "function": {
                        "name": "load_skill",
                        "arguments": '{"name": "shell"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c2", "content": "loaded shell"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c3",
                    "type": "function",
                    "function": {
                        "name": "unload_skill",
                        "arguments": '{"name": "shell"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c3", "content": "unloaded shell"},
        {
            "role": "assistant",
            "content": "",
            "reasoning": "look at the notes",
            "tool_calls": [
                {
                    "id": "c4",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "notes.md"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c4", "content": "notes"},
        {"role": "assistant", "content": "ready"},
    ]
    _write_transcript(past, "saved-model", messages)
    current = tmp_path / "current"
    session = agent.Session(workspace=current, model="custom-model")

    session.resume(past)

    assert session.model == "saved-model"
    assert session.workspace == past.resolve()
    assert session.messages[1]["content"] == "remember this"
    assert session.messages[-1]["content"] == "ready"
    assert "stale" not in session.messages[0]["content"]
    assert str(session.workspace) in session.messages[0]["content"]
    assert FILE_OPS_LOADED in session.messages[0]["content"]
    assert "[loaded: run_shell]" not in session.messages[0]["content"]
    names = [tool["function"]["name"] for tool in session.skills.schemas()]
    assert "read_file" in names
    assert "run_shell" not in names
    assert [item["content"] for item in session.messages if item["role"] == "tool"] == [
        "loaded file-ops",
        "loaded shell",
        "unloaded shell",
        "notes",
    ]
    assert session.messages[-3]["reasoning"] == "look at the notes"
    assert session.context_tokens is None
    assert session.context_limit is None


def test_resume_restores_context_tokens(tmp_path):
    past = tmp_path / "past"
    past.mkdir()
    (past / TRANSCRIPT_FILE).write_text(
        json.dumps(
            {
                "model": "saved-model",
                "context_tokens": 4321,
                "messages": [
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "hello"},
                ],
            }
        ),
        encoding="utf-8",
    )
    session = agent.Session(workspace=tmp_path / "current", model="custom-model")
    session.context_limit = 8192
    session.context_tokens = 10

    session.resume(past)

    assert session.context_tokens == 4321
    assert session.context_limit is None


def test_cache_rounds_are_saved_and_restored_on_resume(tmp_path):
    def create(**kwargs):
        usage = SimpleNamespace(
            total_tokens=90,
            prompt_tokens=80,
            prompt_tokens_details=SimpleNamespace(cached_tokens=70),
        )
        return [_delta(content="ok"), SimpleNamespace(choices=[], usage=usage)]

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    past = tmp_path / "past"
    session = agent.Session(workspace=past)
    session.context_limit = 8192
    session.context_tokens = 75

    assert agent.run("hi", session, client=client) == "ok"
    saved = json.loads((past / TRANSCRIPT_FILE).read_text(encoding="utf-8"))
    assert saved["cache_rounds"] == [
        {"read": 70, "write": None, "reusable": 75, "prompt": 80}
    ]

    saved["cache_rounds"] += [
        {"read": "many", "write": None, "reusable": 1, "prompt": 2},
        "junk",
        {"read": 5},
    ]
    (past / TRANSCRIPT_FILE).write_text(json.dumps(saved), encoding="utf-8")
    fresh = agent.Session(workspace=tmp_path / "current")
    fresh.cache_rounds.append(agent.CacheRound(1, None, 1, 1))

    fresh.resume(past)

    assert fresh.cache_rounds == [
        agent.CacheRound(read=70, write=None, reusable=75, prompt=80),
        agent.CacheRound(read=5, write=None, reusable=None, prompt=None),
    ]
    fresh.reset()
    assert fresh.cache_rounds == []


def test_resume_rejects_a_broken_transcript(tmp_path):
    current = tmp_path / "current"
    session = agent.Session(workspace=current, model="keep")
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / TRANSCRIPT_FILE).write_text("{", encoding="utf-8")

    with pytest.raises(ValueError, match="Cannot read"):
        session.resume(broken)

    assert session.model == "keep"
    assert session.workspace == current.resolve()
    assert [message["role"] for message in session.messages] == ["system"]


def test_session_workspace_argument_and_env_override_the_default(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit"
    monkeypatch.setenv("WORKSPACE", str(tmp_path / "from-env"))
    chosen = agent.Session(workspace=explicit)
    assert chosen.workspace == explicit.resolve()
    assert explicit.is_dir()

    monkeypatch.chdir(tmp_path)
    from_env = tmp_path / "from-env"
    session = agent.Session()
    assert session.workspace == from_env.resolve()
    assert from_env.is_dir()
    assert not (tmp_path / "workspaces").exists()


def test_base_dir_accepts_an_absolute_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WORKSPACE", raising=False)
    root = tmp_path / "elsewhere"
    monkeypatch.setattr(config, "BASE_DIR", str(root))

    session = agent.Session()

    assert session.workspace.parent == (root / "workspaces").resolve()
    assert session.workspace.is_absolute()
    assert agent.list_sessions() == []


def test_workspace_accepts_a_home_path(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("WORKSPACE", raising=False)
    monkeypatch.setattr(config, "BASE_DIR", "~/.harnh3x")

    session = agent.Session()
    chosen = agent.Session(workspace=Path("~/jobs/two"))
    monkeypatch.setenv("WORKSPACE", "~/jobs/one")
    from_env = agent.Session()
    past = home / ".harnh3x" / "workspaces" / session.workspace.name
    _write_transcript(
        past,
        "saved-model",
        [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "hi"},
        ],
    )

    assert session.workspace == past.resolve()
    assert chosen.workspace == (home / "jobs" / "two").resolve()
    assert from_env.workspace == (home / "jobs" / "one").resolve()
    assert [
        item.workspace for item in agent.list_sessions(Path("~/.harnh3x/workspaces"))
    ] == [past.resolve()]


def test_default_base_dir_is_the_harnh3x_home_directory():
    assert config._SETTINGS["base_dir"].default == "~/.harnh3x"
