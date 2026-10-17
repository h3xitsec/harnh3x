import fnmatch
import os
import re
from pathlib import Path

from harnh3x.config import (
    FETCH_MAX_BYTES,
    LIST_FILES_MAX,
    MAX_TOOL_RESULT,
    SEARCH_FILES_MAX,
    TRANSCRIPT_FILE,
)
from harnh3x.tools.paths import (
    _count_arg,
    _existing_file,
    _path_param,
    _positive_int,
    _relative,
    _text_arg,
    cap_result,
    resolve_in_workspace,
    resolve_readable,
)

SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a text file. Long files are paged: the result ends with the "
                "next offset to pass to continue reading."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": _path_param("Path to the file"),
                    "offset": {
                        "type": "integer",
                        "description": "First line to return, 1-based (default 1)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of lines to return",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write content to a file, creating parent directories, and replace "
                "any existing content. A .py file is checked for syntax; a syntax "
                "error is reported and the file is still written."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": _path_param("Path to the file"),
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace an exact text fragment in a file, leaving the rest untouched. "
                "old_text must match exactly once unless replace_all is true, so "
                "include enough surrounding text to make it unique. A .py file is "
                "checked for syntax after the edit."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": _path_param("Path to the file"),
                    "old_text": {
                        "type": "string",
                        "description": "Exact text to find",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "Replacement text",
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "Replace every match (default false)",
                    },
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_file",
            "description": "Append content to the end of a file (creates it if missing)",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": _path_param("Path to the file"),
                    "content": {"type": "string", "description": "Content to append"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a file. Directories are refused.",
            "parameters": {
                "type": "object",
                "properties": {"path": _path_param("Path to the file")},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_file",
            "description": "Move or rename a file. Fails if the destination exists.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": _path_param("Current path of the file"),
                    "destination": _path_param("New path for the file"),
                },
                "required": ["source", "destination"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "List files and directories with sizes. Use it before guessing a "
                "path. Skips .git, .venv, node_modules, and caches."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": _path_param("Directory to list (default: the root)"),
                    "pattern": {
                        "type": "string",
                        "description": "Glob on file names, for example *.md",
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "How many directory levels to descend (default 3)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": (
                "Search UTF-8 text files with a regular expression. Returns "
                "path:line: text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regular expression to search for",
                    },
                    "path": _path_param("Directory to search (default: the root)"),
                    "glob": {
                        "type": "string",
                        "description": "Only search file names matching this glob, for example *.py",
                    },
                    "ignore_case": {
                        "type": "boolean",
                        "description": "Case-insensitive match (default false)",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
]

# Directories that list_files and search_files never descend into.
SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".tmp"}


def read_file(workspace: Path, args: dict) -> str:
    path = resolve_readable(workspace, args.get("path", ""))
    if not path.exists():
        return "Error: not found"
    if path.is_dir():
        return "Error: is a directory"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    total = len(lines)
    offset = _positive_int(args.get("offset"), "offset", 1)
    limit = _positive_int(args.get("limit"), "limit", 0)
    if offset > total and total:
        return f"Error: offset {offset} is past the last line ({total})"
    chosen = lines[offset - 1 :]
    if limit:
        chosen = chosen[:limit]
    body = "".join(chosen)
    end = offset - 1 + len(chosen)

    def marker(last: int) -> str:
        return (
            f"\n...[showing lines {offset}-{last} of {total}; "
            f"call read_file with offset={last + 1} to continue]"
        )

    if len(body) <= MAX_TOOL_RESULT:
        if end >= total:
            return body
        return body + marker(end)
    kept = []
    size = 0
    budget = MAX_TOOL_RESULT - 160
    for line in chosen:
        if size + len(line) > budget:
            break
        kept.append(line)
        size += len(line)
    if kept:
        last = offset - 1 + len(kept)
        return "".join(kept) + marker(last)
    return cap_result(
        body,
        hint=f"line {offset} alone exceeds the limit; use search_files or run_python to read it",
    )


def _python_syntax_note(path: Path) -> str:
    if path.suffix != ".py":
        return ""
    try:
        compile(path.read_text(encoding="utf-8"), path.name, "exec")
    except SyntaxError as exc:
        detail = exc.msg or "invalid syntax"
        if exc.lineno:
            detail = f"line {exc.lineno}: {detail}"
        return f"\nSyntax error: {detail}"
    return ""


def write_file(workspace: Path, args: dict) -> str:
    path = resolve_in_workspace(workspace, args.get("path", ""))
    content = args.get("content")
    if not isinstance(content, str):
        return "Error: content is required"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode("utf-8")
    path.write_bytes(data)
    return f"Wrote {len(data)} bytes to {path}" + _python_syntax_note(path)


def edit_file(workspace: Path, args: dict) -> str:
    path = _existing_file(workspace, args.get("path", ""))
    old = args.get("old_text")
    new = args.get("new_text")
    if not isinstance(old, str) or not old:
        return "Error: old_text is required"
    if not isinstance(new, str):
        return "Error: new_text is required"
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    shown = _relative(workspace, path)
    if count == 0:
        return f"Error: old_text not found in {shown}"
    replace_all = args.get("replace_all") is True
    if count > 1 and not replace_all:
        return (
            f"Error: old_text matches {count} places in {shown}; "
            "include more surrounding text or set replace_all"
        )
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    path.write_text(updated, encoding="utf-8")
    replaced = count if replace_all else 1
    return f"Replaced {replaced} occurrence(s) in {shown}" + _python_syntax_note(path)


def append_file(workspace: Path, args: dict) -> str:
    path = resolve_in_workspace(workspace, args.get("path", ""))
    content = args.get("content")
    if not isinstance(content, str):
        return "Error: content is required"
    if path.is_dir():
        return "Error: is a directory"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode("utf-8")
    with path.open("ab") as handle:
        handle.write(data)
    return f"Appended {len(data)} bytes to {path}"


def delete_file(workspace: Path, args: dict) -> str:
    path = resolve_in_workspace(workspace, args.get("path", ""))
    if not path.exists():
        return "Error: not found"
    if path.is_dir():
        return "Error: is a directory"
    path.unlink()
    return f"Deleted {path}"


def move_file(workspace: Path, args: dict) -> str:
    source = resolve_in_workspace(workspace, args.get("source", ""))
    destination = resolve_in_workspace(workspace, args.get("destination", ""))
    if not source.exists():
        return "Error: not found"
    if source.is_dir():
        return "Error: is a directory"
    if destination.exists():
        return "Error: destination exists"
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.rename(destination)
    return (
        f"Moved {_relative(workspace, source)} to {_relative(workspace, destination)}"
    )


def _walk(root: Path, max_depth: int):
    for current, dirs, files in os.walk(root):
        current_path = Path(current)
        depth = len(current_path.relative_to(root).parts)
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        if depth >= max_depth - 1:
            yield current_path, list(dirs), sorted(files)
            dirs[:] = []
            continue
        yield current_path, list(dirs), sorted(files)


def _directory_arg(workspace: Path, raw) -> Path:
    path = resolve_readable(workspace, raw if isinstance(raw, str) and raw else ".")
    if not path.exists():
        raise ValueError("not found")
    if not path.is_dir():
        raise ValueError("not a directory")
    return path


def list_files(workspace: Path, args: dict) -> str:
    root = _directory_arg(workspace, args.get("path"))
    pattern = args.get("pattern")
    if pattern is not None and not isinstance(pattern, str):
        return "Error: pattern must be a string"
    max_depth = _count_arg(args.get("max_depth"), "max_depth", 3, 20)
    entries: list[str] = []
    overflow = 0
    for current, dirs, files in _walk(root, max_depth):
        rel_dir = current.relative_to(root)
        for name in dirs:
            if pattern:
                continue
            entries.append(f"{(rel_dir / name).as_posix()}/")
        for name in files:
            if pattern and not fnmatch.fnmatch(name, pattern):
                continue
            path = current / name
            try:
                size = path.stat().st_size
            except OSError:
                continue
            entries.append(f"{(rel_dir / name).as_posix()} ({size} bytes)")
    if not entries:
        return "No files found."
    if len(entries) > LIST_FILES_MAX:
        overflow = len(entries) - LIST_FILES_MAX
        entries = entries[:LIST_FILES_MAX]
    text = "\n".join(entries)
    if overflow:
        text += f"\n...[{overflow} more entries; narrow with path or pattern]"
    return cap_result(text)


def search_files(workspace: Path, args: dict) -> str:
    pattern = _text_arg(args, "pattern")
    flags = re.IGNORECASE if args.get("ignore_case") is True else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        return f"Error: invalid pattern: {exc}"
    root = _directory_arg(workspace, args.get("path"))
    glob = args.get("glob")
    if glob is not None and not isinstance(glob, str):
        return "Error: glob must be a string"
    matches: list[str] = []
    capped = False
    for current, _dirs, files in _walk(root, 50):
        for name in files:
            if glob and not fnmatch.fnmatch(name, glob):
                continue
            path = current / name
            try:
                if path.stat().st_size > FETCH_MAX_BYTES:
                    continue
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            shown = path.relative_to(root).as_posix()
            for number, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append(f"{shown}:{number}: {line.strip()[:200]}")
                    if len(matches) >= SEARCH_FILES_MAX:
                        capped = True
                        break
            if capped:
                break
        if capped:
            break
    if not matches:
        return "No matches."
    text = "\n".join(matches)
    if capped:
        text += (
            f"\n...[stopped at {SEARCH_FILES_MAX} matches; narrow the pattern or path]"
        )
    return cap_result(text)


def workspace_listing(workspace: Path, limit: int = 20) -> str:
    try:
        entries = sorted(
            workspace.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
        )
    except OSError:
        return "The workspace could not be listed."
    names = []
    for entry in entries:
        if entry.name.startswith(".") or entry.name in {"session.log", TRANSCRIPT_FILE}:
            continue
        names.append(f"{entry.name}/" if entry.is_dir() else entry.name)
    if not names:
        return "The workspace is empty."
    shown = ", ".join(names[:limit])
    extra = len(names) - limit
    if extra > 0:
        shown += f", and {extra} more"
    return f"Workspace files: {shown}"


FNS = {
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "append_file": append_file,
    "delete_file": delete_file,
    "move_file": move_file,
    "list_files": list_files,
    "search_files": search_files,
}
