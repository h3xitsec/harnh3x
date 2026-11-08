"""The command line: flags, plain output, and the plain prompt."""

import re
import sys
from types import SimpleNamespace

import pytest
from helpers import _delta, _export_messages, _load_cli, _past_workspace, _unreachable

import harnh3x.agent.client as agent_client
from harnh3x import agent, config
from harnh3x.agent import ModelRef, ProviderModels
from harnh3x.commands import DETAILS_PLAIN
from harnh3x.config import MODEL, Provider
from harnh3x.tui import EXPORT_USAGE, NO_EXPORT, YOLO_PLAIN

_LOCAL = ProviderModels("local", ["alpha", "beta"])


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


def test_coding_option_prints_the_grant_and_rejects_a_missing_directory(
    monkeypatch, capsys, tmp_path
):
    cli = _load_cli()
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(cli, "run", lambda *args, **kwargs: "final answer")

    cli.main(["--coding", str(project), "fix the notes"])

    output = capsys.readouterr().out
    project_line = f"Project: {project.resolve()}"
    assert project_line in output
    assert output.index(project_line) < output.index("\nCoding\n")
    assert "Agent: final answer" in output

    with pytest.raises(SystemExit) as exc:
        cli.main(["--coding", str(tmp_path / "missing"), "fix the notes"])
    assert "existing directory" in str(exc.value)


def test_coding_option_requires_a_path_and_rejects_project():
    cli = _load_cli()
    with pytest.raises(SystemExit):
        cli.main(["--coding"])
    with pytest.raises(SystemExit):
        cli.main(["--project", "/tmp"])


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
    assert output.index("Turn 1") < output.index("🔧 load_skill")
    assert output.index("Turn 2") < output.index("### **Done**")
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
    monkeypatch.setattr(agent_client, "get_client", lambda provider=None: client)

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


def test_list_provider_models_keeps_order_and_each_providers_error(monkeypatch):
    _two_providers(monkeypatch)
    clients = {
        "routerly": SimpleNamespace(
            models=SimpleNamespace(
                list=lambda: [SimpleNamespace(id="qwen"), SimpleNamespace(id="gemma")]
            )
        ),
        "claude": SimpleNamespace(
            models=SimpleNamespace(
                list=lambda: (_ for _ in ()).throw(_unreachable("connection"))
            )
        ),
    }
    monkeypatch.setattr(
        agent_client, "get_client", lambda provider=None: clients[provider]
    )

    assert agent.list_provider_models() == [
        ProviderModels("routerly", ["gemma", "qwen"]),
        ProviderModels("claude", [], "Could not reach the API."),
    ]
    assert agent.list_provider_models("routerly") == [
        ProviderModels("routerly", ["gemma", "qwen"])
    ]


def test_configured_models_skip_the_models_endpoint(monkeypatch):
    azure = Provider(
        "azure", "openai", "http://azure/v1", "k", models=("gpt-5-mini", "gpt-5")
    )
    local = Provider("local", "openai", "http://local/v1", "k")
    monkeypatch.setattr(config, "PROVIDERS", (azure, local))
    listing = SimpleNamespace(
        models=SimpleNamespace(list=lambda: [SimpleNamespace(id="qwen")])
    )

    def client_for(provider=None):
        assert provider == "local", "azure lists its configured models"
        return listing

    monkeypatch.setattr(agent_client, "get_client", client_for)

    assert agent.list_provider_models() == [
        ProviderModels("azure", ["gpt-5-mini", "gpt-5"]),
        ProviderModels("local", ["qwen"]),
    ]


def test_model_option_selects_that_model(monkeypatch, capsys, tmp_path):
    cli = _load_cli()
    seen = {}

    def refuse_list(only=None):
        raise AssertionError("list_provider_models")

    monkeypatch.setattr(cli, "list_provider_models", refuse_list)
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
    assert seen["provider"] == "local"
    assert "Model: gemma4:12b" in capsys.readouterr().out


def test_configured_model_skips_the_chooser_when_it_is_listed(
    monkeypatch, capsys, tmp_path
):
    cli = _load_cli()
    seen = {}

    def refuse_prompt(prompt=""):
        raise AssertionError(prompt)

    monkeypatch.setattr(
        cli,
        "list_provider_models",
        lambda only=None: [ProviderModels("local", [MODEL, "other"])],
    )
    monkeypatch.setattr("builtins.input", refuse_prompt)
    monkeypatch.setattr(
        cli,
        "Session",
        lambda **kwargs: (
            seen.update(kwargs) or SimpleNamespace(workspace=tmp_path, **kwargs)
        ),
    )
    monkeypatch.setattr(cli, "run", lambda *args, **kwargs: "final answer")

    cli.main(["fix the notes"])

    assert seen["model"] == MODEL
    assert f"Model: {MODEL}" in capsys.readouterr().out


def test_missing_configured_model_prompts(monkeypatch, capsys, tmp_path):
    cli = _load_cli()
    seen = {}
    calls = []
    answers = iter(["0", "nope", "2"])

    def listed(only=None):
        calls.append(True)
        return [_LOCAL]

    monkeypatch.setattr(cli, "list_provider_models", listed)
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

    cli.main(["fix the notes"])

    assert calls == [True]
    assert seen["model"] == "beta"
    assert seen["provider"] == "local"
    output = capsys.readouterr().out
    assert f"{MODEL} is missing from local. Choose a model to continue." in output
    assert "1. alpha" in output
    assert "2. beta" in output
    assert "Enter a number from 1 to 2." in output
    assert "Model: beta" in output


def test_missing_configured_model_tells_the_textual_picker_why(monkeypatch):
    cli = _load_cli()
    seen = {}
    monkeypatch.setattr(cli, "list_provider_models", lambda only=None: [_LOCAL])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    def fake_pick(groups, reason=None):
        seen["groups"] = groups
        seen["reason"] = reason
        return ModelRef("local", "alpha")

    monkeypatch.setattr(cli, "pick_model", fake_pick)

    assert cli.resolve_model(None) == ModelRef("local", "alpha")
    assert seen["groups"] == [_LOCAL]
    assert seen["reason"] == (
        f"{MODEL} is missing from local. Choose a model to continue."
    )


def test_unreachable_model_list_keeps_the_configured_model(
    monkeypatch, capsys, tmp_path
):
    cli = _load_cli()
    seen = {}

    def unreachable(only=None):
        return [ProviderModels("local", [], "Could not reach the API.")]

    def refuse_prompt(prompt=""):
        raise AssertionError(prompt)

    monkeypatch.setattr(cli, "list_provider_models", unreachable)
    monkeypatch.setattr("builtins.input", refuse_prompt)
    monkeypatch.setattr(
        cli,
        "Session",
        lambda **kwargs: (
            seen.update(kwargs) or SimpleNamespace(workspace=tmp_path, **kwargs)
        ),
    )
    monkeypatch.setattr(cli, "run", lambda *args, **kwargs: "final answer")

    cli.main(["fix the notes"])

    assert seen["model"] == MODEL
    assert f"Model: {MODEL}" in capsys.readouterr().out


def test_model_flag_without_a_name_prompts(monkeypatch, capsys, tmp_path):
    cli = _load_cli()
    seen = {}
    answers = iter(["0", "nope", "2"])
    monkeypatch.setattr(cli, "list_provider_models", lambda only=None: [_LOCAL])
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
    assert "is missing from" not in output
    assert "1. alpha" in output
    assert "2. beta" in output
    assert "Enter a number from 1 to 2." in output
    assert "Model: beta" in output


def test_model_menu_exits_when_the_api_has_no_models(monkeypatch):
    cli = _load_cli()
    monkeypatch.setattr(
        cli, "list_provider_models", lambda only=None: [ProviderModels("local", [])]
    )

    with pytest.raises(SystemExit, match="No models available"):
        cli.main(["--model"])

    monkeypatch.setattr(
        cli,
        "list_provider_models",
        lambda only=None: [ProviderModels("local", [], "Could not reach the API.")],
    )
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--model"])
    assert str(stopped.value).splitlines() == [
        "No models available from the API.",
        "local: Could not reach the API.",
    ]


def _two_providers(monkeypatch):
    monkeypatch.setattr(
        config,
        "PROVIDERS",
        (
            Provider("routerly", "openai", "http://localhost:3000/v1", "k1"),
            Provider("claude", "anthropic", "https://api.anthropic.com", "k2"),
        ),
    )


def test_parse_model_ref_splits_only_on_a_configured_provider(monkeypatch):
    _two_providers(monkeypatch)

    assert agent.parse_model_ref("routerly/qwen3.5:9b") == ModelRef(
        "routerly", "qwen3.5:9b"
    )
    assert agent.parse_model_ref("claude/claude-sonnet") == ModelRef(
        "claude", "claude-sonnet"
    )
    assert agent.parse_model_ref("hf.co/unsloth/gpt-oss") == ModelRef(
        None, "hf.co/unsloth/gpt-oss"
    )
    assert agent.parse_model_ref("openai/gpt-4o") == ModelRef(None, "openai/gpt-4o")
    assert agent.parse_model_ref("routerly/") == ModelRef(None, "routerly/")
    assert agent.parse_model_ref("gemma") == ModelRef(None, "gemma")


def test_explicit_model_resolves_its_provider(monkeypatch):
    cli = _load_cli()
    _two_providers(monkeypatch)
    calls = []

    def listed(only=None):
        calls.append(only)
        return [
            ProviderModels("routerly", [], "Could not reach the API."),
            ProviderModels("claude", ["claude-sonnet", "shared"]),
        ]

    monkeypatch.setattr(cli, "list_provider_models", listed)

    assert cli.resolve_model("claude/opus") == ModelRef("claude", "opus")
    assert calls == []
    assert cli.resolve_model("shared") == ModelRef("claude", "shared")
    assert cli.resolve_model("unlisted") == ModelRef("routerly", "unlisted")
    assert calls == [None, None]


def test_configured_model_uses_the_first_provider_that_lists_it(monkeypatch):
    cli = _load_cli()
    _two_providers(monkeypatch)
    monkeypatch.setattr(cli, "MODEL", "shared")
    monkeypatch.setattr(
        cli,
        "list_provider_models",
        lambda only=None: [
            ProviderModels("routerly", ["shared"]),
            ProviderModels("claude", ["shared"]),
        ],
    )

    assert cli.resolve_model(None) == ModelRef("routerly", "shared")


def test_missing_qualified_default_lists_every_provider(monkeypatch, capsys):
    cli = _load_cli()
    _two_providers(monkeypatch)
    monkeypatch.setattr(cli, "MODEL", "claude/gone")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "2")
    calls = []
    groups = {
        "routerly": ProviderModels("routerly", ["qwen"]),
        "claude": ProviderModels("claude", ["claude-sonnet"]),
    }

    def listed(only=None):
        calls.append(only)
        return [groups[only]] if only else list(groups.values())

    monkeypatch.setattr(cli, "list_provider_models", listed)

    assert cli.resolve_model(None) == ModelRef("claude", "claude-sonnet")
    assert calls == ["claude", None]
    output = capsys.readouterr().out
    assert "gone is missing from claude. Choose a model to continue." in output
    assert output.splitlines()[1:] == [
        "Available models:",
        "  routerly",
        "    1. qwen",
        "  claude",
        "    2. claude-sonnet",
    ]


def test_missing_bare_default_names_every_provider(monkeypatch, capsys):
    cli = _load_cli()
    _two_providers(monkeypatch)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "1")
    monkeypatch.setattr(
        cli,
        "list_provider_models",
        lambda only=None: [
            ProviderModels("routerly", [], "Could not reach the API."),
            ProviderModels("claude", ["claude-sonnet"]),
        ],
    )

    assert cli.resolve_model(None) == ModelRef("claude", "claude-sonnet")
    output = capsys.readouterr().out
    assert (
        f"{MODEL} is missing from every configured provider. Choose a model to continue."
        in output
    )
    assert "  routerly\n    Could not reach the API.\n  claude\n" in output


def test_unreachable_providers_keep_a_qualified_default(monkeypatch):
    cli = _load_cli()
    _two_providers(monkeypatch)
    monkeypatch.setattr(cli, "MODEL", "claude/opus")
    monkeypatch.setattr(
        cli,
        "list_provider_models",
        lambda only=None: [ProviderModels("claude", [], "Could not reach the API.")],
    )

    assert cli.resolve_model(None) == ModelRef("claude", "opus")


def test_plain_output_names_the_provider_with_several(monkeypatch, capsys, tmp_path):
    cli = _load_cli()
    _two_providers(monkeypatch)
    monkeypatch.setattr(
        cli, "Session", lambda **kwargs: SimpleNamespace(workspace=tmp_path, **kwargs)
    )
    monkeypatch.setattr(cli, "run", lambda *args, **kwargs: "final answer")

    cli.main(["--model", "claude/opus", "fix the notes"])

    assert "Model: claude/opus" in capsys.readouterr().out


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


def test_module_main_calls_cli_main(monkeypatch):
    import runpy

    calls = []
    monkeypatch.setattr("harnh3x.cli.main", lambda: calls.append(True))

    runpy.run_module("harnh3x", run_name="__main__")

    assert calls == [True]
