import asyncio
import http.client
import importlib.util
import io
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import harnh3x.agent.client as agent_client
import harnh3x.agent.loop as agent_loop
import harnh3x.agent.transcript as agent_transcript
from harnh3x import (
    __version__,
    agent,
    anthropic_api,
    config,
    rules,
    sandbox,
    skills,
    tools,
)
from harnh3x.anthropic_api import AnthropicClient, to_anthropic
from harnh3x.config import (
    COMPACT_AT,
    MAX_TOOL_RESULT,
    MODEL,
    PDF_MAX_PAGES,
    SEARCH_FILES_MAX,
    TRANSCRIPT_FILE,
)
from harnh3x.prompt import YOLO_NOTE, project_note, system_prompt
from harnh3x.skills import SkillRegistry
from harnh3x.tools import _tool_result, execute_tool
from harnh3x.tools import system as host_system
from harnh3x.tui import (
    EXPORT_USAGE,
    NO_EXPORT,
    YOLO_PLAIN,
    AgentAnswer,
    CopyAnswer,
    DebugBox,
    ExportAnswer,
    ExportPathScreen,
    ExportScreen,
    HarnessApp,
    HarnessScreen,
    HelpScreen,
    ModelApp,
    ResumeOptions,
    ResumeScreen,
    ReviewApp,
    Thinking,
    ToolBox,
    WarningScreen,
    context_line,
    conversation_turns,
    export_base,
    exportable_answers,
    render_export,
    transcript_items,
    write_export,
)

ARTICLE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<title>City Council Approves Transit Plan</title>
<meta name="author" content="Ada Lovelace">
<meta property="article:published_time" content="2026-03-04">
</head>
<body>
<article>
<h1>City Council Approves Transit Plan</h1>
<p>The council voted Tuesday to fund a new bus line through the harbor district. Residents packed the chamber and spoke for two hours.</p>
</article>
</body>
</html>
"""


def test_path_escape_is_rejected(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")

    for name, args in (
        ("read_file", {"path": str(outside)}),
        ("read_file", {"path": "../secret.txt"}),
        ("write_file", {"path": "../pwned.txt", "content": "nope"}),
        ("write_file", {"path": str(tmp_path / "pwned.txt"), "content": "nope"}),
        ("delete_file", {"path": str(outside)}),
        ("delete_file", {"path": "../secret.txt"}),
    ):
        result = execute_tool(name, args, workspace)
        assert "outside workspace" in result

    assert outside.read_text() == "secret"
    assert not (tmp_path / "pwned.txt").exists()


def test_read_file_rejects_symlink_escape(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    (workspace / "escape.txt").symlink_to(outside)

    result = execute_tool("read_file", {"path": "escape.txt"}, workspace)

    assert "outside workspace" in result
    assert "secret" not in result


def test_denied_shell_command_is_not_executed(tmp_path):
    target = tmp_path / "poem.txt"
    target.write_text("keep")

    result = execute_tool("run_shell", {"command": "rm poem.txt"}, tmp_path)

    assert "blocked" in result
    assert target.read_text() == "keep"


def test_shell_success_with_empty_stdout(tmp_path):
    result = execute_tool("run_shell", {"command": "true"}, tmp_path)

    assert "status: success" in result
    assert "exit_code: 0" in result
    assert "stdout:\n\nstderr:\n" in result


def test_tool_result_truncation_keeps_a_suffix(tmp_path):
    (tmp_path / "big.txt").write_text("a" * (MAX_TOOL_RESULT + 500))

    result = execute_tool("read_file", {"path": "big.txt"}, tmp_path)

    assert len(result) <= MAX_TOOL_RESULT
    assert "chars omitted" in result
    assert result.endswith("]")


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


def _delta(**fields):
    usage = fields.pop("usage", None)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(**fields))],
        usage=usage,
    )


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


def _status_error(message: str):
    import httpx2
    from openai import BadRequestError

    response = httpx2.Response(
        400,
        request=httpx2.Request("POST", "http://127.0.0.1:9/v1/chat/completions"),
    )
    return BadRequestError(message, response=response, body=None)


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


def _unreachable(kind: str):
    import httpx2
    from openai import APIConnectionError, APITimeoutError

    error = APITimeoutError if kind == "timeout" else APIConnectionError
    return error(
        request=httpx2.Request("POST", "http://127.0.0.1:9/v1/chat/completions")
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


def test_fetch_url_rejects_blocked_targets(tmp_path):
    for url in ("file:///etc/passwd", "http://127.0.0.1/", "http://169.254.169.254/"):
        result = execute_tool("fetch_url", {"url": url}, tmp_path)
        assert result.startswith("Error:")
        assert "blocked" in result


def test_fetch_redirect_to_private_host_is_blocked():
    handler = tools.http._CheckedRedirectHandler()
    try:
        handler.redirect_request(
            None, None, 302, "Found", None, "http://127.0.0.1/secret"
        )
    except ValueError as exc:
        assert "blocked" in str(exc)
    else:
        raise AssertionError("redirect was allowed")


def test_fetch_url_saves_html_in_workspace(tmp_path, monkeypatch):
    body = b"<html><title>Hi</title><body><p>Hello</p></body></html>"

    def fake_get(url):
        return 200, "text/html", body

    monkeypatch.setattr(tools.http, "_http_get", fake_get)
    result = execute_tool(
        "fetch_url",
        {"url": "https://1.1.1.1/news", "path": "pages/story.html"},
        tmp_path,
    )

    assert "status: success" in result
    assert "path: pages/story.html" in result
    assert (tmp_path / "pages" / "story.html").read_bytes() == body

    outside = execute_tool(
        "fetch_url",
        {"url": "https://1.1.1.1/news", "path": "../secret.html"},
        tmp_path,
    )
    assert "outside workspace" in outside
    assert not (tmp_path.parent / "secret.html").exists()


def test_fetch_url_rejects_unsupported_types(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tools.http,
        "_http_get",
        lambda url: (200, "image/png", b"\x89PNG"),
    )

    result = execute_tool("fetch_url", {"url": "https://1.1.1.1/pic"}, tmp_path)

    assert result.startswith("Error:")
    assert "unsupported content type" in result
    assert list(tmp_path.iterdir()) == []


def test_fetch_url_saves_json_and_pdf_with_matching_extensions(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tools.http, "_http_get", lambda url: (200, "application/json", b'{"a": 1}')
    )
    as_json = execute_tool("fetch_url", {"url": "https://1.1.1.1/data"}, tmp_path)
    assert "content_type: application/json" in as_json
    assert "path: downloads/1.1.1.1-" in as_json
    assert as_json.rstrip().endswith(".json")

    monkeypatch.setattr(
        tools.http, "_http_get", lambda url: (200, "application/pdf", b"%PDF-1.4")
    )
    as_pdf = execute_tool("fetch_url", {"url": "https://1.1.1.1/paper"}, tmp_path)
    assert as_pdf.rstrip().endswith(".pdf")


def test_parse_html_reads_saved_html_and_does_not_download(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_get(url):
        calls["n"] += 1
        return 200, "text/html", ARTICLE_HTML.encode()

    monkeypatch.setattr(tools.http, "_http_get", fake_get)
    saved = execute_tool("fetch_url", {"url": "https://1.1.1.1/news"}, tmp_path)
    path_line = next(line for line in saved.splitlines() if line.startswith("path: "))
    relative = path_line.removeprefix("path: ")

    result = execute_tool("parse_html", {"path": relative}, tmp_path)

    assert calls["n"] == 1
    assert "title: City Council Approves Transit Plan" in result
    assert "harbor district" in result

    ignored = execute_tool(
        "parse_html",
        {
            "path": "https://thehackernews.com/2026/10/mi5-says-chinas-mss-funded-research.html",
            "url": "https://thehackernews.com/2026/10/mi5-says-chinas-mss-funded-research.html",
        },
        tmp_path,
    )
    assert calls["n"] == 1
    assert ignored.startswith("Error:")
    assert not (tmp_path / "fetch_url").exists()


def test_parse_html_extracts_article(tmp_path):
    (tmp_path / "article.html").write_text(ARTICLE_HTML)

    result = execute_tool("parse_html", {"path": "article.html"}, tmp_path)

    assert "title: City Council Approves Transit Plan" in result
    assert "harbor district" in result
    assert "author: Ada Lovelace" in result


def test_parse_html_errors_for_missing_directory_and_empty(tmp_path):
    assert "Error: not found" in execute_tool(
        "parse_html", {"path": "missing.html"}, tmp_path
    )

    (tmp_path / "pages").mkdir()
    assert "Error: is a directory" in execute_tool(
        "parse_html", {"path": "pages"}, tmp_path
    )

    (tmp_path / "empty.html").write_text("<html><head></head><body></body></html>")
    result = execute_tool("parse_html", {"path": "empty.html"}, tmp_path)
    assert result == "Error: no article text extracted"


def _search_page(rows):
    parts = ["<html><body>"]
    for title, href, snippet in rows:
        parts.append(
            "<div class='result'>"
            f"<a class='result__a' href='{href}'>{title}</a>"
            f"<a class='result__snippet'>{snippet}</a>"
            "</div>"
        )
    parts.append("</body></html>")
    return "".join(parts)


def _ddg_href(url):
    return "https://duckduckgo.com/l/?uddg=" + urllib.parse.quote(url, safe="")


def test_web_search_returns_unwrapped_results(tmp_path, monkeypatch):
    page = _search_page(
        [
            ("Alpha Title", _ddg_href("https://1.1.1.1/alpha"), "Alpha snippet"),
            (
                "Beta Title",
                "//duckduckgo.com/l/?uddg="
                + urllib.parse.quote("https://8.8.8.8/beta", safe=""),
                "Beta snippet",
            ),
            ("Secret", _ddg_href("http://127.0.0.1/secret"), "hidden"),
            ("Bad Scheme", "javascript:alert(1)", "nope"),
            ("Gamma Title", "https://9.9.9.9/gamma", "Gamma snippet"),
        ]
    )
    monkeypatch.setattr(tools.search, "_search_html", lambda query, recency=None: page)

    result = execute_tool(
        "web_search",
        {"query": "transit", "max_results": 2},
        tmp_path,
    )

    assert result == (
        "1. Alpha Title\n"
        "   url: https://1.1.1.1/alpha\n"
        "   snippet: Alpha snippet\n"
        "2. Beta Title\n"
        "   url: https://8.8.8.8/beta\n"
        "   snippet: Beta snippet\n"
        "\n" + tools.search.SEARCH_HINT
    )
    assert "127.0.0.1" not in result
    assert "javascript" not in result
    assert "Gamma" not in result


def test_web_search_caps_results_and_rejects_bad_input(tmp_path, monkeypatch):
    rows = [
        (f"Result {index}", f"https://1.1.1.1/{index}", f"snippet {index}")
        for index in range(1, 11)
    ]
    monkeypatch.setattr(
        tools.search, "_search_html", lambda query, recency=None: _search_page(rows)
    )

    capped = execute_tool(
        "web_search",
        {"query": "transit", "max_results": 100},
        tmp_path,
    )
    assert capped.startswith("1. Result 1\n")
    assert "8. Result 8\n" in capped
    assert "9. Result 9" not in capped

    calls = {"n": 0}

    def fail(query, recency=None):
        calls["n"] += 1
        raise AssertionError("search should not run")

    monkeypatch.setattr(tools.search, "_search_html", fail)
    assert execute_tool("web_search", {}, tmp_path) == "Error: query is required"
    assert execute_tool("web_search", {"query": "  "}, tmp_path) == (
        "Error: query is required"
    )
    assert calls["n"] == 0

    bad_limit = execute_tool(
        "web_search",
        {"query": "transit", "max_results": 0},
        tmp_path,
    )
    assert bad_limit == "Error: max_results must be a positive integer"
    assert calls["n"] == 0


def test_web_search_errors_when_nothing_parseable(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tools.search,
        "_search_html",
        lambda query, recency=None: _search_page(
            [("Secret", _ddg_href("http://127.0.0.1/secret"), "hidden")]
        ),
    )
    private_only = execute_tool("web_search", {"query": "transit"}, tmp_path)
    assert private_only == "Error: no results"

    monkeypatch.setattr(
        tools.search,
        "_search_html",
        lambda query, recency=None: "<html><body></body></html>",
    )
    empty = execute_tool("web_search", {"query": "transit"}, tmp_path)
    assert empty == "Error: no results"


def test_web_search_site_and_time_filters(tmp_path, monkeypatch):
    seen = []

    def fake_search(query, recency=None):
        seen.append((query, recency))
        return _search_page([("Alpha", "https://1.1.1.1/alpha", "snippet")])

    monkeypatch.setattr(tools.search, "_search_html", fake_search)

    result = execute_tool(
        "web_search",
        {"query": "asyncio", "site": "docs.python.org", "time": "w"},
        tmp_path,
    )
    assert "url: https://1.1.1.1/alpha" in result
    assert seen == [("asyncio site:docs.python.org", "w")]

    plain = execute_tool("web_search", {"query": "asyncio"}, tmp_path)
    assert "Alpha" in plain
    assert seen[-1] == ("asyncio", None)

    bad = execute_tool("web_search", {"query": "asyncio", "time": "hour"}, tmp_path)
    assert bad == "Error: time must be one of d, w, m, y"
    assert len(seen) == 2


def test_research_skill_gates_web_search(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tools.search,
        "_search_html",
        lambda query, recency=None: _search_page(
            [("Alpha Title", "https://1.1.1.1/alpha", "Alpha snippet")]
        ),
    )
    registry = SkillRegistry()

    blocked = registry.execute("web_search", {"query": "transit"}, tmp_path)
    assert blocked == "Error: Tool 'web_search' not found. Is the skill loaded?"

    loaded = registry.load_skill("research")
    assert loaded.startswith(
        "Loaded skill 'research' with 5 tools: "
        "web_search, read_url, fetch_url, parse_html, read_feed"
    )
    assert "Snippets are not answers" in loaded
    assert "[loaded: web_search, read_url, fetch_url, parse_html, read_feed]" in (
        registry.get_menu()
    )

    result = registry.execute("web_search", {"query": "transit"}, tmp_path)
    assert "url: https://1.1.1.1/alpha" in result

    assert registry.unload_skill("research") == "Unloaded skill 'research'."
    blocked_again = registry.execute("web_search", {"query": "transit"}, tmp_path)
    assert blocked_again == "Error: Tool 'web_search' not found. Is the skill loaded?"


def test_parse_html_truncates_long_article(tmp_path):
    paragraph = "The council voted to fund the harbor bus line. " * 400
    html = (
        "<html><head><title>Long Story</title></head><body><article>"
        f"<h1>Long Story</h1><p>{paragraph}</p></article></body></html>"
    )
    (tmp_path / "long.html").write_text(html)

    result = execute_tool("parse_html", {"path": "long.html"}, tmp_path)

    assert len(result) <= MAX_TOOL_RESULT
    assert result.endswith("chars omitted]")


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
    ]


def _write_transcript(workspace: Path, model: str, messages: list) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    payload = {"model": model, "messages": messages}
    (workspace / TRANSCRIPT_FILE).write_text(json.dumps(payload), encoding="utf-8")


def _past_workspace(root: Path) -> Path:
    workspace = root / "workspaces" / "abcdef01-0000-0000-0000-000000000001"
    _write_transcript(
        workspace,
        "saved-model",
        [
            {"role": "system", "content": "stale"},
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
            {"role": "assistant", "content": "### **Done**\n- notes updated"},
        ],
    )
    return workspace


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


def _prior_turn(session) -> None:
    session.messages.append({"role": "user", "content": "earlier"})
    session.messages.append({"role": "assistant", "content": "did the earlier work"})


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


FILE_OPS_LOADED = (
    "[loaded: read_file, write_file, edit_file, append_file, "
    "delete_file, move_file, list_files, search_files]"
)


def _tool_names(schemas):
    return [tool["function"]["name"] for tool in schemas]


def test_skill_menu_starts_unloaded():
    registry = SkillRegistry()
    menu = registry.get_menu()

    for name in (
        "file-ops",
        "shell",
        "python",
        "coding",
        "research",
        "reference",
        "http",
        "documents",
        "data",
        "memory",
        "tasks",
    ):
        assert f"- {name}:" in menu
    assert "web-testing:\n- sql-injection:" in menu
    assert menu.index("- http:") < menu.index("web-testing:")
    assert "news:" not in menu
    assert "- search:" not in menu
    assert "[loaded" not in menu
    assert _tool_names(registry.schemas()) == ["load_skill", "unload_skill"]


def test_skill_load_unload_and_gated_execute(tmp_path):
    registry = SkillRegistry()

    unknown = registry.execute("load_skill", {"name": "nope"}, tmp_path)
    assert unknown == "Error: Unknown skill 'nope'. Check the skill menu."

    blocked = registry.execute("read_file", {"path": "a.txt"}, tmp_path)
    assert blocked == "Error: Tool 'read_file' not found. Is the skill loaded?"

    loaded = registry.load_skill("file-ops")
    assert loaded.startswith("Loaded skill 'file-ops' with 8 tools: ")
    assert "read_file" in loaded
    assert "Documentation:" in loaded
    assert "give the user the content of the file" in loaded
    assert registry.load_skill("file-ops") == (
        "Skill 'file-ops' is already loaded. Call one of: read_file, write_file, "
        "edit_file, append_file, delete_file, move_file, list_files, search_files."
    )
    assert FILE_OPS_LOADED in registry.get_menu()

    (tmp_path / "a.txt").write_text("hi")
    assert registry.execute("read_file", {"path": "a.txt"}, tmp_path) == "hi"

    assert registry.unload_skill("file-ops") == "Unloaded skill 'file-ops'."
    assert registry.unload_skill("file-ops") == "Skill 'file-ops' is not loaded."
    assert "[loaded]" not in registry.get_menu()

    registry.load_skill("http")
    mistook = registry.execute(
        "http",
        {"http_get_json": "https://1.1.1.1/api"},
        tmp_path,
    )
    assert mistook.startswith(
        "Error: 'http' is a skill, not a tool. It is already loaded."
    )
    assert "Call one of: http_get_json, http_request." in mistook
    assert "Call http_get_json directly, with its own parameters." in mistook
    unloaded = SkillRegistry().execute("http", {}, tmp_path)
    assert "Call load_skill with name 'http'" in unloaded


def test_skill_manifest_requires_tools_entries():
    with pytest.raises(RuntimeError, match="missing from TOOLS"):
        skills._tool_schemas(("read_file", "not_a_tool"), tools.TOOLS)


def _write_skill(
    root: Path,
    *parts: str,
    description: str = "Do the thing.",
    body: str = "Do the thing.\n",
    tools: tuple[str, ...] = ("http_request",),
    name: str | None = None,
    frontmatter: str | None = None,
) -> None:
    folder = root.joinpath(*parts)
    folder.mkdir(parents=True)
    if frontmatter is None:
        skill_name = parts[-1] if name is None else name
        allowed = ""
        if tools:
            allowed = f"allowed-tools: {' '.join(tools)}\n"
        frontmatter = (
            f"---\nname: {skill_name}\ndescription: {description}\n{allowed}---\n"
        )
    elif not frontmatter.endswith("\n"):
        frontmatter += "\n"
    (folder / "SKILL.md").write_text(f"{frontmatter}\n{body}")


def test_catalog_loads_skills_in_category_folders(tmp_path):
    root = tmp_path / "skills"
    _write_skill(root, "http", description="Send requests.", body="Send requests.\n")
    _write_skill(
        root,
        "web-testing",
        "sql-injection",
        description="Test endpoints.",
        body="Ignore this sentence.\n",
    )
    _write_skill(root, "web-testing", "owasp", "probe", body="Probe a page.\n")
    (root / "web-testing" / "notes.txt").write_text("not a skill")
    catalog = skills._load_catalog(root, tools=tools.TOOLS)

    assert list(catalog) == ["http", "sql-injection", "probe"]
    assert "web-testing" not in catalog
    assert catalog["http"].category == ""
    assert catalog["sql-injection"].category == "web-testing"
    assert catalog["probe"].category == "web-testing/owasp"
    assert catalog["sql-injection"].description == "Test endpoints."
    assert catalog["sql-injection"].doc == "Ignore this sentence."


def test_catalog_rejects_duplicate_skill_names(tmp_path):
    root = tmp_path / "skills"
    _write_skill(root, "other", "probe")
    _write_skill(root, "web-testing", "probe")

    with pytest.raises(
        RuntimeError,
        match=("duplicate skill 'probe' in other/probe and web-testing/probe"),
    ):
        skills._load_catalog(root, tools=tools.TOOLS)


def test_catalog_skill_without_allowed_tools_has_no_tools(tmp_path):
    root = tmp_path / "skills"
    _write_skill(root, "notes", tools=())

    catalog = skills._load_catalog(root, tools=tools.TOOLS)

    assert catalog["notes"].tools == ()
    assert catalog["notes"].description == "Do the thing."


def test_catalog_rejects_a_skill_without_frontmatter(tmp_path):
    root = tmp_path / "skills"
    _write_skill(root, "http", frontmatter="# HTTP\n\nSend requests.")

    with pytest.raises(RuntimeError, match="skill 'http' has no frontmatter"):
        skills._load_catalog(root, tools=tools.TOOLS)


def test_catalog_rejects_a_name_that_does_not_match_the_directory(tmp_path):
    root = tmp_path / "skills"
    _write_skill(root, "http", name="shell")

    with pytest.raises(
        RuntimeError, match="skill name 'shell' does not match directory 'http'"
    ):
        skills._load_catalog(root, tools=tools.TOOLS)


def test_catalog_rejects_an_invalid_skill_name(tmp_path):
    root = tmp_path / "skills"
    _write_skill(root, "http", name="HTTP")

    with pytest.raises(RuntimeError, match="skill 'http' has an invalid name"):
        skills._load_catalog(root, tools=tools.TOOLS)


def test_catalog_rejects_a_bad_description(tmp_path):
    root = tmp_path / "skills"
    _write_skill(root, "http", description="x" * 1025)

    with pytest.raises(RuntimeError, match="skill 'http' description is too long"):
        skills._load_catalog(root, tools=tools.TOOLS)


def test_catalog_rejects_an_unknown_frontmatter_key(tmp_path):
    root = tmp_path / "skills"
    _write_skill(
        root,
        "http",
        frontmatter="---\nname: http\ndescription: Send.\ntools: http_request\n---\n",
    )

    with pytest.raises(RuntimeError, match="unknown frontmatter key: tools"):
        skills._load_catalog(root, tools=tools.TOOLS)


def test_catalog_rejects_a_bad_metadata_value(tmp_path):
    root = tmp_path / "skills"
    _write_skill(
        root,
        "http",
        frontmatter=(
            "---\nname: http\ndescription: Send.\nmetadata:\n  author: 1\n---\n"
        ),
    )

    with pytest.raises(RuntimeError, match="metadata values must be strings"):
        skills._load_catalog(root, tools=tools.TOOLS)


def test_catalog_accepts_optional_frontmatter_fields(tmp_path):
    root = tmp_path / "skills"
    _write_skill(
        root,
        "http",
        description="Send requests.",
        body="Call the endpoint.\n",
        frontmatter=(
            "---\n"
            "name: http\n"
            "description: Send requests.\n"
            "license: Apache-2.0\n"
            "compatibility: Requires network access\n"
            "metadata:\n"
            '  author: "example"\n'
            "allowed-tools: http_request\n"
            "---\n"
        ),
    )

    catalog = skills._load_catalog(root, tools=tools.TOOLS)

    assert catalog["http"].description == "Send requests."
    assert catalog["http"].doc == "Call the endpoint."
    assert _tool_names(catalog["http"].tools) == ["http_request"]


def test_catalog_rejects_an_unknown_allowed_tool(tmp_path):
    root = tmp_path / "skills"
    _write_skill(root, "http", tools=("not_a_tool",))

    with pytest.raises(RuntimeError, match="tool 'not_a_tool' is missing from TOOLS"):
        skills._load_catalog(root, tools=tools.TOOLS)


def test_shipped_skills_fit_the_token_budget():
    over = []
    for path in skills._skill_files(skills.SKILLS_DIR):
        location = path.parent.relative_to(skills.SKILLS_DIR).as_posix()
        text = path.read_text(encoding="utf-8")
        raw, body = skills._skill_sections(text, location)
        front_tokens = skills._token_count(raw)
        body_tokens = skills._token_count(body)
        if (
            front_tokens > skills._METADATA_TOKEN_LIMIT
            or body_tokens >= skills._BODY_TOKEN_LIMIT
        ):
            over.append(f"{location}: frontmatter {front_tokens}, body {body_tokens}")
    assert over == []


def test_load_skill_returns_a_freeform_body(tmp_path):
    root = tmp_path / "skills"
    _write_skill(root, "http", description="Send requests.", body="Just do the work.\n")
    catalog = skills._load_catalog(root, tools=tools.TOOLS)
    registry = SkillRegistry()
    registry._catalog["http"] = catalog["http"]

    loaded = registry.load_skill("http")

    assert "Just do the work." in loaded
    assert "name: http" not in loaded
    assert "## When to Use" not in loaded
    assert registry.get_menu().split("- http: ", 1)[1].startswith("Send requests.")


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
    assert _tool_names(seen[0]) == ["load_skill", "unload_skill"]
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


def _load_cli():
    path = Path(__file__).resolve().parents[1] / "src" / "harnh3x" / "cli.py"
    spec = importlib.util.spec_from_file_location("harnh3x_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_version_flag_prints_the_package_version(capsys):
    cli = _load_cli()

    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])

    assert exc.value.code == 0
    assert capsys.readouterr().out == f"harnh3x {cli.__version__}\n"


def test_query_argument_runs_once(monkeypatch, capsys, tmp_path):
    cli = _load_cli()
    calls = []
    monkeypatch.setattr(
        cli, "Session", lambda **kwargs: SimpleNamespace(workspace=tmp_path, **kwargs)
    )

    def fake_run(message, session, show_tools=True):
        calls.append((message, session.workspace, show_tools))
        return "final answer"

    monkeypatch.setattr(cli, "run", fake_run)

    cli.main(["fix the notes"])

    assert calls == [("fix the notes", tmp_path, True)]
    output = capsys.readouterr().out
    assert f"Workspace: {tmp_path}" in output
    assert f"Model: {MODEL}" in output
    assert "Agent: final answer" in output


def test_yolo_option_prints_the_warning_and_selects_the_session(
    monkeypatch, capsys, tmp_path
):
    cli = _load_cli()
    seen = {}
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(
        cli,
        "Session",
        lambda **kwargs: (
            seen.update(kwargs) or SimpleNamespace(workspace=tmp_path, **kwargs)
        ),
    )
    monkeypatch.setattr(cli, "run", lambda *args, **kwargs: "final answer")

    cli.main(["--yolo", "fix the notes"])

    assert seen["yolo"] is True
    output = capsys.readouterr().out
    assert output.index(YOLO_PLAIN) < output.index(f"Workspace: {tmp_path}")
    assert "Agent: final answer" in output


def test_project_option_prints_the_grant_and_rejects_a_missing_directory(
    monkeypatch, capsys, tmp_path
):
    cli = _load_cli()
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(cli, "run", lambda *args, **kwargs: "final answer")

    cli.main(["--project", str(project), "fix the notes"])

    output = capsys.readouterr().out
    assert f"Project: {project.resolve()}" in output
    assert "Agent: final answer" in output

    with pytest.raises(SystemExit) as exc:
        cli.main(["--project", str(tmp_path / "missing"), "fix the notes"])
    assert "existing directory" in str(exc.value)


def test_coding_option_requires_a_project_and_prints_the_mode(
    monkeypatch, capsys, tmp_path
):
    cli = _load_cli()
    with pytest.raises(SystemExit) as missing:
        cli.main(["--coding", "fix the notes"])
    assert str(missing.value) == "--coding requires --project"

    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(cli, "run", lambda *args, **kwargs: "final answer")

    cli.main(["--coding", "--project", str(project), "fix the notes"])

    output = capsys.readouterr().out
    project_line = f"Project: {project.resolve()}"
    assert project_line in output
    assert output.index(project_line) < output.index("\nCoding\n")
    assert "Agent: final answer" in output


def test_debug_option_prints_the_request_and_hides_it_for_answer_only(
    monkeypatch, capsys, tmp_path
):
    cli = _load_cli()
    seen = {}
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(cli, "terminal_width", lambda: 80)
    monkeypatch.setattr(
        cli,
        "Session",
        lambda **kwargs: (
            seen.update(kwargs)
            or SimpleNamespace(workspace=tmp_path, messages=[], **kwargs)
        ),
    )

    def fake_run(message, session, show_tools=True):
        session.on_debug(
            "\n".join(f"system-{index}" for index in range(6)),
            f"user: {message}",
        )
        return "final answer"

    monkeypatch.setattr(cli, "run", fake_run)

    cli.main(["--debug", "fix the notes"])

    assert seen["debug"] is True
    output = capsys.readouterr().out
    assert "\nDebug\n" in output
    assert output.index("\nDebug\n") < output.index("\nModel:")
    assert "\x1b[36m" in output
    assert "System" in output
    assert "Request" in output
    assert "system-0" not in output
    assert "system-5" in output
    assert "user: fix the notes" in output
    assert "Agent: final answer" in output

    seen.clear()
    monkeypatch.setattr(cli, "run", lambda *args, **kwargs: "final answer")
    cli.main(["--debug", "--answer-only", "fix the notes"])

    assert seen["debug"] is False
    assert capsys.readouterr().out == "final answer\n"


def test_answer_only_hides_the_yolo_warning(monkeypatch, capsys, tmp_path):
    cli = _load_cli()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(
        cli,
        "Session",
        lambda **kwargs: SimpleNamespace(workspace=tmp_path, **kwargs),
    )
    monkeypatch.setattr(cli, "run", lambda *args, **kwargs: "final answer")

    cli.main(["--yolo", "--answer-only", "fix the notes"])

    assert capsys.readouterr().out == "final answer\n"


def test_answer_only_prints_the_final_answer(monkeypatch, capsys, tmp_path):
    cli = _load_cli()
    calls = []
    monkeypatch.setattr(
        cli, "Session", lambda **kwargs: SimpleNamespace(workspace=tmp_path, **kwargs)
    )

    def fake_run(message, session, show_tools=True):
        calls.append((message, show_tools))
        return "final answer"

    monkeypatch.setattr(cli, "run", fake_run)

    cli.main(["--answer-only", "fix the notes"])

    assert calls == [("fix the notes", False)]
    assert capsys.readouterr().out == "final answer\n"


def test_plain_resume_reprints_the_session(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WORKSPACE", raising=False)
    cli = _load_cli()
    monkeypatch.setattr(cli, "_tty", lambda: False)
    past = _past_workspace(tmp_path)
    answers = iter(["/nope", "/resume", "1", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    cli.main([])

    output = capsys.readouterr().out
    assert "Unknown command: /nope" in output
    assert "You: fix the notes" in output
    assert "🔧 load_skill({'name': 'file-ops'})" in output
    assert "### **Done**" in output
    assert "alpha" not in output
    assert "delta" in output
    assert f"Workspace: {past.resolve()}" in output
    assert "saved-model" in output


def test_plain_help_lists_commands(monkeypatch, capsys, tmp_path):
    cli = _load_cli()
    monkeypatch.setattr(cli, "_tty", lambda: False)
    monkeypatch.setattr(
        cli,
        "Session",
        lambda **kwargs: SimpleNamespace(workspace=tmp_path, model="m", messages=[]),
    )
    answers = iter(["/help", "/help extra", "/exit extra", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    cli.main([])

    output = capsys.readouterr().out
    assert "🤖 Harnh3x\n" in output
    assert "Shift+Enter" not in output
    assert "Ctrl+Q" not in output
    assert "/resume" in output
    assert "/compact" in output
    assert "/export" in output
    assert "/exit" in output
    assert "Usage: /help" in output
    assert "Usage: /exit" in output


def test_thinking_panel_keeps_the_tail_until_expanded():
    cli = _load_cli()
    reasoning = "alpha\nbeta\ngamma\ndelta"
    collapsed = cli.format_thinking(reasoning, expanded=False, width=80, hold=False)
    assert "alpha" not in collapsed
    assert "beta" in collapsed
    assert "delta" in collapsed
    assert "ctrl+t" not in collapsed
    assert "Thinking" in collapsed

    expanded = cli.format_thinking(reasoning, expanded=True, width=80, hold=True)
    assert expanded.index("alpha") < expanded.index("delta")
    assert "ctrl+t" not in expanded
    assert "enter to close" in expanded

    text = "".join(f"{digit}" * 10 for digit in "1234")
    tail = cli.format_thinking(text, expanded=False, width=12, hold=False)
    assert "1" * 10 not in tail
    assert "4444" in tail
    plain = re.sub(r"\x1b\[[0-9;]*m", "", tail)
    for line in plain.split("\n"):
        assert len(line) < 12


def test_reply_lines_stay_short_of_the_terminal():
    cli = _load_cli()
    text = cli.format_reply(
        "a" * 80,
        "r" * 100,
        answer_only=False,
        hold=False,
        width=20,
        expanded=True,
    )
    plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
    for line in plain.split("\n"):
        assert len(line) < 20


def test_query_shows_a_collapsed_thinking_panel(monkeypatch, capsys, tmp_path):
    cli = _load_cli()
    messages = []
    monkeypatch.setattr(
        cli,
        "Session",
        lambda **kwargs: SimpleNamespace(
            workspace=tmp_path, messages=messages, **kwargs
        ),
    )
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(cli, "terminal_width", lambda: 80)

    def fake_run(message, session, show_tools=True):
        session.messages.append({"role": "user", "content": message})
        session.messages.append(
            {
                "role": "assistant",
                "content": "final answer",
                "reasoning": "alpha\nbeta\ngamma\ndelta",
            }
        )
        return "final answer"

    monkeypatch.setattr(cli, "run", fake_run)

    cli.main(["fix the notes"])

    output = capsys.readouterr().out
    assert "alpha" not in output
    assert "delta" in output
    assert "ctrl+t" not in output
    assert "Agent: final answer" in output


def test_plain_stdout_streams_tokens_before_the_turn_returns(
    monkeypatch, capsys, tmp_path
):
    cli = _load_cli()
    monkeypatch.setattr(cli, "_tty", lambda: False)
    monkeypatch.setattr(
        agent_client, "model_context_limit", lambda model, client=None: None
    )
    monkeypatch.setattr(
        cli,
        "Session",
        lambda **kwargs: agent.Session(workspace=tmp_path, **kwargs),
    )
    mid = {}

    def create(**kwargs):
        def chunks():
            yield _delta(reasoning="alpha ")
            mid["out"] = capsys.readouterr().out
            yield _delta(content="done")

        return chunks()

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    monkeypatch.setattr(agent_client, "get_client", lambda: client)

    cli.main(["fix the notes"])

    assert "alpha" in mid["out"]
    assert "done" not in mid["out"]
    output = capsys.readouterr().out
    assert "Agent: done" in output
    assert "ctrl+t" not in output


def test_answer_only_hides_the_thinking_panel(monkeypatch, capsys, tmp_path):
    cli = _load_cli()
    messages = []
    monkeypatch.setattr(
        cli,
        "Session",
        lambda **kwargs: SimpleNamespace(
            workspace=tmp_path, messages=messages, **kwargs
        ),
    )

    def fake_run(message, session, show_tools=True):
        session.messages.append(
            {
                "role": "assistant",
                "content": "final answer",
                "reasoning": "alpha\nbeta\ngamma\ndelta",
            }
        )
        return "final answer"

    monkeypatch.setattr(cli, "run", fake_run)

    cli.main(["--answer-only", "fix the notes"])

    assert capsys.readouterr().out == "final answer\n"


def test_reasoning_used_as_the_answer_is_not_repeated(monkeypatch, capsys, tmp_path):
    cli = _load_cli()
    messages = []
    monkeypatch.setattr(
        cli,
        "Session",
        lambda **kwargs: SimpleNamespace(
            workspace=tmp_path, messages=messages, **kwargs
        ),
    )

    def fake_run(message, session, show_tools=True):
        session.messages.append(
            {"role": "assistant", "content": "", "reasoning": "only thought"}
        )
        return "only thought"

    monkeypatch.setattr(cli, "run", fake_run)

    cli.main(["fix the notes"])

    output = capsys.readouterr().out
    assert "ctrl+t" not in output
    assert "Agent: only thought" in output


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


def test_list_models_returns_sorted_ids():
    models = [
        SimpleNamespace(id="zeta"),
        SimpleNamespace(id="alpha"),
        SimpleNamespace(id="alpha"),
        SimpleNamespace(id=""),
    ]
    client = SimpleNamespace(models=SimpleNamespace(list=lambda: models))

    assert agent.list_models(client) == ["alpha", "zeta"]


def test_list_models_exits_when_the_api_is_unreachable():
    def listing():
        raise _unreachable("connection")

    client = SimpleNamespace(models=SimpleNamespace(list=listing))

    with pytest.raises(SystemExit, match="Could not reach the API"):
        agent.list_models(client)


def test_model_option_selects_that_model(monkeypatch, capsys, tmp_path):
    cli = _load_cli()
    seen = {}
    monkeypatch.setattr(
        cli,
        "Session",
        lambda **kwargs: (
            seen.update(kwargs) or SimpleNamespace(workspace=tmp_path, **kwargs)
        ),
    )
    monkeypatch.setattr(cli, "run", lambda *args, **kwargs: "final answer")

    cli.main(["--model", "gemma4:12b", "fix the notes"])

    assert seen["model"] == "gemma4:12b"
    assert "Model: gemma4:12b" in capsys.readouterr().out


def test_model_flag_without_a_name_prompts(monkeypatch, capsys, tmp_path):
    cli = _load_cli()
    seen = {}
    answers = iter(["0", "nope", "2"])
    monkeypatch.setattr(cli, "list_models", lambda: ["alpha", "beta"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(
        cli,
        "Session",
        lambda **kwargs: (
            seen.update(kwargs) or SimpleNamespace(workspace=tmp_path, **kwargs)
        ),
    )
    monkeypatch.setattr(cli, "run", lambda *args, **kwargs: "final answer")

    cli.main(["fix the notes", "--model"])

    assert seen["model"] == "beta"
    output = capsys.readouterr().out
    assert "1. alpha" in output
    assert "2. beta" in output
    assert "Enter a number from 1 to 2." in output
    assert "Model: beta" in output


def test_model_menu_exits_when_the_api_has_no_models(monkeypatch):
    cli = _load_cli()
    monkeypatch.setattr(cli, "list_models", list)

    with pytest.raises(SystemExit, match="No models available"):
        cli.main(["--model"])


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
    assert context_line(1234, 8192) == "Context: 1,234 / 8,192"
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


def _export_messages() -> list[dict]:
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first question"},
        {
            "role": "assistant",
            "content": "draft",
            "reasoning": "hidden thought",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "1", "content": "tool body"},
        {
            "role": "assistant",
            "content": "final alpha",
            "reasoning": "more thought",
        },
        {"role": "user", "content": "second question"},
        {
            "role": "assistant",
            "content": "beta answer",
            "reasoning": "secret thinking",
        },
    ]


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

    monkeypatch.setattr("harnh3x.tui.compact_session", fake_compact)
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


def _plain_export_session(tmp_path, messages):
    return SimpleNamespace(
        workspace=tmp_path,
        model="m",
        messages=messages,
        project=None,
    )


def test_plain_export_writes_the_only_answer(monkeypatch, capsys, tmp_path):
    cli = _load_cli()
    monkeypatch.setattr(cli, "_tty", lambda: False)
    monkeypatch.chdir(tmp_path)
    session = _plain_export_session(
        tmp_path,
        [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "only answer",
                "reasoning": "think",
                "tool_calls": [],
            },
        ],
    )
    monkeypatch.setattr(cli, "Session", lambda **kwargs: session)
    answers = iter(["/export out.md", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    cli.main([])

    output = capsys.readouterr().out
    assert f"Exported to {(tmp_path / 'out.md').resolve()}" in output
    assert (tmp_path / "out.md").read_text(encoding="utf-8") == "only answer\n"
    assert "think" not in (tmp_path / "out.md").read_text(encoding="utf-8")


def test_plain_export_can_pick_one_or_all(monkeypatch, capsys, tmp_path):
    cli = _load_cli()
    monkeypatch.setattr(cli, "_tty", lambda: False)
    monkeypatch.chdir(tmp_path)
    session = _plain_export_session(tmp_path, _export_messages())
    monkeypatch.setattr(cli, "Session", lambda **kwargs: session)
    answers = iter(["/export picked.md", "2", "/export all.md", "all", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    cli.main([])

    picked = (tmp_path / "picked.md").read_text(encoding="utf-8")
    assert picked == "beta answer\n"
    assert "final alpha" not in picked
    assert "secret thinking" not in picked
    assert "draft" not in picked
    everything = (tmp_path / "all.md").read_text(encoding="utf-8")
    assert everything == "final alpha\n\n---\n\nbeta answer\n"
    assert "tool body" not in everything
    assert capsys.readouterr().out.count("Exported to") == 2


def test_plain_export_cancel_and_usage(monkeypatch, capsys, tmp_path):
    cli = _load_cli()
    monkeypatch.setattr(cli, "_tty", lambda: False)
    monkeypatch.chdir(tmp_path)
    session = _plain_export_session(tmp_path, _export_messages())
    monkeypatch.setattr(cli, "Session", lambda **kwargs: session)
    steps = iter(["/export", "/export missing.md", "", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(steps))

    cli.main([])

    output = capsys.readouterr().out
    assert EXPORT_USAGE in output
    assert not (tmp_path / "missing.md").exists()

    empty = _plain_export_session(tmp_path, [])
    monkeypatch.setattr(cli, "Session", lambda **kwargs: empty)
    empty_steps = iter(["/export nowhere.md", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(empty_steps))

    cli.main([])

    output = capsys.readouterr().out
    assert NO_EXPORT in output
    assert not (tmp_path / "nowhere.md").exists()


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


# --- file-ops additions ---------------------------------------------------


def test_read_file_pages_with_offset_and_limit(tmp_path):
    (tmp_path / "lines.txt").write_text("".join(f"line {n}\n" for n in range(1, 11)))

    page = execute_tool(
        "read_file", {"path": "lines.txt", "offset": 3, "limit": 2}, tmp_path
    )
    assert (
        page
        == "line 3\nline 4\n\n...[showing lines 3-4 of 10; call read_file with offset=5 to continue]"
    )

    tail = execute_tool("read_file", {"path": "lines.txt", "offset": 9}, tmp_path)
    assert tail == "line 9\nline 10\n"

    assert execute_tool("read_file", {"path": "lines.txt", "offset": 11}, tmp_path) == (
        "Error: offset 11 is past the last line (10)"
    )
    assert execute_tool("read_file", {"path": "lines.txt", "offset": 0}, tmp_path) == (
        "Error: offset must be a positive integer"
    )


def test_read_file_long_file_cuts_on_a_line_and_names_the_next_offset(tmp_path):
    line = "x" * 100 + "\n"
    (tmp_path / "big.txt").write_text(line * 200)

    result = execute_tool("read_file", {"path": "big.txt"}, tmp_path)

    assert len(result) <= MAX_TOOL_RESULT
    match = re.search(
        r"showing lines 1-(\d+) of 200; call read_file with offset=(\d+)", result
    )
    assert match
    shown = int(match.group(1))
    assert int(match.group(2)) == shown + 1
    assert result.startswith(line * shown)


def test_edit_file_replaces_exactly_one_match(tmp_path):
    (tmp_path / "notes.md").write_text("alpha beta alpha\n")

    ambiguous = execute_tool(
        "edit_file",
        {"path": "notes.md", "old_text": "alpha", "new_text": "gamma"},
        tmp_path,
    )
    assert ambiguous.startswith("Error: old_text matches 2 places")
    assert (tmp_path / "notes.md").read_text() == "alpha beta alpha\n"

    one = execute_tool(
        "edit_file",
        {"path": "notes.md", "old_text": "beta", "new_text": "delta"},
        tmp_path,
    )
    assert one == "Replaced 1 occurrence(s) in notes.md"
    assert (tmp_path / "notes.md").read_text() == "alpha delta alpha\n"

    everything = execute_tool(
        "edit_file",
        {"path": "notes.md", "old_text": "alpha", "new_text": "z", "replace_all": True},
        tmp_path,
    )
    assert everything == "Replaced 2 occurrence(s) in notes.md"
    assert (tmp_path / "notes.md").read_text() == "z delta z\n"

    missing = execute_tool(
        "edit_file",
        {"path": "notes.md", "old_text": "nope", "new_text": "x"},
        tmp_path,
    )
    assert missing == "Error: old_text not found in notes.md"
    assert "outside workspace" in execute_tool(
        "edit_file",
        {"path": "../notes.md", "old_text": "a", "new_text": "b"},
        tmp_path,
    )
    assert "Error: not found" == execute_tool(
        "edit_file",
        {"path": "missing.md", "old_text": "a", "new_text": "b"},
        tmp_path,
    )


def test_append_file_creates_and_extends(tmp_path):
    first = execute_tool(
        "append_file", {"path": "log/out.txt", "content": "one\n"}, tmp_path
    )
    second = execute_tool(
        "append_file", {"path": "log/out.txt", "content": "two\n"}, tmp_path
    )

    assert first.startswith("Appended 4 bytes")
    assert second.startswith("Appended 4 bytes")
    assert (tmp_path / "log" / "out.txt").read_text() == "one\ntwo\n"
    assert "outside workspace" in execute_tool(
        "append_file", {"path": "../x.txt", "content": "no"}, tmp_path
    )
    assert execute_tool("append_file", {"path": "a.txt"}, tmp_path) == (
        "Error: content is required"
    )


def test_move_file_renames_without_overwriting(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "taken.txt").write_text("taken")
    (tmp_path / "dir").mkdir()

    moved = execute_tool(
        "move_file", {"source": "a.txt", "destination": "sub/b.txt"}, tmp_path
    )
    assert moved == "Moved a.txt to sub/b.txt"
    assert not (tmp_path / "a.txt").exists()
    assert (tmp_path / "sub" / "b.txt").read_text() == "a"

    assert (
        execute_tool(
            "move_file", {"source": "sub/b.txt", "destination": "taken.txt"}, tmp_path
        )
        == "Error: destination exists"
    )
    assert (tmp_path / "taken.txt").read_text() == "taken"
    assert (
        execute_tool("move_file", {"source": "dir", "destination": "dir2"}, tmp_path)
        == "Error: is a directory"
    )
    assert (
        execute_tool("move_file", {"source": "missing", "destination": "x"}, tmp_path)
        == "Error: not found"
    )
    outside = execute_tool(
        "move_file", {"source": "sub/b.txt", "destination": "../escaped.txt"}, tmp_path
    )
    assert "outside workspace" in outside
    assert (tmp_path / "sub" / "b.txt").exists()


def test_list_files_shows_tree_with_sizes_and_filters(tmp_path):
    (tmp_path / "a.md").write_text("hello")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print(1)\n")
    (tmp_path / "src" / "deep").mkdir()
    (tmp_path / "src" / "deep" / "x.md").write_text("x")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref")

    listing = execute_tool("list_files", {}, tmp_path)
    assert "src/" in listing
    assert "a.md (5 bytes)" in listing
    assert "src/main.py (9 bytes)" in listing
    assert "src/deep/x.md (1 bytes)" in listing
    assert "HEAD" not in listing

    only_md = execute_tool("list_files", {"pattern": "*.md"}, tmp_path)
    assert "a.md" in only_md
    assert "x.md" in only_md
    assert "main.py" not in only_md
    assert "src/\n" not in only_md and not only_md.endswith("src/")

    shallow = execute_tool("list_files", {"max_depth": 1}, tmp_path)
    assert "src/" in shallow
    assert "main.py" not in shallow

    assert execute_tool("list_files", {"path": "a.md"}, tmp_path) == (
        "Error: not a directory"
    )
    assert execute_tool("list_files", {"path": "nope"}, tmp_path) == "Error: not found"
    assert "outside workspace" in execute_tool("list_files", {"path": ".."}, tmp_path)
    (tmp_path / "empty").mkdir()
    assert execute_tool("list_files", {"path": "empty"}, tmp_path) == "No files found."


def test_search_files_reports_path_line_and_text(tmp_path):
    (tmp_path / "a.py").write_text("import os\nTODO: fix\n")
    (tmp_path / "b.txt").write_text("todo later\n")
    (tmp_path / "bin.dat").write_bytes(b"\xff\xfeTODO")

    result = execute_tool("search_files", {"pattern": "TODO"}, tmp_path)
    assert result == "a.py:2: TODO: fix"

    loose = execute_tool(
        "search_files", {"pattern": "todo", "ignore_case": True}, tmp_path
    )
    assert "a.py:2: TODO: fix" in loose
    assert "b.txt:1: todo later" in loose

    globbed = execute_tool(
        "search_files",
        {"pattern": "todo", "ignore_case": True, "glob": "*.txt"},
        tmp_path,
    )
    assert globbed == "b.txt:1: todo later"

    assert execute_tool("search_files", {"pattern": "zzz"}, tmp_path) == "No matches."
    assert execute_tool("search_files", {"pattern": "("}, tmp_path).startswith(
        "Error: invalid pattern"
    )
    assert execute_tool("search_files", {}, tmp_path) == "Error: pattern is required"
    assert "outside workspace" in execute_tool(
        "search_files", {"pattern": "x", "path": ".."}, tmp_path
    )


def test_search_files_stops_at_the_match_cap(tmp_path):
    (tmp_path / "many.txt").write_text("hit\n" * (SEARCH_FILES_MAX + 20))

    result = execute_tool("search_files", {"pattern": "hit"}, tmp_path)

    assert result.count("many.txt:") == SEARCH_FILES_MAX
    assert f"stopped at {SEARCH_FILES_MAX} matches" in result


# --- shell and python -----------------------------------------------------


def test_shell_accepts_stdin_and_timeout(tmp_path):
    result = execute_tool(
        "run_shell", {"command": "cat", "stdin": "from stdin", "timeout": 5}, tmp_path
    )
    assert "exit_code: 0" in result
    assert "stdout:\nfrom stdin\n" in result

    assert (
        execute_tool("run_shell", {"command": "true", "timeout": 0}, tmp_path)
        == "Error: timeout must be a positive integer"
    )
    assert (
        execute_tool("run_shell", {"command": "true", "stdin": 5}, tmp_path)
        == "Error: stdin must be a string"
    )


def test_abort_kills_a_running_shell_child(tmp_path):
    finished = threading.Event()
    holder = {}

    def work():
        holder["result"] = tools.process._run_process(
            ["sleep", "30"], tmp_path, 30, None
        )
        finished.set()

    thread = threading.Thread(target=work)
    thread.start()
    try:
        for _ in range(100):
            if tools.process._children:
                break
            threading.Event().wait(0.05)
        else:
            raise AssertionError("shell child did not start")
        tools.process.kill_children()
        assert finished.wait(timeout=2)
        assert "exit_code: 0" not in holder["result"]
        assert "exit_code:" in holder["result"]
    finally:
        tools.process.kill_children()
        thread.join(timeout=2)


def test_shell_timeout_is_capped(tmp_path, monkeypatch):
    seen = {}

    class Finished:
        pid = 0
        returncode = 0

        def poll(self):
            return 0

        def communicate(self, input=None, timeout=None):
            seen["timeout"] = timeout
            return "", ""

    monkeypatch.setattr(
        tools.process.subprocess, "Popen", lambda argv, **kwargs: Finished()
    )

    execute_tool("run_shell", {"command": "true", "timeout": 10_000}, tmp_path)
    assert seen["timeout"] == tools.process.SHELL_MAX_TIMEOUT

    execute_tool("run_shell", {"command": "true"}, tmp_path)
    assert seen["timeout"] == tools.process.SHELL_TIMEOUT


def test_run_python_executes_code_in_the_workspace(tmp_path):
    (tmp_path / "data.txt").write_text("3\n4\n")

    result = execute_tool(
        "run_python",
        {
            "code": "import sys\nnums=[int(x) for x in open('data.txt')]\nprint(sum(nums) + int(sys.stdin.read()))",
            "stdin": "5",
        },
        tmp_path,
    )
    assert "status: success" in result
    assert "stdout:\n12\n" in result

    failing = execute_tool("run_python", {"code": "raise SystemExit(3)"}, tmp_path)
    assert "status: error" in failing
    assert "exit_code: 3" in failing

    assert (
        execute_tool("run_python", {"code": "  "}, tmp_path)
        == "Error: code is required"
    )


def test_write_source_checks_python_syntax(tmp_path):
    written = execute_tool(
        "write_source",
        {"path": "dice.py", "content": "print(1 + 1)\n"},
        tmp_path,
    )
    assert written.startswith("Wrote ")
    assert "Syntax error" not in written
    assert (tmp_path / "dice.py").read_text() == "print(1 + 1)\n"

    note = execute_tool(
        "write_source",
        {"path": "src/broken.py", "content": "def (\n"},
        tmp_path,
    )
    assert note.startswith("Wrote ")
    assert "Syntax error: line 1:" in note
    assert (tmp_path / "src" / "broken.py").read_text() == "def (\n"

    text = execute_tool(
        "write_source", {"path": "notes.md", "content": "def (\n"}, tmp_path
    )
    assert "Syntax error" not in text
    assert (tmp_path / "notes.md").read_text() == "def (\n"

    assert execute_tool("write_source", {"path": "a.py"}, tmp_path) == (
        "Error: content is required"
    )
    assert "outside workspace" in execute_tool(
        "write_source", {"path": "../escaped.py", "content": "print(1)\n"}, tmp_path
    )
    assert not (tmp_path.parent / "escaped.py").exists()


def test_edit_source_replaces_and_reports_syntax(tmp_path):
    (tmp_path / "dice.py").write_text("print(1)\n")

    edited = execute_tool(
        "edit_source",
        {"path": "dice.py", "old_text": "print(1)", "new_text": "print(2)"},
        tmp_path,
    )
    assert edited == "Replaced 1 occurrence(s) in dice.py"
    assert (tmp_path / "dice.py").read_text() == "print(2)\n"

    broken = execute_tool(
        "edit_source",
        {"path": "dice.py", "old_text": "print(2)", "new_text": "def ("},
        tmp_path,
    )
    assert broken.startswith(
        "Replaced 1 occurrence(s) in dice.py\nSyntax error: line 1:"
    )
    assert (tmp_path / "dice.py").read_text() == "def (\n"

    assert "outside workspace" in execute_tool(
        "edit_source",
        {"path": "../dice.py", "old_text": "a", "new_text": "b"},
        tmp_path,
    )


def test_read_source_pages_like_read_file(tmp_path):
    (tmp_path / "dice.py").write_text("one\ntwo\nthree\n")

    page = execute_tool(
        "read_source", {"path": "dice.py", "offset": 2, "limit": 1}, tmp_path
    )
    assert page.startswith("two\n")
    assert "call read_source with offset=3" in page


def test_run_script_executes_a_workspace_file(tmp_path):
    (tmp_path / "dice.py").write_text(
        "import sys\nprint(sum(int(x) for x in sys.argv[1:]))\n"
        "print(sys.stdin.read(), end='')\n"
    )

    result = execute_tool(
        "run_script",
        {"path": "dice.py", "args": ["3", "4"], "stdin": "ok"},
        tmp_path,
    )
    assert "status: success" in result
    assert "stdout:\n7\nok" in result

    assert execute_tool("run_script", {"path": "missing.py"}, tmp_path) == (
        "Error: not found"
    )
    (tmp_path / "pkg").mkdir()
    assert execute_tool("run_script", {"path": "pkg"}, tmp_path) == (
        "Error: is a directory"
    )
    (tmp_path / "notes.md").write_text("hi\n")
    assert execute_tool("run_script", {"path": "notes.md"}, tmp_path) == (
        "Error: path must be a .py file"
    )
    assert (
        execute_tool("run_script", {"path": "dice.py", "args": [1]}, tmp_path)
        == "Error: args must be a list of strings"
    )
    assert "outside workspace" in execute_tool(
        "run_script", {"path": "../dice.py"}, tmp_path
    )


def test_run_python_times_out(tmp_path):
    result = execute_tool(
        "run_python",
        {"code": "import time\ntime.sleep(5)", "timeout": 1},
        tmp_path,
    )
    assert "exit_code: 124" in result
    assert "timed out after 1s" in result


def test_sandbox_blocks_reads_writes_symlinks_and_network(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret-outside")
    (workspace / "inside.txt").write_text("inside-ok\n")
    (workspace / "escape.txt").symlink_to(outside)

    inside = execute_tool(
        "run_python",
        {"code": "print(open('inside.txt').read(), end='')"},
        workspace,
    )
    assert "status: success" in inside
    assert "stdout:\ninside-ok\n" in inside

    denied_sibling = execute_tool(
        "run_python",
        {"code": f"print(open({str(outside)!r}).read())"},
        workspace,
    )
    assert "secret-outside" not in denied_sibling
    assert "Operation not permitted" in denied_sibling

    denied_passwd = execute_tool(
        "run_python",
        {"code": "print(open('/etc/passwd').read())"},
        workspace,
    )
    assert "root:" not in denied_passwd
    assert "Operation not permitted" in denied_passwd

    sibling_out = tmp_path / "pwned.txt"
    write_out = execute_tool(
        "run_python",
        {"code": f"open({str(sibling_out)!r}, 'w').write('pwned')"},
        workspace,
    )
    assert "Operation not permitted" in write_out
    assert not sibling_out.exists()

    linked = execute_tool("run_shell", {"command": "cat escape.txt"}, workspace)
    assert "secret-outside" not in linked
    assert "Operation not permitted" in linked

    networked = execute_tool(
        "run_python",
        {
            "code": "import socket; socket.create_connection(('1.1.1.1', 443), timeout=3)",
            "timeout": 5,
        },
        workspace,
    )
    assert "Operation not permitted" in networked


def test_yolo_reads_outside_the_workspace_and_keeps_writes_inside(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret-outside")
    (workspace / "escape.txt").symlink_to(outside)

    direct = execute_tool("read_file", {"path": str(outside)}, workspace, yolo=True)
    linked = execute_tool("read_file", {"path": "escape.txt"}, workspace, yolo=True)
    blocked = execute_tool(
        "write_file",
        {"path": str(tmp_path / "pwned.txt"), "content": "nope"},
        workspace,
        yolo=True,
    )

    assert direct == "secret-outside"
    assert linked == "secret-outside"
    assert "outside workspace" in blocked
    assert not (tmp_path / "pwned.txt").exists()


def test_yolo_runs_sudo_without_the_seatbelt(tmp_path, monkeypatch):
    seen = {}

    class Finished:
        pid = 0
        returncode = 0

        def poll(self):
            return 0

        def communicate(self, input=None, timeout=None):
            return "", ""

    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs["env"]
        seen["shell"] = kwargs["shell"]
        return Finished()

    monkeypatch.setattr(tools.process.subprocess, "Popen", fake_popen)

    result = execute_tool("run_shell", {"command": "sudo true"}, tmp_path, yolo=True)

    assert "blocked" not in result
    assert "status: success" in result
    assert seen["argv"] == ["/bin/sh", "-c", "sudo true"]
    assert seen["shell"] is False
    assert seen["env"] == dict(os.environ)
    assert "HARNESS_SANDBOX_PROFILE" not in seen["env"]


def test_yolo_shell_runs_a_pipe_and_a_chain(tmp_path):
    piped = execute_tool(
        "run_shell",
        {"command": "printf hi | tr a-z A-Z"},
        tmp_path,
        yolo=True,
    )
    assert "status: success" in piped
    assert "stdout:\nHI\n" in piped
    assert not (tmp_path / ".shell").exists()

    chained = execute_tool(
        "run_shell", {"command": "true && printf ok"}, tmp_path, yolo=True
    )
    assert "stdout:\nok\n" in chained


def test_shell_blocks_a_pipe(tmp_path, monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("shell ran")

    monkeypatch.setattr(tools.process.subprocess, "Popen", fail)

    result = execute_tool("run_shell", {"command": "printf hi | tr a-z A-Z"}, tmp_path)

    assert "blocked" in result


def test_long_process_output_is_saved_for_read_file(tmp_path):
    code = "print('\\n'.join(f'{i:04d}-' + ('x' * 40) for i in range(400)))"
    result = execute_tool("run_python", {"code": code}, tmp_path)

    assert len(result) <= MAX_TOOL_RESULT
    match = re.search(
        r"full output saved to (\S+); call read_file with path \1 and offset (\d+)",
        result,
    )
    assert match
    path = Path(match.group(1))
    offset = int(match.group(2))
    assert path.is_relative_to((tmp_path / ".shell").resolve())
    saved = path.read_text(encoding="utf-8")
    assert len(saved) > MAX_TOOL_RESULT
    prefix = result.split("\n...[truncated", 1)[0]
    assert prefix.endswith("\n")
    assert saved.startswith(prefix)
    rest = execute_tool("read_file", {"path": str(path), "offset": offset}, tmp_path)
    assert saved[len(prefix) :].splitlines()[0] in rest
    assert "0399-" in saved


def test_yolo_python_reads_outside_the_workspace(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret-outside")

    result = execute_tool(
        "run_python",
        {"code": f"print(open({str(outside)!r}).read(), end='')"},
        workspace,
        yolo=True,
    )

    assert "status: success" in result
    assert "stdout:\nsecret-outside\n" in result


def test_project_grant_writes_relative_paths_and_rejects_escapes(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    (workspace / "notes.md").write_text("session-note")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret-outside")
    (project / "escape.txt").symlink_to(outside)

    written = execute_tool(
        "write_file",
        {"path": "app.txt", "content": "from-project"},
        workspace,
        project=project,
    )
    listed = execute_tool("list_files", {}, workspace, project=project)
    session_file = execute_tool(
        "read_file",
        {"path": str(workspace / "notes.md")},
        workspace,
        project=project,
    )
    escaped = execute_tool(
        "read_file", {"path": "escape.txt"}, workspace, project=project
    )
    blocked = execute_tool(
        "write_file",
        {"path": str(outside), "content": "nope"},
        workspace,
        project=project,
    )
    yolo_write = execute_tool(
        "write_file",
        {"path": "yolo.txt", "content": "granted"},
        workspace,
        yolo=True,
        project=project,
    )
    yolo_blocked = execute_tool(
        "write_file",
        {"path": str(tmp_path / "pwned.txt"), "content": "nope"},
        workspace,
        yolo=True,
        project=project,
    )

    assert "Wrote" in written
    assert (project / "app.txt").read_text() == "from-project"
    assert not (workspace / "app.txt").exists()
    assert "app.txt" in listed
    assert "notes.md" not in listed
    assert session_file == "session-note"
    assert "outside workspace" in escaped
    assert "secret-outside" not in escaped
    assert "outside workspace" in blocked
    assert outside.read_text() == "secret-outside"
    assert "Wrote" in yolo_write
    assert (project / "yolo.txt").read_text() == "granted"
    assert "outside workspace" in yolo_blocked
    assert not (tmp_path / "pwned.txt").exists()


def test_project_shell_accepts_only_granted_absolute_paths(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    inside = project / "main.py"
    inside.write_text("print('inside')\n")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret-outside")

    allowed = execute_tool(
        "run_shell",
        {"command": f"cat {shlex.quote(str(inside))}"},
        workspace,
        project=project,
    )
    denied = execute_tool(
        "run_shell",
        {"command": f"cat {shlex.quote(str(outside))}"},
        workspace,
        project=project,
    )
    ran = execute_tool(
        "run_python",
        {"code": "open('written.txt', 'w').write('ok')"},
        workspace,
        project=project,
    )
    sibling = tmp_path / "pwned.txt"
    blocked_write = execute_tool(
        "run_python",
        {"code": f"open({str(sibling)!r}, 'w').write('pwned')"},
        workspace,
        project=project,
    )

    assert "status: success" in allowed
    assert "print('inside')" in allowed
    assert "outside workspace" in denied
    assert "status: success" in ran
    assert (project / "written.txt").read_text() == "ok"
    assert not (workspace / "written.txt").exists()
    assert "Operation not permitted" in blocked_write
    assert not sibling.exists()


def test_project_seatbelt_profile_grants_the_folder_and_keeps_home(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    project = tmp_path / "proj"
    project.mkdir()

    profile = sandbox.seatbelt_profile(workspace, project)
    _argv, env = sandbox.sandboxed_argv(["true"], workspace, project)

    assert f'(subpath "{project.resolve()}")' in profile
    assert "(deny network*)" in profile
    assert "(allow file-write*" in profile
    assert profile == env[sandbox.PROFILE_ENV]
    assert env["HOME"] == str(workspace.resolve())
    assert env["TMPDIR"] == str((workspace / ".tmp").resolve())


def test_project_is_stored_restored_and_kept_across_reset(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    session = agent.Session(workspace=tmp_path / "ws", project=project)
    session.messages.append({"role": "user", "content": "work"})
    agent_transcript._save_transcript(session)
    saved = json.loads((session.workspace / TRANSCRIPT_FILE).read_text())
    assert saved["project"] == str(project.resolve())

    resumed = agent.Session(workspace=tmp_path / "fresh", project=other)
    resumed.resume(session.workspace)
    assert resumed.project == project.resolve()
    assert f"Your project is {project.resolve()}." in resumed.messages[0]["content"]
    assert "write it to a file in the project" in resumed.messages[0]["content"]

    plain = agent.Session(workspace=tmp_path / "plain")
    plain.messages.append({"role": "user", "content": "notes"})
    agent_transcript._save_transcript(plain)
    launched = agent.Session(workspace=tmp_path / "launch", project=other)
    launched.resume(plain.workspace)
    assert launched.project is None
    assert "Your project is" not in launched.messages[0]["content"]

    previous = session.workspace
    session.reset()
    assert session.project == project.resolve()
    assert session.workspace != previous
    assert str(project.resolve()) in session.messages[0]["content"]


def test_coding_is_stored_restored_and_kept_across_reset(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    with pytest.raises(ValueError, match="--coding requires --project"):
        agent.Session(workspace=tmp_path / "nocode", coding=True)

    session = agent.Session(workspace=tmp_path / "ws", project=project, coding=True)
    assert session.messages[0]["content"].startswith(
        "You are a coding agent working in a granted project."
    )
    session.messages.append({"role": "user", "content": "work"})
    agent_transcript._save_transcript(session)
    saved = json.loads((session.workspace / TRANSCRIPT_FILE).read_text())
    assert saved["coding"] is True
    assert saved["project"] == str(project.resolve())

    resumed = agent.Session(workspace=tmp_path / "fresh", project=project)
    assert resumed.coding is False
    resumed.resume(session.workspace)
    assert resumed.coding is True
    assert resumed.project == project.resolve()
    assert resumed.messages[0]["content"].startswith(
        "You are a coding agent working in a granted project."
    )

    previous = session.workspace
    session.reset()
    assert session.coding is True
    assert session.project == project.resolve()
    assert session.workspace != previous
    assert session.messages[0]["content"].startswith(
        "You are a coding agent working in a granted project."
    )

    plain = agent.Session(workspace=tmp_path / "plain", project=project)
    plain.messages.append({"role": "user", "content": "notes"})
    agent_transcript._save_transcript(plain)
    assert "coding" not in json.loads((plain.workspace / TRANSCRIPT_FILE).read_text())
    launched = agent.Session(
        workspace=tmp_path / "launch", project=project, coding=True
    )
    launched.resume(plain.workspace)
    assert launched.coding is False
    assert launched.messages[0]["content"].startswith(
        "You are a helpful personal assistant."
    )

    bad = agent.Session(workspace=tmp_path / "bad", project=project)
    bad.messages.append({"role": "user", "content": "work"})
    agent_transcript._save_transcript(bad)
    payload = json.loads((bad.workspace / TRANSCRIPT_FILE).read_text())
    payload["coding"] = "yes"
    (bad.workspace / TRANSCRIPT_FILE).write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="invalid coding"):
        agent.Session().resume(bad.workspace)


def test_project_must_be_a_separate_existing_directory(tmp_path):
    workspace = tmp_path / "ws"
    parent = tmp_path / "parent"
    parent.mkdir()
    with pytest.raises(ValueError, match="existing directory"):
        agent.Session(workspace=workspace, project=tmp_path / "missing")
    with pytest.raises(ValueError, match="overlap"):
        agent.Session(workspace=workspace, project=workspace)
    with pytest.raises(ValueError, match="overlap"):
        agent.Session(workspace=parent / "child", project=parent)
    with pytest.raises(ValueError, match="not allowed"):
        agent.Session(workspace=workspace, project="/")

    gone = tmp_path / "gone"
    stored = agent.Session(workspace=tmp_path / "stored")
    stored.messages.append({"role": "user", "content": "work"})
    agent_transcript._save_transcript(stored)
    payload = json.loads((stored.workspace / TRANSCRIPT_FILE).read_text())
    payload["project"] = str(gone)
    (stored.workspace / TRANSCRIPT_FILE).write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="Cannot resume"):
        agent.Session().resume(stored.workspace)


def _write_rule(path: Path, front: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{front}\n---\n{body}\n", encoding="utf-8")


def test_project_rules_enter_the_prompt_and_attach_on_a_matching_read(tmp_path):
    project = tmp_path / "proj"
    _write_rule(
        project / ".cursor/rules/always.mdc",
        "description: Core standards\nalwaysApply: true",
        "ALWAYS-RULE-BODY",
    )
    _write_rule(
        project / ".cursor/rules/py.mdc",
        "description: Python files\nglobs: src/**/*.py,tests/**/*.py\nalwaysApply: false",
        "GLOB-RULE-BODY",
    )
    _write_rule(
        project / ".cursor/rules/nested.mdc",
        "description: Package files\nglobs: src/pkg/**\nalwaysApply: false",
        "NESTED-RULE-BODY",
    )
    _write_rule(
        project / ".cursor/rules/manual.mdc",
        "description: Naming conventions\nalwaysApply: false",
        "MANUAL-RULE-BODY",
    )
    _write_rule(
        project / ".cursor/rules/both.mdc",
        "description: Always and globs\nglobs: **/*.py\nalwaysApply: true",
        "BOTH-RULE-BODY",
    )
    (project / ".cursor/rules/broken.mdc").write_text("not a rule\n")
    (project / ".cursor/rules/bad.mdc").write_text(
        "---\nalwaysApply: yes\n---\nBAD-RULE\n"
    )
    module = project / "src/pkg/mod.py"
    module.parent.mkdir(parents=True)
    module.write_text("print(1)\n")
    (project / "notes.md").write_text("hello\n")
    (project / "tests").mkdir()
    (project / "tests/test_mod.py").write_text("def test_mod():\n    pass\n")

    granted = agent.Session(workspace=tmp_path / "granted", project=project)
    assert "Project rules" not in granted.messages[0]["content"]
    assert "ALWAYS-RULE-BODY" not in granted.messages[0]["content"]
    assert "Python files" not in granted.messages[0]["content"]
    granted.skills.load_skill("file-ops")
    granted_calls = {"n": 0}

    def granted_create(**kwargs):
        granted_calls["n"] += 1
        if granted_calls["n"] == 1:
            reply = SimpleNamespace(
                content="",
                tool_calls=[
                    SimpleNamespace(
                        id="g1",
                        function=SimpleNamespace(
                            name="read_file",
                            arguments=json.dumps({"path": "src/pkg/mod.py"}),
                        ),
                    )
                ],
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=reply)])
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="plain", tool_calls=None)
                )
            ]
        )

    granted_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=granted_create))
    )
    assert agent.run("read the module", granted, client=granted_client) == "plain"
    granted_tool = next(
        message for message in granted.messages if message["role"] == "tool"
    )
    assert "print(1)" in granted_tool["content"]
    assert "GLOB-RULE-BODY" not in granted_tool["content"]
    assert "NESTED-RULE-BODY" not in granted_tool["content"]

    session = agent.Session(workspace=tmp_path / "ws", project=project, coding=True)
    prompt = session.messages[0]["content"]
    assert "Project rules (always apply)" in prompt
    assert "ALWAYS-RULE-BODY" in prompt
    assert "BOTH-RULE-BODY" in prompt
    assert "Python files" in prompt
    assert "src/**/*.py" in prompt
    assert "GLOB-RULE-BODY" not in prompt
    assert "NESTED-RULE-BODY" not in prompt
    assert "Naming conventions" in prompt
    assert "read that .mdc file" in prompt
    assert "MANUAL-RULE-BODY" not in prompt
    assert "BAD-RULE" not in prompt
    assert "not a rule" not in prompt
    assert rules.glob_matches("src/pkg/mod.py", "src/pkg/**")
    assert rules.glob_matches("tests/test_mod.py", "tests/**/*.py")

    session.skills.load_skill("file-ops")
    calls = {"n": 0}

    def create(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            message = SimpleNamespace(
                content="",
                tool_calls=[
                    SimpleNamespace(
                        id="c1",
                        function=SimpleNamespace(
                            name="read_file",
                            arguments=json.dumps({"path": "src/pkg/mod.py"}),
                        ),
                    ),
                    SimpleNamespace(
                        id="c2",
                        function=SimpleNamespace(
                            name="read_file",
                            arguments=json.dumps({"path": "src/pkg/mod.py"}),
                        ),
                    ),
                ],
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])
        if calls["n"] == 2:
            message = SimpleNamespace(
                content="",
                tool_calls=[
                    SimpleNamespace(
                        id="c3",
                        function=SimpleNamespace(
                            name="read_file",
                            arguments=json.dumps({"path": "notes.md"}),
                        ),
                    )
                ],
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="done", tool_calls=None)
                )
            ]
        )

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    assert agent.run("read the module", session, client=client) == "done"
    tool_messages = [
        message for message in session.messages if message["role"] == "tool"
    ]
    assert "print(1)" in tool_messages[0]["content"]
    assert "GLOB-RULE-BODY" in tool_messages[0]["content"]
    assert "NESTED-RULE-BODY" in tool_messages[0]["content"]
    assert "BOTH-RULE-BODY" not in tool_messages[0]["content"]
    assert "ALWAYS-RULE-BODY" not in tool_messages[0]["content"]
    assert "GLOB-RULE-BODY" not in tool_messages[1]["content"]
    assert "hello" in tool_messages[2]["content"]
    assert "GLOB-RULE-BODY" not in tool_messages[2]["content"]
    assert "NESTED-RULE-BODY" not in tool_messages[2]["content"]

    assert (
        rules.append_matching_rules(project, {"path": "src"}, "files", set()) == "files"
    )
    assert (
        rules.append_matching_rules(
            project, {"command": "cat src/pkg/mod.py"}, "ok", set()
        )
        == "ok"
    )
    tests_hit = rules.append_matching_rules(
        project, {"path": "tests/test_mod.py"}, "code", set()
    )
    assert "GLOB-RULE-BODY" in tests_hit
    assert "NESTED-RULE-BODY" not in tests_hit


def test_project_rules_skip_escapes_and_sessions_without_a_project(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    outside = tmp_path / "outside.mdc"
    outside.write_text(
        "---\ndescription: leaked\nalwaysApply: true\n---\nOUTSIDE-RULE\n"
    )
    rules_dir = project / ".cursor" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "linked.mdc").symlink_to(outside)
    assert rules.load_project_rules(project) == []

    workspace = tmp_path / "ws"
    _write_rule(
        workspace / ".cursor/rules/local.mdc",
        "description: local\nalwaysApply: true",
        "WORKSPACE-RULE",
    )
    plain = agent.Session(workspace=workspace)
    assert "WORKSPACE-RULE" not in plain.messages[0]["content"]
    assert "Project rules" not in plain.messages[0]["content"]

    session = agent.Session(workspace=tmp_path / "agent", project=project, coding=True)
    assert "OUTSIDE-RULE" not in session.messages[0]["content"]
    _write_rule(
        project / ".cursor/rules/later.mdc",
        "description: later\nalwaysApply: true",
        "LATER-RULE",
    )
    granted = agent.Session(workspace=tmp_path / "granted", project=project)
    assert "LATER-RULE" not in granted.messages[0]["content"]
    session.sync_prompt()
    assert "LATER-RULE" in session.messages[0]["content"]
    session.applied_rules.add(".cursor/rules/later.mdc")
    session.reset()
    assert session.applied_rules == set()
    assert "LATER-RULE" in session.messages[0]["content"]


def test_rule_globs_accept_a_yaml_list(tmp_path):
    project = tmp_path / "proj"
    path = project / ".cursor/rules/view.mdc"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\n"
        "description: Views\n"
        "globs:\n"
        '  - "**/*.tsx"\n'
        '  - "src/**/*.ts"\n'
        "alwaysApply: false\n"
        "---\n"
        "VIEW-RULE\n"
    )
    loaded = rules.load_project_rules(project)
    assert loaded[0].globs == ("**/*.tsx", "src/**/*.ts")
    applied: set[str] = set()
    result = rules.append_matching_rules(
        project, {"path": "src/App.tsx"}, "file", applied
    )
    assert "VIEW-RULE" in result
    assert applied == {".cursor/rules/view.mdc"}


def test_sandbox_refuses_to_run_off_macos(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", "linux")

    def fail_popen(*args, **kwargs):
        raise AssertionError("command ran without a sandbox")

    monkeypatch.setattr(tools.process.subprocess, "Popen", fail_popen)

    result = execute_tool("run_shell", {"command": "true"}, tmp_path)

    assert result == "Error: sandbox is unavailable"


# --- research additions ---------------------------------------------------


def test_read_url_fetches_extracts_and_saves_text(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tools.http,
        "_http_get",
        lambda url: (200, "text/html; charset=utf-8", ARTICLE_HTML.encode()),
    )

    result = execute_tool("read_url", {"url": "https://1.1.1.1/news"}, tmp_path)

    assert result.startswith("url: https://1.1.1.1/news\n")
    assert "title: City Council Approves Transit Plan" in result
    assert "author: Ada Lovelace" in result
    assert "harbor district" in result
    saved = re.search(r"saved: (\S+)", result).group(1)
    text_file = re.search(r"text_file: (\S+)", result).group(1)
    assert saved.startswith("downloads/1.1.1.1-") and saved.endswith(".html")
    assert text_file == saved[: -len(".html")] + ".txt"
    assert (tmp_path / saved).read_bytes() == ARTICLE_HTML.encode()
    assert "harbor district" in (tmp_path / text_file).read_text()

    blocked = execute_tool("read_url", {"url": "http://127.0.0.1/"}, tmp_path)
    assert "blocked" in blocked
    assert (
        execute_tool(
            "read_url", {"url": "https://1.1.1.1/news", "mode": "raw"}, tmp_path
        )
        == "Error: mode must be 'article' or 'full'"
    )


def test_read_url_handles_plain_text_and_truncates_with_a_pointer(
    tmp_path, monkeypatch
):
    body = ("word " * 3000).encode()
    monkeypatch.setattr(tools.http, "_http_get", lambda url: (200, "text/plain", body))

    result = execute_tool("read_url", {"url": "https://1.1.1.1/notes"}, tmp_path)

    assert len(result) <= MAX_TOOL_RESULT
    assert "full text is in downloads/1.1.1.1-" in result
    assert "use read_file with offset to continue" in result
    text_file = re.search(r"text_file: (\S+)", result).group(1)
    assert text_file.endswith(".txt")
    assert (tmp_path / text_file).read_text() == body.decode().strip()


def test_read_url_reads_a_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tools.http,
        "_http_get",
        lambda url: (200, "application/pdf", _pdf_bytes(["Hello PDF"])),
    )

    result = execute_tool("read_url", {"url": "https://1.1.1.1/paper"}, tmp_path)

    assert "Hello PDF" in result
    assert re.search(r"saved: downloads/\S+\.pdf", result)


def test_parse_html_full_mode_and_fallback_keep_non_article_text(tmp_path):
    page = (
        "<html><head><title>Docs</title></head><body><nav>Home</nav>"
        "<table><tr><td>Option</td><td>Meaning</td></tr>"
        "<tr><td>-x</td><td>exit</td></tr></table>"
        "<script>var hidden = 1;</script></body></html>"
    )
    (tmp_path / "docs.html").write_text(page)

    article = execute_tool("parse_html", {"path": "docs.html"}, tmp_path)
    assert article.startswith("title: Docs")
    assert "Meaning" in article

    full = execute_tool("parse_html", {"path": "docs.html", "mode": "full"}, tmp_path)
    assert "Option Meaning" in full
    assert "hidden" not in full

    assert (
        execute_tool("parse_html", {"path": "docs.html", "mode": "raw"}, tmp_path)
        == "Error: mode must be 'article' or 'full'"
    )


RSS_FEED = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Harbor News</title>
<item><title>Bus line funded</title><link>https://1.1.1.1/bus</link>
<pubDate>Tue, 06 Oct 2026 10:00:00 GMT</pubDate>
<description><![CDATA[<p>The council <b>voted</b> yes.</p>]]></description></item>
<item><title>Second</title><link>https://1.1.1.1/two</link><description>Two</description></item>
</channel></rss>
"""

ATOM_FEED = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom Blog</title>
<entry><title>Entry One</title><link rel="alternate" href="https://1.1.1.1/one"/>
<updated>2026-10-06T10:00:00Z</updated><summary>First summary</summary></entry>
</feed>
"""


def test_read_feed_parses_rss_and_atom(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tools.http, "_http_get", lambda url: (200, "application/rss+xml", RSS_FEED)
    )
    rss = execute_tool(
        "read_feed", {"url": "https://1.1.1.1/feed", "max_items": 1}, tmp_path
    )
    assert rss.startswith("feed: Harbor News\nentries: 2\n")
    assert (
        "1. Bus line funded\n   link: https://1.1.1.1/bus\n   date: Tue, 06 Oct 2026 10:00:00 GMT\n   summary: The council voted yes."
        in rss
    )
    assert "Second" not in rss
    assert "Call read_url on an entry link" in rss

    monkeypatch.setattr(
        tools.http, "_http_get", lambda url: (200, "application/atom+xml", ATOM_FEED)
    )
    atom = execute_tool("read_feed", {"url": "https://1.1.1.1/atom"}, tmp_path)
    assert "feed: Atom Blog" in atom
    assert "link: https://1.1.1.1/one" in atom
    assert "date: 2026-10-06T10:00:00Z" in atom
    assert "summary: First summary" in atom


def test_read_feed_rejects_bad_xml_and_entities(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tools.http,
        "_http_get",
        lambda url: (200, "text/xml", b"<rss><channel></channel></rss>"),
    )
    assert execute_tool("read_feed", {"url": "https://1.1.1.1/feed"}, tmp_path) == (
        "Error: no feed entries found"
    )

    monkeypatch.setattr(
        tools.http, "_http_get", lambda url: (200, "text/xml", b"<rss><item>")
    )
    assert execute_tool(
        "read_feed", {"url": "https://1.1.1.1/feed"}, tmp_path
    ).startswith("Error: invalid XML")

    bomb = b'<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "y">]><rss><channel><item><title>&x;</title></item></channel></rss>'
    monkeypatch.setattr(tools.http, "_http_get", lambda url: (200, "text/xml", bomb))
    assert "DOCTYPE" in execute_tool(
        "read_feed", {"url": "https://1.1.1.1/feed"}, tmp_path
    )

    assert "blocked" in execute_tool(
        "read_feed", {"url": "http://127.0.0.1/feed"}, tmp_path
    )


# --- reference and http ---------------------------------------------------


def _json_get(payloads):
    seen = []

    def fake_get(url):
        seen.append(url)
        for prefix, payload in payloads:
            if url.startswith(prefix):
                return (
                    200,
                    "application/json; charset=utf-8",
                    json.dumps(payload).encode(),
                )
        raise AssertionError(url)

    return fake_get, seen


def test_wikipedia_search_lists_titles_with_urls(tmp_path, monkeypatch):
    payload = {
        "query": {
            "search": [
                {
                    "title": "Ada Lovelace",
                    "snippet": "English <span class='x'>mathematician</span>",
                },
                {"title": "Lovelace Medal", "snippet": "award"},
            ]
        }
    }
    fake_get, seen = _json_get([("https://en.wikipedia.org/w/api.php", payload)])
    monkeypatch.setattr(tools.http, "_http_get", fake_get)

    result = execute_tool("wikipedia_search", {"query": "ada lovelace"}, tmp_path)

    assert (
        "1. Ada Lovelace\n   url: https://en.wikipedia.org/wiki/Ada_Lovelace\n   snippet: English mathematician"
        in result
    )
    assert "2. Lovelace Medal" in result
    assert "wikipedia_summary" in result
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(seen[0]).query)
    assert query["srsearch"] == ["ada lovelace"]
    assert query["srlimit"] == ["5"]
    assert (
        execute_tool("wikipedia_search", {"query": " "}, tmp_path)
        == "Error: query is required"
    )


def test_wikipedia_summary_returns_extract_and_flags_missing(tmp_path, monkeypatch):
    payload = {
        "title": "Ada Lovelace",
        "description": "English mathematician",
        "extract": "Ada Lovelace was an English mathematician.",
        "content_urls": {
            "desktop": {"page": "https://en.wikipedia.org/wiki/Ada_Lovelace"}
        },
    }
    fake_get, _seen = _json_get(
        [("https://en.wikipedia.org/api/rest_v1/page/summary/Ada_Lovelace", payload)]
    )
    monkeypatch.setattr(tools.http, "_http_get", fake_get)

    result = execute_tool("wikipedia_summary", {"title": "Ada Lovelace"}, tmp_path)
    assert result == (
        "title: Ada Lovelace\n"
        "description: English mathematician\n"
        "url: https://en.wikipedia.org/wiki/Ada_Lovelace\n"
        "summary:\nAda Lovelace was an English mathematician."
    )

    def missing(url):
        raise ValueError("http status 404")

    monkeypatch.setattr(tools.http, "_http_get", missing)
    assert execute_tool("wikipedia_summary", {"title": "Nope Nope"}, tmp_path) == (
        "Error: no Wikipedia article named 'Nope Nope'"
    )
    assert execute_tool("wikipedia_summary", {}, tmp_path) == "Error: title is required"


def test_http_get_json_adds_params_and_rejects_non_json(tmp_path, monkeypatch):
    fake_get, seen = _json_get([("https://1.1.1.1/api", {"temp": 21.5, "ok": True})])
    monkeypatch.setattr(tools.http, "_http_get", fake_get)

    result = execute_tool(
        "http_get_json",
        {
            "url": "https://1.1.1.1/api?units=metric",
            "params": {"lat": 45.5, "city": "Montréal", "dry": True},
        },
        tmp_path,
    )
    assert json.loads(result) == {"temp": 21.5, "ok": True}
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(seen[0]).query)
    assert query == {
        "units": ["metric"],
        "lat": ["45.5"],
        "city": ["Montréal"],
        "dry": ["true"],
    }

    monkeypatch.setattr(
        tools.http, "_http_get", lambda url: (200, "text/html", b"<html></html>")
    )
    html = execute_tool("http_get_json", {"url": "https://1.1.1.1/page"}, tmp_path)
    assert html.startswith("Error: unsupported content type")

    monkeypatch.setattr(
        tools.http, "_http_get", lambda url: (200, "application/json", b"{nope")
    )
    assert execute_tool(
        "http_get_json", {"url": "https://1.1.1.1/api"}, tmp_path
    ).startswith("Error: invalid JSON response")
    assert "blocked" in execute_tool(
        "http_get_json", {"url": "http://10.0.0.1/api"}, tmp_path
    )
    assert (
        execute_tool(
            "http_get_json", {"url": "https://1.1.1.1/api", "params": ["a"]}, tmp_path
        )
        == "Error: params must be an object"
    )


class _FakeHTTPResponse:
    def __init__(self, code, reason, body, url, headers):
        self.code = code
        self.status = code
        self.reason = reason
        self.msg = reason
        self.url = url
        self._body = body
        self.headers = http.client.HTTPMessage()
        for key, value in headers.items():
            self.headers[key] = value

    def read(self, amt=-1):
        if amt is None or amt < 0:
            data, self._body = self._body, b""
            return data
        data, self._body = self._body[:amt], self._body[amt:]
        return data

    def info(self):
        return self.headers

    def geturl(self):
        return self.url

    def close(self):
        return None


def _install_http(monkeypatch, replies):
    seen = []
    pending = list(replies)

    def http_open(self, req):
        seen.append(req)
        reply = pending.pop(0)
        return _FakeHTTPResponse(
            reply["code"],
            reply.get("reason", "OK"),
            reply.get("body", b""),
            req.full_url,
            reply.get("headers", {}),
        )

    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", http_open)
    return seen


def test_http_request_blocks_targets_and_bad_arguments(tmp_path):
    for url in (
        "file:///etc/passwd",
        "http://user:pass@1.1.1.1/",
        "http://127.0.0.1/",
        "http://10.0.0.1/",
    ):
        result = execute_tool("http_request", {"method": "GET", "url": url}, tmp_path)
        assert result.startswith("Error:")
        assert "blocked" in result
    assert (
        execute_tool(
            "http_request", {"method": "TRACE", "url": "http://1.1.1.1/"}, tmp_path
        )
        == "Error: Method not allowed: TRACE"
    )
    assert (
        execute_tool(
            "http_request",
            {"method": "GET", "url": "http://1.1.1.1/", "params": ["a"]},
            tmp_path,
        )
        == "Error: params must be an object"
    )
    assert (
        execute_tool(
            "http_request",
            {
                "method": "POST",
                "url": "http://1.1.1.1/",
                "json_body": {"a": 1},
                "body": "raw",
            },
            tmp_path,
        )
        == "Error: json_body and body cannot both be set"
    )


def test_http_request_posts_json_and_returns_status(tmp_path, monkeypatch):
    seen = _install_http(
        monkeypatch,
        [
            {
                "code": 404,
                "reason": "Not Found",
                "body": b"missing",
                "headers": {"Content-Type": "application/json"},
            }
        ],
    )

    result = execute_tool(
        "http_request",
        {
            "method": "post",
            "url": "http://1.1.1.1/api",
            "params": {"q": "1"},
            "json_body": {"name": "Ada"},
            "headers": {"X-Test": "yes"},
        },
        tmp_path,
    )

    assert not result.startswith("Error:")
    assert "status_code: 404" in result
    assert "reason: Not Found" in result
    assert "final_url: http://1.1.1.1/api?q=1" in result
    assert "content_type: application/json" in result
    assert "truncated: false" in result
    assert "body:\nmissing" in result
    request = seen[0]
    assert request.get_method() == "POST"
    assert request.data == b'{"name": "Ada"}'
    assert request.headers["Content-type"] == "application/json"
    assert request.headers["User-agent"] == tools.http.USER_AGENT
    assert request.headers["X-test"] == "yes"
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
    assert query == {"q": ["1"]}

    seen = _install_http(
        monkeypatch,
        [
            {
                "code": 401,
                "reason": "Unauthorized",
                "body": b"Invalid email or password.",
                "headers": {"Content-Type": "text/html"},
            }
        ],
    )
    as_string = execute_tool(
        "http_request",
        {
            "method": "POST",
            "url": "http://1.1.1.1/login",
            "json_body": ('{"email": "test@example.com", "password": "password123"}'),
        },
        tmp_path,
    )
    assert "status_code: 401" in as_string
    assert seen[-1].data == (
        b'{"email": "test@example.com", "password": "password123"}'
    )
    assert seen[-1].headers["Content-type"] == "application/json"
    rejected = execute_tool(
        "http_request",
        {"method": "POST", "url": "http://1.1.1.1/login", "json_body": "not-json"},
        tmp_path,
    )
    assert rejected.startswith("Error: json_body must be an object or array.")
    assert '{"email": "a@b.c", "password": "secret"}' in rejected


def test_http_request_redirects_and_truncates(tmp_path, monkeypatch):
    stayed_seen = _install_http(
        monkeypatch,
        [
            {
                "code": 302,
                "reason": "Found",
                "body": b"stay",
                "headers": {
                    "Location": "http://127.0.0.1/secret",
                    "Content-Type": "text/plain",
                },
            }
        ],
    )
    stayed = execute_tool(
        "http_request", {"method": "GET", "url": "http://1.1.1.1/go"}, tmp_path
    )
    assert "status_code: 302" in stayed
    assert "final_url: http://1.1.1.1/go" in stayed
    assert "body:\nstay" in stayed
    assert len(stayed_seen) == 1

    followed_seen = _install_http(
        monkeypatch,
        [
            {
                "code": 302,
                "reason": "Found",
                "body": b"go",
                "headers": {"Location": "http://127.0.0.1/secret"},
            }
        ],
    )
    blocked = execute_tool(
        "http_request",
        {
            "method": "GET",
            "url": "http://1.1.1.1/go",
            "follow_redirects": True,
        },
        tmp_path,
    )
    assert blocked.startswith("Error:")
    assert "blocked" in blocked
    assert len(followed_seen) == 1

    monkeypatch.setattr(tools.http, "FETCH_MAX_BYTES", 9000)
    _install_http(
        monkeypatch,
        [
            {
                "code": 200,
                "reason": "OK",
                "body": b"y" * 9001,
                "headers": {"Content-Type": "text/plain"},
            }
        ],
    )
    truncated = execute_tool(
        "http_request", {"method": "GET", "url": "http://1.1.1.1/big"}, tmp_path
    )
    assert "truncated: true" in truncated
    assert "...[truncated," in truncated
    assert "save the full response with fetch_url" in truncated


def test_http_request_yolo_allows_localhost(tmp_path, monkeypatch):
    _install_http(
        monkeypatch,
        [
            {
                "code": 200,
                "reason": "OK",
                "body": b"local",
                "headers": {"Content-Type": "text/plain"},
            }
        ],
    )
    result = execute_tool(
        "http_request",
        {"method": "GET", "url": "http://127.0.0.1/local"},
        tmp_path,
        yolo=True,
    )
    assert "status_code: 200" in result
    assert "body:\nlocal" in result
    assert "blocked" in execute_tool(
        "http_request",
        {"method": "GET", "url": "http://127.0.0.1/local"},
        tmp_path,
    )


def test_yolo_http_allows_localhost_and_private_hosts(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tools.http,
        "_http_get",
        lambda url: (200, "application/json", b'{"ok": true}'),
    )
    for url in (
        "http://localhost/local",
        "http://127.0.0.1/local",
        "http://10.0.0.1/local",
    ):
        fetched = execute_tool("fetch_url", {"url": url}, tmp_path, yolo=True)
        assert "status: success" in fetched
    got = execute_tool(
        "http_get_json", {"url": "http://127.0.0.1/api"}, tmp_path, yolo=True
    )
    assert json.loads(got) == {"ok": True}
    assert "blocked" in execute_tool(
        "fetch_url", {"url": "http://127.0.0.1/local"}, tmp_path
    )
    assert "blocked" in execute_tool(
        "http_get_json", {"url": "http://10.0.0.1/api"}, tmp_path
    )
    scheme = execute_tool(
        "fetch_url", {"url": "file:///etc/passwd"}, tmp_path, yolo=True
    )
    assert scheme.startswith("Error:")
    assert "blocked" in scheme


# --- documents ------------------------------------------------------------


def _pdf_bytes(pages):
    objects = []
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(pages)))
    objects.append("<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>")
    objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, text in enumerate(pages):
        content = f"BT /F1 12 Tf 72 712 Td ({text}) Tj ET"
        objects.append(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>"
        )
        objects.append(f"<< /Length {len(content)} >>\nstream\n{content}\nendstream")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for index, obj in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{index} 0 obj\n{obj}\nendobj\n".encode("latin-1"))
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def test_read_pdf_pages_through_the_document(tmp_path):
    pages = [f"Page {n} text" for n in range(1, PDF_MAX_PAGES + 3)]
    (tmp_path / "doc.pdf").write_bytes(_pdf_bytes(pages))

    first = execute_tool("read_pdf", {"path": "doc.pdf"}, tmp_path)
    assert f"pages: {PDF_MAX_PAGES + 2}" in first
    assert f"showing: 1-{PDF_MAX_PAGES}" in first
    assert "--- page 1 ---\nPage 1 text" in first
    assert f"Page {PDF_MAX_PAGES + 1} text" not in first
    assert f"call read_pdf with first_page={PDF_MAX_PAGES + 1}" in first

    rest = execute_tool(
        "read_pdf", {"path": "doc.pdf", "first_page": PDF_MAX_PAGES + 1}, tmp_path
    )
    assert f"showing: {PDF_MAX_PAGES + 1}-{PDF_MAX_PAGES + 2}" in rest
    assert f"Page {PDF_MAX_PAGES + 2} text" in rest
    assert "remain" not in rest

    one = execute_tool(
        "read_pdf", {"path": "doc.pdf", "first_page": 2, "last_page": 2}, tmp_path
    )
    assert "showing: 2-2" in one
    assert "Page 2 text" in one
    assert "Page 3 text" not in one

    assert execute_tool(
        "read_pdf", {"path": "doc.pdf", "first_page": 99}, tmp_path
    ) == (f"Error: first_page 99 is past the last page ({PDF_MAX_PAGES + 2})")
    assert (
        execute_tool(
            "read_pdf", {"path": "doc.pdf", "first_page": 3, "last_page": 2}, tmp_path
        )
        == "Error: last_page must not be before first_page"
    )
    (tmp_path / "bad.pdf").write_bytes(b"not a pdf")
    assert execute_tool("read_pdf", {"path": "bad.pdf"}, tmp_path).startswith(
        "Error: could not read PDF"
    )
    assert "outside workspace" in execute_tool(
        "read_pdf", {"path": "../x.pdf"}, tmp_path
    )


def _docx_bytes(paragraphs):
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    document = f'<?xml version="1.0"?><w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>'
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", document)
    return out.getvalue()


def test_read_docx_returns_paragraphs(tmp_path):
    (tmp_path / "memo.docx").write_bytes(_docx_bytes(["Title line", "Body text here"]))

    result = execute_tool("read_docx", {"path": "memo.docx"}, tmp_path)
    assert result == "file: memo.docx\nparagraphs: 2\ntext:\nTitle line\nBody text here"

    (tmp_path / "bad.docx").write_bytes(b"zip? no")
    assert (
        execute_tool("read_docx", {"path": "bad.docx"}, tmp_path)
        == "Error: not a .docx file"
    )
    assert "outside workspace" in execute_tool(
        "read_docx", {"path": "../m.docx"}, tmp_path
    )


def test_read_docx_truncation_saves_a_text_copy(tmp_path):
    paragraphs = [f"Paragraph {n} " + "word " * 40 for n in range(100)]
    (tmp_path / "long.docx").write_bytes(_docx_bytes(paragraphs))

    result = execute_tool("read_docx", {"path": "long.docx"}, tmp_path)

    assert len(result) <= MAX_TOOL_RESULT
    assert "full text is in long.txt" in result
    assert (tmp_path / "long.txt").read_text().startswith("Paragraph 0")


# --- data -----------------------------------------------------------------


def test_inspect_csv_describes_columns_and_previews_rows(tmp_path):
    (tmp_path / "sales.csv").write_text(
        "city;amount;note\nParis;10;\nLyon;20;late\nNice;;\n"
    )

    result = execute_tool("inspect_csv", {"path": "sales.csv", "rows": 2}, tmp_path)

    assert "file: sales.csv" in result
    assert "delimiter: ';'" in result
    assert "rows: 3 (excluding header)" in result
    assert "columns: 3" in result
    assert "1. city (filled 3/3)" in result
    assert "2. amount (filled 2/3)" in result
    assert "3. note (filled 1/3)" in result
    assert "preview (first 2 rows):" in result
    assert "  Paris | 10 | " in result
    assert "Nice" not in result

    forced = execute_tool(
        "inspect_csv", {"path": "sales.csv", "delimiter": ","}, tmp_path
    )
    assert "columns: 1" in forced
    assert (
        execute_tool("inspect_csv", {"path": "sales.csv", "delimiter": ";;"}, tmp_path)
        == "Error: delimiter must be a single character"
    )
    (tmp_path / "empty.csv").write_text("")
    assert (
        execute_tool("inspect_csv", {"path": "empty.csv"}, tmp_path)
        == "Error: empty file"
    )


def test_inspect_json_describes_structure(tmp_path):
    data = {
        "items": [{"id": 1, "name": "a", "tags": ["x"]}],
        "count": 1,
        "ok": False,
        "none": None,
    }
    (tmp_path / "data.json").write_text(json.dumps(data))

    result = execute_tool("inspect_json", {"path": "data.json"}, tmp_path)

    assert result.splitlines()[0] == "file: data.json"
    assert "object with 4 keys" in result
    assert "items: array of 1" in result
    assert "[0]: object with 3 keys" in result
    assert "id: number 1" in result
    assert "name: string 'a'" in result
    assert "tags: array of 1" in result
    assert "count: number 1" in result
    assert "ok: boolean false" in result
    assert "none: null" in result

    shallow = execute_tool(
        "inspect_json", {"path": "data.json", "max_depth": 1}, tmp_path
    )
    assert "items: array of 1" in shallow
    assert "[0]:" not in shallow

    (tmp_path / "bad.json").write_text("{nope")
    assert execute_tool("inspect_json", {"path": "bad.json"}, tmp_path).startswith(
        "Error: invalid JSON"
    )


# --- memory and tasks -----------------------------------------------------


def test_remember_and_recall_round_trip_through_memory_md(tmp_path):
    assert execute_tool("recall", {}, tmp_path) == "Memory is empty."

    first = execute_tool(
        "remember", {"key": " user  timezone ", "text": "Europe/Paris"}, tmp_path
    )
    assert first == "Remembered 'user timezone'. 1 memories stored in memory.md."
    execute_tool(
        "remember", {"key": "project", "text": "# harnh3x\nagent loop"}, tmp_path
    )
    updated = execute_tool(
        "remember", {"key": "user timezone", "text": "America/Toronto"}, tmp_path
    )
    assert updated == "Updated 'user timezone'. 2 memories stored in memory.md."

    stored = (tmp_path / "memory.md").read_text()
    assert stored.startswith(
        "# Memory\n\n## user timezone\nAmerica/Toronto\n\n## project\nharnh3x\nagent loop\n"
    )
    assert tools.memory_keys() == ["user timezone", "project"]

    everything = execute_tool("recall", {}, tmp_path)
    assert "## user timezone\nAmerica/Toronto" in everything
    assert "## project" in everything

    assert execute_tool("recall", {"query": "toronto"}, tmp_path) == (
        "## user timezone\nAmerica/Toronto"
    )
    assert (
        execute_tool("recall", {"query": "zzz"}, tmp_path) == "No memories match 'zzz'."
    )
    assert execute_tool("remember", {"key": "x"}, tmp_path) == "Error: text is required"
    assert execute_tool("remember", {"text": "x"}, tmp_path) == "Error: key is required"


def test_memory_is_shared_across_workspaces(tmp_path):
    first = tmp_path / "workspaces" / "one"
    second = tmp_path / "workspaces" / "two"
    first.mkdir(parents=True)
    second.mkdir()

    execute_tool("remember", {"key": "user timezone", "text": "America/Toronto"}, first)

    recalled = execute_tool("recall", {}, second)

    assert recalled == "## user timezone\nAmerica/Toronto"
    assert (tmp_path / "memory.md").is_file()
    assert not (first / "memory.md").exists()
    assert not (second / "memory.md").exists()


def test_plan_tools_write_update_and_show(tmp_path):
    assert execute_tool("show_plan", {}, tmp_path) == (
        "No plan yet. Call write_plan with a list of steps."
    )
    assert execute_tool("update_plan", {"step": 1, "status": "done"}, tmp_path) == (
        "Error: no plan yet; call write_plan first"
    )

    written = execute_tool(
        "write_plan",
        {"steps": ["search the web", "read two pages", "answer"]},
        tmp_path,
    )
    assert (
        written
        == "# Plan\n\n1. [ ] search the web\n2. [ ] read two pages\n3. [ ] answer\n\n0/3 done, 0 skipped"
    )
    assert (
        (tmp_path / "plan.md").read_text()
        == "# Plan\n\n1. [ ] search the web\n2. [ ] read two pages\n3. [ ] answer\n"
    )

    done = execute_tool("update_plan", {"step": 1, "status": "done"}, tmp_path)
    assert "1. [x] search the web" in done
    assert "1/3 done, 0 skipped" in done

    skipped = execute_tool(
        "update_plan",
        {"step": 2, "status": "skipped", "note": "pages unreachable"},
        tmp_path,
    )
    assert "2. [-] read two pages (pages unreachable)" in skipped
    assert "1/3 done, 1 skipped" in skipped

    shown = execute_tool("show_plan", {}, tmp_path)
    assert shown == skipped

    assert execute_tool("update_plan", {"step": 9, "status": "done"}, tmp_path) == (
        "Error: step must be a number from 1 to 3"
    )
    assert execute_tool("update_plan", {"step": 1, "status": "later"}, tmp_path) == (
        "Error: status must be done, todo, or skipped"
    )
    assert execute_tool("write_plan", {"steps": []}, tmp_path).startswith(
        "Error: steps"
    )
    assert execute_tool("write_plan", {"steps": ["ok", 3]}, tmp_path).startswith(
        "Error: steps"
    )


# --- prompt ---------------------------------------------------------------


def test_system_prompt_mentions_yolo_only_when_enabled(tmp_path):
    now = datetime(2026, 10, 6, 16, 30, tzinfo=UTC)

    plain = system_prompt(tmp_path, "MENU", now=now)
    yolo = system_prompt(tmp_path, "MENU", now=now, yolo=True)

    assert "Yolo mode" not in plain
    assert YOLO_NOTE in yolo
    assert "/bin/sh" in yolo
    assert "are blocked" not in yolo
    assert "localhost" in yolo
    assert yolo.endswith("MENU")
    loaded = SkillRegistry(yolo=True).load_skill("shell")
    assert loaded.endswith(YOLO_NOTE)
    assert "are blocked" not in loaded
    assert "`/bin/sh -c`" in loaded
    assert "are blocked" in SkillRegistry().load_skill("shell")
    assert YOLO_NOTE not in SkillRegistry(yolo=True).load_skill("memory")
    assert YOLO_NOTE not in SkillRegistry().load_skill("shell")
    for name in ("http", "research", "reference"):
        assert SkillRegistry(yolo=True).load_skill(name).endswith(YOLO_NOTE)
        assert "localhost" not in SkillRegistry().load_skill(name)

    project = tmp_path / "proj"
    granted = system_prompt(tmp_path, "MENU", now=now, project=project)
    assert granted.startswith("You are a helpful personal assistant.")
    assert f"Your project is {project}." in granted
    assert "write it to a file in the project" in granted
    assert "When the user asks for a program, script, or code change" in granted
    assert "Yolo mode" not in granted
    coding = system_prompt(
        tmp_path, "MENU", now=now, project=project, coding=True, rules="RULES"
    )
    assert coding.startswith("You are a coding agent working in a granted project.")
    assert "When the user asks for a program" not in coding
    assert "Read the project before you change it" in coding
    assert "Load the coding skill before you read or change source." in coding
    assert "Make the smallest change that does the job." in coding
    assert "Always confirm what you changed, including the paths." in coding
    assert "RULES" in coding
    assert coding.endswith("MENU")
    both = system_prompt(tmp_path, "MENU", now=now, yolo=True, project=project)
    assert f"or the project at {project}." in both
    loaded = SkillRegistry(project=project).load_skill("coding")
    assert loaded.endswith(project_note(project))
    assert project_note(project) not in SkillRegistry(project=project).load_skill(
        "memory"
    )
    shell = SkillRegistry(yolo=True, project=project).load_skill("shell")
    assert project_note(project) in shell
    assert f"or the project at {project}." in shell
    assert "are blocked" not in shell
    assert "`/bin/sh -c`" in shell


def test_system_prompt_includes_date_listing_and_memory_index(tmp_path):
    now = datetime(2026, 10, 6, 16, 30, tzinfo=UTC)

    empty = system_prompt(tmp_path, "MENU", now=now)
    assert "Today is Tuesday, October 06, 2026, 16:30 UTC." in empty
    assert f"Your workspace is {tmp_path}." in empty
    assert "The workspace is empty." in empty
    assert "No saved memories yet." in empty
    assert "Several skills can stay loaded at once" in empty
    assert "search snippets are not answers" in empty
    assert "load the coding skill and write it to a file" in empty
    assert "Use the python skill only to compute an answer." in empty
    assert empty.endswith("MENU")

    session = tmp_path / "workspaces" / "session"
    session.mkdir(parents=True)
    (session / "notes.md").write_text("n")
    (session / "downloads").mkdir()
    (session / "session.log").write_text("{}")
    (session / TRANSCRIPT_FILE).write_text("{}")
    (session / ".hidden").write_text("h")
    execute_tool("remember", {"key": "user name", "text": "Alex"}, session)

    filled = system_prompt(session, "MENU", now=now)
    assert "Workspace files: downloads/, notes.md" in filled
    assert "memory.md" not in filled.split("Saved memories:")[0]
    listing = filled.split("Workspace files:")[1].split("\n")[0]
    assert "session.log" not in listing
    assert "transcript.json" not in listing
    assert ".hidden" not in filled
    assert "Saved memories: user name." in filled


def test_workspace_listing_caps_the_number_of_names(tmp_path):
    for index in range(25):
        (tmp_path / f"f{index:02d}.txt").write_text("x")

    listing = tools.workspace_listing(tmp_path, limit=20)

    assert listing.startswith("Workspace files: f00.txt, ")
    assert listing.endswith(", and 5 more")


def test_session_prompt_refreshes_after_skill_load(tmp_path):
    session = agent.Session(workspace=tmp_path)
    assert "The workspace is empty." in session.messages[0]["content"]

    (tmp_path / "new.txt").write_text("x")
    session.skills.load_skill("tasks")
    session.sync_prompt()

    assert "Workspace files: new.txt" in session.messages[0]["content"]
    assert (
        "[loaded: write_plan, update_plan, show_plan]" in session.messages[0]["content"]
    )


def test_config_file_skips_comments_and_reads_quotes():
    text = """
# comment

api_key: "sk-quoted"
api_url: "http://127.0.0.1:11434/v1"
model: from-file
max_turns: 12
"""
    path = Path("harnh3x.yaml")
    values, servers = config.parse_config(text, path)
    resolved = config.resolve_settings(values, {}, path)

    assert servers == ()
    assert values["api_key"] == "sk-quoted"
    assert resolved["API_KEY"] == "sk-quoted"
    assert resolved["API_URL"] == "http://127.0.0.1:11434/v1"
    assert resolved["MODEL"] == "from-file"
    assert resolved["MAX_TURNS"] == 12
    assert resolved["COMPACT_AT"] == 0.5


def test_config_file_rejects_unknown_keys_and_bad_numbers():
    path = Path("demo.yaml")
    with pytest.raises(SystemExit, match=r"demo.yaml: unknown setting nope"):
        config.parse_config("#\nnope: 1\n", path)
    with pytest.raises(SystemExit, match=r"demo.yaml: max_turns: must be an integer"):
        values, _servers = config.parse_config("max_turns: lots\n", path)
        config.resolve_settings(values, {}, path)
    with pytest.raises(SystemExit, match=r"demo.yaml: compact_at: must be a number"):
        values, _servers = config.parse_config("compact_at: nope\n", path)
        config.resolve_settings(values, {}, path)


def test_environment_overrides_the_config_file():
    path = Path("harnh3x.yaml")
    values, _servers = config.parse_config(
        "api_key: file-key\nmodel: file-model\napi_url: http://file/v1\nmax_turns: 3\n",
        path,
    )
    resolved = config.resolve_settings(
        values,
        {"OPENAI_API_KEY": "env-key", "HARNH3X_MODEL": "env-model"},
        path,
    )

    assert resolved["API_KEY"] == "env-key"
    assert resolved["MODEL"] == "env-model"
    assert resolved["API_URL"] == "http://file/v1"
    assert resolved["MAX_TURNS"] == 3


def test_config_file_fills_settings_the_environment_omits(tmp_path):
    conf = tmp_path / "harnh3x.yaml"
    conf.write_text("api_url: http://file/v1\nmax_turns: 9\n")
    resolved = config.load_settings(
        {"HARNH3X_CONFIG": str(conf), "OPENAI_API_KEY": "   "}
    )

    assert resolved["API_URL"] == "http://file/v1"
    assert resolved["MAX_TURNS"] == 9
    assert resolved["API_KEY"] is None
    assert resolved["MODEL"] == "qwen3.5-128k"


def test_missing_default_config_keeps_defaults(tmp_path):
    resolved = config.load_settings({"XDG_CONFIG_HOME": str(tmp_path)})

    assert resolved["MODEL"] == "qwen3.5-128k"
    assert resolved["API_KEY"] is None
    assert resolved["MAX_TURNS"] == 45
    assert resolved["MCP_SERVERS"] == ()


def test_startup_requires_api_key_and_url(monkeypatch):
    cli = _load_cli()
    monkeypatch.setattr(config, "API_KEY", None)
    monkeypatch.setattr(config, "API_URL", None)

    def refuse_session(**kwargs):
        raise AssertionError("session started")

    monkeypatch.setattr(cli, "Session", refuse_session)

    with pytest.raises(SystemExit, match="Missing api_key") as missing:
        cli.main(["--yolo"])

    message = str(missing.value)
    assert "api_url (OPENAI_BASE_URL)" in message
    assert "the environment" in message


def test_startup_names_only_the_missing_setting(monkeypatch, tmp_path):
    conf = tmp_path / "harnh3x.yaml"
    monkeypatch.setenv("HARNH3X_CONFIG", str(conf))
    monkeypatch.setattr(config, "API_KEY", None)
    monkeypatch.setattr(config, "API_URL", "http://127.0.0.1:11434/v1")

    with pytest.raises(SystemExit, match="Missing api_key") as missing:
        config.require_api_settings()

    message = str(missing.value)
    assert "OPENAI_API_KEY" in message
    assert str(conf) in message
    assert "api_url" not in message


def test_explicit_config_path_must_exist(tmp_path):
    missing = tmp_path / "missing.conf"
    with pytest.raises(SystemExit, match="config file not found"):
        config.load_settings({"HARNH3X_CONFIG": str(missing)})


def test_import_applies_the_config_file(tmp_path):
    conf = tmp_path / "harnh3x.yaml"
    conf.write_text("model: from-file\napi_key: file-key\n")
    env = os.environ.copy()
    env["HARNH3X_CONFIG"] = str(conf)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    env.pop("HARNH3X_MODEL", None)
    env.pop("OPENAI_API_KEY", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import harnh3x.config as c; print(c.MODEL); print(c.API_KEY)",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["from-file", "file-key"]


def test_get_client_passes_resolved_credentials(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "file-key")
    monkeypatch.setattr(config, "API_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setattr(agent_client, "_client", None)
    seen = {}

    def fake_openai(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(agent_client, "OpenAI", fake_openai)

    client = agent.get_client()

    assert seen == {
        "api_key": "file-key",
        "base_url": "http://127.0.0.1:11434/v1",
    }
    assert agent.get_client() is client


def test_get_client_omits_unset_credentials(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", None)
    monkeypatch.setattr(config, "API_URL", None)
    monkeypatch.setattr(agent_client, "_client", None)
    seen = {}

    def fake_openai(**kwargs):
        seen["kwargs"] = kwargs
        return SimpleNamespace()

    monkeypatch.setattr(agent_client, "OpenAI", fake_openai)

    agent.get_client()

    assert seen["kwargs"] == {}


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
    assert session.context_tokens == 7
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


def test_every_tool_belongs_to_exactly_one_skill():
    assigned = [
        tool["function"]["name"]
        for skill in skills._CATALOG.values()
        for tool in skill.tools
    ]
    assert set(assigned) == set(tools.TOOL_FNS)
    assert set(assigned) == {tool["function"]["name"] for tool in tools.TOOLS}


_FAKE_MCP = str(Path(__file__).resolve().parent / "fake_mcp.py")


def _mcp_server(name="caido", **kwargs):
    env = dict(kwargs.pop("env", ()))
    return config.McpServer(
        name=name,
        command=kwargs.pop("command", sys.executable),
        args=kwargs.pop("args", (_FAKE_MCP,)),
        env=env,
        **kwargs,
    )


def test_yaml_without_mcp_leaves_the_menu_unchanged(tmp_path):
    path = tmp_path / "harnh3x.yaml"
    path.write_text("model: from-file\n")
    resolved = config.load_settings({"HARNH3X_CONFIG": str(path)})

    assert resolved["MODEL"] == "from-file"
    assert resolved["MCP_SERVERS"] == ()
    assert "mcp:" not in SkillRegistry().get_menu()


def test_leftover_conf_without_yaml_fails_startup(tmp_path):
    root = tmp_path / "xdg"
    folder = root / "harnh3x"
    folder.mkdir(parents=True)
    (folder / "harnh3x.conf").write_text("model = old\n")

    with pytest.raises(SystemExit, match="no longer read") as missing:
        config.load_settings({"XDG_CONFIG_HOME": str(root)})

    message = str(missing.value)
    assert str(folder / "harnh3x.conf") in message
    assert str(folder / "harnh3x.yaml") in message


def test_environment_overrides_scalars_and_keeps_mcp(tmp_path):
    path = tmp_path / "harnh3x.yaml"
    path.write_text(
        "model: file-model\n"
        "mcp:\n"
        "  caido:\n"
        "    command: caido-mcp-server\n"
        "    args: [serve]\n"
        "    env:\n"
        '      CAIDO_ACCESS_TOKEN: "sekret"\n'
    )
    resolved = config.load_settings(
        {"HARNH3X_CONFIG": str(path), "HARNH3X_MODEL": "env-model"}
    )

    assert resolved["MODEL"] == "env-model"
    servers = resolved["MCP_SERVERS"]
    assert servers[0].command == "caido-mcp-server"
    assert servers[0].args == ("serve",)
    assert servers[0].env["CAIDO_ACCESS_TOKEN"] == "sekret"


def test_mcp_server_block_names_the_file_and_key():
    path = Path("demo.yaml")
    text = "mcp:\n  caido:\n    args: [serve]\n"
    with pytest.raises(SystemExit, match=r"demo.yaml: mcp.caido: command is required"):
        config.parse_config(text, path)


def test_mcp_server_name_cannot_match_a_built_in_skill():
    with pytest.raises(RuntimeError, match="matches a built-in skill"):
        SkillRegistry(servers=(_mcp_server("file-ops"),))


def test_configured_mcp_server_stays_out_of_schemas_until_load(tmp_path):
    registry = SkillRegistry(servers=(_mcp_server(),))
    try:
        base_skills, base_tools = SkillRegistry().available_counts()
        assert registry.available_counts() == (base_skills + 1, base_tools)
        menu = registry.get_menu()
        assert "mcp:\n- caido: Browse and replay HTTP traffic" in menu
        assert menu.index("mcp:") < menu.index("web-testing:")
        assert _tool_names(registry.schemas()) == ["load_skill", "unload_skill"]

        loaded = registry.load_skill("caido")

        names = _tool_names(registry.schemas())
        assert names[0:2] == ["load_skill", "unload_skill"]
        assert "caido_list_requests" in names
        assert "caido_send_request" in names
        assert "caido_read_resource" in names
        assert registry.available_counts() == (base_skills + 1, base_tools + 8)
        assert "local Caido proxy" in loaded
        assert "Use HTTPQL." in loaded
        assert "caido://project: Current project" in loaded
        assert "[loaded: caido_list_requests" in registry.get_menu()
    finally:
        registry.close()


def test_mcp_tool_call_joins_text_and_reports_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "parent-secret")
    registry = SkillRegistry(
        servers=(_mcp_server(env={"CAIDO_ACCESS_TOKEN": "sekret", "FAKE_MCP": ""}),)
    )
    try:
        registry.load_skill("caido")
        client = registry._clients["caido"]
        assert client.argv == (sys.executable, _FAKE_MCP)
        assert "sekret" not in client.argv

        assert registry.execute("caido_list_requests", {}, tmp_path) == "alpha\nbeta"
        assert registry.execute("boom", {}, tmp_path) == "Error: nope"
        picture = registry.execute("picture", {}, tmp_path)
        assert picture == "Unsupported content type: image"
        wide = registry.execute("wide", {}, tmp_path)
        assert "truncated" in wide
        assert "bodyLimit" in wide
        assert (
            registry.execute(
                "caido_read_resource", {"uri": "caido://project"}, tmp_path
            )
            == "project-body"
        )
        echo = json.loads(registry.execute("echo_env", {}, tmp_path))
        assert echo["token"] == "sekret"
        assert echo["openai"] is None
        assert "sekret" not in echo["argv"]

        dead = registry.execute("die", {}, tmp_path)
        assert dead.startswith("Error:")
        assert registry.execute("caido_list_requests", {}, tmp_path) == "alpha\nbeta"
    finally:
        registry.close()


def test_mcp_include_filters_schemas_and_collision_fails_the_load(tmp_path):
    included = SkillRegistry(servers=(_mcp_server(include=("caido_list_requests",)),))
    try:
        included.load_skill("caido")
        names = _tool_names(included.schemas())
        assert "caido_list_requests" in names
        assert "caido_send_request" not in names
        assert "caido_read_resource" in names
    finally:
        included.close()

    colliding = SkillRegistry(servers=(_mcp_server(env={"FAKE_MCP": "collide"}),))
    try:
        failed = colliding.load_skill("caido")
        assert failed == (
            "Error: tool 'read_file' from 'caido' collides with an existing tool"
        )
        assert "caido" not in colliding.loaded_names()
    finally:
        colliding.close()


def test_other_mcp_servers_skip_the_caido_guide(tmp_path):
    registry = SkillRegistry(
        servers=(
            _mcp_server(
                "widgets",
                description="Widgets from the bench.",
                env={"FAKE_MCP": "no-resources"},
            ),
        )
    )
    try:
        loaded = registry.load_skill("widgets")
        assert "Widgets from the bench." in loaded
        assert "local Caido proxy" not in loaded
        assert "widgets_read_resource" not in _tool_names(registry.schemas())
        assert "caido_list_requests" in _tool_names(registry.schemas())
    finally:
        registry.close()


def test_unload_keeps_the_mcp_process_and_reset_stops_it(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MCP_SERVERS", (_mcp_server(),))
    session = agent.Session(workspace=tmp_path)
    registry = session.skills
    try:
        registry.load_skill("caido")
        client = registry._clients["caido"]
        assert client.alive()
        assert registry.unload_skill("caido") == "Unloaded skill 'caido'."
        assert client.alive()
        assert "caido_list_requests" not in _tool_names(registry.schemas())
        session.reset()
        assert not client.alive()
    finally:
        session.skills.close()


def test_missing_mcp_command_is_an_error(tmp_path):
    registry = SkillRegistry(
        servers=(_mcp_server(command="harnh3x-missing-mcp", args=()),)
    )
    try:
        failed = registry.load_skill("caido")
        assert failed == (
            "Error: command not found: harnh3x-missing-mcp "
            "Skill 'caido' is no longer available."
        )
        assert "caido" not in registry.loaded_names()
        assert "caido" not in registry.get_menu()
        assert registry.warnings == [
            ("caido", "command not found: harnh3x-missing-mcp")
        ]
        again = registry.load_skill("caido")
        assert again == "Error: Unknown skill 'caido'. Check the skill menu."
    finally:
        registry.close()


def test_unreachable_mcp_url_is_omitted_and_a_server_without_one_stays(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    closed = SkillRegistry(
        servers=(_mcp_server(env={"CAIDO_URL": f"http://127.0.0.1:{port}"}),)
    )
    try:
        menu = closed.get_menu()
        assert "mcp:" not in menu
        assert "caido" not in menu
        assert "caido_list_requests" not in _tool_names(closed.schemas())
        assert closed.warnings == [
            ("caido", f"not accepting connections: http://127.0.0.1:{port}")
        ]
        base_skills, base_tools = SkillRegistry().available_counts()
        assert closed.available_counts() == (base_skills, base_tools)
    finally:
        closed.close()

    plain = SkillRegistry(servers=(_mcp_server(description="Widgets."),))
    try:
        assert "- caido: Widgets." in plain.get_menu()
        assert plain.warnings == []
        assert plain.available_counts() == (base_skills + 1, base_tools)
    finally:
        plain.close()


def test_status_bar_warns_when_an_mcp_server_is_unavailable(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    session = agent.Session(workspace=tmp_path)
    session.skills.close()
    session.skills = SkillRegistry(servers=(_mcp_server(env={"CAIDO_URL": url}),))
    app = HarnessApp(session, lambda *args, **kwargs: "done", show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            content = app.query_one("#status").content
            status = str(content)
            skills, tools = session.skills.available_counts()
            assert f"Model: {session.model}" in status
            assert f"{skills} skills · {tools} tools" in status
            warning = "⚠ caido unavailable · Ctrl+W for warning details"
            assert warning in status
            red = [
                status[span.start : span.end]
                for span in content.spans
                if span.style == "bold ansi_bright_red"
            ]
            assert red == [warning]
            assert "caido" not in str(app.query_one("#banner").content)
            composer = app.query_one("#composer")
            status_bar = app.query_one("#status")
            assert composer.region.bottom == status_bar.region.y
            assert status_bar.region.bottom == app.size.height
            await pilot.press("ctrl+w")
            await pilot.pause()
            assert isinstance(app.screen, WarningScreen)
            detail = str(app.screen.query_one("#warning-box").content)
            assert "caido" in detail
            assert url in detail
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, WarningScreen)

    asyncio.run(drive())


class _HostProbe:
    def __init__(self, files=None, links=None, commands=None, sysctl=None):
        self.files = files or {}
        self.links = links or {}
        self.commands = commands or {}
        self.sysctl = sysctl or {}

    def read(self, path):
        return self.files.get(path)

    def list(self, path):
        prefix = path.rstrip("/") + "/"
        names = set()
        known = path in self.files or path in self.links
        for key in (*self.files, *self.links):
            if key.startswith(prefix):
                known = True
                rest = key[len(prefix) :]
                if rest:
                    names.add(rest.split("/")[0])
        if not known:
            return None
        return sorted(names)

    def exists(self, path):
        if path in self.files or path in self.links:
            return True
        prefix = path.rstrip("/") + "/"
        return any(key.startswith(prefix) for key in (*self.files, *self.links))

    def link(self, path):
        return self.links.get(path)

    def which(self, name):
        if name in self.commands:
            return name
        return None

    def run(self, argv, timeout):
        del timeout
        name = Path(argv[0]).name
        if name == "sysctl":
            key = argv[-1]
            if key not in self.sysctl:
                return None, "sysctl: unknown oid"
            return self.sysctl[key], None
        if name not in self.commands:
            return None, f"{name} is not available"
        result = self.commands[name]
        if isinstance(result, tuple):
            return result
        return result, None


_DARWIN_PROFILER = """
Hardware:

    Hardware Overview:

      Model Name: MacBook Pro
      Model Identifier: MacBookPro18,3
      Chip: Apple M4
      Total Number of Cores: 10 (4 performance and 6 efficiency)
      Memory: 24 GB

Memory:

    Memory:

      Size: 24 GB
      Type: LPDDR5
      Speed: 7500 MT/s

Storage:

    APPLE SSD AP0512Z:

      Medium Type: SSD
      Protocol: NVMe
      Capacity: 512 GB (549755813888 bytes)
      Mount Point: /

USB:

    USB 3.1 Bus:

      Host Controller Driver: AppleUSBXHCI

        USB Receiver:

          Product ID: 0xc52b
          Manufacturer: Logitech

Displays:

    Color LCD:

      Resolution: 3024 x 1964 Retina

PCI:

    Apple M4:

      Type: GPU

Power:

    Battery Information:

      Charge Information:

          State of Charge (%): 86

      Health Information:

          Cycle Count: 42

Network:

    Ethernet:

      Type: Ethernet
      BSD Device Name: en0

      Ethernet:

          MAC Address: aa:bb:cc:dd:ee:ff
"""

_DARWIN_SYSCTL = {
    "hw.model": "MacBookPro18,3",
    "hw.memsize": "25769803776",
    "hw.ncpu": "10",
    "hw.physicalcpu": "10",
    "hw.logicalcpu": "10",
    "kern.hostname": "mbp",
    "kern.boottime": "{ sec = 1700000000, usec = 0 }",
    "kern.osrelease": "24.1.0",
}

_DARWIN_SW = """ProductName:\tmacOS
ProductVersion:\t15.1
BuildVersion:\t24B83
"""


def _darwin_probe(profiler=_DARWIN_PROFILER):
    return _HostProbe(
        commands={"sw_vers": _DARWIN_SW, "system_profiler": profiler},
        sysctl=_DARWIN_SYSCTL,
    )


def _linux_files(chassis="3"):
    files = {
        "/etc/os-release": 'NAME="Ubuntu"\nVERSION="24.04.1 LTS (Noble Numbat)"\n',
        "/proc/sys/kernel/ostype": "Linux",
        "/proc/sys/kernel/osrelease": "6.8.0-45-generic",
        "/proc/sys/kernel/hostname": "devbox",
        "/proc/uptime": "90061.0 0",
        "/proc/meminfo": "MemTotal:       16777216 kB\n",
        "/proc/cpuinfo": "processor\t: 0\nmodel name\t: Intel placeholder\n",
        "/sys/class/dmi/id/sys_vendor": "Dell Inc.",
        "/sys/class/dmi/id/product_name": "XPS 15",
        "/sys/bus/usb/devices/1-1/manufacturer": "Logitech",
        "/sys/bus/usb/devices/1-1/product": "USB Receiver",
        "/sys/class/drm/card0-eDP-1/status": "connected",
        "/sys/class/power_supply/BAT0/type": "Battery",
        "/sys/class/power_supply/BAT0/capacity": "86",
        "/sys/class/power_supply/BAT0/status": "Discharging",
        "/sys/class/power_supply/BAT0/cycle_count": "42",
        "/sys/class/net/lo/address": "00:00:00:00:00:00",
        "/sys/class/net/docker0/address": "02:42:00:00:00:01",
        "/sys/class/net/enp0s31f6/address": "aa:bb:cc:dd:ee:ff",
        "/sys/class/net/enp0s31f6/operstate": "up",
    }
    if chassis is not None:
        files["/sys/class/dmi/id/chassis_type"] = chassis
    return files


_LINUX_LSBLK = json.dumps(
    {
        "blockdevices": [
            {
                "name": "nvme0n1",
                "size": 512110190592,
                "type": "disk",
                "model": "Samsung SSD 980",
                "tran": "nvme",
                "rota": False,
                "mountpoint": None,
                "children": [
                    {
                        "name": "nvme0n1p2",
                        "type": "part",
                        "mountpoint": "/",
                    }
                ],
            }
        ]
    }
)

_LINUX_LSCPU = """Architecture: x86_64
CPU(s): 8
Model name: Intel(R) Core(TM) i7-8550U CPU @ 1.80GHz
Core(s) per socket: 4
Socket(s): 1
"""

_LINUX_LSPCI = (
    '00:02.0 "VGA compatible controller" "Intel Corporation" "UHD Graphics"\n'
)


def _linux_probe(chassis="3"):
    return _HostProbe(
        files=_linux_files(chassis),
        links={"/sys/class/net/enp0s31f6/device/driver": "e1000e"},
        commands={
            "lsblk": _LINUX_LSBLK,
            "lscpu": _LINUX_LSCPU,
            "lspci": _LINUX_LSPCI,
            "dmidecode": (None, "dmidecode: Permission denied"),
        },
    )


def test_system_info_reads_a_mac(tmp_path):
    result = host_system.system_info(tmp_path, {}, probe=_darwin_probe())

    assert "name: macOS" in result
    assert "version: 15.1" in result
    assert "build: 24B83" in result
    assert "kernel: Darwin 24.1.0" in result
    assert "kind: laptop" in result
    assert "model: MacBook Pro" in result
    assert "identifier: MacBookPro18,3" in result
    assert "model: Apple M4" in result
    assert "architecture: arm64" in result
    assert "size: 24 GB" in result
    assert "speed: 7500 MT/s" in result
    assert "APPLE SSD AP0512Z" in result
    assert "NVMe" in result
    assert "USB: Logitech USB Receiver" in result
    assert "Display: Color LCD" in result
    assert "GPU: Apple M4" in result
    assert "Battery: 86%, 42 cycles" in result
    assert "en0" in result
    assert "aa:bb:cc:dd:ee:ff" in result
    assert "Notes" not in result


def test_system_info_reads_linux_hardware(tmp_path, monkeypatch):
    monkeypatch.setattr(host_system.sys, "platform", "linux")
    result = host_system.system_info(tmp_path, {}, probe=_linux_probe())

    assert "name: Ubuntu" in result
    assert "version: 24.04.1 LTS (Noble Numbat)" in result
    assert "kernel: Linux 6.8.0-45-generic" in result
    assert "kind: desktop" in result
    assert "vendor: Dell Inc." in result
    assert "model: XPS 15" in result
    assert "i7-8550U" in result
    assert "architecture: x86_64" in result
    assert "size: 16 GB" in result
    assert "Samsung SSD 980" in result
    assert "nvme" in result
    assert "mount /" in result
    assert "USB: Logitech USB Receiver" in result
    assert "Display: card0-eDP-1" in result
    assert "GPU: Intel Corporation UHD Graphics" in result
    assert "Battery: BAT0, 86%, Discharging, 42 cycles" in result
    assert "Network: enp0s31f6, e1000e, aa:bb:cc:dd:ee:ff, up" in result
    assert "docker0" not in result
    assert "dmidecode: Permission denied" in result


def test_system_info_maps_linux_chassis(tmp_path, monkeypatch):
    monkeypatch.setattr(host_system.sys, "platform", "linux")
    laptop = host_system.system_info(tmp_path, {}, probe=_linux_probe("8"))
    missing = host_system.system_info(tmp_path, {}, probe=_linux_probe(None))

    assert "kind: laptop" in laptop
    assert "kind: unknown" in missing
    assert not missing.startswith("Error:")


def test_system_info_keeps_sysctl_when_profiler_times_out(tmp_path):
    probe = _darwin_probe((None, "system_profiler timed out"))

    result = host_system.system_info(tmp_path, {}, probe=probe)

    assert "kind: laptop" in result
    assert "model: MacBookPro18,3" in result
    assert "size: 24 GB" in result
    assert "system_profiler timed out" in result
    assert not result.startswith("Error:")


def test_system_info_returns_one_section(tmp_path):
    result = host_system.system_info(
        tmp_path, {"sections": ["cpu"]}, probe=_darwin_probe()
    )

    assert result.startswith("CPU\n")
    assert "Apple M4" in result
    assert "Storage" not in result
    assert "APPLE SSD" not in result


def test_system_info_rejects_windows_and_unknown_sections(tmp_path, monkeypatch):
    monkeypatch.setattr(host_system.sys, "platform", "win32")

    windows = execute_tool("system_info", {}, tmp_path)
    monkeypatch.setattr(host_system.sys, "platform", "darwin")
    unknown = execute_tool("system_info", {"sections": ["disk"]}, tmp_path)

    assert windows == "Error: system_info supports Linux and macOS only"
    assert unknown == "Error: unknown section 'disk'"


def test_system_info_is_a_hardware_diagnosis_tool():
    names = [tool["function"]["name"] for tool in tools.TOOLS]
    schema = next(
        tool for tool in tools.TOOLS if tool["function"]["name"] == "system_info"
    )
    skill = skills._CATALOG["hardware-diagnosis"]
    assigned = [tool["function"]["name"] for tool in skill.tools]

    assert "system_info" in names
    assert "command" not in schema["function"]["parameters"]["properties"]
    assert "system_info" in assigned
