import re
from pathlib import Path

from harnh3x.config import PLAN_FILE

SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "write_plan",
            "description": f"Write a numbered checklist of steps to {PLAN_FILE}, replacing any earlier plan",
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Steps in order",
                    },
                },
                "required": ["steps"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_plan",
            "description": "Mark a plan step done, todo, or skipped and return the plan",
            "parameters": {
                "type": "object",
                "properties": {
                    "step": {"type": "integer", "description": "Step number, 1-based"},
                    "status": {
                        "type": "string",
                        "description": "done, todo, or skipped",
                    },
                    "note": {
                        "type": "string",
                        "description": "Optional short note to attach to the step",
                    },
                },
                "required": ["step", "status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_plan",
            "description": "Return the current plan and how many steps are done",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

PLAN_MARKS = {"done": "x", "todo": " ", "skipped": "-"}
_PLAN_LINE = re.compile(r"^(\d+)\. \[([ x\-])\] (.*)$")


def _plan_path(workspace: Path) -> Path:
    return workspace.resolve() / PLAN_FILE


def _read_plan(path: Path) -> list[tuple[str, str]]:
    if not path.exists() or path.is_dir():
        return []
    steps = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _PLAN_LINE.match(line)
        if match:
            steps.append((match.group(2), match.group(3)))
    return steps


def _format_plan(steps: list[tuple[str, str]]) -> str:
    lines = ["# Plan", ""]
    for index, (mark, text) in enumerate(steps, start=1):
        lines.append(f"{index}. [{mark}] {text}")
    done = sum(1 for mark, _ in steps if mark == "x")
    skipped = sum(1 for mark, _ in steps if mark == "-")
    lines.append("")
    lines.append(f"{done}/{len(steps)} done, {skipped} skipped")
    return "\n".join(lines)


def _write_plan(path: Path, steps: list[tuple[str, str]]) -> None:
    lines = ["# Plan", ""]
    for index, (mark, text) in enumerate(steps, start=1):
        lines.append(f"{index}. [{mark}] {text}")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_plan(workspace: Path, args: dict) -> str:
    raw = args.get("steps")
    if not isinstance(raw, list) or not raw:
        return "Error: steps must be a non-empty list of strings"
    steps = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            return "Error: steps must be a non-empty list of strings"
        steps.append((" ", " ".join(item.split())))
    _write_plan(_plan_path(workspace), steps)
    return _format_plan(steps)


def update_plan(workspace: Path, args: dict) -> str:
    path = _plan_path(workspace)
    steps = _read_plan(path)
    if not steps:
        return "Error: no plan yet; call write_plan first"
    step = args.get("step")
    if (
        isinstance(step, bool)
        or not isinstance(step, int)
        or not 1 <= step <= len(steps)
    ):
        return f"Error: step must be a number from 1 to {len(steps)}"
    status = args.get("status")
    if status not in PLAN_MARKS:
        return "Error: status must be done, todo, or skipped"
    note = args.get("note")
    text = steps[step - 1][1]
    if isinstance(note, str) and note.strip():
        text = f"{text} ({' '.join(note.split())})"
    steps[step - 1] = (PLAN_MARKS[status], text)
    _write_plan(path, steps)
    return _format_plan(steps)


def show_plan(workspace: Path, args: dict) -> str:
    steps = _read_plan(_plan_path(workspace))
    if not steps:
        return "No plan yet. Call write_plan with a list of steps."
    return _format_plan(steps)


FNS = {
    "write_plan": write_plan,
    "update_plan": update_plan,
    "show_plan": show_plan,
}
