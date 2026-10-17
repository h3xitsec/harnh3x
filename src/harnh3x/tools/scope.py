"""Session-dependent sentences appended to tool descriptions.

`TOOLS` stays mode-free. `SkillRegistry.schemas()` sends a copy of each schema
whose description ends with the clauses for the session's sandbox and project.
"""

import copy
from pathlib import Path

READ_TOOLS = (
    "read_file",
    "list_files",
    "search_files",
    "parse_html",
    "read_pdf",
    "read_docx",
    "inspect_csv",
    "inspect_json",
)
WRITE_TOOLS = ("write_file", "edit_file", "append_file", "delete_file", "move_file")
NET_TOOLS = (
    "web_search",
    "read_url",
    "fetch_url",
    "read_feed",
    "http_get_json",
    "http_request",
)

SCOPE: dict[str, tuple[str, ...]] = {
    **{name: ("read",) for name in READ_TOOLS},
    **{name: ("path",) for name in WRITE_TOOLS},
    **{name: ("net",) for name in NET_TOOLS},
    "run_python": ("process",),
    "run_script": ("path", "process"),
    "run_shell": ("shell", "process"),
}


def _either(project: Path | None) -> str:
    return "the workspace" if project is None else "the project or the workspace"


def _roots(project: Path | None) -> str:
    if project is None:
        return "the workspace"
    return f"{_either(project)}; relative paths use the project"


def _home(project: Path | None) -> str:
    return "the workspace" if project is None else "the project"


def _allowed(project: Path | None) -> str:
    return "the workspace" if project is None else "the project and the workspace"


def scope_clause(kind: str, *, yolo: bool, project: Path | None) -> str:
    if kind == "read":
        if not yolo:
            return f"Paths must be inside {_roots(project)}."
        if project is None:
            return "Any path may be read."
        return "Any path may be read; relative paths use the project."
    if kind == "path":
        return f"Paths must be inside {_roots(project)}."
    if kind == "process":
        if yolo:
            return (
                f"Runs in {_home(project)}. The sandbox is off: it may read or "
                "write any path and use the network."
            )
        return (
            f"Runs in {_home(project)}. The kernel sandbox allows only "
            f"{_allowed(project)} and no network."
        )
    if kind == "shell":
        if yolo:
            return (
                "Runs as /bin/sh -c, so pipes, redirects, `;`, `&&`, backticks, "
                "and `$` work."
            )
        return (
            "One program per call: pipes, redirects, `;`, `&&`, backticks, and "
            "`$` are blocked, and so are rm, mv, chmod, and sudo. Pass input "
            "through stdin. Arguments may not contain `..` or an absolute path "
            f"outside {_either(project)}."
        )
    if kind == "net":
        if yolo:
            return "Any host is allowed, including localhost and private addresses."
        return "Public hosts only; localhost and private addresses are refused."
    raise ValueError(f"unknown scope '{kind}'")


def scoped(schema: dict, *, yolo: bool, project: Path | None) -> dict:
    kinds = SCOPE.get(schema["function"]["name"], ())
    if not kinds:
        return schema
    clauses = " ".join(scope_clause(kind, yolo=yolo, project=project) for kind in kinds)
    result = copy.deepcopy(schema)
    base = result["function"].get("description", "").rstrip()
    if base and not base.endswith("."):
        base += "."
    result["function"]["description"] = f"{base} {clauses}".strip()
    return result
