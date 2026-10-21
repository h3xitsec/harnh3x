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


def test_config_file_skips_comments_and_reads_quotes():
    text = """
# comment

openai_api_key: "sk-quoted"
openai_api_url: "http://127.0.0.1:11434/v1"
model: from-file
max_turns: 12
"""
    path = Path("harnh3x.yaml")
    values, servers = config.parse_config(text, path)
    resolved = config.resolve_settings(values, {}, path)

    assert servers == ()
    assert values["openai_api_key"] == "sk-quoted"
    assert resolved["API_KEY"] == "sk-quoted"
    assert resolved["API_URL"] == "http://127.0.0.1:11434/v1"
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
        values, _servers = config.parse_config("max_turns: lots\n", path)
        config.resolve_settings(values, {}, path)
    with pytest.raises(SystemExit, match=r"demo.yaml: compact_at: must be a number"):
        values, _servers = config.parse_config("compact_at: nope\n", path)
        config.resolve_settings(values, {}, path)


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
        values, _servers = config.parse_config(f"{key}: {raw}\n", path)
        config.resolve_settings(values, {}, path)
    env = "HARNH3X_" + key.upper()
    with pytest.raises(SystemExit, match=rf"{env}: must be a positive integer"):
        config.resolve_settings({}, {env: raw}, path)


def test_range_checked_settings():
    path = Path("demo.yaml")

    def resolve(text):
        values, _servers = config.parse_config(text, path)
        return config.resolve_settings(values, {}, path)

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


def test_environment_overrides_the_config_file():
    path = Path("harnh3x.yaml")
    values, _servers = config.parse_config(
        "openai_api_key: file-key\n"
        "model: file-model\n"
        "openai_api_url: http://file/v1\n"
        "max_turns: 3\n"
        "anthropic_api_key: ant-file\n",
        path,
    )
    resolved = config.resolve_settings(
        values,
        {
            "OPENAI_API_KEY": "env-key",
            "OPENAI_BASE_URL": "http://env/v1",
            "HARNH3X_MODEL": "env-model",
        },
        path,
    )

    assert resolved["API_KEY"] == "env-key"
    assert resolved["MODEL"] == "env-model"
    assert resolved["API_URL"] == "http://env/v1"
    assert resolved["MAX_TURNS"] == 3
    assert resolved["ANTHROPIC_API_KEY"] == "ant-file"


def test_config_file_fills_settings_the_environment_omits(tmp_path):
    conf = tmp_path / "harnh3x.yaml"
    conf.write_text("openai_api_url: http://file/v1\nmax_turns: 9\n")
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
    assert resolved["SUBAGENT_MAX_TURNS"] == 12
    assert resolved["MCP_SERVERS"] == ()


def test_startup_requires_api_key_and_url(monkeypatch):
    cli = _load_cli()
    monkeypatch.setattr(config, "API_KEY", None)
    monkeypatch.setattr(config, "API_URL", None)

    def refuse_session(**kwargs):
        raise AssertionError("session started")

    monkeypatch.setattr(cli, "Session", refuse_session)

    with pytest.raises(SystemExit, match="Missing openai_api_key") as missing:
        cli.main(["--yolo"])

    message = str(missing.value)
    assert "openai_api_url (OPENAI_BASE_URL)" in message
    assert "the environment" in message


def test_startup_names_only_the_missing_setting(monkeypatch, tmp_path):
    conf = tmp_path / "harnh3x.yaml"
    monkeypatch.setenv("HARNH3X_CONFIG", str(conf))
    monkeypatch.setattr(config, "API_KEY", None)
    monkeypatch.setattr(config, "API_URL", "http://127.0.0.1:11434/v1")

    with pytest.raises(SystemExit, match="Missing openai_api_key") as missing:
        config.require_api_settings()

    message = str(missing.value)
    assert "OPENAI_API_KEY" in message
    assert str(conf) in message
    assert "openai_api_url" not in message


def test_explicit_config_path_must_exist(tmp_path):
    missing = tmp_path / "missing.conf"
    with pytest.raises(SystemExit, match="config file not found"):
        config.load_settings({"HARNH3X_CONFIG": str(missing)})


def test_import_applies_the_config_file(tmp_path):
    conf = tmp_path / "harnh3x.yaml"
    conf.write_text("model: from-file\nopenai_api_key: file-key\n")
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
    _, servers = config.parse_config(text, path)
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
