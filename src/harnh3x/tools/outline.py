import ast
from pathlib import Path

from harnh3x.config import FETCH_MAX_BYTES
from harnh3x.tools.files import _walk
from harnh3x.tools.paths import (
    _count_arg,
    _path_param,
    _relative,
    cap_result,
    resolve_readable,
)

OUTLINE_MAX_FILES = 40
_SIGNATURE_LIMIT = 80

SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "outline_python",
            "description": (
                "Outline a Python file, or every .py file under a directory: "
                "classes, functions, and nested definitions. Each line is a "
                "signature and its start-end lines, including decorators. "
                "Read that span with read_file: offset is the start line and "
                "limit is end - start + 1. Skips .git, __pycache__, .venv, "
                "node_modules, and .tmp. At most 40 files; a longer result "
                "says to narrow path or lower max_depth."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": _path_param("Python file or directory to outline"),
                    "max_depth": {
                        "type": "integer",
                        "description": (
                            "How many directory levels to descend "
                            "(default 3, max 20). Ignored for a file."
                        ),
                    },
                },
                "required": ["path"],
            },
        },
    },
]


def outline_python(workspace: Path, args: dict) -> str:
    path = resolve_readable(workspace, args.get("path", ""))
    if not path.exists():
        return "Error: not found"
    max_depth = _count_arg(args.get("max_depth"), "max_depth", 3, 20)
    if path.is_file():
        text = _outline_single(workspace, path)
    elif path.is_dir():
        text = _outline_directory(workspace, path, max_depth)
    else:
        return "Error: not found"
    if text.startswith("Error:"):
        return text
    return cap_result(text, hint="narrow path or lower max_depth")


def _outline_single(workspace: Path, path: Path) -> str:
    if path.suffix != ".py":
        return "Error: not a Python file"
    shown = _shown(workspace, path)
    try:
        body = _file_body(path)
    except UnicodeDecodeError:
        return "Error: not utf-8"
    syntax = _syntax_message(body)
    if syntax is not None:
        return f"Error: {syntax}"
    return shown + "\n" + "\n".join(body)


def _outline_directory(workspace: Path, root: Path, max_depth: int) -> str:
    files, extra = _python_files(root, max_depth)
    if not files:
        return "No Python files found."
    blocks = []
    for path in files:
        shown = _shown(workspace, path)
        try:
            body = _file_body(path)
        except UnicodeDecodeError:
            body = ["  skipped: not utf-8"]
        except OSError as exc:
            body = [f"  skipped: {exc}"]
        blocks.append(shown + "\n" + "\n".join(body))
    text = "\n".join(blocks)
    if extra:
        text += f"\n...[{extra} more files; narrow path or lower max_depth]"
    return text


def _python_files(root: Path, max_depth: int) -> tuple[list[Path], int]:
    found: list[Path] = []
    extra = 0
    for current, _dirs, files in _walk(root, max_depth):
        for name in files:
            if not name.endswith(".py"):
                continue
            if len(found) >= OUTLINE_MAX_FILES:
                extra += 1
                continue
            found.append(current / name)
    return found, extra


def _file_body(path: Path) -> list[str]:
    if path.stat().st_size > FETCH_MAX_BYTES:
        return [f"  skipped: larger than {FETCH_MAX_BYTES} bytes"]
    return _outline_text(path.read_text(encoding="utf-8"))


def _shown(workspace: Path, path: Path) -> str:
    return Path(_relative(workspace, path)).as_posix()


def _outline_text(text: str) -> list[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [_syntax_line(exc)]
    lines = _definitions(tree.body, 0)
    if not lines:
        return ["  no classes or functions"]
    return lines


def _syntax_line(exc: SyntaxError) -> str:
    detail = exc.msg or "invalid syntax"
    if exc.lineno:
        return f"  syntax error: line {exc.lineno}: {detail}"
    return f"  syntax error: {detail}"


def _syntax_message(lines: list[str]) -> str | None:
    if len(lines) == 1 and lines[0].startswith("  syntax error:"):
        return lines[0].strip()
    return None


def _definitions(body: list[ast.stmt], depth: int) -> list[str]:
    lines: list[str] = []
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            lines.append(_definition(node, depth))
            lines.extend(_definitions(node.body, depth + 1))
            continue
        # A def inside if/for/try stays at this depth; only a def or class nests.
        for child in _child_bodies(node):
            lines.extend(_definitions(child, depth))
    return lines


def _child_bodies(node: ast.AST) -> list[list[ast.stmt]]:
    bodies: list[list[ast.stmt]] = []
    for attr in ("body", "orelse", "finalbody"):
        value = getattr(node, attr, None)
        if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
            bodies.append(value)
    for handler in getattr(node, "handlers", ()):
        handler_body = getattr(handler, "body", None)
        if handler_body:
            bodies.append(handler_body)
    for case in getattr(node, "cases", ()):
        case_body = getattr(case, "body", None)
        if case_body:
            bodies.append(case_body)
    return bodies


def _definition(node: ast.AST, depth: int) -> str:
    start, end = _span(node)
    indent = "  " * (depth + 1)
    deco = _decorators(node)
    suffix = f"  {deco}" if deco else ""
    return f"{indent}{_clip(_signature(node))}  {start}-{end}{suffix}"


def _span(node: ast.AST) -> tuple[int, int]:
    start = node.lineno
    for deco in getattr(node, "decorator_list", ()):
        start = min(start, deco.lineno)
    end = node.end_lineno if node.end_lineno is not None else node.lineno
    return start, end


def _signature(node: ast.AST) -> str:
    if isinstance(node, ast.ClassDef):
        if not node.bases:
            return f"class {node.name}"
        bases = ", ".join(ast.unparse(base) for base in node.bases)
        return f"class {node.name}({bases})"
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    args = ast.unparse(node.args)
    if node.returns is None:
        return f"{prefix} {node.name}({args})"
    return f"{prefix} {node.name}({args}) -> {ast.unparse(node.returns)}"


def _decorators(node: ast.AST) -> str:
    parts = ["@" + ast.unparse(deco) for deco in getattr(node, "decorator_list", ())]
    if not parts:
        return ""
    return _clip(" ".join(parts))


def _clip(text: str, limit: int = _SIGNATURE_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


FNS = {
    "outline_python": outline_python,
}
