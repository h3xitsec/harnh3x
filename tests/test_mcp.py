"""MCP stdio servers as skills."""

import asyncio
import json
import socket
import sys

from helpers import _FAKE_MCP, _mcp_server, _tool_names

from harnh3x import __version__, agent, config, skills
from harnh3x.mcp.client import upstream_problem
from harnh3x.skills import SkillRegistry
from harnh3x.tui import HarnessApp, StatusBar, WarningScreen


def test_mcp_handshake_at_start_keeps_schemas_out_until_load(tmp_path):
    server = _mcp_server(
        description="Browse and replay HTTP traffic in Caido.",
        instructions="Check scope first.\n",
    )
    registry = SkillRegistry(servers=(server,))
    try:
        client = registry._clients["caido"]
        assert client.alive()
        base_skills, base_tools, *_mcp = SkillRegistry().available_counts()
        assert _mcp == [0, 0, 0]
        assert registry.available_counts() == (base_skills, base_tools, 0, 1, 8)
        menu = registry.get_menu()
        assert "mcp:\n- caido: Browse and replay HTTP traffic in Caido." in menu
        assert menu.index("- tasks:") < menu.index("mcp:")
        assert _tool_names(registry.schemas()) == [
            "load_skill",
            "unload_skill",
            "task",
        ]

        loaded = registry.load_skill("caido")

        assert registry._clients["caido"] is client
        names = _tool_names(registry.schemas())
        assert names[0:2] == ["load_skill", "unload_skill"]
        assert "caido_list_requests" in names
        assert "caido_send_request" in names
        assert "caido_read_resource" in names
        assert registry.available_counts() == (base_skills, base_tools, 0, 1, 8)
        assert loaded.index("Browse and replay HTTP traffic in Caido.") < (
            loaded.index("Check scope first.\n\n# Fake\nUse HTTPQL.")
        )
        assert loaded.index("Use HTTPQL.") < loaded.index(
            "caido://project: Current project"
        )
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
        assert "narrow the request and call the tool again" in wide
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


def test_mcp_include_filters_schemas_and_collision_drops_the_server(tmp_path):
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
        assert "caido" not in colliding.get_menu()
        assert "caido" not in colliding._clients
        assert colliding.warnings == [
            ("caido", "tool 'read_file' from 'caido' collides with an existing tool")
        ]
        failed = colliding.load_skill("caido")
        assert failed == "Error: Unknown skill 'caido'. Check the skill menu."
    finally:
        colliding.close()


def test_mcp_menu_line_comes_from_the_server_without_a_description(tmp_path):
    def menu_line(mode):
        registry = SkillRegistry(servers=(_mcp_server(env={"FAKE_MCP": mode}),))
        try:
            menu = registry.get_menu()
            loaded = registry.load_skill("caido")
        finally:
            registry.close()
        line = next(row for row in menu.splitlines() if row.startswith("- caido:"))
        return line, loaded

    described, loaded = menu_line("described")
    assert described == "- caido: Fake proxy traffic."
    assert "Use HTTPQL." in loaded
    assert "Caido" not in loaded

    instructed, _ = menu_line("")
    assert instructed == "- caido: Use HTTPQL."

    quiet, _ = menu_line("quiet")
    assert quiet == (
        "- caido: fake 1, 8 tools: caido_list_requests, caido_send_request, "
        "echo_env, boom, die, ..."
    )


def test_mcp_description_without_resources(tmp_path):
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


def test_missing_mcp_command_is_dropped_at_start(tmp_path):
    registry = SkillRegistry(
        servers=(_mcp_server(command="harnh3x-missing-mcp", args=()),)
    )
    try:
        assert "caido" not in registry.get_menu()
        assert registry.warnings == [
            ("caido", "command not found: harnh3x-missing-mcp")
        ]
        again = registry.load_skill("caido")
        assert again == "Error: Unknown skill 'caido'. Check the skill menu."
    finally:
        registry.close()


def test_server_that_cannot_restart_on_load_is_dropped(tmp_path):
    registry = SkillRegistry(servers=(_mcp_server(),))
    try:
        client = registry._clients["caido"]
        client.close()
        client.argv = ("harnh3x-missing-mcp",)
        failed = registry.load_skill("caido")
        assert failed.startswith("Error: command not found: ")
        assert failed.endswith("Skill 'caido' is no longer available.")
        assert "caido" not in registry.loaded_names()
        assert "caido" not in registry.get_menu()
        assert registry.warnings[0][0] == "caido"
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
        base_skills, base_tools, *_mcp = SkillRegistry().available_counts()
        assert closed.available_counts() == (base_skills, base_tools, 0, 0, 0)
    finally:
        closed.close()

    plain = SkillRegistry(servers=(_mcp_server(description="Widgets."),))
    try:
        assert "- caido: Widgets." in plain.get_menu()
        assert plain.warnings == []
        assert plain.available_counts() == (base_skills, base_tools, 0, 1, 8)
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
            bar = app.query_one("#status", StatusBar)
            skills, tools, _custom, servers, _mcp_tools = (
                session.skills.available_counts()
            )
            assert servers == 0
            assert [segment.text for segment in bar.shown()] == [
                session.model,
                f"skills {skills}",
                f"tools {tools}",
                "⚠ 1",
                __version__,
            ]
            red = [
                segment.text
                for segment in bar.segments
                if segment.style == "bold ansi_white on ansi_red"
            ]
            assert red == ["⚠ 1"]
            assert "caido" not in str(app.query_one("#banner").content)
            composer = app.query_one("#composer")
            status_bar = app.query_one("#status")
            assert status_bar.region.y == composer.region.bottom + 1
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


def test_status_bar_counts_mcp_servers_and_tools_apart(tmp_path):
    session = agent.Session(workspace=tmp_path)
    session.skills.close()
    session.skills = SkillRegistry(servers=(_mcp_server(description="Widgets."),))
    app = HarnessApp(session, lambda *args, **kwargs: "done", show_tools=True)

    async def drive():
        async with app.run_test(size=(120, 24)):
            bar = app.query_one("#status", StatusBar)
            skills, tools, _custom, servers, mcp_tools = (
                session.skills.available_counts()
            )
            assert (servers, mcp_tools) == (1, 8)
            shown = [segment.text for segment in bar.shown()]
            assert shown[1:] == [
                f"skills {skills}",
                f"tools {tools}",
                "mcp 1 · 8 tools",
                __version__,
            ]

    asyncio.run(drive())


def test_mcp_content_length_frames_and_server_requests(tmp_path):
    framed = SkillRegistry(servers=(_mcp_server(env={"FAKE_MCP": "framed"}),))
    try:
        framed.load_skill("caido")
        assert framed.execute("caido_list_requests", {}, tmp_path) == "alpha\nbeta"
    finally:
        framed.close()

    asked = SkillRegistry(servers=(_mcp_server(env={"FAKE_MCP": "server-request"}),))
    try:
        asked.load_skill("caido")
        assert asked.execute("caido_list_requests", {}, tmp_path) == "alpha\nbeta"
    finally:
        asked.close()


def test_mcp_rejects_bad_frames_and_a_non_list_page(tmp_path):
    length = SkillRegistry(servers=(_mcp_server(env={"FAKE_MCP": "bad-length"}),))
    try:
        assert length.warnings == [("caido", "MCP server sent a bad Content-Length")]
        assert "caido" not in length.get_menu()
    finally:
        length.close()

    shape = SkillRegistry(servers=(_mcp_server(env={"FAKE_MCP": "non-object"}),))
    try:
        assert shape.warnings == [("caido", "MCP server sent a non-object message")]
    finally:
        shape.close()

    page = SkillRegistry(servers=(_mcp_server(env={"FAKE_MCP": "bad-tools"}),))
    try:
        assert page.warnings == [("caido", "tools/list returned no list")]
    finally:
        page.close()


def test_mcp_keeps_resource_text_and_names_unsupported_blocks(tmp_path):
    registry = SkillRegistry(servers=(_mcp_server(env={"FAKE_MCP": "bundle"}),))
    try:
        registry.load_skill("caido")
        assert registry.execute("caido_list_requests", {}, tmp_path) == (
            "inside\nUnsupported content type: resource, unknown"
        )
    finally:
        registry.close()


def test_mcp_closed_message_includes_stderr(tmp_path):
    registry = SkillRegistry(servers=(_mcp_server(env={"FAKE_MCP": "stderr"}),))
    try:
        registry.load_skill("caido")
        dead = registry.execute("die", {}, tmp_path)
        assert dead == "Error: MCP server closed: boom on stderr"
    finally:
        registry.close()


def test_upstream_problem_reports_a_bad_url_without_connecting():
    server = _mcp_server(
        env={"BARE": "http://", "COUNT": 1, "PORT": "https://example.com:99999"}
    )

    problem = upstream_problem(server)

    assert problem == (
        "not accepting connections: invalid URL, https://example.com (invalid port)"
    )


def _custom_skill(root, name, allowed):
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Custom {name}.\n"
        f"allowed-tools: {allowed}\n---\n\nUse it.\n"
    )


def test_custom_skill_uses_mcp_tools_by_server_name(tmp_path):
    root = tmp_path / "skills"
    _custom_skill(root, "triage", "read_file caido:caido_list_requests")
    _custom_skill(root, "everything", "caido:* caido:boom")
    registry = SkillRegistry(servers=(_mcp_server(),), custom_dir=root)
    try:
        assert registry.warnings == []
        assert registry.available_counts()[2:] == (2, 1, 8)
        menu = registry.get_menu()
        assert menu.index("custom:") < menu.index("mcp:")

        loaded = registry.load_skill("triage")
        assert loaded.startswith(
            "Loaded skill 'triage' with 2 tools: read_file, caido_list_requests"
        )
        assert _tool_names(registry.schemas()) == [
            "load_skill",
            "unload_skill",
            "task",
            "read_file",
            "caido_list_requests",
        ]
        assert registry.execute("caido_list_requests", {}, tmp_path) == "alpha\nbeta"
        blocked = registry.execute("boom", {}, tmp_path)
        assert "It is in skills: everything, caido." in blocked

        everything = registry.load_skill("everything")
        assert "with 8 tools" in everything
        assert registry.execute("boom", {}, tmp_path) == "Error: nope"
        assert (
            registry.execute(
                "caido_read_resource", {"uri": "caido://project"}, tmp_path
            )
            == "project-body"
        )

        both = registry.load_skill("caido")
        assert both.startswith("Loaded skill 'caido' with 8 tools")
        names = _tool_names(registry.schemas())
        assert len(names) == len(set(names))
        assert registry.execute("caido_list_requests", {}, tmp_path) == "alpha\nbeta"
    finally:
        registry.close()


def test_custom_skill_with_a_missing_mcp_tool_or_server_is_skipped(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    root = tmp_path / "skills"
    _custom_skill(root, "excluded", "caido:caido_send_request")
    _custom_skill(root, "offline", "widgets:*")
    _custom_skill(root, "works", "caido:caido_list_requests")
    registry = SkillRegistry(
        servers=(
            _mcp_server(exclude=("caido_send_request",)),
            _mcp_server("widgets", env={"CAIDO_URL": f"http://127.0.0.1:{port}"}),
        ),
        custom_dir=root,
    )
    try:
        assert dict(registry.warnings) == {
            "widgets": f"not accepting connections: http://127.0.0.1:{port}",
            "excluded": (
                "skill 'excluded' lists 'caido:caido_send_request', "
                "which MCP server 'caido' does not offer"
            ),
            "offline": (
                "skill 'offline' needs MCP server 'widgets', which is unavailable"
            ),
        }
        menu = registry.get_menu()
        assert "- works:" in menu
        assert "- excluded:" not in menu
        assert "- offline:" not in menu
    finally:
        registry.close()


def test_custom_skill_reports_a_dropped_mcp_server(tmp_path):
    root = tmp_path / "skills"
    _custom_skill(root, "triage", "caido:caido_list_requests")
    registry = SkillRegistry(servers=(_mcp_server(),), custom_dir=root)
    try:
        registry.load_skill("triage")
        client = registry._clients["caido"]
        client.close()
        client.argv = ("harnh3x-missing-mcp",)
        assert registry.load_skill("caido").startswith("Error: command not found")

        assert registry.execute("caido_list_requests", {}, tmp_path) == (
            "Error: MCP server 'caido' is no longer available"
        )
        registry.unload_skill("triage")
        assert registry.load_skill("triage") == (
            "Error: skill 'triage' needs MCP server 'caido', "
            "which is no longer available."
        )
    finally:
        registry.close()


def test_status_bar_counts_custom_skills_apart(tmp_path):
    root = tmp_path / "skills"
    _custom_skill(root, "triage", "read_file caido:caido_list_requests")
    session = agent.Session(workspace=tmp_path / "work")
    session.skills.close()
    session.skills = SkillRegistry(
        servers=(_mcp_server(description="Widgets."),), custom_dir=root
    )
    app = HarnessApp(session, lambda *args, **kwargs: "done", show_tools=True)

    async def drive():
        async with app.run_test(size=(160, 24)):
            bar = app.query_one("#status", StatusBar)
            skills, tools, custom, *_mcp = session.skills.available_counts()
            assert custom == 1
            shown = [segment.text for segment in bar.shown()]
            assert shown[1:4] == [
                f"skills {skills}+1",
                f"tools {tools}",
                "mcp 1 · 8 tools",
            ]

    asyncio.run(drive())
