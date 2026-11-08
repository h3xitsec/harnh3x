"""The chat loop: completions, tool calls, streaming, and retries."""

import json
from types import SimpleNamespace

from helpers import FILE_OPS_LOADED, _delta, _prior_turn, _tool_names, _unreachable

import harnh3x.agent.client as agent_client
import harnh3x.agent.loop as agent_loop
from harnh3x import agent, config
from harnh3x.config import TRANSCRIPT_FILE
from harnh3x.tools import _tool_result


def test_assistant_message_drops_null_metadata():
    message = SimpleNamespace(
        content=None,
        refusal=None,
        annotations=None,
        audio=None,
        function_call=None,
        tool_calls=[
            SimpleNamespace(
                id="call_1",
                index=0,
                type="function",
                function=SimpleNamespace(
                    name="read_file", arguments='{"path": "a.txt"}'
                ),
            )
        ],
    )

    stored = agent.assistant_message(message)

    assert set(stored) == {"role", "content", "tool_calls"}
    assert "index" not in stored["tool_calls"][0]
    assert set(stored["tool_calls"][0]) == {"id", "type", "function"}
    assert stored["tool_calls"][0]["function"] == {
        "name": "read_file",
        "arguments": '{"path": "a.txt"}',
    }
    assert "reasoning" not in stored


def test_assistant_message_stores_reasoning():
    from_reasoning = agent.assistant_message(
        SimpleNamespace(content="", reasoning="think first", tool_calls=None)
    )
    from_alias = agent.assistant_message(
        SimpleNamespace(content="done", reasoning_content="scratch", tool_calls=None)
    )
    blank = agent.assistant_message(
        SimpleNamespace(content="done", reasoning="", tool_calls=None)
    )

    assert from_reasoning["reasoning"] == "think first"
    assert "tool_calls" not in from_reasoning
    assert from_alias["reasoning"] == "scratch"
    assert from_alias["content"] == "done"
    assert "reasoning" not in blank


def test_run_sends_reasoning_back(tmp_path):
    seen = []

    def create(**kwargs):
        seen.append(kwargs["messages"])
        if len(seen) == 1:
            message = SimpleNamespace(
                content="",
                reasoning="need the file",
                tool_calls=[
                    SimpleNamespace(
                        id="call_1",
                        function=SimpleNamespace(
                            name="read_file", arguments='{"path": "a.txt"}'
                        ),
                    )
                ],
            )
        else:
            message = SimpleNamespace(content="ready", reasoning="", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    session = agent.Session(workspace=tmp_path)

    text = agent.run("read a.txt", session, client=client)

    assert text == "ready"
    assistant = next(msg for msg in seen[1] if msg["role"] == "assistant")
    assert assistant["reasoning"] == "need the file"
    assert assistant["content"] == ""
    assert session.messages[2]["reasoning"] == "need the file"


def _usage_chunk(total: int):
    return SimpleNamespace(choices=[], usage=SimpleNamespace(total_tokens=total))


def test_run_streams_deltas_into_one_message(tmp_path, monkeypatch):
    monkeypatch.setattr(
        agent_client, "model_context_limit", lambda model, client=None: None
    )
    seen = []
    (tmp_path / "a.txt").write_text("body")

    def create(**kwargs):
        assert kwargs["stream"] is True
        assert kwargs["stream_options"] == {"include_usage": True}
        if len(seen) == 0:
            seen.append(kwargs["messages"])
            return [
                _delta(reasoning="need "),
                _delta(reasoning_content="the file"),
                _delta(
                    tool_calls=[
                        SimpleNamespace(
                            index=0,
                            id="call_1",
                            function=SimpleNamespace(name="read_", arguments=""),
                        )
                    ]
                ),
                _delta(
                    tool_calls=[
                        SimpleNamespace(
                            index=0,
                            id=None,
                            function=SimpleNamespace(
                                name="file", arguments='{"path": "'
                            ),
                        )
                    ]
                ),
                _delta(
                    tool_calls=[
                        SimpleNamespace(
                            index=0,
                            id=None,
                            function=SimpleNamespace(name=None, arguments='a.txt"}'),
                        )
                    ]
                ),
                _usage_chunk(42),
            ]
        seen.append(kwargs["messages"])
        return [
            _delta(content="all "),
            _delta(content="set"),
            _usage_chunk(50),
        ]

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    session = agent.Session(workspace=tmp_path)
    deltas = []
    session.on_delta = lambda kind, text: deltas.append((kind, text))

    text = agent.run("read a.txt", session, client=client)

    assert text == "all set"
    assert deltas == [
        ("reasoning", "need "),
        ("reasoning", "the file"),
        ("content", "all "),
        ("content", "set"),
    ]
    stored = session.messages[2]
    assert stored["reasoning"] == "need the file"
    assert stored["content"] == ""
    assert stored["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'},
        }
    ]
    assert session.context_tokens == 50
    assistant = next(msg for msg in seen[1] if msg["role"] == "assistant")
    assert assistant["reasoning"] == "need the file"


def test_run_logs_cached_prompt_tokens(tmp_path, monkeypatch):
    monkeypatch.setattr(
        agent_client, "model_context_limit", lambda model, client=None: None
    )
    replies = [
        [_delta(content="first"), _usage_chunk(50)],
        [
            _delta(content="second"),
            SimpleNamespace(
                choices=[],
                usage=SimpleNamespace(
                    total_tokens=80,
                    prompt_tokens=60,
                    prompt_tokens_details=SimpleNamespace(cached_tokens=48),
                ),
            ),
        ],
    ]

    def create(**kwargs):
        return replies.pop(0)

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    session = agent.Session(workspace=tmp_path)

    assert agent.run("one", session, client=client) == "first"
    assert agent.run("two", session, client=client) == "second"
    assert session.cache_rounds == [
        agent.CacheRound(read=None, write=None, reusable=None, prompt=None),
        agent.CacheRound(read=48, write=None, reusable=50, prompt=60),
    ]
    assert session.context_tokens == 80
    events = [
        json.loads(line)
        for line in (tmp_path / "session.log").read_text(encoding="utf-8").splitlines()
    ]
    model = [event for event in events if event["event"] == "model"]
    assert "cache_read" not in model[0]
    assert model[1]["cache_read"] == 48
    assert "cache_write" not in model[1]


def test_run_emits_a_whole_message_once(tmp_path, monkeypatch):
    monkeypatch.setattr(
        agent_client, "model_context_limit", lambda model, client=None: None
    )
    deltas = []

    def create(**kwargs):
        assert kwargs["stream"] is True
        message = SimpleNamespace(content="ready", reasoning="think", tool_calls=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=SimpleNamespace(total_tokens=7),
        )

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    session = agent.Session(workspace=tmp_path)
    session.on_delta = lambda kind, text: deltas.append((kind, text))

    assert agent.run("hi", session, client=client) == "ready"
    assert deltas == [("reasoning", "think"), ("content", "ready")]
    assert session.context_tokens == 7


def test_run_retries_when_stream_options_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(
        agent_client, "model_context_limit", lambda model, client=None: None
    )
    calls = []

    def create(**kwargs):
        calls.append("stream_options" in kwargs)
        if "stream_options" in kwargs:
            raise _status_error("unknown parameter stream_options")
        message = SimpleNamespace(content="ok", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    session = agent.Session(workspace=tmp_path)

    assert agent.run("hi", session, client=client) == "ok"
    assert calls == [True, False]


def test_run_sends_max_tokens_on_every_openai_call(tmp_path, monkeypatch):
    monkeypatch.setattr(
        agent_client, "model_context_limit", lambda model, client=None: None
    )
    monkeypatch.setattr(config, "MAX_TOKENS", 777)
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        message = SimpleNamespace(content="ok", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    session = agent.Session(workspace=tmp_path)

    assert agent.run("hi", session, client=client) == "ok"
    assert calls[0]["max_tokens"] == 777
    assert "max_completion_tokens" not in calls[0]

    _prior_turn(session)
    session.context_tokens = 10
    session.context_limit = 100
    assert agent.compact_session(session, client=client) == "Context compacted."
    assert calls[-1]["max_tokens"] == 777
    assert "stream" not in calls[-1]


def test_run_falls_back_to_max_completion_tokens(tmp_path, monkeypatch):
    monkeypatch.setattr(
        agent_client, "model_context_limit", lambda model, client=None: None
    )
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if "max_tokens" in kwargs:
            raise _status_error(
                "Unsupported parameter: 'max_tokens' is not supported with this "
                "model. Use 'max_completion_tokens' instead."
            )
        if "stream_options" in kwargs:
            raise _status_error("unknown parameter stream_options")
        message = SimpleNamespace(content="ok", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    session = agent.Session(workspace=tmp_path)

    assert agent.run("hi", session, client=client) == "ok"
    assert len(calls) == 3
    assert "max_tokens" not in calls[-1]
    assert calls[-1]["max_completion_tokens"] == config.MAX_TOKENS
    assert "stream_options" not in calls[-1]
    assert calls[-1]["stream"] is True

    def reject_everything(**kwargs):
        raise _status_error("max_tokens is too large for this model")

    client.chat.completions.create = reject_everything
    assert agent.run("again", session, client=client).startswith(
        "The API rejected the request: max_tokens is too large"
    )


def test_abort_during_a_stream_skips_the_rest_of_the_turn(tmp_path):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)

        def chunks():
            yield _delta(content="partial")
            session.abort.set()
            yield _delta(content=" more")
            yield _delta(
                tool_calls=[
                    SimpleNamespace(
                        index=0,
                        id="call_1",
                        function=SimpleNamespace(
                            name="load_skill", arguments='{"name": "shell"}'
                        ),
                    )
                ]
            )

        return chunks()

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    session = agent.Session(workspace=tmp_path)

    text = agent.run("hello", session, client=client)

    assert text == ""
    assert len(calls) == 1
    assert [message["role"] for message in session.messages] == ["system", "user"]


def test_abort_skips_the_next_completion(tmp_path):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        message = SimpleNamespace(
            content=None,
            tool_calls=[
                SimpleNamespace(
                    id="call_1",
                    type="function",
                    function=SimpleNamespace(
                        name="load_skill", arguments='{"name": "shell"}'
                    ),
                )
            ],
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    session = agent.Session(workspace=tmp_path)

    def on_tool(name, args):
        session.abort.set()

    text = agent.run("hello", session, client=client, on_tool=on_tool)

    assert text == ""
    assert len(calls) == 1
    assert session.messages[-1]["role"] == "tool"


def test_abort_answers_every_pending_tool_call(tmp_path):
    def call(call_id):
        return SimpleNamespace(
            id=call_id,
            type="function",
            function=SimpleNamespace(name="load_skill", arguments='{"name": "shell"}'),
        )

    def create(**kwargs):
        message = SimpleNamespace(
            content=None, tool_calls=[call("call_1"), call("call_2"), call("call_3")]
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    session = agent.Session(workspace=tmp_path)

    def on_tool(name, args):
        session.abort.set()

    assert agent.run("hello", session, client=client, on_tool=on_tool) == ""

    tools = [m for m in session.messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tools] == ["call_1", "call_2", "call_3"]
    assert "shell" in tools[0]["content"]
    assert tools[1]["content"] == agent_loop.INTERRUPTED_RESULT
    assert tools[2]["content"] == agent_loop.INTERRUPTED_RESULT
    saved = json.loads((tmp_path / TRANSCRIPT_FILE).read_text(encoding="utf-8"))
    assert [m["tool_call_id"] for m in saved["messages"] if m["role"] == "tool"] == [
        "call_1",
        "call_2",
        "call_3",
    ]
    events = [
        json.loads(line) for line in (tmp_path / "session.log").read_text().splitlines()
    ]
    results = [e for e in events if e["event"] == "tool_result"]
    assert len(results) == 3
    assert results[2]["content"] == agent_loop.INTERRUPTED_RESULT


def test_run_drops_a_partial_stream_when_the_api_drops(tmp_path):
    def create(**kwargs):
        def chunks():
            yield _delta(content="partial")
            raise _unreachable("connection")

        return chunks()

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    session = agent.Session(workspace=tmp_path)
    seen = []
    session.on_delta = lambda kind, text: seen.append((kind, text))

    text = agent.run("hello", session, client=client)

    assert text == "Could not reach the API."
    assert seen == [("content", "partial")]
    assert [message["role"] for message in session.messages] == [
        "system",
        "user",
        "assistant",
    ]
    assert session.messages[-1] == {"role": "assistant", "content": text}


def _status_error(message: str, status: int = 400):
    import httpx2
    from openai import BadRequestError, NotFoundError

    response = httpx2.Response(
        status,
        request=httpx2.Request("POST", "http://127.0.0.1:9/v1/chat/completions"),
    )
    error = NotFoundError if status == 404 else BadRequestError
    return error(message, response=response, body=None)


_ASTRO_REJECTION = (
    "Function tools with reasoning_effort are not supported for gpt-6-astra "
    "in /v1/chat/completions. To use function tools, use /v1/responses or "
    "set reasoning_effort to 'none'."
)


def _responses_message(text: str):
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


def _responses_output(text: str):
    return SimpleNamespace(output=[_responses_message(text)])


def _client(chat_create, responses_create=None):
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=chat_create))
    )
    if responses_create is not None:
        client.responses = SimpleNamespace(create=responses_create)
    return client


def test_run_reports_an_openai_rejection(tmp_path):
    def create(**kwargs):
        raise _status_error("model not found")

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    session = agent.Session(workspace=tmp_path)

    text = agent.run("hello", session, client=client)

    assert text == "The API rejected the request: model not found"
    assert session.messages[-1] == {"role": "assistant", "content": text}


def test_run_returns_reasoning_when_content_is_empty(tmp_path):
    def create(**kwargs):
        message = SimpleNamespace(content="", reasoning="the summary", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    session = agent.Session(workspace=tmp_path)

    assert agent.run("summarize", session, client=client) == "the summary"


def test_run_reports_an_empty_model_answer(tmp_path):
    def create(**kwargs):
        message = SimpleNamespace(content="", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    session = agent.Session(workspace=tmp_path)

    assert agent.run("summarize", session, client=client) == (
        "The model returned an empty answer."
    )


def test_run_reports_when_the_api_is_unreachable(tmp_path):
    def create(**kwargs):
        raise _unreachable("connection")

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    session = agent.Session(workspace=tmp_path)

    text = agent.run("hello", session, client=client)

    assert text == "Could not reach the API."
    assert session.messages[-2]["content"] == "hello"
    assert session.messages[-1] == {"role": "assistant", "content": text}
    events = [
        json.loads(line)["event"]
        for line in (tmp_path / "session.log").read_text().splitlines()
    ]
    assert events == ["turn", "model"]


def test_run_reports_when_the_api_times_out(tmp_path):
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
                            name="read_file", arguments='{"path": "a.txt"}'
                        ),
                    )
                ],
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])
        raise _unreachable("timeout")

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    session = agent.Session(workspace=tmp_path)

    text = agent.run("read a.txt", session, client=client)

    assert text == "The API request timed out."
    assert session.messages[-2]["role"] == "tool"
    assert session.messages[-1] == {"role": "assistant", "content": text}


def test_model_log_truncates_reasoning(tmp_path):
    thinking = "r" * 600

    def create(**kwargs):
        message = SimpleNamespace(content="ok", reasoning=thinking, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    session = agent.Session(workspace=tmp_path)

    assert agent.run("think", session, client=client) == "ok"
    assert session.messages[-1]["reasoning"] == thinking
    events = [
        json.loads(line) for line in (tmp_path / "session.log").read_text().splitlines()
    ]
    logged = next(
        event["message"]["reasoning"] for event in events if event["event"] == "model"
    )
    assert logged == "r" * 500 + "...[truncated]"


def test_invalid_tool_arguments_become_a_tool_error(tmp_path):
    args, result = _tool_result("read_file", "not-json", tmp_path)

    assert args is None
    assert result.startswith("Error: invalid tool arguments:")


def test_max_turns_closes_with_an_assistant_message(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_loop, "MAX_TURNS", 1)

    def create(**kwargs):
        message = SimpleNamespace(
            content="",
            tool_calls=[
                SimpleNamespace(
                    id="call_1",
                    function=SimpleNamespace(
                        name="read_file", arguments='{"path": "missing.txt"}'
                    ),
                )
            ],
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    session = agent.Session(workspace=tmp_path)

    text = agent.run("read missing.txt", session, client=client)

    assert text == "Max turns reached."
    assert session.messages[-1] == {
        "role": "assistant",
        "content": "Max turns reached.",
    }
    assert session.messages[-2]["role"] == "tool"
    log_path = tmp_path / "session.log"
    events = [json.loads(line)["event"] for line in log_path.read_text().splitlines()]
    assert events == ["turn", "model", "tool_call", "tool_result", "model"]


def _tool_call_client(scripts):
    calls = {"n": 0}

    def create(**kwargs):
        calls["n"] += 1
        step = scripts[calls["n"] - 1]
        if step == "answer":
            message = SimpleNamespace(content="finished", tool_calls=None)
        else:
            message = SimpleNamespace(
                content="",
                tool_calls=[
                    SimpleNamespace(
                        id=f"call_{calls['n']}",
                        function=SimpleNamespace(
                            name="read_file", arguments='{"path": "missing.txt"}'
                        ),
                    )
                ],
            )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return client, calls


def test_confirm_continue_grants_another_window(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_loop, "MAX_TURNS", 1)
    client, calls = _tool_call_client(["tool", "answer"])
    session = agent.Session(workspace=tmp_path)
    asked = {"n": 0}

    def confirm():
        asked["n"] += 1
        return True

    session.confirm_continue = confirm

    text = agent.run("read missing.txt", session, client=client)

    assert text == "finished"
    assert asked["n"] == 1
    assert calls["n"] == 2
    assert all(
        message.get("content") != "Max turns reached." for message in session.messages
    )
    events = [
        json.loads(line) for line in (tmp_path / "session.log").read_text().splitlines()
    ]
    assert [event["turn"] for event in events if event["event"] == "turn"] == [0, 1]


def test_declining_another_window_closes_after_two(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_loop, "MAX_TURNS", 1)
    client, calls = _tool_call_client(["tool", "tool"])
    session = agent.Session(workspace=tmp_path)
    answers = iter([True, False])
    session.confirm_continue = lambda: next(answers)

    text = agent.run("read missing.txt", session, client=client)

    assert text == "Max turns reached."
    assert calls["n"] == 2
    assert session.messages[-1] == {
        "role": "assistant",
        "content": "Max turns reached.",
    }
    assert session.messages[-2]["role"] == "tool"
    events = [
        json.loads(line) for line in (tmp_path / "session.log").read_text().splitlines()
    ]
    assert [event["turn"] for event in events if event["event"] == "turn"] == [0, 1]
    assert [event["event"] for event in events] == [
        "turn",
        "model",
        "tool_call",
        "tool_result",
        "turn",
        "model",
        "tool_call",
        "tool_result",
        "model",
    ]


def test_abort_during_confirm_continue_does_not_close(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_loop, "MAX_TURNS", 1)
    client, _calls = _tool_call_client(["tool"])
    session = agent.Session(workspace=tmp_path)

    def confirm():
        session.abort.set()
        return True

    session.confirm_continue = confirm

    text = agent.run("read missing.txt", session, client=client)

    assert text == ""
    assert session.messages[-1]["role"] == "tool"
    assert all(
        message.get("content") != "Max turns reached." for message in session.messages
    )


def test_run_sends_skill_schemas_after_load(tmp_path):
    seen = []
    calls = {"n": 0}

    def create(**kwargs):
        seen.append(kwargs["tools"])
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
        else:
            message = SimpleNamespace(content="ready", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    session = agent.Session(workspace=tmp_path)

    text = agent.run("read a file", session, client=client)

    assert text == "ready"
    assert _tool_names(seen[0]) == ["load_skill", "unload_skill", "task"]
    assert "read_file" in _tool_names(seen[1])
    assert "write_file" in _tool_names(seen[1])
    assert "delete_file" in _tool_names(seen[1])
    assert "run_shell" not in _tool_names(seen[1])
    file_ops_line = next(
        line
        for line in session.messages[0]["content"].splitlines()
        if line.startswith("- file-ops:")
    )
    assert file_ops_line.endswith(FILE_OPS_LOADED)
    assert "- shell:" in session.messages[0]["content"]
    assert " [loaded]" not in next(
        line
        for line in session.messages[0]["content"].splitlines()
        if line.startswith("- shell:")
    )


def _tool_client(name: str, arguments: str, reply: str):
    calls = {"n": 0}

    def create(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            message = SimpleNamespace(
                content="",
                tool_calls=[
                    SimpleNamespace(
                        id="call_1",
                        function=SimpleNamespace(name=name, arguments=arguments),
                    )
                ],
            )
        else:
            message = SimpleNamespace(content=reply, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )


def test_show_tools_prints_the_tool_call(tmp_path, capsys):
    session = agent.Session(workspace=tmp_path)
    client = _tool_client("load_skill", '{"name": "file-ops"}', "loaded")

    text = agent.run("load file ops", session, client=client)

    assert text == "loaded"
    assert "load_skill" in capsys.readouterr().out


def test_show_tools_can_hide_tool_call_output(tmp_path, capsys):
    session = agent.Session(workspace=tmp_path)
    client = _tool_client("load_skill", '{"name": "file-ops"}', "loaded")

    text = agent.run("load file ops", session, client=client, show_tools=False)

    assert text == "loaded"
    assert capsys.readouterr().out == ""


def test_on_tool_receives_the_call_instead_of_printing(tmp_path, capsys):
    session = agent.Session(workspace=tmp_path)
    client = _tool_client("load_skill", '{"name": "file-ops"}', "loaded")
    seen = []

    text = agent.run(
        "load file ops",
        session,
        client=client,
        on_tool=lambda name, args: seen.append((name, args)),
    )

    assert text == "loaded"
    assert seen == [("load_skill", {"name": "file-ops"})]
    assert capsys.readouterr().out == ""


def test_on_tool_fires_before_the_tool_runs_and_on_tool_done_gets_the_result(
    tmp_path, monkeypatch
):
    session = agent.Session(workspace=tmp_path)
    client = _tool_client("read_file", '{"path": "missing.txt"}', "done")
    order = []
    real = agent_loop._tool_result

    def tracked(name, arguments, *args, **kwargs):
        order.append(("run", name))
        return real(name, arguments, *args, **kwargs)

    monkeypatch.setattr(agent_loop, "_tool_result", tracked)
    session.on_tool_done = lambda name, result: order.append(("done", name, result))

    text = agent.run(
        "read",
        session,
        client=client,
        on_tool=lambda name, args: order.append(("start", name, args)),
    )

    assert text == "done"
    assert [step[0] for step in order] == ["start", "run", "done"]
    assert order[0] == ("start", "read_file", {"path": "missing.txt"})
    assert order[2][1] == "read_file"
    assert order[2][2] == session.messages[-2]["content"]
    assert session.messages[-2]["role"] == "tool"


def test_on_tool_done_is_skipped_when_tools_are_hidden(tmp_path):
    session = agent.Session(workspace=tmp_path)
    client = _tool_client("load_skill", '{"name": "file-ops"}', "loaded")
    seen = []
    session.on_tool_done = lambda name, result: seen.append(name)

    assert agent.run("load", session, client=client, show_tools=False) == "loaded"
    assert seen == []


def test_run_uses_the_session_model(tmp_path):
    seen = {}

    def create(**kwargs):
        seen["model"] = kwargs["model"]
        message = SimpleNamespace(content="ok", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    session = agent.Session(workspace=tmp_path, model="custom-model")

    text = agent.run("hi", session, client=client, show_tools=False)

    assert text == "ok"
    assert seen["model"] == "custom-model"


def _reject_astra(**kwargs):
    raise _status_error(_ASTRO_REJECTION)


def test_run_switches_to_responses_and_replays_items(tmp_path):
    session = agent.Session(workspace=tmp_path, model="gpt-6-astra")
    chat_calls = []
    response_calls = []
    reasoning = {
        "type": "reasoning",
        "id": "rs_1",
        "encrypted_content": "secret",
    }
    call = {
        "type": "function_call",
        "call_id": "call_1",
        "name": "load_skill",
        "arguments": '{"name": "shell"}',
    }

    def chat_create(**kwargs):
        chat_calls.append(kwargs)
        raise _status_error(_ASTRO_REJECTION)

    def responses_create(**kwargs):
        response_calls.append(kwargs)
        if len(response_calls) == 1:
            return SimpleNamespace(output=[reasoning, call])
        return _responses_output("ready")

    client = _client(chat_create, responses_create)

    assert agent.run("load shell", session, client=client) == "ready"

    assert len(chat_calls) == 1
    assert "reasoning_effort" not in chat_calls[0]
    sent = response_calls[0]
    assert sent["model"] == "gpt-6-astra"
    assert sent["stream"] is True
    assert sent["store"] is False
    assert sent["include"] == ["reasoning.encrypted_content"]
    assert sent["max_output_tokens"] == config.MAX_TOKENS
    assert all(
        tool["type"] == "function" and "function" not in tool for tool in sent["tools"]
    )
    assert any(tool["name"] == "load_skill" for tool in sent["tools"])
    stored = session.messages[2]
    assert stored["tool_calls"][0]["id"] == "call_1"
    assert stored["response_items"] == [reasoning, call]
    follow = response_calls[1]
    assert follow["model"] == "gpt-6-astra"
    assert reasoning in follow["input"]
    assert call in follow["input"]
    output = next(
        item for item in follow["input"] if item.get("type") == "function_call_output"
    )
    assert output["call_id"] == "call_1"
    assert output["output"].startswith("Loaded skill 'shell'")


def test_a_different_model_stays_on_chat_completions(tmp_path):
    session = agent.Session(workspace=tmp_path, model="gpt-6-astra")
    response_calls = []

    def chat_create(**kwargs):
        if kwargs["model"] == "gpt-6-astra":
            raise _status_error(_ASTRO_REJECTION)
        message = SimpleNamespace(content="from chat", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    def responses_create(**kwargs):
        response_calls.append(kwargs["model"])
        return _responses_output("from responses")

    client = _client(chat_create, responses_create)

    assert agent.run("hi", session, client=client) == "from responses"
    session.model = "qwen"
    assert agent.run("next", session, client=client) == "from chat"
    assert response_calls == ["gpt-6-astra"]


def test_missing_responses_route_uses_reasoning_effort_none(tmp_path):
    session = agent.Session(workspace=tmp_path, model="gpt-6-astra")
    chat_calls = []
    response_calls = []

    def chat_create(**kwargs):
        chat_calls.append(kwargs)
        if kwargs.get("reasoning_effort") != "none":
            raise _status_error(_ASTRO_REJECTION)
        message = SimpleNamespace(content="ok", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    def responses_create(**kwargs):
        response_calls.append(kwargs)
        raise _status_error("unknown route /v1/responses", status=404)

    client = _client(chat_create, responses_create)

    assert agent.run("hi", session, client=client) == "ok"
    assert chat_calls[0].get("reasoning_effort") is None
    assert chat_calls[1]["reasoning_effort"] == "none"
    assert "tools" in chat_calls[1]
    assert len(response_calls) == 1

    assert agent.run("again", session, client=client) == "ok"
    assert len(response_calls) == 1
    assert chat_calls[-1]["reasoning_effort"] == "none"


def test_responses_model_compacts_without_tools(tmp_path):
    session = agent.Session(workspace=tmp_path, model="gpt-6-astra")
    _prior_turn(session)
    chat_calls = []
    response_calls = []

    def chat_create(**kwargs):
        chat_calls.append(kwargs)
        raise _status_error(_ASTRO_REJECTION)

    def responses_create(**kwargs):
        response_calls.append(kwargs)
        text = "ok" if "tools" in kwargs else "the summary"
        return _responses_output(text)

    client = _client(chat_create, responses_create)

    assert agent.run("hi", session, client=client) == "ok"
    assert len(chat_calls) == 1
    session.context_tokens = 80
    session.context_limit = 100

    assert agent.compact_session(session, client=client) == "Context compacted."

    assert len(chat_calls) == 1
    assert "tools" in response_calls[0]
    assert "tools" not in response_calls[-1]
    assert "stream" not in response_calls[-1]
    assert "the summary" in session.messages[1]["content"]


def test_chat_completions_omit_response_items(tmp_path):
    session = agent.Session(workspace=tmp_path)
    session.messages.append({"role": "user", "content": "earlier"})
    session.messages.append(
        {
            "role": "assistant",
            "content": "done",
            "response_items": [{"type": "reasoning", "encrypted_content": "secret"}],
        }
    )
    seen = []

    def chat_create(**kwargs):
        seen.append(kwargs["messages"])
        message = SimpleNamespace(content="ok", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    assert agent.run("next", session, client=_client(chat_create)) == "ok"
    assert "response_items" not in json.dumps(seen[0])
    assert "secret" not in json.dumps(seen[0])


def test_responses_stream_joins_text_and_tool_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(
        agent_client, "model_context_limit", lambda model, client=None: None
    )
    session = agent.Session(workspace=tmp_path, model="gpt-6-astra")
    deltas = []
    session.on_delta = lambda kind, text: deltas.append((kind, text))
    calls = []

    def responses_create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return [
                SimpleNamespace(type="response.reasoning_text.delta", delta="need "),
                SimpleNamespace(
                    type="response.output_item.added",
                    output_index=0,
                    item=SimpleNamespace(
                        type="function_call",
                        call_id="call_1",
                        name="load_skill",
                        arguments="",
                    ),
                ),
                SimpleNamespace(
                    type="response.function_call_arguments.delta",
                    output_index=0,
                    delta='{"name": ',
                ),
                SimpleNamespace(
                    type="response.function_call_arguments.delta",
                    output_index=0,
                    delta='"shell"}',
                ),
                SimpleNamespace(
                    type="response.output_item.done",
                    output_index=1,
                    item={
                        "type": "reasoning",
                        "id": "rs_1",
                        "encrypted_content": "secret",
                    },
                ),
                SimpleNamespace(
                    type="response.completed",
                    response=SimpleNamespace(
                        usage=SimpleNamespace(
                            input_tokens=20,
                            total_tokens=30,
                            input_tokens_details=SimpleNamespace(
                                cached_tokens=4, cache_write_tokens=2
                            ),
                        )
                    ),
                ),
            ]
        return [
            SimpleNamespace(type="response.output_text.delta", delta="all "),
            SimpleNamespace(type="response.output_text.delta", delta="set"),
        ]

    assert (
        agent.run("load", session, client=_client(_reject_astra, responses_create))
        == "all set"
    )
    assert deltas == [
        ("reasoning", "need "),
        ("content", "all "),
        ("content", "set"),
    ]
    stored = session.messages[2]
    assert stored["reasoning"] == "need "
    assert stored["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "load_skill",
                "arguments": '{"name": "shell"}',
            },
        }
    ]
    assert stored["response_items"][0]["encrypted_content"] == "secret"
    assert session.context_tokens == 30
    assert session.cache_rounds[0] == agent.CacheRound(
        read=4, write=2, reusable=None, prompt=20
    )


def test_responses_summary_is_kept_only_without_reasoning_text(tmp_path):
    session = agent.Session(workspace=tmp_path, model="gpt-6-astra")
    calls = []

    def responses_create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return [
                SimpleNamespace(
                    type="response.reasoning_summary_text.delta", delta="sum"
                ),
                SimpleNamespace(type="response.reasoning_text.delta", delta="real"),
                SimpleNamespace(type="response.output_text.delta", delta="ok"),
            ]
        return [
            SimpleNamespace(
                type="response.reasoning_summary_text.delta", delta="only summary"
            ),
            SimpleNamespace(type="response.output_text.delta", delta="done"),
        ]

    client = _client(_reject_astra, responses_create)

    assert agent.run("hi", session, client=client) == "ok"
    assert session.messages[-1]["reasoning"] == "real"
    assert agent.run("next", session, client=client) == "done"
    assert session.messages[-1]["reasoning"] == "only summary"


def test_responses_rebuilds_a_chat_tool_call(tmp_path):
    session = agent.Session(workspace=tmp_path, model="gpt-6-astra")
    session.messages.append({"role": "user", "content": "earlier"})
    session.messages.append(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_9",
                    "type": "function",
                    "function": {
                        "name": "load_skill",
                        "arguments": '{"name": "shell"}',
                    },
                }
            ],
        }
    )
    session.messages.append(
        {"role": "tool", "tool_call_id": "call_9", "content": "loaded"}
    )
    seen = []

    def responses_create(**kwargs):
        seen.append(kwargs)
        return _responses_output("ready")

    assert (
        agent.run("continue", session, client=_client(_reject_astra, responses_create))
        == "ready"
    )
    call = next(
        item for item in seen[0]["input"] if item.get("type") == "function_call"
    )
    assert call == {
        "type": "function_call",
        "call_id": "call_9",
        "name": "load_skill",
        "arguments": '{"name": "shell"}',
    }
    output = next(
        item for item in seen[0]["input"] if item.get("type") == "function_call_output"
    )
    assert output == {
        "type": "function_call_output",
        "call_id": "call_9",
        "output": "loaded",
    }


def test_missing_responses_attribute_uses_reasoning_effort_none(tmp_path):
    session = agent.Session(workspace=tmp_path, model="gpt-6-astra")
    calls = []

    def chat_create(**kwargs):
        calls.append(kwargs)
        if kwargs.get("reasoning_effort") != "none":
            raise _status_error(_ASTRO_REJECTION)
        message = SimpleNamespace(content="ok", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    client = _client(chat_create)

    assert agent.run("hi", session, client=client) == "ok"
    assert calls[0].get("reasoning_effort") is None
    assert calls[1]["reasoning_effort"] == "none"

    assert agent.run("again", session, client=client) == "ok"
    assert len(calls) == 3
    assert calls[-1]["reasoning_effort"] == "none"
