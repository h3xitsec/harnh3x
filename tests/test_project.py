"""The --project grant and Cursor rules."""

import json
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

import harnh3x.agent.transcript as agent_transcript
from harnh3x import agent, rules, sandbox, tools
from harnh3x.config import TRANSCRIPT_FILE
from harnh3x.tools import execute_tool


def test_project_grant_writes_relative_paths_and_rejects_escapes(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    (workspace / "notes.md").write_text("session-note")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret-outside")
    (project / "escape.txt").symlink_to(outside)

    written = execute_tool(
        "write_file",
        {"path": "app.txt", "content": "from-project"},
        workspace,
        project=project,
    )
    listed = execute_tool("list_files", {}, workspace, project=project)
    session_file = execute_tool(
        "read_file",
        {"path": str(workspace / "notes.md")},
        workspace,
        project=project,
    )
    escaped = execute_tool(
        "read_file", {"path": "escape.txt"}, workspace, project=project
    )
    blocked = execute_tool(
        "write_file",
        {"path": str(outside), "content": "nope"},
        workspace,
        project=project,
    )
    yolo_write = execute_tool(
        "write_file",
        {"path": "yolo.txt", "content": "granted"},
        workspace,
        yolo=True,
        project=project,
    )
    yolo_blocked = execute_tool(
        "write_file",
        {"path": str(tmp_path / "pwned.txt"), "content": "nope"},
        workspace,
        yolo=True,
        project=project,
    )

    assert "Wrote" in written
    assert (project / "app.txt").read_text() == "from-project"
    assert not (workspace / "app.txt").exists()
    assert "app.txt" in listed
    assert "notes.md" not in listed
    assert session_file == "session-note"
    assert "outside workspace" in escaped
    assert "secret-outside" not in escaped
    assert "outside workspace" in blocked
    assert outside.read_text() == "secret-outside"
    assert "Wrote" in yolo_write
    assert (project / "yolo.txt").read_text() == "granted"
    assert "outside workspace" in yolo_blocked
    assert not (tmp_path / "pwned.txt").exists()


def test_project_shell_accepts_only_granted_absolute_paths(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    inside = project / "main.py"
    inside.write_text("print('inside')\n")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret-outside")

    allowed = execute_tool(
        "run_shell",
        {"command": f"cat {shlex.quote(str(inside))}"},
        workspace,
        project=project,
    )
    denied = execute_tool(
        "run_shell",
        {"command": f"cat {shlex.quote(str(outside))}"},
        workspace,
        project=project,
    )
    ran = execute_tool(
        "run_python",
        {"code": "open('written.txt', 'w').write('ok')"},
        workspace,
        project=project,
    )
    sibling = tmp_path / "pwned.txt"
    blocked_write = execute_tool(
        "run_python",
        {"code": f"open({str(sibling)!r}, 'w').write('pwned')"},
        workspace,
        project=project,
    )

    assert "status: success" in allowed
    assert "print('inside')" in allowed
    assert "outside workspace" in denied
    assert "status: success" in ran
    assert (project / "written.txt").read_text() == "ok"
    assert not (workspace / "written.txt").exists()
    assert "Operation not permitted" in blocked_write
    assert not sibling.exists()


def test_project_seatbelt_profile_grants_the_folder_and_keeps_home(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    project = tmp_path / "proj"
    project.mkdir()

    profile = sandbox.seatbelt_profile(workspace, project)
    _argv, env = sandbox.sandboxed_argv(["true"], workspace, project)

    assert f'(subpath "{project.resolve()}")' in profile
    assert "(deny network*)" in profile
    assert "(allow file-write*" in profile
    assert profile == env[sandbox.PROFILE_ENV]
    assert env["HOME"] == str(workspace.resolve())
    assert env["TMPDIR"] == str((workspace / ".tmp").resolve())


def test_project_is_stored_restored_and_kept_across_reset(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    session = agent.Session(workspace=tmp_path / "ws", project=project)
    session.messages.append({"role": "user", "content": "work"})
    agent_transcript._save_transcript(session)
    saved = json.loads((session.workspace / TRANSCRIPT_FILE).read_text())
    assert saved["project"] == str(project.resolve())

    resumed = agent.Session(workspace=tmp_path / "fresh", project=other)
    resumed.resume(session.workspace)
    assert resumed.project == project.resolve()
    assert f"Your project is {project.resolve()}." in resumed.messages[0]["content"]
    assert "write it to a file in the project" in resumed.messages[0]["content"]

    plain = agent.Session(workspace=tmp_path / "plain")
    plain.messages.append({"role": "user", "content": "notes"})
    agent_transcript._save_transcript(plain)
    launched = agent.Session(workspace=tmp_path / "launch", project=other)
    launched.resume(plain.workspace)
    assert launched.project is None
    assert "Your project is" not in launched.messages[0]["content"]

    previous = session.workspace
    session.reset()
    assert session.project == project.resolve()
    assert session.workspace != previous
    assert str(project.resolve()) in session.messages[0]["content"]


def test_coding_is_stored_restored_and_kept_across_reset(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    with pytest.raises(ValueError, match="--coding requires --project"):
        agent.Session(workspace=tmp_path / "nocode", coding=True)

    session = agent.Session(workspace=tmp_path / "ws", project=project, coding=True)
    assert session.messages[0]["content"].startswith(
        "You are a coding agent working in a granted project."
    )
    session.messages.append({"role": "user", "content": "work"})
    agent_transcript._save_transcript(session)
    saved = json.loads((session.workspace / TRANSCRIPT_FILE).read_text())
    assert saved["coding"] is True
    assert saved["project"] == str(project.resolve())

    resumed = agent.Session(workspace=tmp_path / "fresh", project=project)
    assert resumed.coding is False
    resumed.resume(session.workspace)
    assert resumed.coding is True
    assert resumed.project == project.resolve()
    assert resumed.messages[0]["content"].startswith(
        "You are a coding agent working in a granted project."
    )

    previous = session.workspace
    session.reset()
    assert session.coding is True
    assert session.project == project.resolve()
    assert session.workspace != previous
    assert session.messages[0]["content"].startswith(
        "You are a coding agent working in a granted project."
    )

    plain = agent.Session(workspace=tmp_path / "plain", project=project)
    plain.messages.append({"role": "user", "content": "notes"})
    agent_transcript._save_transcript(plain)
    assert "coding" not in json.loads((plain.workspace / TRANSCRIPT_FILE).read_text())
    launched = agent.Session(
        workspace=tmp_path / "launch", project=project, coding=True
    )
    launched.resume(plain.workspace)
    assert launched.coding is False
    assert launched.messages[0]["content"].startswith(
        "You are a helpful personal assistant."
    )

    bad = agent.Session(workspace=tmp_path / "bad", project=project)
    bad.messages.append({"role": "user", "content": "work"})
    agent_transcript._save_transcript(bad)
    payload = json.loads((bad.workspace / TRANSCRIPT_FILE).read_text())
    payload["coding"] = "yes"
    (bad.workspace / TRANSCRIPT_FILE).write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="invalid coding"):
        agent.Session().resume(bad.workspace)


def test_project_must_be_a_separate_existing_directory(tmp_path):
    workspace = tmp_path / "ws"
    parent = tmp_path / "parent"
    parent.mkdir()
    with pytest.raises(ValueError, match="existing directory"):
        agent.Session(workspace=workspace, project=tmp_path / "missing")
    with pytest.raises(ValueError, match="overlap"):
        agent.Session(workspace=workspace, project=workspace)
    with pytest.raises(ValueError, match="overlap"):
        agent.Session(workspace=parent / "child", project=parent)
    with pytest.raises(ValueError, match="not allowed"):
        agent.Session(workspace=workspace, project="/")

    gone = tmp_path / "gone"
    stored = agent.Session(workspace=tmp_path / "stored")
    stored.messages.append({"role": "user", "content": "work"})
    agent_transcript._save_transcript(stored)
    payload = json.loads((stored.workspace / TRANSCRIPT_FILE).read_text())
    payload["project"] = str(gone)
    (stored.workspace / TRANSCRIPT_FILE).write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="Cannot resume"):
        agent.Session().resume(stored.workspace)


def _write_rule(path: Path, front: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{front}\n---\n{body}\n", encoding="utf-8")


def test_project_rules_enter_the_prompt_and_attach_on_a_matching_read(tmp_path):
    project = tmp_path / "proj"
    _write_rule(
        project / ".cursor/rules/always.mdc",
        "description: Core standards\nalwaysApply: true",
        "ALWAYS-RULE-BODY",
    )
    _write_rule(
        project / ".cursor/rules/py.mdc",
        "description: Python files\nglobs: src/**/*.py,tests/**/*.py\nalwaysApply: false",
        "GLOB-RULE-BODY",
    )
    _write_rule(
        project / ".cursor/rules/nested.mdc",
        "description: Package files\nglobs: src/pkg/**\nalwaysApply: false",
        "NESTED-RULE-BODY",
    )
    _write_rule(
        project / ".cursor/rules/manual.mdc",
        "description: Naming conventions\nalwaysApply: false",
        "MANUAL-RULE-BODY",
    )
    _write_rule(
        project / ".cursor/rules/both.mdc",
        "description: Always and globs\nglobs: **/*.py\nalwaysApply: true",
        "BOTH-RULE-BODY",
    )
    (project / ".cursor/rules/broken.mdc").write_text("not a rule\n")
    (project / ".cursor/rules/bad.mdc").write_text(
        "---\nalwaysApply: yes\n---\nBAD-RULE\n"
    )
    module = project / "src/pkg/mod.py"
    module.parent.mkdir(parents=True)
    module.write_text("print(1)\n")
    (project / "notes.md").write_text("hello\n")
    (project / "tests").mkdir()
    (project / "tests/test_mod.py").write_text("def test_mod():\n    pass\n")

    granted = agent.Session(workspace=tmp_path / "granted", project=project)
    assert "Project rules" not in granted.messages[0]["content"]
    assert "ALWAYS-RULE-BODY" not in granted.messages[0]["content"]
    assert "Python files" not in granted.messages[0]["content"]
    granted.skills.load_skill("file-ops")
    granted_calls = {"n": 0}

    def granted_create(**kwargs):
        granted_calls["n"] += 1
        if granted_calls["n"] == 1:
            reply = SimpleNamespace(
                content="",
                tool_calls=[
                    SimpleNamespace(
                        id="g1",
                        function=SimpleNamespace(
                            name="read_file",
                            arguments=json.dumps({"path": "src/pkg/mod.py"}),
                        ),
                    )
                ],
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=reply)])
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="plain", tool_calls=None)
                )
            ]
        )

    granted_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=granted_create))
    )
    assert agent.run("read the module", granted, client=granted_client) == "plain"
    granted_tool = next(
        message for message in granted.messages if message["role"] == "tool"
    )
    assert "print(1)" in granted_tool["content"]
    assert "GLOB-RULE-BODY" not in granted_tool["content"]
    assert "NESTED-RULE-BODY" not in granted_tool["content"]

    session = agent.Session(workspace=tmp_path / "ws", project=project, coding=True)
    prompt = session.messages[0]["content"]
    assert "Project rules (always apply)" in prompt
    assert "ALWAYS-RULE-BODY" in prompt
    assert "BOTH-RULE-BODY" in prompt
    assert "Python files" in prompt
    assert "src/**/*.py" in prompt
    assert "GLOB-RULE-BODY" not in prompt
    assert "NESTED-RULE-BODY" not in prompt
    assert "Naming conventions" in prompt
    assert "read that .mdc file" in prompt
    assert "MANUAL-RULE-BODY" not in prompt
    assert "BAD-RULE" not in prompt
    assert "not a rule" not in prompt
    assert rules.glob_matches("src/pkg/mod.py", "src/pkg/**")
    assert rules.glob_matches("tests/test_mod.py", "tests/**/*.py")

    session.skills.load_skill("file-ops")
    calls = {"n": 0}

    def create(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            message = SimpleNamespace(
                content="",
                tool_calls=[
                    SimpleNamespace(
                        id="c1",
                        function=SimpleNamespace(
                            name="read_file",
                            arguments=json.dumps({"path": "src/pkg/mod.py"}),
                        ),
                    ),
                    SimpleNamespace(
                        id="c2",
                        function=SimpleNamespace(
                            name="read_file",
                            arguments=json.dumps({"path": "src/pkg/mod.py"}),
                        ),
                    ),
                ],
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])
        if calls["n"] == 2:
            message = SimpleNamespace(
                content="",
                tool_calls=[
                    SimpleNamespace(
                        id="c3",
                        function=SimpleNamespace(
                            name="read_file",
                            arguments=json.dumps({"path": "notes.md"}),
                        ),
                    )
                ],
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="done", tool_calls=None)
                )
            ]
        )

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    assert agent.run("read the module", session, client=client) == "done"
    tool_messages = [
        message for message in session.messages if message["role"] == "tool"
    ]
    assert "print(1)" in tool_messages[0]["content"]
    assert "GLOB-RULE-BODY" in tool_messages[0]["content"]
    assert "NESTED-RULE-BODY" in tool_messages[0]["content"]
    assert "BOTH-RULE-BODY" not in tool_messages[0]["content"]
    assert "ALWAYS-RULE-BODY" not in tool_messages[0]["content"]
    assert "GLOB-RULE-BODY" not in tool_messages[1]["content"]
    assert "hello" in tool_messages[2]["content"]
    assert "GLOB-RULE-BODY" not in tool_messages[2]["content"]
    assert "NESTED-RULE-BODY" not in tool_messages[2]["content"]

    assert (
        rules.append_matching_rules(project, {"path": "src"}, "files", set()) == "files"
    )
    assert (
        rules.append_matching_rules(
            project, {"command": "cat src/pkg/mod.py"}, "ok", set()
        )
        == "ok"
    )
    tests_hit = rules.append_matching_rules(
        project, {"path": "tests/test_mod.py"}, "code", set()
    )
    assert "GLOB-RULE-BODY" in tests_hit
    assert "NESTED-RULE-BODY" not in tests_hit


def test_project_rules_skip_escapes_and_sessions_without_a_project(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    outside = tmp_path / "outside.mdc"
    outside.write_text(
        "---\ndescription: leaked\nalwaysApply: true\n---\nOUTSIDE-RULE\n"
    )
    rules_dir = project / ".cursor" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "linked.mdc").symlink_to(outside)
    assert rules.load_project_rules(project) == []

    workspace = tmp_path / "ws"
    _write_rule(
        workspace / ".cursor/rules/local.mdc",
        "description: local\nalwaysApply: true",
        "WORKSPACE-RULE",
    )
    plain = agent.Session(workspace=workspace)
    assert "WORKSPACE-RULE" not in plain.messages[0]["content"]
    assert "Project rules" not in plain.messages[0]["content"]

    session = agent.Session(workspace=tmp_path / "agent", project=project, coding=True)
    assert "OUTSIDE-RULE" not in session.messages[0]["content"]
    _write_rule(
        project / ".cursor/rules/later.mdc",
        "description: later\nalwaysApply: true",
        "LATER-RULE",
    )
    granted = agent.Session(workspace=tmp_path / "granted", project=project)
    assert "LATER-RULE" not in granted.messages[0]["content"]
    session.sync_prompt()
    assert "LATER-RULE" in session.messages[0]["content"]
    session.applied_rules.add(".cursor/rules/later.mdc")
    session.reset()
    assert session.applied_rules == set()
    assert "LATER-RULE" in session.messages[0]["content"]


def test_rule_globs_accept_a_yaml_list(tmp_path):
    project = tmp_path / "proj"
    path = project / ".cursor/rules/view.mdc"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\n"
        "description: Views\n"
        "globs:\n"
        '  - "**/*.tsx"\n'
        '  - "src/**/*.ts"\n'
        "alwaysApply: false\n"
        "---\n"
        "VIEW-RULE\n"
    )
    loaded = rules.load_project_rules(project)
    assert loaded[0].globs == ("**/*.tsx", "src/**/*.ts")
    applied: set[str] = set()
    result = rules.append_matching_rules(
        project, {"path": "src/App.tsx"}, "file", applied
    )
    assert "VIEW-RULE" in result
    assert applied == {".cursor/rules/view.mdc"}


def test_sandbox_refuses_to_run_off_macos(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", "linux")

    def fail_popen(*args, **kwargs):
        raise AssertionError("command ran without a sandbox")

    monkeypatch.setattr(tools.process.subprocess, "Popen", fail_popen)

    result = execute_tool("run_shell", {"command": "true"}, tmp_path)

    assert result == "Error: sandbox is unavailable"
