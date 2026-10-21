"""The task tool and the read-only explore subagent."""

import json
from types import SimpleNamespace

from helpers import _tool_names

from harnh3x import agent
from harnh3x.agent import subagent
from harnh3x.agent.subagent import EXPLORE_TOOLS, INTERRUPTED
from harnh3x.config import TRANSCRIPT_FILE
from harnh3x.failures import MAX_TURNS_REACHED
from harnh3x.prompt import system_prompt
from harnh3x.skills import SkillRegistry
from harnh3x.tools import TOOLS


def _message(content="", calls=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls))
        ]
    )


def _call(name, arguments, call_id="call_1"):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _client(responder):
    seen = []

    def create(**kwargs):
        seen.append(kwargs)
        return responder(len(seen), kwargs)

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return seen, client


def _task_args(**overrides):
    payload = {
        "description": "Find answer",
        "prompt": "What does answer return?",
        "subagent_type": "explore",
    }
    payload.update(overrides)
    return json.dumps(payload)


def _events(workspace):
    text = (workspace / "session.log").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def test_task_is_always_available_and_not_a_domain_tool(tmp_path):
    session = agent.Session(workspace=tmp_path)
    names = _tool_names(session.skills.schemas())

    assert names[:3] == ["load_skill", "unload_skill", "task"]
    assert "task" not in [tool["function"]["name"] for tool in TOOLS]
    task = next(
        tool for tool in session.skills.schemas() if tool["function"]["name"] == "task"
    )
    parameters = task["function"]["parameters"]
    assert parameters["required"] == ["description", "prompt", "subagent_type"]
    assert parameters["properties"]["subagent_type"]["enum"] == ["explore"]
    collision = session.skills._collision(
        "extra",
        [{"type": "function", "function": {"name": "task"}}],
    )
    assert collision == "Error: tool 'task' from 'extra' collides with an existing tool"


def test_explore_reads_the_project_and_returns_one_answer(tmp_path, capsys):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "app.py").write_text("def answer():\n    return 1\n", encoding="utf-8")
    workspace = tmp_path / "work"
    session = agent.Session(workspace=workspace, project=project)
    deltas = []
    rounds = []
    tools = []
    finished = []
    debugs = []
    session.on_delta = lambda kind, text: deltas.append((kind, text))
    session.on_model_turn = rounds.append
    session.on_tool_done = lambda name, result: finished.append((name, result))
    session.debug = True
    session.on_debug = lambda system, request: debugs.append(system)

    def respond(n, kwargs):
        if n == 1:
            return _message("", [_call("task", _task_args())])
        if n == 2:
            return _message(
                "", [_call("read_file", json.dumps({"path": "app.py"}), "call_read")]
            )
        if n == 3:
            return _message("returns 1 in app.py")
        return _message("done")

    seen, client = _client(respond)

    text = agent.run(
        "Where is answer defined?",
        session,
        client=client,
        on_tool=lambda name, args: tools.append(name),
    )

    assert text == "done"
    assert _tool_names(seen[1]["tools"]) == list(EXPLORE_TOOLS)
    for blocked in ("write_file", "run_shell", "load_skill", "task"):
        assert blocked not in _tool_names(seen[1]["tools"])
    child_prompt = seen[1]["messages"][0]["content"]
    assert "You are a read-only explore agent" in child_prompt
    assert f"Search the project at {project}." in child_prompt
    assert "Relative paths use the project." in child_prompt
    tool_messages = [
        message["content"]
        for message in seen[2]["messages"]
        if message.get("role") == "tool"
    ]
    assert any("return 1" in content for content in tool_messages)
    assert finished == [("task", "returns 1 in app.py")]
    assert tools == ["task"]
    assert rounds == [1, 2]
    assert ("content", "done") in deltas
    assert all("returns 1 in app.py" not in piece for _kind, piece in deltas)
    assert all(
        system is None or "You are a read-only explore agent" not in system
        for system in debugs
    )
    assert capsys.readouterr().out == ""

    transcript = json.loads((workspace / TRANSCRIPT_FILE).read_text(encoding="utf-8"))
    blob = json.dumps(transcript)
    assert "returns 1 in app.py" in blob
    assert "You are a read-only explore agent" not in blob
    assert "def answer" not in blob

    events = _events(workspace)
    child = [event for event in events if event.get("subagent") == "explore"]
    assert any(
        event["event"] == "tool_call" and event["name"] == "read_file"
        for event in child
    )
    parent_task = [
        event
        for event in events
        if event.get("event") == "tool_call" and event.get("name") == "task"
    ]
    assert len(parent_task) == 1
    assert "subagent" not in parent_task[0]


def test_explore_reports_thinking_and_tools_apart_from_the_parent(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "app.py").write_text("return 1\n", encoding="utf-8")
    session = agent.Session(workspace=workspace)
    parent = []
    child = []
    session.on_delta = lambda kind, text: parent.append((kind, text))
    session.on_subagent_start = lambda kind, description: child.append(
        ("start", kind, description)
    )
    session.on_subagent_turn = lambda number: child.append(("turn", number))
    session.on_subagent_delta = lambda kind, text: child.append(("delta", kind, text))
    session.on_subagent_tool = lambda name, args: child.append(("tool", name, args))
    session.on_subagent_tool_done = lambda name, result: child.append(
        ("done", name, result)
    )

    def respond(n, kwargs):
        if n == 1:
            return _message("", [_call("task", _task_args(description="Find answer"))])
        if n == 2:
            message = SimpleNamespace(
                content="",
                reasoning="checking app.py",
                tool_calls=[
                    _call("read_file", json.dumps({"path": "app.py"}), "call_read")
                ],
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])
        if n == 3:
            return _message("returns 1")
        return _message("done")

    _seen, client = _client(respond)
    text = agent.run("where", session, client=client)

    assert text == "done"
    assert ("start", "explore", "Find answer") in child
    assert ("turn", 1) in child
    assert ("delta", "reasoning", "checking app.py") in child
    assert ("tool", "read_file", {"path": "app.py"}) in child
    assert any(
        item[0] == "done" and item[1] == "read_file" and "return 1" in item[2]
        for item in child
    )
    assert ("delta", "content", "returns 1") in child
    assert all("checking app.py" not in text for _kind, text in parent)
    assert ("content", "done") in parent


def test_explore_cannot_modify_files(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    target = project / "app.py"
    target.write_text("keep\n", encoding="utf-8")
    session = agent.Session(workspace=tmp_path / "work", project=project)

    def respond(n, kwargs):
        if n == 1:
            return _message("", [_call("task", _task_args(prompt="Overwrite app.py"))])
        if n == 2:
            return _message(
                "",
                [
                    _call(
                        "write_file",
                        json.dumps({"path": "app.py", "content": "changed\n"}),
                        "call_write",
                    )
                ],
            )
        if n == 3:
            return _message("blocked")
        return _message("ok")

    seen, client = _client(respond)

    text = agent.run("change it", session, client=client)

    assert text == "ok"
    assert target.read_text(encoding="utf-8") == "keep\n"
    assert not (project / "spawned.py").exists()
    errors = [
        message["content"]
        for message in seen[2]["messages"]
        if message.get("role") == "tool"
    ]
    assert errors == ["Error: Unknown tool 'write_file'"]


def test_unknown_subagent_and_bad_arguments_are_errors(tmp_path):
    session = agent.Session(workspace=tmp_path)

    def respond(n, kwargs):
        if n == 1:
            return _message(
                "",
                [_call("task", _task_args(subagent_type="general"))],
            )
        return _message("stopped")

    seen, client = _client(respond)
    text = agent.run("delegate", session, client=client)

    assert text == "stopped"
    assert len(seen) == 2
    result = session.messages[-2]["content"]
    assert result == "Error: Unknown subagent 'general'. Known subagents: explore."

    def missing(n, kwargs):
        if n == 1:
            return _message("", [_call("task", json.dumps({"description": "x"}))])
        return _message("nope")

    seen, client = _client(missing)
    agent.run("again", session, client=client)
    assert session.messages[-2]["content"] == "Error: prompt is required"
    assert len(seen) == 2

    args, error = subagent.execute_task(session, "not-json", client)
    assert args is None
    assert error.startswith("Error: invalid tool arguments:")
    args, error = subagent.execute_task(session, "[]", client)
    assert error == "Error: tool arguments must be an object"
    for field in ("description", "subagent_type"):
        body = json.loads(_task_args())
        del body[field]
        _args, error = subagent.execute_task(session, json.dumps(body), client)
        assert error == f"Error: {field} is required"
    _args, error = subagent.execute_task(session, _task_args(prompt="  "), client)
    assert error == "Error: prompt is required"


def test_abort_during_explore_stops_the_turn(tmp_path):
    workspace = tmp_path / "work"
    session = agent.Session(workspace=workspace)

    def respond(n, kwargs):
        if n == 1:
            return _message("", [_call("task", _task_args())])
        session.abort.set()
        return _message("child answer")

    seen, client = _client(respond)
    text = agent.run("look around", session, client=client)

    assert text == ""
    assert len(seen) == 2
    assert "Relative paths use the workspace." in seen[1]["messages"][0]["content"]
    assert session.messages[-1]["content"] == INTERRUPTED
    transcript = (workspace / TRANSCRIPT_FILE).read_text(encoding="utf-8")
    assert INTERRUPTED in transcript
    assert "You are a read-only explore agent" not in transcript
    assert any(event.get("subagent") == "explore" for event in _events(workspace))


def test_explore_stops_at_its_turn_cap_without_asking(tmp_path, monkeypatch):
    monkeypatch.setattr(subagent, "SUBAGENT_MAX_TURNS", 1)
    session = agent.Session(workspace=tmp_path)
    asked = []
    session.confirm_continue = lambda: asked.append(True) or False

    def respond(n, kwargs):
        if n == 1:
            return _message("", [_call("task", _task_args())])
        if n == 2:
            return _message("", [_call("glob", json.dumps({"pattern": "*.py"}))])
        return _message("gave up")

    _seen, client = _client(respond)
    text = agent.run("map the code", session, client=client)

    assert text == "gave up"
    assert asked == []
    assert session.messages[-2]["content"] == MAX_TURNS_REACHED


def test_a_subagent_cannot_start_another(tmp_path):
    session = agent.Session(workspace=tmp_path)
    token = subagent._depth.set(1)
    try:
        _args, result = subagent.execute_task(session, _task_args(), None)
    finally:
        subagent._depth.reset(token)

    assert result == "Error: a subagent cannot start another subagent."


def test_a_long_explore_answer_is_truncated(tmp_path, monkeypatch):
    monkeypatch.setattr(subagent, "MAX_TOOL_RESULT", 120)
    session = agent.Session(workspace=tmp_path)

    def respond(n, kwargs):
        if n == 1:
            return _message("", [_call("task", _task_args())])
        if n == 2:
            return _message("x" * 200)
        return _message("short")

    _seen, client = _client(respond)
    agent.run("summarize", session, client=client)

    result = session.messages[-2]["content"]
    assert "truncated" in result
    assert "ask a narrower question" in result
    assert len(result) <= 120


def test_several_task_calls_run_one_after_another(tmp_path):
    session = agent.Session(workspace=tmp_path)

    def respond(n, kwargs):
        if n == 1:
            return _message(
                "",
                [
                    _call("task", _task_args(prompt="first"), "call_a"),
                    _call("task", _task_args(prompt="second"), "call_b"),
                ],
            )
        if n == 2:
            return _message("one")
        if n == 3:
            return _message("two")
        return _message("both")

    _seen, client = _client(respond)
    text = agent.run("two questions", session, client=client)

    assert text == "both"
    results = [
        message["content"]
        for message in session.messages
        if message.get("role") == "tool"
    ]
    assert results == ["one", "two"]


def test_both_prompts_say_when_to_use_explore(tmp_path):
    plain = system_prompt(tmp_path, "MENU")
    project = tmp_path / "proj"
    project.mkdir()
    coding = system_prompt(tmp_path / "work", "MENU", project=project, coding=True)
    for prompt in (plain, coding):
        assert 'call task with subagent_type "explore"' in prompt
        assert "one known file or one symbol" in prompt
        assert "explore cannot change files" in prompt


def test_child_sync_prompt_keeps_the_explore_prompt(tmp_path):
    session = agent.Session(workspace=tmp_path)
    child = subagent._ChildSession(session, subagent.EXPLORE)
    original = child.messages[0]["content"]

    child.sync_prompt()

    assert child.messages[0]["content"] == original
    assert "list_files" in original
    assert child.skills.loaded_names() == []
    assert "write_file" not in _tool_names(child.skills.schemas())


def test_workspace_registry_still_lists_task_after_a_skill_loads(tmp_path):
    registry = SkillRegistry()
    registry.load_skill("file-ops")
    names = _tool_names(registry.schemas())
    assert names[:3] == ["load_skill", "unload_skill", "task"]
    assert "read_file" in names
    assert names.count("task") == 1
