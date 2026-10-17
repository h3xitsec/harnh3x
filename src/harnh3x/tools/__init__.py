import json
from pathlib import Path

from harnh3x.tools.data import FNS as _DATA_FNS
from harnh3x.tools.data import SCHEMAS as _DATA_SCHEMAS
from harnh3x.tools.documents import FNS as _DOCUMENT_FNS
from harnh3x.tools.documents import SCHEMAS as _DOCUMENT_SCHEMAS
from harnh3x.tools.feed import FNS as _FEED_FNS
from harnh3x.tools.feed import SCHEMAS as _FEED_SCHEMAS
from harnh3x.tools.files import FNS as _FILE_FNS
from harnh3x.tools.files import SCHEMAS as _FILE_SCHEMAS
from harnh3x.tools.files import workspace_listing
from harnh3x.tools.http import FNS as _HTTP_FNS
from harnh3x.tools.http import SCHEMAS as _HTTP_SCHEMAS
from harnh3x.tools.http import get_json
from harnh3x.tools.memory import FNS as _MEMORY_FNS
from harnh3x.tools.memory import SCHEMAS as _MEMORY_SCHEMAS
from harnh3x.tools.memory import memory_keys
from harnh3x.tools.page import FNS as _PAGE_FNS
from harnh3x.tools.page import SCHEMAS as _PAGE_SCHEMAS
from harnh3x.tools.page import extract_page, strip_html
from harnh3x.tools.paths import _project, _yolo
from harnh3x.tools.process import FNS as _PROCESS_FNS
from harnh3x.tools.process import SCHEMAS as _PROCESS_SCHEMAS
from harnh3x.tools.reference import FNS as _REFERENCE_FNS
from harnh3x.tools.reference import SCHEMAS as _REFERENCE_SCHEMAS
from harnh3x.tools.search import FNS as _SEARCH_FNS
from harnh3x.tools.search import SCHEMAS as _SEARCH_SCHEMAS
from harnh3x.tools.system import FNS as _SYSTEM_FNS
from harnh3x.tools.system import SCHEMAS as _SYSTEM_SCHEMAS
from harnh3x.tools.tasks import FNS as _TASK_FNS
from harnh3x.tools.tasks import SCHEMAS as _TASK_SCHEMAS

_SCHEMAS = {
    schema["function"]["name"]: schema
    for schema in (
        *_FILE_SCHEMAS,
        *_PROCESS_SCHEMAS,
        *_SEARCH_SCHEMAS,
        *_PAGE_SCHEMAS,
        *_HTTP_SCHEMAS,
        *_FEED_SCHEMAS,
        *_REFERENCE_SCHEMAS,
        *_DOCUMENT_SCHEMAS,
        *_DATA_SCHEMAS,
        *_MEMORY_SCHEMAS,
        *_TASK_SCHEMAS,
        *_SYSTEM_SCHEMAS,
    )
}
_FNS = {
    **_FILE_FNS,
    **_PROCESS_FNS,
    **_SEARCH_FNS,
    **_PAGE_FNS,
    **_HTTP_FNS,
    **_FEED_FNS,
    **_REFERENCE_FNS,
    **_DOCUMENT_FNS,
    **_DATA_FNS,
    **_MEMORY_FNS,
    **_TASK_FNS,
    **_SYSTEM_FNS,
}
_ORDER = (
    "read_file",
    "write_file",
    "edit_file",
    "append_file",
    "delete_file",
    "move_file",
    "list_files",
    "search_files",
    "run_shell",
    "run_python",
    "run_script",
    "system_info",
    "web_search",
    "read_url",
    "fetch_url",
    "parse_html",
    "read_feed",
    "wikipedia_search",
    "wikipedia_summary",
    "http_get_json",
    "http_request",
    "read_pdf",
    "read_docx",
    "inspect_csv",
    "inspect_json",
    "remember",
    "recall",
    "write_plan",
    "update_plan",
    "show_plan",
)

TOOLS = [_SCHEMAS[name] for name in _ORDER]
TOOL_FNS = {name: _FNS[name] for name in _ORDER}


def execute_tool(
    name: str,
    args: dict,
    workspace: Path,
    *,
    yolo: bool = False,
    project: Path | None = None,
) -> str:
    yolo_token = _yolo.set(yolo)
    project_token = _project.set(project)
    try:
        fn = TOOL_FNS.get(name)
        if fn is None:
            return f"Error: Unknown tool '{name}'"
        try:
            return fn(workspace, args)
        except ValueError as exc:
            return f"Error: {exc}"
        except OSError as exc:
            return f"Error: {exc}"
    finally:
        _project.reset(project_token)
        _yolo.reset(yolo_token)


def _tool_result(
    name: str,
    arguments: str,
    workspace: Path,
    registry=None,
    *,
    yolo: bool = False,
    project: Path | None = None,
) -> tuple[dict | None, str]:
    try:
        args = json.loads(arguments)
    except json.JSONDecodeError as exc:
        return None, f"Error: invalid tool arguments: {exc}"
    if not isinstance(args, dict):
        return None, "Error: tool arguments must be an object"
    if registry is None:
        return args, execute_tool(name, args, workspace, yolo=yolo, project=project)
    return args, registry.execute(name, args, workspace, yolo=yolo, project=project)
