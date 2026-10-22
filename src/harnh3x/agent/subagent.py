"""Nested agents the primary agent can run with the task tool.

explore is read-only: it searches and reads the codebase and returns one answer.
"""

import json
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from harnh3x.agent.transcript import _log_fields
from harnh3x.config import MAX_TOOL_RESULT, SUBAGENT_MAX_TURNS
from harnh3x.tools import TOOLS, execute_tool
from harnh3x.tools.paths import cap_result
from harnh3x.tools.scope import scoped

EXPLORE_TOOLS = ("glob", "grep", "read_file", "list_files", "outline_python")
INTERRUPTED = "Error: interrupted"
_NESTED = "Error: a subagent cannot start another subagent."
_REQUIRED = ("description", "prompt", "subagent_type")
_depth: ContextVar[int] = ContextVar("harnh3x_subagent_depth", default=0)


def _explore_prompt(workspace: Path, project: Path | None) -> str:
    if project is None:
        where = (
            f"Search the workspace at {workspace}. Relative paths use the workspace."
        )
    else:
        where = (
            f"Search the project at {project}. Relative paths use the project. "
            f"The session workspace is {workspace}."
        )
    return (
        "You are a read-only explore agent. You cannot modify files.\n"
        "Find files with glob, search code with grep, list a directory with "
        "list_files, outline Python with outline_python, and read the files "
        "that answer the question with read_file.\n"
        "Search, then read. Do not answer from a filename alone.\n"
        "The final answer names the paths and the facts that were asked for.\n"
        f"{where}"
    )


@dataclass(frozen=True)
class Subagent:
    name: str
    tools: tuple[str, ...]
    prompt: Callable[[Path, Path | None], str]


EXPLORE = Subagent("explore", EXPLORE_TOOLS, _explore_prompt)
SUBAGENTS = {"explore": EXPLORE}

_TOOL_SCHEMAS = {tool["function"]["name"]: tool for tool in TOOLS}
_missing = [name for name in EXPLORE_TOOLS if name not in _TOOL_SCHEMAS]
if _missing:
    raise RuntimeError(f"explore tools missing from TOOLS: {', '.join(_missing)}")


def _task_description() -> str:
    return (
        "Run a subagent and return its final answer. "
        "explore is read-only: it finds files, searches code, and answers "
        "questions about the codebase. It cannot modify files. Use it for a "
        "broad codebase question. For one known file or one symbol, call glob, "
        "grep, or read_file yourself. The subagent does not see this "
        "conversation; put every path and question it needs in prompt."
    )


TASK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "task",
        "description": _task_description(),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Short title, shown while it runs.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Full task, including paths and what to return.",
                },
                "subagent_type": {
                    "type": "string",
                    "enum": list(SUBAGENTS),
                    "description": (
                        "explore: read-only codebase search and questions."
                    ),
                },
            },
            "required": list(_REQUIRED),
        },
    },
}


class _FixedTools:
    def __init__(self, names: tuple[str, ...], *, yolo: bool, project: Path | None):
        self._names = set(names)
        self._schemas = [
            scoped(_TOOL_SCHEMAS[name], yolo=yolo, project=project) for name in names
        ]

    def schemas(self) -> list[dict]:
        return list(self._schemas)

    def execute(
        self,
        name: str,
        args: dict,
        workspace: Path,
        *,
        yolo: bool = False,
        project: Path | None = None,
    ) -> str:
        if name not in self._names:
            return f"Error: Unknown tool '{name}'"
        return execute_tool(name, args, workspace, yolo=yolo, project=project)

    def loaded_names(self) -> list[str]:
        return []


class _ChildSession:
    """Enough of a Session for `_complete` without a transcript or MCP."""

    def __init__(self, parent, spec: Subagent):
        self.workspace = parent.workspace
        self.model = parent.model
        self.yolo = parent.yolo
        self.project = parent.project
        self.coding = False
        self.applied_rules: set[str] = set()
        self.abort = parent.abort
        self.context_tokens = None
        self.context_limit = parent.context_limit
        self.cache_rounds = []
        self.debug = False
        self.on_debug = None
        self._debug_system = None
        self.skills = _FixedTools(spec.tools, yolo=parent.yolo, project=parent.project)
        self.messages = [
            {
                "role": "system",
                "content": spec.prompt(parent.workspace, parent.project),
            }
        ]

    def sync_prompt(self) -> None:
        return None


def _parse_args(arguments: str) -> tuple[dict | None, str | None]:
    try:
        args = json.loads(arguments)
    except json.JSONDecodeError as exc:
        return None, f"Error: invalid tool arguments: {exc}"
    if not isinstance(args, dict):
        return None, "Error: tool arguments must be an object"
    return args, None


def _require(args: dict) -> str | None:
    for name in _REQUIRED:
        value = args.get(name)
        if not isinstance(value, str) or not value.strip():
            return f"Error: {name} is required"
    return None


def _known() -> str:
    return ", ".join(SUBAGENTS)


def _hook(session, name: str):
    hook = getattr(session, name, None)
    if callable(hook):
        return hook
    return None


def execute_task(session, arguments: str, client) -> tuple[dict | None, str]:
    args, error = _parse_args(arguments)
    if error is not None:
        return args, error
    assert args is not None
    if _depth.get() > 0:
        return args, _NESTED
    missing = _require(args)
    if missing is not None:
        return args, missing
    spec = SUBAGENTS.get(args["subagent_type"])
    if spec is None:
        shown = args["subagent_type"]
        return args, f"Error: Unknown subagent '{shown}'. Known subagents: {_known()}."
    answer = _run(session, spec, args["prompt"], args["description"].strip(), client)
    return args, cap_result(
        answer, limit=MAX_TOOL_RESULT, hint="ask a narrower question"
    )


def _run(parent, spec: Subagent, prompt: str, description: str, client) -> str:
    # Imported here so this module can load without the loop, which imports us.
    from harnh3x.agent.loop import _complete

    child = _ChildSession(parent, spec)
    child.messages.append({"role": "user", "content": prompt})
    start = _hook(parent, "on_subagent_start")
    if start is not None:
        start(spec.name, description)
    delta = _hook(parent, "on_subagent_delta")
    if delta is not None:
        child.on_delta = delta
    done = _hook(parent, "on_subagent_tool_done")
    if done is not None:
        child.on_tool_done = done
    turn = _hook(parent, "on_subagent_turn")
    if turn is not None:
        child.on_model_turn = turn
    tool = _hook(parent, "on_subagent_tool")
    depth_token = _depth.set(_depth.get() + 1)
    log_token = _log_fields.set({"subagent": spec.name})
    try:
        text = _complete(
            child,
            client,
            tool is not None,
            tool,
            max_turns=SUBAGENT_MAX_TURNS,
        )
    finally:
        _depth.reset(depth_token)
        _log_fields.reset(log_token)
    if parent.abort.is_set() or text == "":
        return INTERRUPTED
    return text
