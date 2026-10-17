"""Memory, the plan file, and the system prompt."""

from datetime import UTC, datetime

from harnh3x import agent, tools
from harnh3x.config import TRANSCRIPT_FILE
from harnh3x.prompt import YOLO_NOTE, system_prompt
from harnh3x.skills import SkillRegistry
from harnh3x.tools import execute_tool


def test_remember_and_recall_round_trip_through_memory_md(tmp_path):
    assert execute_tool("recall", {}, tmp_path) == "Memory is empty."

    first = execute_tool(
        "remember", {"key": " user  timezone ", "text": "Europe/Paris"}, tmp_path
    )
    assert first == "Remembered 'user timezone'. 1 memories stored in memory.md."
    execute_tool(
        "remember", {"key": "project", "text": "# harnh3x\nagent loop"}, tmp_path
    )
    updated = execute_tool(
        "remember", {"key": "user timezone", "text": "America/Toronto"}, tmp_path
    )
    assert updated == "Updated 'user timezone'. 2 memories stored in memory.md."

    stored = (tmp_path / "memory.md").read_text()
    assert stored.startswith(
        "# Memory\n\n## user timezone\nAmerica/Toronto\n\n## project\nharnh3x\nagent loop\n"
    )
    assert tools.memory_keys() == ["user timezone", "project"]

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


def test_memory_is_shared_across_workspaces(tmp_path):
    first = tmp_path / "workspaces" / "one"
    second = tmp_path / "workspaces" / "two"
    first.mkdir(parents=True)
    second.mkdir()

    execute_tool("remember", {"key": "user timezone", "text": "America/Toronto"}, first)

    recalled = execute_tool("recall", {}, second)

    assert recalled == "## user timezone\nAmerica/Toronto"
    assert (tmp_path / "memory.md").is_file()
    assert not (first / "memory.md").exists()
    assert not (second / "memory.md").exists()


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
        (tmp_path / "plan.md").read_text()
        == "# Plan\n\n1. [ ] search the web\n2. [ ] read two pages\n3. [ ] answer\n"
    )

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


def test_system_prompt_mentions_yolo_only_when_enabled(tmp_path):
    now = datetime(2026, 10, 6, 16, 30, tzinfo=UTC)

    plain = system_prompt(tmp_path, "MENU", now=now)
    yolo = system_prompt(tmp_path, "MENU", now=now, yolo=True)

    assert "Yolo mode" not in plain
    assert YOLO_NOTE in yolo
    assert "/bin/sh" in yolo
    assert "are blocked" not in yolo
    assert "localhost" in yolo
    assert yolo.endswith("MENU")
    for registry in (SkillRegistry(), SkillRegistry(yolo=True)):
        loaded = registry.load_skill("shell")
        assert YOLO_NOTE not in loaded
        assert "are blocked" not in loaded
        assert "/bin/sh" not in loaded

    project = tmp_path / "proj"
    granted = system_prompt(tmp_path, "MENU", now=now, project=project)
    assert granted.startswith("You are a helpful personal assistant.")
    assert f"Your project is {project}." in granted
    assert "write it to a file in the project" in granted
    assert "When the user asks for a program, script, or code change" in granted
    assert "Yolo mode" not in granted
    coding = system_prompt(
        tmp_path, "MENU", now=now, project=project, coding=True, rules="RULES"
    )
    assert coding.startswith("You are a coding agent working in a granted project.")
    assert "When the user asks for a program" not in coding
    assert "Read the project before you change it" in coding
    assert "Load the coding skill before you read or change source;" in coding
    assert "Load file-ops" not in coding
    assert "Make the smallest change that does the job." in coding
    assert "Always confirm what you changed, including the paths." in coding
    mention = (
        "When the user writes @path, that path is a file or folder in the project; "
        "read it before you answer. A trailing slash is a folder."
    )
    assert mention in coding
    assert mention not in plain
    assert mention not in granted
    assert "RULES" in coding
    assert coding.endswith("MENU")
    both = system_prompt(tmp_path, "MENU", now=now, yolo=True, project=project)
    assert f"or the project at {project}." in both
    loaded = SkillRegistry(project=project).load_skill("coding")
    assert str(project) not in loaded
    assert "relative paths use the project" not in loaded


def test_system_prompt_includes_date_listing_and_memory_index(tmp_path):
    now = datetime(2026, 10, 6, 16, 30, tzinfo=UTC)

    empty = system_prompt(tmp_path, "MENU", now=now)
    assert "Today is Tuesday, October 06, 2026, 16:30 UTC." in empty
    assert f"Your workspace is {tmp_path}." in empty
    assert "The workspace is empty." in empty
    assert "No saved memories yet." in empty
    assert "Several skills can stay loaded at once" in empty
    assert "search snippets are not answers" in empty
    assert "load the coding skill and write it to a file" in empty
    assert "Use the python skill only to compute an answer." in empty
    assert empty.endswith("MENU")

    session = tmp_path / "workspaces" / "session"
    session.mkdir(parents=True)
    (session / "notes.md").write_text("n")
    (session / "downloads").mkdir()
    (session / "session.log").write_text("{}")
    (session / TRANSCRIPT_FILE).write_text("{}")
    (session / ".hidden").write_text("h")
    execute_tool("remember", {"key": "user name", "text": "Alex"}, session)

    filled = system_prompt(session, "MENU", now=now)
    assert "Workspace files: downloads/, notes.md" in filled
    assert "memory.md" not in filled.split("Saved memories:")[0]
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
