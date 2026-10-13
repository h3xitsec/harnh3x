import contextvars
from pathlib import Path

from harnh3x.config import MAX_TOOL_RESULT

_yolo: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "harnh3x_yolo", default=False
)
_project: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "harnh3x_project", default=None
)


def _path_param(description: str) -> dict:
    return {"type": "string", "description": description}


def cap_result(text: str, limit: int = MAX_TOOL_RESULT, hint: str = "") -> str:
    if len(text) <= limit:
        return text
    suffix = f"; {hint}" if hint else ""
    omitted = len(text) - limit
    marker = f"\n...[truncated, {omitted} chars omitted{suffix}]"
    keep = limit - len(marker)
    while keep >= 0 and len(text) - keep != omitted:
        omitted = len(text) - keep
        marker = f"\n...[truncated, {omitted} chars omitted{suffix}]"
        keep = limit - len(marker)
    if keep < 0:
        return f"...[truncated, {len(text)} chars omitted{suffix}]"[:limit]
    return text[:keep] + marker


def resolve_in_workspace(
    workspace: Path, raw: str, *, include_project: bool = True
) -> Path:
    return _resolve_path(
        workspace, raw, allow_outside=False, include_project=include_project
    )


def resolve_readable(workspace: Path, raw: str) -> Path:
    return _resolve_path(workspace, raw, allow_outside=_yolo.get())


def _resolve_path(
    workspace: Path, raw: str, *, allow_outside: bool, include_project: bool = True
) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("path is required")
    path = Path(raw)
    if not path.is_absolute():
        path = _relative_base(workspace, include_project=include_project) / path
    resolved = path.resolve()
    if allow_outside or _inside_roots(
        workspace, resolved, include_project=include_project
    ):
        return resolved
    raise ValueError("outside workspace")


def _granted_project() -> Path | None:
    project = _project.get()
    if project is None:
        return None
    return project.resolve()


def _relative_base(workspace: Path, *, include_project: bool) -> Path:
    project = _granted_project() if include_project else None
    if project is not None:
        return project
    return workspace.resolve()


def _inside_roots(workspace: Path, path: Path, *, include_project: bool) -> bool:
    roots = [workspace.resolve()]
    if include_project:
        project = _granted_project()
        if project is not None:
            roots.append(project)
    return any(path.is_relative_to(root) for root in roots)


def _relative(workspace: Path, path: Path) -> str:
    resolved = path.resolve()
    bases = []
    project = _granted_project()
    if project is not None:
        bases.append(project)
    bases.append(workspace.resolve())
    for base in bases:
        try:
            return str(resolved.relative_to(base))
        except ValueError:
            continue
    return str(resolved)


def _positive_int(raw, name: str, default: int) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise ValueError(f"{name} must be a positive integer")
    return raw


def _count_arg(raw, name: str, default: int, cap: int) -> int:
    return min(_positive_int(raw, name, default), cap)


def _text_arg(args: dict, name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value


def _existing_file(workspace: Path, raw) -> Path:
    return _require_file(
        resolve_in_workspace(workspace, raw if isinstance(raw, str) else "")
    )


def _readable_file(workspace: Path, raw) -> Path:
    return _require_file(
        resolve_readable(workspace, raw if isinstance(raw, str) else "")
    )


def _require_file(path: Path) -> Path:
    if not path.exists():
        raise ValueError("not found")
    if path.is_dir():
        raise ValueError("is a directory")
    return path
