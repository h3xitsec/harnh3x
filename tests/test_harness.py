import asyncio
import importlib.util
import io
import json
import os
import re
import sys
import urllib.parse
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import harness.agent as agent
import harness.skills as skills
import harness.tools as tools
from harness.config import (
    MAX_TOOL_RESULT,
    MODEL,
    PDF_MAX_PAGES,
    SEARCH_FILES_MAX,
    TRANSCRIPT_FILE,
)
from harness.prompt import system_prompt
from harness.skills import SkillRegistry
from harness.tools import _tool_result, execute_tool
from harness.tui import HarnessApp, ModelApp, ResumeOptions, ResumeScreen, ReviewApp

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
    monkeypatch.setattr(agent, "MAX_TURNS", 1)

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
    handler = tools._CheckedRedirectHandler()
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

    monkeypatch.setattr(tools, "_http_get", fake_get)
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
        tools,
        "_http_get",
        lambda url: (200, "image/png", b"\x89PNG"),
    )

    result = execute_tool("fetch_url", {"url": "https://1.1.1.1/pic"}, tmp_path)

    assert result.startswith("Error:")
    assert "unsupported content type" in result
    assert list(tmp_path.iterdir()) == []


def test_fetch_url_saves_json_and_pdf_with_matching_extensions(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tools, "_http_get", lambda url: (200, "application/json", b'{"a": 1}')
    )
    as_json = execute_tool("fetch_url", {"url": "https://1.1.1.1/data"}, tmp_path)
    assert "content_type: application/json" in as_json
    assert "path: downloads/1.1.1.1-" in as_json
    assert as_json.rstrip().endswith(".json")

    monkeypatch.setattr(
        tools, "_http_get", lambda url: (200, "application/pdf", b"%PDF-1.4")
    )
    as_pdf = execute_tool("fetch_url", {"url": "https://1.1.1.1/paper"}, tmp_path)
    assert as_pdf.rstrip().endswith(".pdf")


def test_parse_html_reads_saved_html_and_does_not_download(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_get(url):
        calls["n"] += 1
        return 200, "text/html", ARTICLE_HTML.encode()

    monkeypatch.setattr(tools, "_http_get", fake_get)
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
    monkeypatch.setattr(tools, "_search_html", lambda query, recency=None: page)

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
        "\n" + tools.SEARCH_HINT
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
        tools, "_search_html", lambda query, recency=None: _search_page(rows)
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

    monkeypatch.setattr(tools, "_search_html", fail)
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
        tools,
        "_search_html",
        lambda query, recency=None: _search_page(
            [("Secret", _ddg_href("http://127.0.0.1/secret"), "hidden")]
        ),
    )
    private_only = execute_tool("web_search", {"query": "transit"}, tmp_path)
    assert private_only == "Error: no results"

    monkeypatch.setattr(
        tools, "_search_html", lambda query, recency=None: "<html><body></body></html>"
    )
    empty = execute_tool("web_search", {"query": "transit"}, tmp_path)
    assert empty == "Error: no results"


def test_web_search_site_and_time_filters(tmp_path, monkeypatch):
    seen = []

    def fake_search(query, recency=None):
        seen.append((query, recency))
        return _search_page([("Alpha", "https://1.1.1.1/alpha", "snippet")])

    monkeypatch.setattr(tools, "_search_html", fake_search)

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
        tools,
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


def test_session_reset_starts_a_new_conversation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WORKSPACE", str(tmp_path / "pinned"))
    session = agent.Session(workspace=tmp_path / "first", model="custom-model")
    previous = session.workspace
    session.messages.append({"role": "user", "content": "remember this"})
    session.skills.load_skill("file_ops")

    session.reset()

    assert session.workspace != previous
    assert session.workspace.parent == (tmp_path / "workspaces").resolve()
    assert session.workspace.is_dir()
    assert previous.is_dir()
    assert str(session.workspace) in session.messages[0]["content"]
    assert session.model == "custom-model"
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
                            "arguments": '{"name": "file_ops"}',
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

    saved = json.loads((tmp_path / TRANSCRIPT_FILE).read_text(encoding="utf-8"))
    assert saved["model"] == "custom-model"
    assert saved["messages"][-1]["reasoning"] == thinking
    assert not (tmp_path / f".{TRANSCRIPT_FILE}.tmp").exists()
    events = [
        json.loads(line) for line in (tmp_path / "session.log").read_text().splitlines()
    ]
    logged = next(
        event["message"]["reasoning"] for event in events if event["event"] == "model"
    )
    assert logged == "r" * 500 + "...[truncated]"


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
                        "arguments": '{"name": "file_ops"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "loaded file_ops"},
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
        "loaded file_ops",
        "loaded shell",
        "unloaded shell",
        "notes",
    ]
    assert session.messages[-3]["reasoning"] == "look at the notes"


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
        "file_ops",
        "shell",
        "python",
        "research",
        "reference",
        "api",
        "documents",
        "data",
        "memory",
        "tasks",
    ):
        assert f"- {name}:" in menu
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

    loaded = registry.load_skill("file_ops")
    assert loaded.startswith("Loaded skill 'file_ops' with 8 tools: ")
    assert "read_file" in loaded
    assert "Documentation:" in loaded
    assert "give the user the content of the file" in loaded
    assert registry.load_skill("file_ops") == "Skill 'file_ops' is already loaded."
    assert FILE_OPS_LOADED in registry.get_menu()

    (tmp_path / "a.txt").write_text("hi")
    assert registry.execute("read_file", {"path": "a.txt"}, tmp_path) == "hi"

    assert registry.unload_skill("file_ops") == "Unloaded skill 'file_ops'."
    assert registry.unload_skill("file_ops") == "Skill 'file_ops' is not loaded."
    assert "[loaded]" not in registry.get_menu()


def test_skill_manifest_requires_tools_entries():
    with pytest.raises(RuntimeError, match="missing from TOOLS"):
        skills._tool_schemas(("read_file", "not_a_tool"), tools.TOOLS)


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
                            arguments='{"name": "file_ops"}',
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
        if line.startswith("- file_ops:")
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
    client = _tool_client("load_skill", '{"name": "file_ops"}', "loaded")

    text = agent.run("load file ops", session, client=client)

    assert text == "loaded"
    assert "load_skill" in capsys.readouterr().out


def test_show_tools_can_hide_tool_call_output(tmp_path, capsys):
    session = agent.Session(workspace=tmp_path)
    client = _tool_client("load_skill", '{"name": "file_ops"}', "loaded")

    text = agent.run("load file ops", session, client=client, show_tools=False)

    assert text == "loaded"
    assert capsys.readouterr().out == ""


def test_on_tool_receives_the_call_instead_of_printing(tmp_path, capsys):
    session = agent.Session(workspace=tmp_path)
    client = _tool_client("load_skill", '{"name": "file_ops"}', "loaded")
    seen = []

    text = agent.run(
        "load file ops",
        session,
        client=client,
        on_tool=lambda name, args: seen.append((name, args)),
    )

    assert text == "loaded"
    assert seen == [("load_skill", {"name": "file_ops"})]
    assert capsys.readouterr().out == ""


def _load_cli():
    path = Path(__file__).resolve().parents[1] / "src" / "harness.py"
    spec = importlib.util.spec_from_file_location("harness_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    answers = iter(["/nope", "/resume", "1", "exit"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    cli.main([])

    output = capsys.readouterr().out
    assert "Unknown command: /nope" in output
    assert "You: fix the notes" in output
    assert "🔧 load_skill({'name': 'file_ops'})" in output
    assert "### **Done**" in output
    assert "alpha" not in output
    assert "delta" in output
    assert f"Workspace: {past.resolve()}" in output
    assert "saved-model" in output


def test_thinking_panel_keeps_the_tail_until_expanded():
    cli = _load_cli()
    reasoning = "\n".join(["alpha", "beta", "gamma", "delta"])
    collapsed = cli.format_thinking(reasoning, expanded=False, width=80, hold=False)
    assert "alpha" not in collapsed
    assert "beta" in collapsed
    assert "delta" in collapsed
    assert "ctrl+t to expand" in collapsed

    expanded = cli.format_thinking(reasoning, expanded=True, width=80, hold=True)
    assert expanded.index("alpha") < expanded.index("delta")
    assert "ctrl+t to collapse" in expanded
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
    assert "ctrl+t to expand" in output
    assert "Agent: final answer" in output


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


def test_model_option_selects_that_model(monkeypatch, capsys, tmp_path):
    cli = _load_cli()
    seen = {}
    monkeypatch.setattr(
        cli,
        "Session",
        lambda **kwargs: seen.update(kwargs)
        or SimpleNamespace(workspace=tmp_path, **kwargs),
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
        lambda **kwargs: seen.update(kwargs)
        or SimpleNamespace(workspace=tmp_path, **kwargs),
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
    monkeypatch.setattr(cli, "list_models", lambda: [])

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


def test_ctrl_q_quits_the_app():
    session = SimpleNamespace(workspace="ws", model="m", messages=[])
    app = HarnessApp(session, lambda *args, **kwargs: "", show_tools=True)

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            assert not app._exit
            await pilot.press("ctrl+q")
            assert app._exit

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
                lambda: "### **Done**" in app.transcript_plain()
                and "fix the notes" in app.transcript_plain(),
            )
            thinking = app.query_one("Thinking")
            assert "alpha" not in thinking.plain
            assert "delta" in thinking.plain
            assert app.query_one("MarkdownH3")
            banner = str(app.query_one("#banner").content)
            assert f"Workspace: {past.resolve()}" in banner
            assert "Model: saved-model" in banner
            captured["plain"] = app.transcript_plain()
            await pilot.press(*"next", "enter")
            await _wait_for(pilot, lambda: bool(seen))

    asyncio.run(drive())
    assert "🔧 load_skill({'name': 'file_ops'})" in captured["plain"]
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


def test_ctrl_t_reveals_the_head_of_the_thinking_text():
    session = SimpleNamespace(workspace="ws", model="m", messages=[])
    app = ReviewApp(
        session,
        "fix the notes",
        [],
        "alpha\nbeta\ngamma\ndelta",
        "final answer",
    )

    async def drive():
        async with app.run_test(size=(80, 24)) as pilot:
            thinking = app.query_one("Thinking")
            assert "alpha" not in thinking.plain
            assert "delta" in thinking.plain
            await pilot.press("ctrl+t")
            assert "alpha" in thinking.plain

    asyncio.run(drive())


def test_tool_callback_line_shows_in_the_transcript():
    session = SimpleNamespace(workspace="ws", model="m", messages=[])

    def fake_run(message, session, show_tools=True, on_tool=None):
        on_tool("load_skill", {"name": "file_ops"})
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
    assert "🔧 load_skill({'name': 'file_ops'})" in captured["plain"]


def test_agent_answer_renders_markdown():
    answer = "\n".join(
        [
            "### **Ransomware**",
            "- **Australia**: 18%",
            "",
            "| Date | Event |",
            "|------|-------|",
            "| Sep 2019 | First ABCD |",
        ]
    )
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


# --- file_ops additions ---------------------------------------------------


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


def test_shell_timeout_is_capped(tmp_path, monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["timeout"] = kwargs["timeout"]
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(tools.subprocess, "run", fake_run)

    execute_tool("run_shell", {"command": "true", "timeout": 10_000}, tmp_path)
    assert seen["timeout"] == tools.SHELL_MAX_TIMEOUT

    execute_tool("run_shell", {"command": "true"}, tmp_path)
    assert seen["timeout"] == tools.SHELL_TIMEOUT


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


def test_run_python_times_out(tmp_path):
    result = execute_tool(
        "run_python",
        {"code": "import time\ntime.sleep(5)", "timeout": 1},
        tmp_path,
    )
    assert "exit_code: 124" in result
    assert "timed out after 1s" in result


# --- research additions ---------------------------------------------------


def test_read_url_fetches_extracts_and_saves_text(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tools,
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
    monkeypatch.setattr(tools, "_http_get", lambda url: (200, "text/plain", body))

    result = execute_tool("read_url", {"url": "https://1.1.1.1/notes"}, tmp_path)

    assert len(result) <= MAX_TOOL_RESULT
    assert "full text is in downloads/1.1.1.1-" in result
    assert "use read_file with offset to continue" in result
    text_file = re.search(r"text_file: (\S+)", result).group(1)
    assert text_file.endswith(".txt")
    assert (tmp_path / text_file).read_text() == body.decode().strip()


def test_read_url_reads_a_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tools,
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
        tools, "_http_get", lambda url: (200, "application/rss+xml", RSS_FEED)
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
        tools, "_http_get", lambda url: (200, "application/atom+xml", ATOM_FEED)
    )
    atom = execute_tool("read_feed", {"url": "https://1.1.1.1/atom"}, tmp_path)
    assert "feed: Atom Blog" in atom
    assert "link: https://1.1.1.1/one" in atom
    assert "date: 2026-10-06T10:00:00Z" in atom
    assert "summary: First summary" in atom


def test_read_feed_rejects_bad_xml_and_entities(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tools,
        "_http_get",
        lambda url: (200, "text/xml", b"<rss><channel></channel></rss>"),
    )
    assert execute_tool("read_feed", {"url": "https://1.1.1.1/feed"}, tmp_path) == (
        "Error: no feed entries found"
    )

    monkeypatch.setattr(
        tools, "_http_get", lambda url: (200, "text/xml", b"<rss><item>")
    )
    assert execute_tool(
        "read_feed", {"url": "https://1.1.1.1/feed"}, tmp_path
    ).startswith("Error: invalid XML")

    bomb = b'<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "y">]><rss><channel><item><title>&x;</title></item></channel></rss>'
    monkeypatch.setattr(tools, "_http_get", lambda url: (200, "text/xml", bomb))
    assert "DOCTYPE" in execute_tool(
        "read_feed", {"url": "https://1.1.1.1/feed"}, tmp_path
    )

    assert "blocked" in execute_tool(
        "read_feed", {"url": "http://127.0.0.1/feed"}, tmp_path
    )


# --- reference and api ----------------------------------------------------


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
    monkeypatch.setattr(tools, "_http_get", fake_get)

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
    fake_get, seen = _json_get(
        [("https://en.wikipedia.org/api/rest_v1/page/summary/Ada_Lovelace", payload)]
    )
    monkeypatch.setattr(tools, "_http_get", fake_get)

    result = execute_tool("wikipedia_summary", {"title": "Ada Lovelace"}, tmp_path)
    assert result == (
        "title: Ada Lovelace\n"
        "description: English mathematician\n"
        "url: https://en.wikipedia.org/wiki/Ada_Lovelace\n"
        "summary:\nAda Lovelace was an English mathematician."
    )

    def missing(url):
        raise ValueError("http status 404")

    monkeypatch.setattr(tools, "_http_get", missing)
    assert execute_tool("wikipedia_summary", {"title": "Nope Nope"}, tmp_path) == (
        "Error: no Wikipedia article named 'Nope Nope'"
    )
    assert execute_tool("wikipedia_summary", {}, tmp_path) == "Error: title is required"


def test_http_get_json_adds_params_and_rejects_non_json(tmp_path, monkeypatch):
    fake_get, seen = _json_get([("https://1.1.1.1/api", {"temp": 21.5, "ok": True})])
    monkeypatch.setattr(tools, "_http_get", fake_get)

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
        tools, "_http_get", lambda url: (200, "text/html", b"<html></html>")
    )
    html = execute_tool("http_get_json", {"url": "https://1.1.1.1/page"}, tmp_path)
    assert html.startswith("Error: unsupported content type")

    monkeypatch.setattr(
        tools, "_http_get", lambda url: (200, "application/json", b"{nope")
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
        "remember", {"key": "project", "text": "# harness\nagent loop"}, tmp_path
    )
    updated = execute_tool(
        "remember", {"key": "user timezone", "text": "America/Toronto"}, tmp_path
    )
    assert updated == "Updated 'user timezone'. 2 memories stored in memory.md."

    stored = (tmp_path / "memory.md").read_text()
    assert stored.startswith(
        "# Memory\n\n## user timezone\nAmerica/Toronto\n\n## project\nharness\nagent loop\n"
    )
    assert tools.memory_keys(tmp_path) == ["user timezone", "project"]

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
        tmp_path / "plan.md"
    ).read_text() == "# Plan\n\n1. [ ] search the web\n2. [ ] read two pages\n3. [ ] answer\n"

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


def test_system_prompt_includes_date_listing_and_memory_index(tmp_path):
    now = datetime(2026, 10, 6, 16, 30, tzinfo=timezone.utc)

    empty = system_prompt(tmp_path, "MENU", now=now)
    assert "Today is Tuesday, October 06, 2026, 16:30 UTC." in empty
    assert f"Your workspace is {tmp_path}." in empty
    assert "The workspace is empty." in empty
    assert "No saved memories yet." in empty
    assert "Several skills can stay loaded at once" in empty
    assert "search snippets are not answers" in empty
    assert empty.endswith("MENU")

    (tmp_path / "notes.md").write_text("n")
    (tmp_path / "downloads").mkdir()
    (tmp_path / "session.log").write_text("{}")
    (tmp_path / TRANSCRIPT_FILE).write_text("{}")
    (tmp_path / ".hidden").write_text("h")
    execute_tool("remember", {"key": "user name", "text": "Alex"}, tmp_path)

    filled = system_prompt(tmp_path, "MENU", now=now)
    assert "Workspace files: downloads/, memory.md, notes.md" in filled
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


def test_every_tool_belongs_to_exactly_one_skill():
    assigned = [name for names in skills.SKILL_TOOLS.values() for name in names]
    assert sorted(assigned) == sorted(set(assigned))
    assert set(assigned) == set(tools.TOOL_FNS)
    assert set(assigned) == {tool["function"]["name"] for tool in tools.TOOLS}
