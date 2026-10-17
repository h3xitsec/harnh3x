"""The command line: flags, plain output, and the plain prompt."""

import re
import sys
from types import SimpleNamespace

import pytest
from helpers import _delta, _export_messages, _load_cli, _past_workspace, _unreachable

import harnh3x.agent.client as agent_client
from harnh3x import agent
from harnh3x.commands import DETAILS_PLAIN
from harnh3x.config import MODEL
from harnh3x.tui import EXPORT_USAGE, NO_EXPORT, YOLO_PLAIN


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


def test_plain_details_and_new_commands(monkeypatch, capsys, tmp_path):
    cli = _load_cli()
    monkeypatch.setattr(cli, "_tty", lambda: False)
    session = SimpleNamespace(workspace=tmp_path / "first", model="m", messages=[])
    resets = []

    def reset():
        resets.append(True)
        session.workspace = tmp_path / "second"

    session.reset = reset
    monkeypatch.setattr(cli, "Session", lambda **kwargs: session)
    answers = iter(["/details", "/new", "/new extra", "/help", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    cli.main([])

    output = capsys.readouterr().out
    assert DETAILS_PLAIN in output
    assert resets == [True]
    assert f"Workspace: {tmp_path / 'second'}" in output
    assert "Usage: /new" in output
    assert "/details" in output
    assert "/new" in output


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
