"""Context compaction."""

import json
from types import SimpleNamespace

from helpers import _prior_turn

import harnh3x.agent.client as agent_client
from harnh3x import agent, config
from harnh3x.config import COMPACT_AT, TRANSCRIPT_FILE
from harnh3x.tui import conversation_turns


def test_context_limit_from_show_prefers_num_ctx():
    payload = {
        "parameters": "temperature 0.8\nnum_ctx 8192\n",
        "model_info": {"qwen35.context_length": 131072},
    }
    assert agent_client._context_limit_from_show(payload) == 8192
    assert (
        agent_client._context_limit_from_show(
            {"model_info": {"qwen35.context_length": 131072, "other": 1}}
        )
        == 131072
    )
    assert (
        agent_client._context_limit_from_show({"parameters": "temperature 0.8"}) is None
    )
    assert agent_client._context_limit_from_show([]) is None
    assert (
        agent_client._context_limit_from_show(
            {"model_info": {"qwen35.context_length": True}}
        )
        is None
    )


def test_model_context_limit_reads_ollama_show(monkeypatch):
    captured = {}

    class Response:
        def read(self):
            return json.dumps({"parameters": "num_ctx 8192\n"}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["body"] = request.data
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(agent_client.urllib.request, "urlopen", urlopen)
    client = SimpleNamespace(base_url="http://host:11434/v1/")

    assert agent.model_context_limit("qwen3.5-128k", client) == 8192
    assert captured["url"] == "http://host:11434/api/show"
    assert json.loads(captured["body"]) == {"model": "qwen3.5-128k"}
    assert captured["timeout"] == agent_client._SHOW_TIMEOUT


def test_model_context_limit_returns_none_when_show_fails(monkeypatch):
    def urlopen(request, timeout=0):
        raise OSError("down")

    monkeypatch.setattr(agent_client.urllib.request, "urlopen", urlopen)
    client = SimpleNamespace(base_url="http://host:11434/v1")

    assert agent.model_context_limit("m", client) is None
    assert agent.model_context_limit("m", SimpleNamespace()) is None


def test_model_context_limit_prefers_the_configured_limit_for_openai(monkeypatch):
    def urlopen(request, timeout=0):
        raise AssertionError("show should not be called")

    monkeypatch.setattr(agent_client.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(config, "CONTEXT_LIMIT", 32_000)
    client = SimpleNamespace(base_url="http://host:11434/v1")

    assert agent.model_context_limit("m", client) == 32_000
    assert agent.model_context_limit("m", SimpleNamespace()) == 32_000


def test_run_stores_the_latest_context_tokens(tmp_path):
    step = {"n": 0}

    def create(**kwargs):
        step["n"] += 1
        if step["n"] == 1:
            message = SimpleNamespace(
                content="",
                tool_calls=[
                    SimpleNamespace(
                        id="call_1",
                        function=SimpleNamespace(
                            name="read_file", arguments='{"path": "a.txt"}'
                        ),
                    )
                ],
            )
            total = 120
        else:
            message = SimpleNamespace(content="ready", tool_calls=None)
            total = 340
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=SimpleNamespace(total_tokens=total),
        )

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    session = agent.Session(workspace=tmp_path, model="custom-model")

    assert agent.run("read a.txt", session, client=client) == "ready"
    assert session.context_tokens == 340
    saved = json.loads((tmp_path / TRANSCRIPT_FILE).read_text(encoding="utf-8"))
    assert saved["context_tokens"] == 340


def _completion(content, total, tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(total_tokens=total),
    )


def test_run_skips_compaction_below_the_threshold(tmp_path):
    seen = []

    def create(**kwargs):
        seen.append(kwargs)
        return _completion("ok", 12)

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    session = agent.Session(workspace=tmp_path, model="custom-model")
    _prior_turn(session)
    session.context_tokens = int(100 * COMPACT_AT) - 1
    session.context_limit = 100

    assert agent.run("now", session, client=client) == "ok"
    assert len(seen) == 1
    assert seen[0]["tools"]
    assert any(message.get("content") == "earlier" for message in session.messages)


def test_run_skips_compaction_without_a_limit_or_prior_turn(tmp_path):
    seen = []

    def create(**kwargs):
        seen.append(kwargs)
        return _completion("ok", 90)

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    no_limit = agent.Session(workspace=tmp_path / "open", model="custom-model")
    _prior_turn(no_limit)
    no_limit.context_tokens = 90
    no_limit.context_limit = None
    assert agent.run("now", no_limit, client=client) == "ok"

    current_only = agent.Session(workspace=tmp_path / "fresh", model="custom-model")
    current_only.context_tokens = 90
    current_only.context_limit = 100
    assert agent.run("now", current_only, client=client) == "ok"

    assert len(seen) == 2
    assert all(call["tools"] for call in seen)
    assert any(message.get("content") == "earlier" for message in no_limit.messages)


def test_run_compacts_older_turns_near_the_limit(tmp_path):
    seen = []
    chats = {"n": 0}

    def create(**kwargs):
        seen.append(kwargs)
        if not kwargs.get("tools"):
            return _completion("kept the plan", 90)
        chats["n"] += 1
        if chats["n"] == 1:
            return _completion(
                "",
                90,
                tool_calls=[
                    SimpleNamespace(
                        id="call_1",
                        function=SimpleNamespace(
                            name="read_file", arguments='{"path": "a.txt"}'
                        ),
                    )
                ],
            )
        return _completion("ready", 30)

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    session = agent.Session(workspace=tmp_path, model="custom-model")
    session.skills.load_skill("file-ops")
    session.messages.append({"role": "user", "content": "OLD" * 200})
    session.messages.append({"role": "assistant", "content": "NEWEST-MARKER"})
    session.context_tokens = 90
    session.context_limit = 100

    assert agent.run("now", session, client=client) == "ready"

    assert [call.get("tools") for call in seen] == [
        None,
        seen[1]["tools"],
        seen[2]["tools"],
    ]
    summary_request = seen[0]["messages"][1]["content"]
    assert "NEWEST-MARKER" in summary_request
    assert "OLD" * 200 not in summary_request
    assert "file-ops" in summary_request
    assert [message["role"] for message in session.messages] == [
        "system",
        "user",
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    note = session.messages[1]["content"]
    assert note.startswith("Context compacted:\nkept the plan")
    assert "Loaded skills: file-ops." in note
    assert session.messages[2]["content"] == "now"
    assert "read_file" in [
        tool["function"]["name"] for tool in session.skills.schemas()
    ]
    assert [turn.user for turn in conversation_turns(session.messages)] == ["now"]
    events = [
        json.loads(line) for line in (tmp_path / "session.log").read_text().splitlines()
    ]
    compact = next(event for event in events if event["event"] == "compact")
    assert compact["before"] == 90
    assert compact["dropped"] == 2
    saved = json.loads((tmp_path / TRANSCRIPT_FILE).read_text(encoding="utf-8"))
    assert saved["skills"] == ["file-ops"]


def test_run_compacts_tool_rounds_that_fill_the_current_turn(tmp_path):
    chats = {"n": 0}
    seen = []

    def create(**kwargs):
        seen.append(kwargs)
        if not kwargs.get("tools"):
            return _completion("folded the tools", 90)
        chats["n"] += 1
        if chats["n"] == 1:
            return _completion(
                "",
                90,
                tool_calls=[
                    SimpleNamespace(
                        id="call_1",
                        function=SimpleNamespace(
                            name="read_file",
                            arguments='{"path": "skill-doc-payload.txt"}',
                        ),
                    )
                ],
            )
        if chats["n"] == 2:
            return _completion(
                "",
                90,
                tool_calls=[
                    SimpleNamespace(
                        id="call_2",
                        function=SimpleNamespace(
                            name="read_file",
                            arguments='{"path": "notes.txt"}',
                        ),
                    )
                ],
            )
        return _completion("the capabilities", 90)

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    session = agent.Session(workspace=tmp_path, model="custom-model")
    session.skills.load_skill("file-ops")
    _prior_turn(session)
    session.context_tokens = 90
    session.context_limit = 100

    assert agent.run("What tools do you have", session, client=client) == (
        "the capabilities"
    )

    stored = json.dumps(session.messages)
    assert "skill-doc-payload.txt" not in stored
    final_request = [call for call in seen if call.get("tools")][-1]
    assert "skill-doc-payload.txt" not in json.dumps(final_request["messages"])
    assert [message["role"] for message in session.messages] == [
        "system",
        "user",
        "user",
        "assistant",
    ]
    assert session.messages[1]["content"].startswith("Context compacted:")
    assert session.messages[2]["content"] == "What tools do you have"
    assert session.messages[3]["content"] == "the capabilities"
    assert [turn.user for turn in conversation_turns(session.messages)] == [
        "What tools do you have"
    ]


def test_compact_session_forces_a_summary_under_the_threshold(tmp_path):
    session = agent.Session(workspace=tmp_path, model="custom-model")
    _prior_turn(session)
    session.messages[2]["content"] = "did the earlier work " + ("x" * 800)
    session.messages.append({"role": "user", "content": "later"})
    session.messages.append({"role": "assistant", "content": "answered"})
    session.context_tokens = 10
    session.context_limit = 100
    phases = []

    def create(**kwargs):
        phases.append("create")
        assert not kwargs.get("tools")
        assert "stream" not in kwargs
        return _completion("forced summary", 10)

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    session.on_compact = phases.append

    assert agent.compact_session(session, client=client) == "Context compacted."
    assert phases == ["start", "create", "done"]

    assert session.messages[1]["content"].startswith(
        "Context compacted:\nforced summary"
    )
    assert session.messages[2]["content"] == "later"
    assert session.messages[3]["content"] == "answered"
    assert session.messages[1]["context_before"] == 10
    assert session.messages[1]["context_after"] < 10
    saved = json.loads((tmp_path / TRANSCRIPT_FILE).read_text(encoding="utf-8"))
    assert "forced summary" in saved["messages"][1]["content"]


def test_compact_keeps_the_scaled_estimate_not_the_summary_usage(tmp_path):
    session = agent.Session(workspace=tmp_path, model="custom-model")
    _prior_turn(session)
    session.messages[2]["content"] = "did the earlier work " + ("x" * 800)
    session.messages.append({"role": "user", "content": "later"})
    session.messages.append({"role": "assistant", "content": "answered"})
    session.context_tokens = 900
    session.context_limit = 1000

    def create(**kwargs):
        return _completion("short summary", 7)

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )

    assert agent.compact_session(session, client=client) == "Context compacted."
    note = session.messages[1]
    assert note["context_before"] == 900
    assert session.context_tokens == note["context_after"]
    assert session.context_tokens != 7
    assert 1 <= session.context_tokens < 900
    saved = json.loads((tmp_path / TRANSCRIPT_FILE).read_text(encoding="utf-8"))
    assert saved["context_tokens"] == note["context_after"]


def test_compact_session_reports_when_there_is_nothing_to_compact(tmp_path):
    def create(**kwargs):
        raise AssertionError("should not call the model")

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    session = agent.Session(workspace=tmp_path, model="custom-model")
    session.messages.append({"role": "user", "content": "only"})
    session.messages.append({"role": "assistant", "content": "done"})
    session.context_tokens = 10
    session.context_limit = 100

    assert agent.compact_session(session, client=client) == "Nothing to compact."
    assert session.messages[1]["content"] == "only"


def test_compact_session_keeps_history_when_the_summary_fails(tmp_path):
    phases = []

    def create(**kwargs):
        phases.append("create")
        raise RuntimeError("summary down")

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    session = agent.Session(workspace=tmp_path, model="custom-model")
    _prior_turn(session)
    session.messages.append({"role": "user", "content": "later"})
    session.messages.append({"role": "assistant", "content": "answered"})
    session.context_tokens = 10
    session.context_limit = 100
    session.on_compact = phases.append

    assert agent.compact_session(session, client=client) == (
        "Could not compact the context."
    )
    assert phases == ["start", "create", "done"]
    assert any(message.get("content") == "earlier" for message in session.messages)


def test_run_keeps_history_when_the_summary_fails(tmp_path):
    def create(**kwargs):
        if not kwargs.get("tools"):
            raise RuntimeError("summary down")
        return _completion("still here", 10)

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    session = agent.Session(workspace=tmp_path / "down", model="custom-model")
    _prior_turn(session)
    session.context_tokens = 90
    session.context_limit = 100

    assert agent.run("now", session, client=client) == "still here"
    assert any(message.get("content") == "earlier" for message in session.messages)

    def empty(**kwargs):
        if not kwargs.get("tools"):
            return _completion("   ", 10)
        return _completion("still here", 10)

    quiet = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=empty))
    )
    blank = agent.Session(workspace=tmp_path / "blank", model="custom-model")
    _prior_turn(blank)
    blank.context_tokens = 90
    blank.context_limit = 100

    assert agent.run("now", blank, client=quiet) == "still here"
    assert any(message.get("content") == "earlier" for message in blank.messages)
