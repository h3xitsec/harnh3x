import importlib.util
import json
import os
import re
import select
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import harness.agent as agent
import harness.skills as skills
import harness.tools as tools
from harness.config import MAX_TOOL_RESULT, MODEL
from harness.line_input import (
    Down,
    Editor,
    Ignore,
    Insert,
    Submit,
    ToggleThinking,
    Up,
    parse_keys,
    render,
)
from harness.skills import SkillRegistry
from harness.tools import _tool_result, execute_tool

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
    assert result.endswith("chars omitted]")


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


def test_fetch_url_rejects_non_html(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tools,
        "_http_get",
        lambda url: (200, "application/json", b"{}"),
    )

    result = execute_tool("fetch_url", {"url": "https://1.1.1.1/data"}, tmp_path)

    assert result.startswith("Error:")
    assert "unsupported content type" in result
    assert list(tmp_path.iterdir()) == []


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


def _tool_names(schemas):
    return [tool["function"]["name"] for tool in schemas]


def test_skill_menu_starts_unloaded():
    registry = SkillRegistry()
    menu = registry.get_menu()

    assert "file_ops:" in menu
    assert "shell:" in menu
    assert "news:" in menu
    assert "[loaded]" not in menu
    assert _tool_names(registry.schemas()) == ["load_skill", "unload_skill"]


def test_skill_load_unload_and_gated_execute(tmp_path):
    registry = SkillRegistry()

    unknown = registry.execute("load_skill", {"name": "nope"}, tmp_path)
    assert unknown == "Error: Unknown skill 'nope'. Check the skill menu."

    blocked = registry.execute("read_file", {"path": "a.txt"}, tmp_path)
    assert blocked == "Error: Tool 'read_file' not found. Is the skill loaded?"

    loaded = registry.load_skill("file_ops")
    assert loaded.startswith("Loaded skill 'file_ops' with 3 tools: ")
    assert "read_file" in loaded
    assert "Documentation:" in loaded
    assert "give the user the content of the file" in loaded
    assert registry.load_skill("file_ops") == "Skill 'file_ops' is already loaded."
    assert "[loaded: read_file, write_file, delete_file]" in registry.get_menu()

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
    assert file_ops_line.endswith("[loaded: read_file, write_file, delete_file]")
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
    assert cli.thinking_rewind(2, 4, prompt_gap=True) == 7
    assert cli.thinking_rewind(0, 4, prompt_gap=False) == 4
    assert cli.ReplyView().toggle(3, prompt_gap=True) == 3


def test_reply_lines_stay_short_of_the_terminal(monkeypatch):
    cli = _load_cli()
    monkeypatch.setattr(cli, "terminal_width", lambda: 20)
    view = cli.ReplyView()
    view.reasoning = "r" * 100
    view.answer = "a" * 80
    view.expanded = True
    plain = re.sub(r"\x1b\[[0-9;]*m", "", view.render())
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


def test_ctrl_t_toggles_thinking():
    events, rest = parse_keys(b"\x14")
    assert rest == b""
    assert events == [ToggleThinking()]
    events, rest = parse_keys(b"\x1b[116;5u")
    assert rest == b""
    assert events == [ToggleThinking()]
    events, rest = parse_keys(b"\x1b[116;5:3u")
    assert events == [Ignore()]


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


def test_model_menu_moves_with_arrows_and_accepts_a_number():
    cli = _load_cli()
    menu = cli.ModelMenu(["alpha", "beta", "gamma"])
    assert menu.handle(Down()) is None
    assert menu.handle(Down()) is None
    assert menu.handle(Down()) is None
    assert "> 3. gamma" in menu.render()
    assert menu.handle(Submit()) == "gamma"

    menu = cli.ModelMenu(["alpha", "beta", "gamma"])
    menu.handle(Up())
    menu.handle(Insert("2"))
    assert menu.selected == 1
    assert "Number: 2" in menu.render()
    menu.handle(Down())
    assert menu.digits == ""
    assert menu.handle(Submit()) == "gamma"

    menu = cli.ModelMenu(["alpha", "beta"])
    menu.handle(Insert("9"))
    assert menu.handle(Submit()) is None
    assert menu.error == "Enter a number from 1 to 2."
    menu.handle(Insert("1"))
    assert menu.handle(Submit()) == "alpha"


def test_model_menu_exits_when_the_api_has_no_models(monkeypatch):
    cli = _load_cli()
    monkeypatch.setattr(cli, "list_models", lambda: [])

    with pytest.raises(SystemExit, match="No models available"):
        cli.main(["--model"])


def _typed(payload: bytes) -> str:
    editor = Editor()
    events, rest = parse_keys(payload)
    assert rest == b""
    submitted = None
    for event in events:
        submitted = editor.handle(event)
    assert submitted is not None
    return submitted


def test_enter_submits_one_line():
    assert _typed(b"sadasd\r") == "sadasd"


def test_shift_enter_stays_in_one_request():
    assert _typed(b"sadasd\nsfdfdfde\nsdfdfdf\r") == "sadasd\nsfdfdfde\nsdfdfdf"
    assert _typed(b"sadasd\x1b[13;2usfdfdfde\x1b[13;2:1usdfdfdf\r") == (
        "sadasd\nsfdfdfde\nsdfdfdf"
    )
    assert _typed(b"sadasd\x1b[27;2;13~sfdfdfde\r") == "sadasd\nsfdfdfde"
    assert _typed(b"sadasd\r\n") == "sadasd"


def test_shift_enter_release_does_not_add_a_line():
    assert _typed(b"sadasd\x1b[13;2:3usfdfdfde\r") == "sadasdsfdfdfde"


def test_pasted_newlines_submit_once():
    assert _typed(b"\x1b[200~sadasd\r\nsfdfdfde\x1b[201~\r") == "sadasd\nsfdfdfde"


def test_backspace_removes_a_shift_enter_break():
    assert _typed(b"sadasd\x1b[13;2u\x7fsfdfdfde\r") == "sadasdsfdfdfde"


def test_partial_shift_enter_sequence_resumes():
    editor = Editor()
    events, rest = parse_keys(b"sadasd\x1b[13")
    assert rest == b"\x1b[13"
    for event in events:
        editor.handle(event)
    events, rest = parse_keys(rest + b";2usfdfdfde\r")
    assert rest == b""
    submitted = None
    for event in events:
        submitted = editor.handle(event)
    assert submitted == "sadasd\nsfdfdfde"


def test_render_keeps_shift_enter_lines_under_the_prompt():
    text = "sadasd\nsfdfdfde"
    frame = render(text, len(text), "You: ", 80)
    assert frame.output == "You: sadasd\n     sfdfdfde"
    assert (frame.cursor_row, frame.cursor_col) == (1, 5 + len("sfdfdfde"))


def test_render_cursor_tracks_terminal_wrap():
    frame = render("abcdef", 6, "You: ", 8)
    assert frame.output == "You: abcdef"
    assert (frame.end_row, frame.end_col) == (1, 3)

    exact = render("abc", 3, "You: ", 8)
    assert (exact.end_row, exact.end_col) == (1, 0)


def _read_until(fd: int, marker: bytes, timeout: float = 5) -> bytes:
    import time

    data = b""
    deadline = time.time() + timeout
    while marker not in data:
        if time.time() > deadline:
            raise AssertionError(f"missing {marker!r} in {data!r}")
        ready, _, _ = select.select([fd], [], [], 0.2)
        if not ready:
            continue
        chunk = os.read(fd, 4096)
        if chunk == b"":
            raise AssertionError(f"eof before {marker!r} in {data!r}")
        data += chunk
    return data


def _pty_request(keys: bytes) -> str:
    import pty

    master, slave = pty.openpty()
    src = str(Path(__file__).resolve().parents[1] / "src")
    script = f"""
import os, sys
os.dup2({slave}, 0)
os.dup2({slave}, 1)
from harness.line_input import read_request
sys.stdout.write("RESULT:" + read_request("You: ") + "\\nENDRESULT\\n")
sys.stdout.flush()
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        pass_fds=(slave,),
        env=env,
        close_fds=True,
    )
    os.close(slave)
    try:
        _read_until(master, b"You: ")
        os.write(master, keys)
        data = _read_until(master, b"ENDRESULT")
    finally:
        os.close(master)
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
    text = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    body = text.split(b"RESULT:", 1)[1]
    return body.split(b"\nENDRESULT", 1)[0].decode()


def test_tty_shift_enter_is_one_request():
    assert _pty_request(b"sadasd\x1b[13;2usfdfdfde\r") == "sadasd\nsfdfdfde"
    assert _pty_request(b"sadasd\nsfdfdfde\r") == "sadasd\nsfdfdfde"
    assert _pty_request(b"sadasd\r") == "sadasd"


def _pty_model(keys: bytes) -> str:
    import pty

    master, slave = pty.openpty()
    root = Path(__file__).resolve().parents[1]
    src = str(root / "src")
    cli = str(root / "src" / "harness.py")
    script = f"""
import importlib.util, os, sys
os.dup2({slave}, 0)
os.dup2({slave}, 1)
spec = importlib.util.spec_from_file_location("harness_cli", {cli!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
sys.stdout.write("RESULT:" + module.choose_model(["alpha", "beta", "gamma"]) + "\\nENDRESULT\\n")
sys.stdout.flush()
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        pass_fds=(slave,),
        env=env,
        close_fds=True,
    )
    os.close(slave)
    try:
        _read_until(master, b"Available models:")
        os.write(master, keys)
        data = _read_until(master, b"ENDRESULT")
    finally:
        os.close(master)
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
    text = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    body = text.split(b"RESULT:", 1)[1]
    return body.split(b"\nENDRESULT", 1)[0].decode(), data


def test_tty_model_menu_uses_arrows_or_a_number():
    choice, data = _pty_model(b"\x1b[B\x1b[B\r")
    assert choice == "gamma"
    tail = data.split(b"RESULT:", 1)[0].rsplit(b"\x1b[J", 1)[-1]
    assert b"Available models:" not in tail
    assert b"alpha" not in tail

    choice, _data = _pty_model(b"2\r")
    assert choice == "beta"
