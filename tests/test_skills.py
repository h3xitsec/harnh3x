"""The skill registry and SKILL.md files."""

from pathlib import Path

import pytest
from helpers import FILE_OPS_LOADED, _tool_names

from harnh3x import skills, tools
from harnh3x.skills import SkillRegistry


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
        "hardware-diagnosis",
    ):
        assert f"- {name}:" in menu
    assert "news:" not in menu
    assert "- search:" not in menu
    assert "[loaded" not in menu
    assert _tool_names(registry.schemas()) == ["load_skill", "unload_skill", "task"]


def test_skill_load_unload_and_gated_execute(tmp_path):
    registry = SkillRegistry()

    unknown = registry.execute("load_skill", {"name": "nope"}, tmp_path)
    assert unknown == "Error: Unknown skill 'nope'. Check the skill menu."

    blocked = registry.execute("read_file", {"path": "a.txt"}, tmp_path)
    assert blocked == (
        "Error: 'read_file' is not loaded. It is in skills: coding, documents, "
        "file-ops, hardware-diagnosis, research. Load the one that fits the task, "
        "then call read_file again."
    )
    single = registry.execute("delete_file", {"path": "a.txt"}, tmp_path)
    assert single == (
        "Error: 'delete_file' is in skill 'file-ops', which is not loaded. "
        "Call load_skill with name 'file-ops', then call delete_file again."
    )
    missing = registry.execute("not_a_tool", {}, tmp_path)
    assert missing == "Error: Tool 'not_a_tool' not found. Is the skill loaded?"

    loaded = registry.load_skill("file-ops")
    assert loaded.startswith("Loaded skill 'file-ops' with 10 tools: ")
    assert "read_file" in loaded
    assert "Documentation:" in loaded
    assert "give the user the content of the file" in loaded
    assert registry.load_skill("file-ops") == (
        "Skill 'file-ops' is already loaded. Call one of: read_file, write_file, "
        "edit_file, append_file, delete_file, move_file, list_files, search_files, "
        "glob, grep."
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


def test_every_tool_belongs_to_a_skill():
    assigned = [
        tool["function"]["name"]
        for skill in skills._CATALOG.values()
        for tool in skill.tools
    ]
    assert set(assigned) == set(tools.TOOL_FNS)
    assert set(assigned) == {tool["function"]["name"] for tool in tools.TOOLS}


def test_shipped_skills_have_no_when_to_use_section():
    for path in skills._skill_files(skills.SKILLS_DIR):
        assert "## when to use" not in path.read_text(encoding="utf-8").lower()


def test_shared_tools_are_sent_once(tmp_path):
    registry = SkillRegistry()
    for name in ("file-ops", "hardware-diagnosis", "coding"):
        registry.load_skill(name)

    names = _tool_names(registry.schemas())

    assert len(names) == len(set(names))
    assert names.count("read_file") == 1
    assert "system_info" in names

    registry.unload_skill("file-ops")
    (tmp_path / "a.txt").write_text("hi")
    assert registry.execute("read_file", {"path": "a.txt"}, tmp_path) == "hi"
    assert "delete_file" not in _tool_names(registry.schemas())


def _description(registry: SkillRegistry, tool: str) -> str:
    for schema in registry.schemas():
        if schema["function"]["name"] == tool:
            return schema["function"]["description"]
    raise AssertionError(f"{tool} not sent")


def test_schemas_carry_the_session_scope(tmp_path):
    project = tmp_path / "proj"
    sandbox = SkillRegistry()
    yolo = SkillRegistry(yolo=True)
    granted = SkillRegistry(project=project)
    for registry in (sandbox, yolo, granted):
        for name in ("file-ops", "shell", "http", "hardware-diagnosis"):
            registry.load_skill(name)

    shell = _description(sandbox, "run_shell")
    assert "are blocked" in shell
    assert "no network" in shell
    open_shell = _description(yolo, "run_shell")
    assert "/bin/sh -c" in open_shell
    assert "are blocked" not in open_shell
    assert "use the network" in open_shell

    assert "Paths must be inside the workspace." in _description(sandbox, "read_file")
    assert "Any path may be read." in _description(yolo, "read_file")
    assert "Paths must be inside the workspace." in _description(yolo, "write_file")
    project_read = _description(granted, "read_file")
    assert "the project or the workspace; relative paths use the project" in (
        project_read
    )
    assert "Runs in the project." in _description(granted, "run_shell")

    assert "Public hosts only" in _description(sandbox, "http_request")
    assert "localhost" in _description(yolo, "http_request")
    assert "Paths must" not in _description(sandbox, "system_info")

    base = {tool["function"]["name"]: tool for tool in tools.TOOLS}
    assert "Paths must" not in base["read_file"]["function"]["description"]
    assert "blocked" not in base["run_shell"]["function"]["description"]


def test_custom_skill_is_listed_apart_and_loads_its_tools(tmp_path):
    root = tmp_path / "skills"
    _write_skill(
        root,
        "notes-helper",
        description="Keep notes.",
        body="Write notes in notes.md.\n",
        tools=("read_file", "write_file", "list_files"),
    )
    _write_skill(root, "team", "standup", tools=())
    base = SkillRegistry().available_counts()
    registry = SkillRegistry(custom_dir=root)

    menu = registry.get_menu()
    assert "custom:\n- notes-helper: Keep notes." in menu
    assert "custom/team:\n- standup: Do the thing." in menu
    assert menu.index("- tasks:") < menu.index("custom:")
    assert registry.warnings == []
    assert registry.available_counts() == (base[0], base[1], 2, 0, 0)

    loaded = registry.load_skill("notes-helper")
    assert loaded.startswith(
        "Loaded skill 'notes-helper' with 3 tools: read_file, write_file, list_files"
    )
    assert "Write notes in notes.md." in loaded
    (tmp_path / "a.txt").write_text("hi")
    assert registry.execute("read_file", {"path": "a.txt"}, tmp_path) == "hi"
    assert "Paths must be inside the workspace." in _description(registry, "read_file")


def test_missing_custom_dir_changes_nothing(tmp_path):
    registry = SkillRegistry(custom_dir=tmp_path / "absent")

    assert registry.get_menu() == SkillRegistry().get_menu()
    assert registry.warnings == []


def test_malformed_custom_skills_are_skipped_with_a_warning(tmp_path):
    root = tmp_path / "skills"
    _write_skill(root, "good", tools=("read_file",))
    _write_skill(
        root,
        "extra-key",
        frontmatter="---\nname: extra-key\ndescription: Do it.\nversion: 1\n---",
    )
    _write_skill(root, "renamed", name="other-name")
    _write_skill(root, "no-description", frontmatter="---\nname: no-description\n---")
    _write_skill(root, "unclosed", frontmatter="---\nname: unclosed\ndescription: x")
    _write_skill(root, "bad-tool", tools=("read_file", "not_a_tool"))
    _write_skill(root, "bad-entry", tools=(":read_file",))
    _write_skill(root, "unknown-server", tools=("nowhere:*",))
    _write_skill(root, "file-ops", tools=("read_file",))
    _write_skill(root, "load_skill", tools=())
    _write_skill(root, "a", "dup", tools=())
    _write_skill(root, "b", "dup", tools=())

    registry = SkillRegistry(custom_dir=root)

    assert dict(registry.warnings) == {
        "b/dup": "skill 'b/dup' uses the name 'dup', which another skill already has",
        "bad-entry": "skill 'bad-entry' lists invalid tool ':read_file'",
        "bad-tool": "skill 'bad-tool' lists unknown tool 'not_a_tool'",
        "extra-key": "skill 'extra-key' has unknown frontmatter key: version",
        "file-ops": (
            "skill 'file-ops' uses the name 'file-ops', which another skill already has"
        ),
        "load_skill": "skill 'load_skill' has an invalid name",
        "no-description": "skill 'no-description' description must be a string",
        "renamed": "skill name 'other-name' does not match directory 'renamed'",
        "unclosed": "skill 'unclosed' frontmatter is not closed",
        "unknown-server": (
            "skill 'unknown-server' lists MCP server 'nowhere', which is not configured"
        ),
    }
    menu = registry.get_menu()
    assert "custom:\n- good: Do the thing." in menu
    assert "custom/a:\n- dup:" in menu
    assert "custom/b:" not in menu
    assert "extra-key" not in menu
    assert registry.available_counts()[2] == 2
    assert registry.load_skill("bad-tool") == (
        "Error: Unknown skill 'bad-tool'. Check the skill menu."
    )
    assert "Loaded skill 'file-ops' with 10 tools" in registry.load_skill("file-ops")


def test_session_reads_custom_skills_from_base_dir(tmp_path):
    from harnh3x import agent, config

    _write_skill(Path(config.BASE_DIR) / config.CUSTOM_SKILLS_DIR, "mine", tools=())

    session = agent.Session(workspace=tmp_path / "work")

    assert "custom:\n- mine: Do the thing." in session.messages[0]["content"]


def test_readme_lists_every_tool():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8"
    )
    missing = [
        tool["function"]["name"]
        for tool in tools.TOOLS
        if f"| `{tool['function']['name']}` |" not in readme
    ]
    assert missing == []


def test_overview_marks_loaded_skills_in_menu_order():
    registry = SkillRegistry()
    registry.load_skill("shell")
    overview = registry.overview()
    names = [name for name, _category, _loaded, _tools in overview]
    assert names[0] == "coding"
    assert "file-ops" in names
    by_name = {name: (loaded, tools) for name, _category, loaded, tools in overview}
    assert by_name["shell"] == (True, ("run_shell",))
    assert by_name["file-ops"][0] is False
    assert "read_file" in by_name["file-ops"][1]
    assert [name for name, _c, loaded, _t in overview if loaded] == ["shell"]
