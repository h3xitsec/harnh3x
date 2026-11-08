"""Settings resolution from YAML, environment, and .env."""

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers import _load_cli, _mcp_server

import harnh3x.agent.client as agent_client
from harnh3x import agent, config
from harnh3x.skills import SkillRegistry


def _resolve(text, path=Path("demo.yaml"), environ=None):
    values, _servers, _providers = config.parse_config(text, path)
    return config.resolve_settings(values, environ or {}, path)


def test_config_file_skips_comments_and_reads_quotes():
    text = """
# comment

providers:
  local:
    type: openai
    api_key: "sk-quoted"
    api_url: "http://127.0.0.1:11434/v1"
model: from-file
max_turns: 12
"""
    path = Path("harnh3x.yaml")
    values, servers, providers = config.parse_config(text, path)
    resolved = config.resolve_settings(values, {}, path)

    assert servers == ()
    assert providers.entries == (
        config.Provider("local", "openai", "http://127.0.0.1:11434/v1", "sk-quoted"),
    )
    assert providers.warnings == ()
    assert resolved["MODEL"] == "from-file"
    assert resolved["MAX_TURNS"] == 12
    assert resolved["COMPACT_AT"] == 0.5


def test_config_file_rejects_unknown_keys_and_bad_numbers():
    path = Path("demo.yaml")
    with pytest.raises(SystemExit, match=r"demo.yaml: unknown setting nope"):
        config.parse_config("#\nnope: 1\n", path)
    for key in ("api_key", "api_url"):
        with pytest.raises(SystemExit, match=rf"demo.yaml: unknown setting {key}"):
            config.parse_config(f"{key}: leftover\n", path)
    with pytest.raises(SystemExit, match=r"demo.yaml: max_turns: must be an integer"):
        _resolve("max_turns: lots\n", path)
    with pytest.raises(SystemExit, match=r"demo.yaml: compact_at: must be a number"):
        _resolve("compact_at: nope\n", path)


def test_providers_keep_file_order_and_default_the_anthropic_url():
    text = (
        "providers:\n"
        "  routerly:\n"
        "    type: openai\n"
        "    api_url: http://localhost:3000/v1\n"
        "    api_key: sk-rt\n"
        "  claude:\n"
        "    type: anthropic\n"
        "    api_key: sk-ant\n"
        "  litellm:\n"
        "    type: openai\n"
        "    api_url: http://localhost:4000/v1\n"
        "    api_key: sk-lite\n"
    )
    _values, _servers, providers = config.parse_config(text, Path("demo.yaml"))

    assert [entry.name for entry in providers.entries] == [
        "routerly",
        "claude",
        "litellm",
    ]
    assert providers.entries[1] == config.Provider(
        "claude", "anthropic", "https://api.anthropic.com", "sk-ant"
    )


def test_provider_models_keep_their_order_without_duplicates():
    text = (
        "providers:\n"
        "  azure:\n"
        "    type: openai\n"
        "    models:\n"
        "      - gpt-5-mini\n"
        "      - ' gpt-5 '\n"
        "      - gpt-5-mini\n"
        "    api_url: https://demo.openai.azure.com/openai/v1\n"
        "    api_key: sk-az\n"
        "  local:\n"
        "    type: openai\n"
        "    models: null\n"
        "    api_url: http://localhost:11434/v1\n"
        "    api_key: sk-local\n"
    )
    _values, _servers, providers = config.parse_config(text, Path("demo.yaml"))

    assert providers.entries[0].models == ("gpt-5-mini", "gpt-5")
    assert providers.entries[1].models is None


def test_provider_without_a_valid_type_is_skipped_with_a_warning():
    text = (
        "providers:\n"
        "  litellm:\n"
        "    api_url: http://localhost:4000/v1\n"
        "    api_key: sk-lite\n"
        "  typo:\n"
        "    type: antropic\n"
        "    api_key: sk-ant\n"
        "  claude:\n"
        "    type: anthropic\n"
        "    api_key: sk-ant\n"
    )
    _values, _servers, providers = config.parse_config(text, Path("demo.yaml"))

    assert [entry.name for entry in providers.entries] == ["claude"]
    assert providers.warnings == (
        (
            "providers.litellm",
            "type is required (openai or anthropic); provider skipped",
        ),
        (
            "providers.typo",
            "type antropic is not openai or anthropic; provider skipped",
        ),
    )


def test_provider_block_rejects_bad_shapes():
    path = Path("demo.yaml")
    cases = (
        ("providers: [a]\n", r"demo.yaml: providers: expected a mapping"),
        (
            "providers:\n  Bad:\n    type: openai\n",
            r"demo.yaml: providers\.Bad: invalid provider name",
        ),
        (
            "providers:\n  local: 1\n",
            r"demo.yaml: providers\.local: expected a mapping",
        ),
        (
            "providers:\n  local:\n    type: openai\n    model: x\n",
            r"demo.yaml: providers\.local: unknown setting model",
        ),
        (
            "providers:\n  local:\n    type: openai\n    api_url: http://x/v1\n",
            r"demo.yaml: providers\.local: api_key is required",
        ),
        (
            "providers:\n  local:\n    type: openai\n    api_key: k\n",
            r"demo.yaml: providers\.local: api_url is required",
        ),
        (
            "providers:\n  claude:\n    type: anthropic\n",
            r"demo.yaml: providers\.claude: api_key is required",
        ),
        (
            "providers:\n  claude:\n    type: anthropic\n    api_key: 5\n",
            r"demo.yaml: providers\.claude: api_key must be a non-empty string",
        ),
    )
    claude = "providers:\n  claude:\n    type: anthropic\n    api_key: k\n"
    models_error = r"demo.yaml: providers\.claude: models must be a non-empty list"
    for models in ("gpt-5", "[]", "[gpt-5, 3]", "['gpt-5', ' ']"):
        cases += ((f"{claude}    models: {models}\n", models_error),)
    for text, match in cases:
        with pytest.raises(SystemExit, match=match):
            config.parse_config(text, path)


@pytest.mark.parametrize(
    "key", ["openai_api_key", "openai_api_url", "anthropic_api_key", "provider"]
)
def test_removed_provider_settings_stop_startup(key):
    with pytest.raises(SystemExit) as stopped:
        config.parse_config(f"{key}: leftover\n", Path("demo.yaml"))

    message = str(stopped.value)
    assert message.startswith(f"demo.yaml: {key} is no longer read.")
    assert "providers:" in message


@pytest.mark.parametrize(
    "key",
    [
        "max_tokens",
        "max_turns",
        "subagent_max_turns",
        "max_tool_result",
        "fetch_timeout",
        "fetch_max_bytes",
        "search_max_results",
        "feed_max_items",
        "shell_timeout",
        "shell_max_timeout",
        "shell_max_output",
        "list_files_max",
        "search_files_max",
        "pdf_max_pages",
        "preview_rows",
    ],
)
@pytest.mark.parametrize("raw", ["0", "-1"])
def test_counts_and_limits_must_be_positive(key, raw):
    path = Path("demo.yaml")
    with pytest.raises(
        SystemExit, match=rf"demo.yaml: {key}: must be a positive integer"
    ):
        _resolve(f"{key}: {raw}\n", path)
    env = "HARNH3X_" + key.upper()
    with pytest.raises(SystemExit, match=rf"{env}: must be a positive integer"):
        config.resolve_settings({}, {env: raw}, path)


def test_range_checked_settings():
    path = Path("demo.yaml")

    def resolve(text):
        return _resolve(text, path)

    assert resolve("fetch_max_redirects: 0\n")["FETCH_MAX_REDIRECTS"] == 0
    with pytest.raises(
        SystemExit,
        match=r"demo.yaml: fetch_max_redirects: must be zero or a positive integer",
    ):
        resolve("fetch_max_redirects: -1\n")

    assert resolve("compact_at: 1\n")["COMPACT_AT"] == 1.0
    assert resolve("compact_at: 0.25\n")["COMPACT_AT"] == 0.25
    for raw in ("0", "-0.5", "1.5"):
        with pytest.raises(
            SystemExit,
            match=r"demo.yaml: compact_at: must be greater than 0 and at most 1",
        ):
            resolve(f"compact_at: {raw}\n")
    with pytest.raises(
        SystemExit, match=r"HARNH3X_COMPACT_AT: must be greater than 0 and at most 1"
    ):
        config.resolve_settings({}, {"HARNH3X_COMPACT_AT": "2"}, path)

    assert resolve("shell_timeout: 30\nshell_max_timeout: 30\n")["SHELL_TIMEOUT"] == 30
    with pytest.raises(
        SystemExit,
        match=r"demo.yaml: shell_max_timeout: shell_max_timeout must be at least shell_timeout",
    ):
        resolve("shell_timeout: 60\nshell_max_timeout: 30\n")
    with pytest.raises(
        SystemExit, match=r"shell_max_timeout: shell_max_timeout must be at least"
    ):
        resolve("shell_timeout: 500\n")


def test_environment_overrides_scalars_but_not_providers(tmp_path):
    conf = tmp_path / "harnh3x.yaml"
    conf.write_text(
        "providers:\n"
        "  local:\n"
        "    type: openai\n"
        "    api_url: http://file/v1\n"
        "    api_key: file-key\n"
        "model: file-model\n"
        "max_turns: 3\n"
    )
    resolved = config.load_settings(
        {
            "HARNH3X_CONFIG": str(conf),
            "OPENAI_API_KEY": "env-key",
            "OPENAI_BASE_URL": "http://env/v1",
            "ANTHROPIC_API_KEY": "ant-env",
            "HARNH3X_PROVIDER": "anthropic",
            "HARNH3X_MODEL": "env-model",
        }
    )

    assert resolved["MODEL"] == "env-model"
    assert resolved["MAX_TURNS"] == 3
    assert resolved["PROVIDERS"] == (
        config.Provider("local", "openai", "http://file/v1", "file-key"),
    )
    assert resolved["PROVIDER_WARNINGS"] == ()


def test_old_provider_environment_variables_add_no_provider(tmp_path):
    conf = tmp_path / "harnh3x.yaml"
    conf.write_text("max_turns: 9\n")
    resolved = config.load_settings(
        {
            "HARNH3X_CONFIG": str(conf),
            "OPENAI_API_KEY": "env-key",
            "OPENAI_BASE_URL": "http://env/v1",
            "ANTHROPIC_API_KEY": "ant-env",
        }
    )

    assert resolved["MAX_TURNS"] == 9
    assert resolved["PROVIDERS"] == ()
    assert resolved["MODEL"] == "qwen3.5-128k"


def test_missing_default_config_keeps_defaults(tmp_path):
    resolved = config.load_settings({"XDG_CONFIG_HOME": str(tmp_path)})

    assert resolved["MODEL"] == "qwen3.5-128k"
    assert resolved["PROVIDERS"] == ()
    assert resolved["PROVIDER_WARNINGS"] == ()
    assert resolved["MAX_TURNS"] == 45
    assert resolved["SUBAGENT_MAX_TURNS"] == 12
    assert resolved["MCP_SERVERS"] == ()


def test_startup_requires_a_provider(monkeypatch, tmp_path):
    cli = _load_cli()
    conf = tmp_path / "harnh3x.yaml"
    monkeypatch.setenv("HARNH3X_CONFIG", str(conf))
    monkeypatch.setattr(config, "PROVIDERS", ())

    def refuse_session(**kwargs):
        raise AssertionError("session started")

    monkeypatch.setattr(cli, "Session", refuse_session)

    with pytest.raises(SystemExit) as missing:
        cli.main(["--yolo"])

    assert str(missing.value) == (
        f"No provider is configured. Add one under providers: in {conf}."
    )


def test_missing_provider_message_lists_skipped_entries(monkeypatch):
    monkeypatch.setenv("HARNH3X_CONFIG", "")
    monkeypatch.setattr(config, "PROVIDERS", ())
    monkeypatch.setattr(
        config,
        "PROVIDER_WARNINGS",
        (("providers.litellm", "type is required (openai or anthropic)"),),
    )

    with pytest.raises(SystemExit) as missing:
        config.require_api_settings()

    assert str(missing.value).splitlines() == [
        "No provider is configured. Add one under providers: in the config file.",
        "providers.litellm: type is required (openai or anthropic)",
    ]


def test_skipped_provider_warnings_print_to_stderr(monkeypatch, capsys, tmp_path):
    cli = _load_cli()
    monkeypatch.setattr(
        config,
        "PROVIDER_WARNINGS",
        (("providers.litellm", "type is required (openai or anthropic)"),),
    )
    monkeypatch.setattr(cli, "_tty", lambda: False)
    monkeypatch.setattr(
        cli, "Session", lambda **kwargs: SimpleNamespace(workspace=tmp_path, **kwargs)
    )
    monkeypatch.setattr(cli, "run", lambda *args, **kwargs: "final answer")

    cli.main(["--answer-only", "hello"])

    captured = capsys.readouterr()
    assert captured.err == (
        "Warning: providers.litellm: type is required (openai or anthropic)\n"
    )
    assert captured.out == "final answer\n"


def test_explicit_config_path_must_exist(tmp_path):
    missing = tmp_path / "missing.conf"
    with pytest.raises(SystemExit, match="config file not found"):
        config.load_settings({"HARNH3X_CONFIG": str(missing)})


def test_import_applies_the_config_file(tmp_path):
    conf = tmp_path / "harnh3x.yaml"
    conf.write_text(
        "model: from-file\n"
        "providers:\n"
        "  local:\n"
        "    type: openai\n"
        "    api_url: http://file/v1\n"
        "    api_key: file-key\n"
    )
    env = os.environ.copy()
    env["HARNH3X_CONFIG"] = str(conf)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    env.pop("HARNH3X_MODEL", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import harnh3x.config as c; print(c.MODEL); print(c.PROVIDERS[0].api_key)",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["from-file", "file-key"]


def test_get_client_builds_one_client_per_provider(monkeypatch):
    first = config.Provider("first", "openai", "http://first/v1", "key-1")
    second = config.Provider("second", "openai", "http://second/v1", "key-2")
    monkeypatch.setattr(config, "PROVIDERS", (first, second))
    monkeypatch.setattr(agent_client, "_clients", {})
    seen = []

    def fake_openai(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(agent_client, "OpenAI", fake_openai)

    default = agent.get_client()
    other = agent.get_client("second")

    assert seen == [
        {"api_key": "key-1", "base_url": "http://first/v1"},
        {"api_key": "key-2", "base_url": "http://second/v1"},
    ]
    assert agent.get_client("first") is default
    assert agent.get_client("second") is other
    with pytest.raises(ValueError, match="Provider gone is not configured"):
        agent.get_client("gone")


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


def test_mcp_instructions_come_from_the_server_block():
    path = Path("demo.yaml")
    text = (
        "mcp:\n"
        "  caido:\n"
        "    command: caido-mcp-server\n"
        "    instructions: |\n"
        "      Check scope first.\n"
        "      Replay through Caido.\n"
    )
    _, servers, _providers = config.parse_config(text, path)
    assert servers[0].instructions == "Check scope first.\nReplay through Caido.\n"

    bad = "mcp:\n  caido:\n    command: caido-mcp-server\n    instructions: [a]\n"
    with pytest.raises(
        SystemExit, match=r"demo.yaml: mcp.caido: instructions must be a string"
    ):
        config.parse_config(bad, path)


def test_mcp_block_rejects_bad_shapes():
    path = Path("demo.yaml")
    cases = (
        ("mcp: [a]\n", r"demo.yaml: mcp: expected a mapping"),
        (
            "mcp:\n  Bad:\n    command: x\n",
            r"demo.yaml: mcp\.Bad: invalid server name",
        ),
        (
            "mcp:\n  caido:\n    command: x\n    timeout: 0\n",
            r"demo.yaml: mcp\.caido: timeout must be a positive integer",
        ),
        (
            "mcp:\n  caido:\n    command: x\n    description: 1\n",
            r"demo.yaml: mcp\.caido: description must be a string",
        ),
        (
            "mcp:\n  caido:\n    command: x\n    env:\n      TOKEN: 1\n",
            r"demo.yaml: mcp\.caido: env must be a mapping of strings",
        ),
    )
    for text, match in cases:
        with pytest.raises(SystemExit, match=match):
            config.parse_config(text, path)


def test_mcp_server_name_cannot_match_a_built_in_skill():
    with pytest.raises(RuntimeError, match="matches a built-in skill"):
        SkillRegistry(servers=(_mcp_server("file-ops"),))
