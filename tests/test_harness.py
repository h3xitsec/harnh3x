import json
import uuid
from types import SimpleNamespace

import harness.agent as agent
import harness.tools as tools
from harness.config import MAX_TOOL_RESULT
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
    log_path = tmp_path / "log.txt"
    session = agent.Session(workspace=tmp_path, log_path=str(log_path))

    text = agent.run("read missing.txt", session, client=client)

    assert text == "Max turns reached."
    assert session.messages[-1] == {
        "role": "assistant",
        "content": "Max turns reached.",
    }
    assert session.messages[-2]["role"] == "tool"
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


def test_parse_page_fetches_url_instead_of_tool_name_path(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_assert_public_host", lambda host: None)
    monkeypatch.setattr(
        tools,
        "_http_get",
        lambda url: (200, "text/html", ARTICLE_HTML.encode()),
    )

    result = execute_tool(
        "parse_page",
        {
            "path": "fetch_url",
            "url": "https://thehackernews.com/2026/10/mi5-says-chinas-mss-funded-research.html",
        },
        tmp_path,
    )

    assert "title: City Council Approves Transit Plan" in result
    assert "harbor district" in result
    assert "url: https://thehackernews.com/" in result
    assert not (tmp_path / "fetch_url").exists()
    assert any((tmp_path / "downloads").iterdir())


def test_parse_page_extracts_article(tmp_path):
    (tmp_path / "article.html").write_text(ARTICLE_HTML)

    result = execute_tool("parse_page", {"path": "article.html"}, tmp_path)

    assert "title: City Council Approves Transit Plan" in result
    assert "harbor district" in result
    assert "author: Ada Lovelace" in result


def test_parse_page_errors_for_missing_directory_and_empty(tmp_path):
    assert "Error: not found" in execute_tool(
        "parse_page", {"path": "missing.html"}, tmp_path
    )

    (tmp_path / "pages").mkdir()
    assert "Error: is a directory" in execute_tool(
        "parse_page", {"path": "pages"}, tmp_path
    )

    (tmp_path / "empty.html").write_text("<html><head></head><body></body></html>")
    result = execute_tool("parse_page", {"path": "empty.html"}, tmp_path)
    assert result == "Error: no article text extracted"


def test_parse_page_truncates_long_article(tmp_path):
    paragraph = "The council voted to fund the harbor bus line. " * 400
    html = (
        "<html><head><title>Long Story</title></head><body><article>"
        f"<h1>Long Story</h1><p>{paragraph}</p></article></body></html>"
    )
    (tmp_path / "long.html").write_text(html)

    result = execute_tool("parse_page", {"path": "long.html"}, tmp_path)

    assert len(result) <= MAX_TOOL_RESULT
    assert result.endswith("chars omitted]")


def test_session_log_lives_in_the_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WORKSPACE", raising=False)

    session = agent.Session()

    assert session.log_path == str(session.workspace / "log.txt")
    assert session.workspace.parent == (tmp_path / "workspaces").resolve()


def test_session_defaults_to_a_unique_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WORKSPACE", raising=False)

    first = agent.Session(log_path=str(tmp_path / "a.txt"))
    second = agent.Session(log_path=str(tmp_path / "b.txt"))

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
    chosen = agent.Session(workspace=explicit, log_path=str(tmp_path / "log.txt"))
    assert chosen.workspace == explicit.resolve()
    assert explicit.is_dir()

    monkeypatch.chdir(tmp_path)
    from_env = tmp_path / "from-env"
    session = agent.Session(log_path=str(tmp_path / "log.txt"))
    assert session.workspace == from_env.resolve()
    assert from_env.is_dir()
    assert not (tmp_path / "workspaces").exists()
