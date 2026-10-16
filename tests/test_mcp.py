"""MCP stdio servers as skills."""

import asyncio
import json
import socket
import sys

from helpers import _FAKE_MCP, _mcp_server, _tool_names

from harnh3x import agent, config, skills
from harnh3x.skills import SkillRegistry
from harnh3x.tui import HarnessApp, WarningScreen


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


def test_one_exit_handler_closes_every_live_registry(monkeypatch):
    registered = []
    monkeypatch.setattr(skills.atexit, "register", registered.append)
    monkeypatch.setattr(skills, "_exit_registered", False)
    monkeypatch.setattr(skills, "_live_registries", skills.weakref.WeakSet())

    first = SkillRegistry(servers=(_mcp_server(),))
    second = SkillRegistry(servers=(_mcp_server(),))
    try:
        assert registered == [skills._close_live_registries]
        assert set(skills._live_registries) == {first, second}
        first.load_skill("caido")
        second.load_skill("caido")
        clients = [first._clients["caido"], second._clients["caido"]]
        assert all(client.alive() for client in clients)

        first.close()
        assert set(skills._live_registries) == {second}

        skills._close_live_registries()
        assert not any(client.alive() for client in clients)
        assert not skills._live_registries
    finally:
        first.close()
        second.close()


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
