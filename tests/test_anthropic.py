"""The Anthropic Messages translation layer."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers import _delta, _prior_turn

import harnh3x.agent.client as agent_client
from harnh3x import agent, anthropic_api, config
from harnh3x.anthropic_api import AnthropicClient, to_anthropic


def test_to_anthropic_lifts_system_and_merges_turns():
    payload = to_anthropic(
        [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "note"},
            {"role": "user", "content": "question"},
            {
                "role": "assistant",
                "content": "calling",
                "thinking_blocks": [
                    {
                        "type": "thinking",
                        "thinking": "hmm",
                        "signature": "sig",
                    },
                    {"type": "redacted_thinking", "data": "abc"},
                ],
                "tool_calls": [
                    {
                        "id": "toolu_1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "a.txt"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "toolu_1", "content": "body"},
            {"role": "tool", "tool_call_id": "toolu_2", "content": "more"},
        ],
        [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )

    assert payload["system"] == "be brief"
    assert [message["role"] for message in payload["messages"]] == [
        "user",
        "assistant",
        "user",
    ]
    assert [block["text"] for block in payload["messages"][0]["content"]] == [
        "note",
        "question",
    ]
    assistant = payload["messages"][1]["content"]
    assert assistant[0] == {"type": "thinking", "thinking": "hmm", "signature": "sig"}
    assert assistant[1] == {"type": "redacted_thinking", "data": "abc"}
    assert assistant[2] == {"type": "text", "text": "calling"}
    assert assistant[3]["type"] == "tool_use"
    assert assistant[3]["input"] == {"path": "a.txt"}
    results = payload["messages"][2]["content"]
    assert [block["type"] for block in results] == ["tool_result", "tool_result"]
    assert results[0]["tool_use_id"] == "toolu_1"
    assert payload["tools"] == [
        {
            "name": "read_file",
            "description": "Read a file",
            "input_schema": {"type": "object", "properties": {}},
        }
    ]


def _anthropic_unreachable(kind: str):
    import httpx2
    from anthropic import APIConnectionError, APITimeoutError

    error = APITimeoutError if kind == "timeout" else APIConnectionError
    return error(
        request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    )


def _anthropic_rejected(message: str):
    import httpx2
    from anthropic import BadRequestError

    body = {
        "type": "error",
        "error": {"type": "invalid_request_error", "message": message},
        "request_id": "req_test",
    }
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(400, json=body, request=request)
    return BadRequestError(f"Error code: 400 - {body}", response=response, body=body)


def _anthropic_client(stream):
    return AnthropicClient(SimpleNamespace(messages=SimpleNamespace(stream=stream)))


def test_run_streams_anthropic_thinking_and_replays_it(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_TOKENS", 8192)
    monkeypatch.setattr(config, "CONTEXT_LIMIT", None)
    (tmp_path / "a.txt").write_text("body")
    seen = []
    deltas = []

    def stream(**kwargs):
        seen.append(kwargs)
        if len(seen) == 1:
            return [
                SimpleNamespace(
                    type="message_start",
                    message=SimpleNamespace(usage=SimpleNamespace(input_tokens=10)),
                ),
                SimpleNamespace(
                    type="content_block_start",
                    index=0,
                    content_block=SimpleNamespace(
                        type="thinking", thinking="", signature=""
                    ),
                ),
                SimpleNamespace(
                    type="content_block_delta",
                    index=0,
                    delta=SimpleNamespace(type="thinking_delta", thinking="need "),
                ),
                SimpleNamespace(
                    type="content_block_delta",
                    index=0,
                    delta=SimpleNamespace(type="thinking_delta", thinking="the file"),
                ),
                SimpleNamespace(
                    type="content_block_delta",
                    index=0,
                    delta=SimpleNamespace(type="signature_delta", signature="sig-1"),
                ),
                SimpleNamespace(
                    type="content_block_start",
                    index=1,
                    content_block=SimpleNamespace(
                        type="tool_use", id="toolu_1", name="read_file", input={}
                    ),
                ),
                SimpleNamespace(
                    type="content_block_delta",
                    index=1,
                    delta=SimpleNamespace(
                        type="input_json_delta", partial_json='{"path": "a.txt"}'
                    ),
                ),
                SimpleNamespace(
                    type="message_delta",
                    usage=SimpleNamespace(output_tokens=5),
                ),
            ]
        return [
            SimpleNamespace(
                type="content_block_delta",
                index=0,
                delta=SimpleNamespace(type="text_delta", text="all "),
            ),
            SimpleNamespace(
                type="content_block_delta",
                index=0,
                delta=SimpleNamespace(type="text_delta", text="set"),
            ),
            SimpleNamespace(
                type="message_start",
                message=SimpleNamespace(usage=SimpleNamespace(input_tokens=20)),
            ),
            SimpleNamespace(
                type="message_delta",
                usage=SimpleNamespace(output_tokens=4),
            ),
        ]

    session = agent.Session(workspace=tmp_path)
    session.on_delta = lambda kind, text: deltas.append((kind, text))

    text = agent.run(
        "read a.txt",
        session,
        client=_anthropic_client(stream),
        show_tools=False,
    )

    assert text == "all set"
    assert ("reasoning", "need ") in deltas
    assert ("reasoning", "the file") in deltas
    assert ("content", "all ") in deltas
    assert ("content", "set") in deltas
    assert seen[0]["max_tokens"] == 8192
    assert seen[0]["tools"][0]["input_schema"]["type"] == "object"
    stored = next(message for message in session.messages if message.get("tool_calls"))
    assert stored["thinking_blocks"] == [
        {"type": "thinking", "thinking": "need the file", "signature": "sig-1"}
    ]
    assert stored["tool_calls"][0]["id"] == "toolu_1"
    assert stored["tool_calls"][0]["function"]["arguments"] == '{"path": "a.txt"}'
    assert session.context_tokens == 24
    assistant = next(
        message for message in seen[1]["messages"] if message["role"] == "assistant"
    )
    assert assistant["content"][0]["signature"] == "sig-1"
    assert assistant["content"][0]["thinking"] == "need the file"


def test_run_reports_an_anthropic_rejection(tmp_path):
    notice = (
        "Your credit balance is too low to access the Anthropic API. "
        "Please go to Plans & Billing to upgrade or purchase credits."
    )

    def stream(**kwargs):
        raise _anthropic_rejected(
            "Your credit balance is too low to access the Anthropic API. Please go to Plans\n& Billing to upgrade or purchase credits."
        )

    session = agent.Session(workspace=tmp_path)

    text = agent.run("hello", session, client=_anthropic_client(stream))

    assert text == f"The API rejected the request: {notice}"
    assert session.messages[-1] == {"role": "assistant", "content": text}
    events = [
        json.loads(line)["event"]
        for line in (tmp_path / "session.log").read_text(encoding="utf-8").splitlines()
    ]
    assert events == ["turn", "model"]


def test_run_drops_a_partial_anthropic_stream_on_rejection(tmp_path):
    def stream(**kwargs):
        def events():
            yield SimpleNamespace(
                type="content_block_delta",
                index=0,
                delta=SimpleNamespace(type="text_delta", text="partial"),
            )
            raise _anthropic_rejected("credit balance is too low")

        return events()

    session = agent.Session(workspace=tmp_path)

    text = agent.run("hello", session, client=_anthropic_client(stream))

    assert text == "The API rejected the request: credit balance is too low"
    assert session.messages[-1] == {"role": "assistant", "content": text}


def test_list_models_exits_when_anthropic_rejects(tmp_path):
    def fail():
        raise _anthropic_rejected("credit balance is too low")

    client = AnthropicClient(SimpleNamespace(models=SimpleNamespace(list=fail)))

    with pytest.raises(SystemExit, match="credit balance is too low"):
        agent.list_models(client)


def test_run_reports_when_anthropic_times_out(tmp_path):
    def stream(**kwargs):
        raise _anthropic_unreachable("timeout")

    session = agent.Session(workspace=tmp_path)

    text = agent.run("hello", session, client=_anthropic_client(stream))

    assert text == "The API request timed out."
    assert session.messages[-1] == {"role": "assistant", "content": text}


def test_run_drops_a_partial_anthropic_stream(tmp_path):
    def stream(**kwargs):
        def events():
            yield SimpleNamespace(
                type="content_block_delta",
                index=0,
                delta=SimpleNamespace(type="text_delta", text="partial"),
            )
            raise _anthropic_unreachable("connection")

        return events()

    session = agent.Session(workspace=tmp_path)
    seen = []
    session.on_delta = lambda kind, text: seen.append((kind, text))

    text = agent.run("hello", session, client=_anthropic_client(stream))

    assert text == "Could not reach the API."
    assert seen == [("content", "partial")]
    assert [message["role"] for message in session.messages] == [
        "system",
        "user",
        "assistant",
    ]
    assert session.messages[-1] == {"role": "assistant", "content": text}


def test_list_models_reads_anthropic_ids():
    page = SimpleNamespace(
        data=[SimpleNamespace(id="claude-b"), SimpleNamespace(id="claude-a")]
    )
    client = AnthropicClient(SimpleNamespace(models=SimpleNamespace(list=lambda: page)))

    assert agent.list_models(client) == ["claude-a", "claude-b"]


def test_list_models_exits_when_anthropic_is_unreachable():
    def fail():
        raise _anthropic_unreachable("connection")

    client = AnthropicClient(SimpleNamespace(models=SimpleNamespace(list=fail)))

    with pytest.raises(SystemExit, match="Could not reach the API"):
        agent.list_models(client)


def test_anthropic_context_limit_defaults(monkeypatch):
    monkeypatch.setattr(config, "CONTEXT_LIMIT", None)
    client = AnthropicClient(SimpleNamespace())

    assert agent.model_context_limit("claude", client) == 200_000

    monkeypatch.setattr(config, "CONTEXT_LIMIT", 1000)

    assert agent.model_context_limit("claude", client) == 1000


def test_openai_requests_omit_thinking_blocks(tmp_path):
    session = agent.Session(workspace=tmp_path)
    session.messages.append({"role": "user", "content": "earlier"})
    session.messages.append(
        {
            "role": "assistant",
            "content": "done",
            "thinking_blocks": [
                {"type": "thinking", "thinking": "secret", "signature": "sig"}
            ],
        }
    )
    seen = []

    def create(**kwargs):
        seen.append(kwargs["messages"])
        return [_delta(content="ok")]

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )

    assert agent.run("next", session, client=client) == "ok"
    assert "thinking_blocks" not in json.dumps(seen[0])
    assert "secret" not in json.dumps(seen[0])


def test_compact_session_uses_the_messages_api(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_TOKENS", 512)
    session = agent.Session(workspace=tmp_path, model="claude")
    _prior_turn(session)
    session.messages.append({"role": "user", "content": "later"})
    session.messages.append({"role": "assistant", "content": "answered"})
    session.context_tokens = 10
    session.context_limit = 100
    seen = []

    def create(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="forced summary")],
            usage=SimpleNamespace(input_tokens=3, output_tokens=4),
        )

    client = AnthropicClient(
        SimpleNamespace(
            messages=SimpleNamespace(create=create, stream=lambda **kwargs: [])
        )
    )

    assert agent.compact_session(session, client=client) == "Context compacted."
    assert seen[0]["max_tokens"] == 512
    assert seen[0]["model"] == "claude"
    assert seen[0]["system"].startswith("Summarize")
    assert "tools" not in seen[0]
    assert all(message["role"] != "system" for message in seen[0]["messages"])
    assert session.context_tokens == session.messages[1]["context_after"]
    assert session.messages[1]["content"].startswith(
        "Context compacted:\nforced summary"
    )


def test_anthropic_provider_prefers_its_own_key():
    path = Path("harnh3x.yaml")
    values, _servers = config.parse_config(
        "provider: anthropic\napi_key: file-key\n", path
    )

    preferred = config.resolve_settings(
        values,
        {"OPENAI_API_KEY": "openai-key", "ANTHROPIC_API_KEY": "ant-key"},
        path,
    )
    from_file = config.resolve_settings(values, {"OPENAI_API_KEY": "openai-key"}, path)

    assert preferred["API_KEY"] == "ant-key"
    assert preferred["API_URL"] == "https://api.anthropic.com"
    assert preferred["PROVIDER"] == "anthropic"
    assert preferred["MAX_TOKENS"] == 8192
    assert preferred["CONTEXT_LIMIT"] is None
    assert from_file["API_KEY"] == "file-key"


def test_provider_must_be_openai_or_anthropic():
    path = Path("demo.yaml")
    with pytest.raises(SystemExit, match="expected openai or anthropic"):
        values, _servers = config.parse_config("provider: claude\n", path)
        config.resolve_settings(values, {}, path)


def test_startup_names_the_anthropic_key(monkeypatch, tmp_path):
    conf = tmp_path / "harnh3x.yaml"
    monkeypatch.setenv("HARNH3X_CONFIG", str(conf))
    monkeypatch.setattr(config, "PROVIDER", "anthropic")
    monkeypatch.setattr(config, "API_KEY", None)
    monkeypatch.setattr(config, "API_URL", "https://api.anthropic.com")

    with pytest.raises(SystemExit, match="Missing api_key") as missing:
        config.require_api_settings()

    message = str(missing.value)
    assert "ANTHROPIC_API_KEY" in message
    assert "api_url" not in message


def test_get_client_builds_an_anthropic_client(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER", "anthropic")
    monkeypatch.setattr(config, "API_KEY", "sk-ant")
    monkeypatch.setattr(config, "API_URL", "https://api.anthropic.com")
    monkeypatch.setattr(agent_client, "_client", None)
    seen = {}

    def fake(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(anthropic_api, "Anthropic", fake)

    client = agent.get_client()

    assert isinstance(client, AnthropicClient)
    assert seen == {
        "api_key": "sk-ant",
        "base_url": "https://api.anthropic.com",
    }
    assert agent.get_client() is client


def test_status_message_reads_nested_and_top_level_text():
    import httpx2
    from anthropic import BadRequestError

    nested = _anthropic_rejected("credit\nbalance")
    assert anthropic_api.status_message(nested) == (
        "The API rejected the request: credit balance"
    )

    body = {"message": "  top\nlevel "}
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(400, json=body, request=request)
    exc = BadRequestError("Error code: 400", response=response, body=body)
    assert anthropic_api.status_message(exc) == (
        "The API rejected the request: top level"
    )
