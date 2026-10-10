import csv
import fnmatch
import hashlib
import html
import io
import ipaddress
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser
from pathlib import Path

import pypdf
import trafilatura

from harness.config import (
    FEED_MAX_ITEMS,
    FETCH_MAX_BYTES,
    FETCH_MAX_REDIRECTS,
    FETCH_TIMEOUT,
    LIST_FILES_MAX,
    MAX_TOOL_RESULT,
    MEMORY_FILE,
    PDF_MAX_PAGES,
    PLAN_FILE,
    PREVIEW_ROWS,
    SEARCH_FILES_MAX,
    SEARCH_MAX_RESULTS,
    SHELL_MAX_TIMEOUT,
    SHELL_TIMEOUT,
    TRANSCRIPT_FILE,
    WIKIPEDIA_LANG,
)

DENIED_COMMANDS = {"rm", "mv", "chmod", "sudo"}
# shell=False does not interpret these; refuse them so a model cannot smuggle a chain or redirect.
SHELL_META = set("><|&;`$")
# Directories that list_files and search_files never descend into.
SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules"}


def _path_param(description: str) -> dict:
    return {"type": "string", "description": description}


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a file inside the workspace. Long files are paged: the result "
                "ends with the next offset to pass to continue reading."
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
            "description": "Write content to a file inside the workspace (creates or overwrites)",
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
                "Replace an exact text fragment in a workspace file. old_text must "
                "match exactly once unless replace_all is true."
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
            "description": "Append content to the end of a workspace file (creates it if missing)",
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
            "description": "Delete a file inside the workspace",
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
            "description": "Move or rename a file inside the workspace. Does not overwrite.",
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
            "description": "List files and directories in the workspace, with sizes",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": _path_param("Directory to list (default: workspace root)"),
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
            "description": "Search text files in the workspace with a regular expression. Returns path:line: text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regular expression to search for",
                    },
                    "path": _path_param(
                        "Directory to search (default: workspace root)"
                    ),
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
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Run a command in the workspace and return its exit code, stdout, and stderr",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Command to run"},
                    "timeout": {
                        "type": "integer",
                        "description": f"Seconds before the command is killed (default {SHELL_TIMEOUT}, max {SHELL_MAX_TIMEOUT})",
                    },
                    "stdin": {
                        "type": "string",
                        "description": "Text to feed to the command's standard input",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": (
                "Run a Python script in the workspace with the standard library and "
                "return its exit code, stdout, and stderr. Use print() to show results."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python source to run"},
                    "timeout": {
                        "type": "integer",
                        "description": f"Seconds before the script is killed (default {SHELL_TIMEOUT}, max {SHELL_MAX_TIMEOUT})",
                    },
                    "stdin": {
                        "type": "string",
                        "description": "Text to feed to the script's standard input",
                    },
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web and return titles, URLs, and snippets. Snippets are "
                "not answers: follow up with read_url on the results that matter."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {
                        "type": "integer",
                        "description": f"How many results to return (default 5, max {SEARCH_MAX_RESULTS})",
                    },
                    "site": {
                        "type": "string",
                        "description": "Only return results from this domain, for example docs.python.org",
                    },
                    "time": {
                        "type": "string",
                        "description": "Only return recent pages: d (day), w (week), m (month), y (year)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_url",
            "description": (
                "Fetch a web page, PDF, or text URL and return its readable text in one "
                "step. Saves the raw file and the extracted text in the workspace."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "http or https URL"},
                    "mode": {
                        "type": "string",
                        "description": "article (default) keeps the main content; full keeps all page text",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": (
                "Download an http or https URL (HTML, text, JSON, XML, or PDF) into the "
                "workspace. Returns the saved path, not the body."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "http or https URL to fetch",
                    },
                    "path": _path_param("Optional workspace path to save the file"),
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "parse_html",
            "description": "Read an HTML file already saved in the workspace and return its title and text. Does not download.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": _path_param(
                        "Workspace path to the HTML file saved by fetch_url"
                    ),
                    "mode": {
                        "type": "string",
                        "description": "article (default) keeps the main content; full keeps all page text",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_feed",
            "description": "Read an RSS or Atom feed and return its latest entries",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Feed URL"},
                    "max_items": {
                        "type": "integer",
                        "description": f"How many entries to return (default 10, max {FEED_MAX_ITEMS})",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wikipedia_search",
            "description": "Search Wikipedia article titles for a topic",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Topic to look up"},
                    "max_results": {
                        "type": "integer",
                        "description": f"How many results to return (default 5, max {SEARCH_MAX_RESULTS})",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wikipedia_summary",
            "description": "Return the summary of a Wikipedia article by its exact title",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Article title"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_get_json",
            "description": "GET a public JSON API and return the parsed response",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "http or https URL"},
                    "params": {
                        "type": "object",
                        "description": "Query parameters to add to the URL",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_pdf",
            "description": f"Extract text from a PDF in the workspace, up to {PDF_MAX_PAGES} pages per call",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": _path_param("Workspace path to the PDF"),
                    "first_page": {
                        "type": "integer",
                        "description": "First page to read, 1-based (default 1)",
                    },
                    "last_page": {
                        "type": "integer",
                        "description": "Last page to read (default: first_page plus the page cap)",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_docx",
            "description": "Extract the paragraphs of a Word .docx file in the workspace",
            "parameters": {
                "type": "object",
                "properties": {"path": _path_param("Workspace path to the .docx file")},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_csv",
            "description": "Describe a CSV file: delimiter, columns, row count, and the first rows",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": _path_param("Workspace path to the CSV file"),
                    "rows": {
                        "type": "integer",
                        "description": f"How many rows to preview (default {PREVIEW_ROWS}, max 50)",
                    },
                    "delimiter": {
                        "type": "string",
                        "description": "Field delimiter (default: detected)",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_json",
            "description": "Describe the structure of a JSON file: keys, array lengths, and sample values",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": _path_param("Workspace path to the JSON file"),
                    "max_depth": {
                        "type": "integer",
                        "description": "How many nesting levels to describe (default 3, max 10)",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": f"Save a fact under a key in {MEMORY_FILE} so later sessions can recall it",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Short name for the memory, for example 'user timezone'",
                    },
                    "text": {"type": "string", "description": "What to remember"},
                },
                "required": ["key", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall",
            "description": f"Return saved memories from {MEMORY_FILE}, optionally filtered by a search word",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Word or phrase to match in keys and text; omit for everything",
                    },
                },
            },
        },
    },
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


def resolve_in_workspace(workspace: Path, raw: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("path is required")
    root = workspace.resolve()
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("outside workspace")
    return resolved


def _relative(workspace: Path, path: Path) -> str:
    try:
        return str(path.relative_to(workspace.resolve()))
    except ValueError:
        return str(path)


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
    path = resolve_in_workspace(workspace, raw if isinstance(raw, str) else "")
    if not path.exists():
        raise ValueError("not found")
    if path.is_dir():
        raise ValueError("is a directory")
    return path


# --- file tools -----------------------------------------------------------


def read_file(workspace: Path, args: dict) -> str:
    path = resolve_in_workspace(workspace, args.get("path", ""))
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


def write_file(workspace: Path, args: dict) -> str:
    path = resolve_in_workspace(workspace, args.get("path", ""))
    content = args.get("content")
    if not isinstance(content, str):
        return "Error: content is required"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode("utf-8")
    path.write_bytes(data)
    return f"Wrote {len(data)} bytes to {path}"


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
    return f"Replaced {replaced} occurrence(s) in {shown}"


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
    path = resolve_in_workspace(workspace, raw if isinstance(raw, str) and raw else ".")
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


# --- shell and python -----------------------------------------------------


def _shell_env() -> dict:
    keep = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "LOGNAME")
    return {key: os.environ[key] for key in keep if key in os.environ}


def prepare_shell(command: str, workspace: Path) -> list[str]:
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command is required")
    if any(ch in command for ch in SHELL_META):
        raise ValueError("blocked shell syntax")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"invalid command: {exc}") from exc
    if not argv:
        raise ValueError("command is required")
    program = Path(argv[0]).name
    if program in DENIED_COMMANDS:
        raise ValueError(f"blocked command '{program}'")
    for arg in argv:
        if ".." in Path(arg).parts:
            raise ValueError("blocked path '..'")
        if Path(arg).is_absolute():
            resolve_in_workspace(workspace, arg)
    return argv


def _format_shell(exit_code: int, stdout: str, stderr: str) -> str:
    status = "success" if exit_code == 0 else "error"
    return cap_result(
        f"status: {status}\nexit_code: {exit_code}\nstdout:\n{stdout}\nstderr:\n{stderr}"
    )


def _timeout_arg(raw) -> int:
    return _count_arg(raw, "timeout", SHELL_TIMEOUT, SHELL_MAX_TIMEOUT)


def _stdin_arg(raw) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError("stdin must be a string")
    return raw


def _run_process(
    argv: list[str], workspace: Path, timeout: int, stdin: str | None
) -> str:
    try:
        completed = subprocess.run(
            argv,
            shell=False,
            cwd=workspace,
            env=_shell_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            input=stdin,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        return _format_shell(124, stdout, stderr or f"timed out after {timeout}s")
    return _format_shell(completed.returncode, completed.stdout, completed.stderr)


def run_shell(workspace: Path, args: dict) -> str:
    argv = prepare_shell(args.get("command", ""), workspace)
    timeout = _timeout_arg(args.get("timeout"))
    stdin = _stdin_arg(args.get("stdin"))
    return _run_process(argv, workspace, timeout, stdin)


def run_python(workspace: Path, args: dict) -> str:
    code = args.get("code")
    if not isinstance(code, str) or not code.strip():
        return "Error: code is required"
    timeout = _timeout_arg(args.get("timeout"))
    stdin = _stdin_arg(args.get("stdin"))
    argv = [sys.executable, "-I", "-c", code]
    return _run_process(argv, workspace, timeout, stdin)


# --- http -----------------------------------------------------------------


HTML_TYPES = {"text/html", "application/xhtml+xml"}
JSON_TYPES = {"application/json", "application/ld+json", "text/json"}
XML_TYPES = {
    "application/xml",
    "text/xml",
    "application/rss+xml",
    "application/atom+xml",
}
PDF_TYPE = "application/pdf"
# Media types fetch_url saves, with the file extension used for the default path.
FETCH_TYPES = {
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "application/json": ".json",
    "application/ld+json": ".json",
    "text/json": ".json",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "application/rss+xml": ".xml",
    "application/atom+xml": ".xml",
    PDF_TYPE: ".pdf",
}
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
ACCEPT_HEADER = (
    "text/html,application/xhtml+xml,application/json,application/xml,"
    "text/plain,application/pdf;q=0.9,*/*;q=0.5"
)


def validate_url(url: str):
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url is required")
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("blocked url scheme")
    if parsed.username or parsed.password:
        raise ValueError("blocked url credentials")
    if not parsed.hostname:
        raise ValueError("url host is required")
    _assert_public_host(parsed.hostname)
    return parsed


def _assert_public_host(host: str) -> None:
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ValueError("could not resolve host") from exc
        addresses = []
        for info in infos:
            try:
                addresses.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue
        if not addresses:
            raise ValueError("could not resolve host")
    for ip in addresses:
        if not ip.is_global:
            raise ValueError("blocked host")


def default_download_path(url: str, host: str, extension: str = ".html") -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", host.lower()).strip("-") or "page"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return f"downloads/{slug}-{digest}{extension}"


def _media_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def _read_limited(response, limit: int) -> bytes:
    chunks = []
    total = 0
    while True:
        chunk = response.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ValueError("response too large")
        chunks.append(chunk)
    return b"".join(chunks)


class _CheckedRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = FETCH_MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_request(request: urllib.request.Request) -> tuple[int, str, bytes]:
    opener = urllib.request.build_opener(
        _CheckedRedirectHandler,
        urllib.request.ProxyHandler({}),
    )
    try:
        with opener.open(request, timeout=FETCH_TIMEOUT) as response:
            media = _media_type(response.headers.get("Content-Type", ""))
            if media not in FETCH_TYPES:
                raise ValueError(f"unsupported content type '{media or 'unknown'}'")
            body = _read_limited(response, FETCH_MAX_BYTES)
            return response.status, media, body
    except ValueError:
        raise
    except urllib.error.HTTPError as exc:
        raise ValueError(f"http status {exc.code}") from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            raise ValueError("timed out") from exc
        raise ValueError(str(reason)) from exc
    except TimeoutError as exc:
        raise ValueError("timed out") from exc


def _http_get(url: str) -> tuple[int, str, bytes]:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": ACCEPT_HEADER}
    )
    return _open_request(request)


def _checked_get(url: str, allowed: set[str] | None = None) -> tuple[int, str, bytes]:
    status, content_type, body = _http_get(url.strip())
    media = _media_type(content_type)
    if status < 200 or status >= 300:
        raise ValueError(f"http status {status}")
    if media not in FETCH_TYPES or (allowed is not None and media not in allowed):
        raise ValueError(f"unsupported content type '{content_type or 'unknown'}'")
    if len(body) > FETCH_MAX_BYTES:
        raise ValueError("response too large")
    return status, media, body


def _write_fetched(workspace: Path, url: str, path_arg) -> tuple[Path, int, str]:
    parsed = validate_url(url)
    path = None
    if path_arg is not None:
        path = resolve_in_workspace(workspace, path_arg)
    status, media, body = _checked_get(url)
    if path is None:
        relative = default_download_path(
            url.strip(), parsed.hostname or "page", FETCH_TYPES[media]
        )
        path = resolve_in_workspace(workspace, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path, status, media


def fetch_url(workspace: Path, args: dict) -> str:
    workspace = workspace.resolve()
    url = args.get("url", "")
    if not isinstance(url, str):
        url = ""
    path_arg = args["path"] if "path" in args and args["path"] is not None else None
    path, status, media = _write_fetched(workspace, url, path_arg)
    return (
        f"status: success\n"
        f"http_status: {status}\n"
        f"content_type: {media}\n"
        f"bytes: {path.stat().st_size}\n"
        f"path: {path.relative_to(workspace)}"
    )


# --- html text ------------------------------------------------------------


class _TextParser(HTMLParser):
    _SKIP = {"script", "style", "noscript", "template"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def strip_html(text: str) -> str:
    parser = _TextParser()
    parser.feed(text)
    parser.close()
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    return "\n".join(line for line in lines if line)


def _html_title(document: bytes) -> str:
    match = re.search(
        rb"<title[^>]*>(.*?)</title>", document, re.IGNORECASE | re.DOTALL
    )
    if not match:
        return ""
    title = html.unescape(match.group(1).decode("utf-8", errors="replace"))
    return " ".join(title.split())


def _article_field(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        return ""
    return value.strip()


def _trafilatura_json(document: bytes, **options) -> dict | None:
    raw = trafilatura.extract(
        document, output_format="json", with_metadata=True, **options
    )
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not _article_field(data, "text"):
        return None
    return data


def _page_mode(raw) -> str:
    if raw is None or raw == "":
        return "article"
    if raw not in {"article", "full"}:
        raise ValueError("mode must be 'article' or 'full'")
    return raw


def extract_page(document: bytes, mode: str = "article") -> dict | None:
    if mode == "article":
        data = _trafilatura_json(document)
        if data is None:
            data = _trafilatura_json(
                document, favor_recall=True, include_tables=True, include_comments=False
            )
        if data is not None:
            return data
    text = strip_html(document.decode("utf-8", errors="replace"))
    if not text:
        return None
    return {"title": _html_title(document), "author": "", "date": "", "text": text}


def _format_article(data: dict) -> str:
    return (
        f"title: {_article_field(data, 'title')}\n"
        f"author: {_article_field(data, 'author')}\n"
        f"date: {_article_field(data, 'date')}\n"
        f"text:\n{_article_field(data, 'text')}"
    )


def parse_html(workspace: Path, args: dict) -> str:
    workspace = workspace.resolve()
    path_arg = args.get("path")
    path = resolve_in_workspace(
        workspace, path_arg if isinstance(path_arg, str) else ""
    )
    if not path.exists():
        return "Error: not found"
    if path.is_dir():
        return "Error: is a directory"
    mode = _page_mode(args.get("mode"))
    data = extract_page(path.read_bytes(), mode)
    if data is None:
        return "Error: no article text extracted"
    return cap_result(_format_article(data))


def _pdf_reader(data: bytes) -> pypdf.PdfReader:
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                unlocked = reader.decrypt("")
            except Exception as exc:  # pypdf raises several types here
                raise ValueError("PDF is encrypted") from exc
            if not unlocked:
                raise ValueError("PDF is encrypted")
        len(reader.pages)
    except ValueError:
        raise
    except pypdf.errors.PyPdfError as exc:
        raise ValueError(f"could not read PDF: {exc}") from exc
    return reader


def _pdf_page_text(reader: pypdf.PdfReader, index: int) -> str:
    try:
        return (reader.pages[index].extract_text() or "").strip()
    except pypdf.errors.PyPdfError:
        return ""


def _pdf_title(reader: pypdf.PdfReader) -> str:
    try:
        meta = reader.metadata
    except pypdf.errors.PyPdfError:
        return ""
    title = getattr(meta, "title", None) if meta is not None else None
    return str(title).strip() if title else ""


def read_url(workspace: Path, args: dict) -> str:
    workspace = workspace.resolve()
    url = args.get("url", "")
    if not isinstance(url, str):
        url = ""
    mode = _page_mode(args.get("mode"))
    path, _status, media = _write_fetched(workspace, url, None)
    body = path.read_bytes()
    saved = path.relative_to(workspace)
    title = author = date = ""
    if media in HTML_TYPES:
        data = extract_page(body, mode)
        if data is None:
            return f"Error: no text extracted; the raw page is saved at {saved}"
        title = _article_field(data, "title")
        author = _article_field(data, "author")
        date = _article_field(data, "date")
        text = _article_field(data, "text")
    elif media == PDF_TYPE:
        reader = _pdf_reader(body)
        title = _pdf_title(reader)
        pages = [_pdf_page_text(reader, i) for i in range(len(reader.pages))]
        text = "\n\n".join(page for page in pages if page)
        if not text:
            return f"Error: no text extracted; the PDF is saved at {saved}"
    else:
        text = body.decode("utf-8", errors="replace").strip()
        if not text:
            return f"Error: empty response; the file is saved at {saved}"
    text_path = path if path.suffix == ".txt" else path.with_suffix(".txt")
    text_path.write_text(text, encoding="utf-8")
    text_file = text_path.relative_to(workspace)
    header = (
        f"url: {url.strip()}\n"
        f"title: {title}\n"
        f"author: {author}\n"
        f"date: {date}\n"
        f"saved: {saved}\n"
        f"text_file: {text_file}\n"
        "text:\n"
    )
    return header + cap_result(
        text,
        max(MAX_TOOL_RESULT - len(header), 200),
        hint=f"full text is in {text_file}; use read_file with offset to continue",
    )


# --- search ---------------------------------------------------------------


SEARCH_URL = "https://html.duckduckgo.com/html/"
SEARCH_DEFAULT_RESULTS = 5
SEARCH_TIME_FILTERS = {"d", "w", "m", "y"}
SEARCH_HINT = (
    "Snippets are not answers. Call read_url on the results that matter "
    "before answering, and cite the URLs you read."
)


def _class_tokens(attrs) -> set[str]:
    for key, value in attrs:
        if key == "class" and isinstance(value, str):
            return set(value.split())
    return set()


def _unwrap_result_url(href: str) -> str:
    if not isinstance(href, str):
        return ""
    href = href.strip()
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlsplit(href)
    host = (parsed.hostname or "").lower()
    if host.endswith("duckduckgo.com"):
        wrapped = urllib.parse.parse_qs(parsed.query).get("uddg")
        if wrapped and wrapped[0].strip():
            return wrapped[0].strip()
    return href


class _SearchResultParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results: list[tuple[str, str, str]] = []
        self._capture = ""
        self._depth = 0
        self._buf: list[str] = []
        self._href = ""
        self._title = ""
        self._url = ""

    def _flush(self, snippet: str = "") -> None:
        title = " ".join(self._title.split())
        url = self._url.strip()
        if title and url:
            self.results.append((title, url, " ".join(snippet.split())))
        self._title = ""
        self._url = ""

    def handle_starttag(self, tag, attrs):
        classes = _class_tokens(attrs)
        if self._capture:
            self._depth += 1
            return
        if tag == "a" and "result__a" in classes:
            self._flush()
            self._capture = "title"
            self._depth = 1
            self._buf = []
            self._href = ""
            for key, value in attrs:
                if key == "href" and isinstance(value, str):
                    self._href = value
                    break
            return
        if "result__snippet" in classes and self._title:
            self._capture = "snippet"
            self._depth = 1
            self._buf = []

    def handle_endtag(self, tag):
        if not self._capture:
            return
        self._depth -= 1
        if self._depth > 0:
            return
        text = "".join(self._buf)
        if self._capture == "title":
            self._title = text
            self._url = _unwrap_result_url(self._href)
        else:
            self._flush(text)
        self._capture = ""
        self._buf = []

    def handle_data(self, data):
        if self._capture:
            self._buf.append(data)

    def close(self):
        super().close()
        if not self._capture:
            self._flush()


def _parse_search_results(html_text: str) -> list[tuple[str, str, str]]:
    parser = _SearchResultParser()
    parser.feed(html_text)
    parser.close()
    return parser.results


def _search_limit(raw) -> int:
    return _count_arg(raw, "max_results", SEARCH_DEFAULT_RESULTS, SEARCH_MAX_RESULTS)


def _search_html(query: str, recency: str | None = None) -> str:
    validate_url(SEARCH_URL)
    fields = {"q": query}
    if recency:
        fields["df"] = recency
    payload = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        SEARCH_URL,
        data=payload,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    status, media, body = _open_request(request)
    if media not in HTML_TYPES:
        raise ValueError(f"unsupported content type '{media or 'unknown'}'")
    if status < 200 or status >= 300:
        raise ValueError(f"http status {status}")
    return body.decode("utf-8", errors="replace")


def _format_search_results(rows: list[tuple[str, str, str]]) -> str:
    blocks = []
    for index, (title, url, snippet) in enumerate(rows, start=1):
        blocks.append(f"{index}. {title}\n   url: {url}\n   snippet: {snippet}")
    return "\n".join(blocks)


def web_search(workspace: Path, args: dict) -> str:
    query = args.get("query", "")
    if not isinstance(query, str) or not query.strip():
        return "Error: query is required"
    limit = _search_limit(args.get("max_results"))
    query = query.strip()
    site = args.get("site")
    if site is not None and site != "":
        if not isinstance(site, str):
            return "Error: site must be a string"
        query = f"{query} site:{site.strip()}"
    recency = args.get("time")
    if recency is not None and recency != "":
        if recency not in SEARCH_TIME_FILTERS:
            return "Error: time must be one of d, w, m, y"
    else:
        recency = None
    rows = []
    for title, url, snippet in _parse_search_results(_search_html(query, recency)):
        try:
            validate_url(url)
        except ValueError:
            continue
        rows.append((title, url, snippet))
        if len(rows) >= limit:
            break
    if not rows:
        return "Error: no results"
    return cap_result(_format_search_results(rows) + "\n\n" + SEARCH_HINT)


# --- feeds ----------------------------------------------------------------


_DOCTYPE_RE = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


def _parse_xml(document: bytes) -> ET.Element:
    if _DOCTYPE_RE.search(document):
        raise ValueError("XML with a DOCTYPE or ENTITY declaration is not accepted")
    try:
        return ET.fromstring(document)
    except ET.ParseError as exc:
        raise ValueError(f"invalid XML: {exc}") from exc


def _local(tag) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, *names: str) -> str:
    for child in element:
        if _local(child.tag) in names:
            if child.text and child.text.strip():
                return child.text.strip()
            href = child.get("href")
            if href and _local(child.tag) == "link":
                return href.strip()
    return ""


def _entry_link(element: ET.Element) -> str:
    fallback = ""
    for child in element:
        if _local(child.tag) != "link":
            continue
        href = child.get("href")
        if href:
            rel = child.get("rel", "alternate")
            if rel == "alternate":
                return href.strip()
            fallback = fallback or href.strip()
        elif child.text and child.text.strip():
            return child.text.strip()
    return fallback


def _feed_entries(root: ET.Element) -> tuple[str, list[dict]]:
    if _local(root.tag) == "feed":
        title = _child_text(root, "title")
    else:
        channel = next((c for c in root.iter() if _local(c.tag) == "channel"), root)
        title = _child_text(channel, "title")
    entries = []
    for element in root.iter():
        if _local(element.tag) not in {"item", "entry"}:
            continue
        summary = _child_text(element, "description", "summary", "content", "encoded")
        entries.append(
            {
                "title": " ".join(_child_text(element, "title").split()),
                "link": _entry_link(element),
                "date": _child_text(element, "pubDate", "published", "updated", "date"),
                "summary": strip_html(summary).replace("\n", " ")[:300],
            }
        )
    return title, entries


def read_feed(workspace: Path, args: dict) -> str:
    url = args.get("url", "")
    limit = _count_arg(args.get("max_items"), "max_items", 10, FEED_MAX_ITEMS)
    validate_url(url)
    _status, _media, body = _checked_get(url)
    root = _parse_xml(body)
    feed_title, entries = _feed_entries(root)
    if not entries:
        return "Error: no feed entries found"
    lines = [f"feed: {feed_title}", f"entries: {len(entries)}", ""]
    for index, entry in enumerate(entries[:limit], start=1):
        lines.append(f"{index}. {entry['title']}")
        lines.append(f"   link: {entry['link']}")
        lines.append(f"   date: {entry['date']}")
        lines.append(f"   summary: {entry['summary']}")
    lines.append("")
    lines.append("Call read_url on an entry link to read the full item.")
    return cap_result("\n".join(lines))


# --- json apis ------------------------------------------------------------


def _with_params(url: str, params) -> str:
    if params is None:
        return url
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    parts = urllib.parse.urlsplit(url.strip())
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    for key, value in params.items():
        if isinstance(value, bool):
            value = "true" if value else "false"
        elif value is None:
            value = ""
        elif not isinstance(value, (str, int, float)):
            raise ValueError(f"param '{key}' must be a string or number")
        query.append((str(key), str(value)))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def get_json(url: str, params=None):
    target = _with_params(url, params)
    validate_url(target)
    _status, _media, body = _checked_get(target, JSON_TYPES)
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON response: {exc}") from exc


def http_get_json(workspace: Path, args: dict) -> str:
    url = args.get("url", "")
    if not isinstance(url, str) or not url.strip():
        return "Error: url is required"
    data = get_json(url, args.get("params"))
    return cap_result(
        json.dumps(data, indent=2, ensure_ascii=False),
        hint="narrow the request with params, or save the full response with fetch_url",
    )


WIKIPEDIA_API = "https://{lang}.wikipedia.org/w/api.php"
WIKIPEDIA_SUMMARY = "https://{lang}.wikipedia.org/api/rest_v1/page/summary/{title}"
WIKIPEDIA_PAGE = "https://{lang}.wikipedia.org/wiki/{title}"


def _wiki_slug(title: str) -> str:
    return urllib.parse.quote(title.strip().replace(" ", "_"), safe="")


def wikipedia_search(workspace: Path, args: dict) -> str:
    query = args.get("query", "")
    if not isinstance(query, str) or not query.strip():
        return "Error: query is required"
    limit = _search_limit(args.get("max_results"))
    data = get_json(
        WIKIPEDIA_API.format(lang=WIKIPEDIA_LANG),
        {
            "action": "query",
            "list": "search",
            "format": "json",
            "utf8": 1,
            "srsearch": query.strip(),
            "srlimit": limit,
        },
    )
    hits = []
    if isinstance(data, dict):
        hits = data.get("query", {}).get("search", []) or []
    rows = []
    for index, hit in enumerate(hits[:limit], start=1):
        if not isinstance(hit, dict):
            continue
        title = str(hit.get("title", "")).strip()
        if not title:
            continue
        snippet = strip_html(str(hit.get("snippet", ""))).replace("\n", " ")
        page = WIKIPEDIA_PAGE.format(lang=WIKIPEDIA_LANG, title=_wiki_slug(title))
        rows.append(f"{index}. {title}\n   url: {page}\n   snippet: {snippet}")
    if not rows:
        return "Error: no results"
    rows.append(
        "\nCall wikipedia_summary with one of these titles to read its summary."
    )
    return cap_result("\n".join(rows))


def wikipedia_summary(workspace: Path, args: dict) -> str:
    title = args.get("title", "")
    if not isinstance(title, str) or not title.strip():
        return "Error: title is required"
    url = WIKIPEDIA_SUMMARY.format(lang=WIKIPEDIA_LANG, title=_wiki_slug(title))
    try:
        data = get_json(url)
    except ValueError as exc:
        if "404" in str(exc):
            return f"Error: no Wikipedia article named '{title.strip()}'"
        raise
    if not isinstance(data, dict):
        return "Error: unexpected response"
    extract = str(data.get("extract") or "").strip()
    if not extract:
        return f"Error: no summary for '{title.strip()}'"
    page = ""
    urls = data.get("content_urls")
    if isinstance(urls, dict):
        desktop = urls.get("desktop")
        if isinstance(desktop, dict):
            page = str(desktop.get("page") or "")
    page = page or WIKIPEDIA_PAGE.format(lang=WIKIPEDIA_LANG, title=_wiki_slug(title))
    description = str(data.get("description") or "").strip()
    kind = str(data.get("type") or "")
    note = ""
    if kind == "disambiguation":
        note = "\nnote: this title is a disambiguation page; pick a more specific title"
    return cap_result(
        f"title: {data.get('title', title.strip())}\n"
        f"description: {description}\n"
        f"url: {page}{note}\n"
        f"summary:\n{extract}"
    )


# --- documents ------------------------------------------------------------


def read_pdf(workspace: Path, args: dict) -> str:
    path = _existing_file(workspace, args.get("path", ""))
    reader = _pdf_reader(path.read_bytes())
    total = len(reader.pages)
    if total == 0:
        return "Error: PDF has no pages"
    first = _positive_int(args.get("first_page"), "first_page", 1)
    if first > total:
        return f"Error: first_page {first} is past the last page ({total})"
    default_last = min(total, first + PDF_MAX_PAGES - 1)
    last = _positive_int(args.get("last_page"), "last_page", default_last)
    if last < first:
        return "Error: last_page must not be before first_page"
    last = min(last, total, first + PDF_MAX_PAGES - 1)
    chunks = []
    for number in range(first, last + 1):
        chunks.append(f"--- page {number} ---\n{_pdf_page_text(reader, number - 1)}")
    shown = _relative(workspace, path)
    header = f"file: {shown}\ntitle: {_pdf_title(reader)}\npages: {total}\nshowing: {first}-{last}\n"
    hint = (
        f"call read_pdf with first_page={last + 1} to continue"
        if last < total
        else "narrow the page range"
    )
    body = cap_result(
        "\n\n".join(chunks), max(MAX_TOOL_RESULT - len(header), 200), hint=hint
    )
    if last < total and "...[truncated" not in body:
        body += f"\n...[pages {last + 1}-{total} remain; call read_pdf with first_page={last + 1}]"
    return header + body


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx_paragraphs(path: Path) -> list[str]:
    try:
        with zipfile.ZipFile(path) as archive:
            document = archive.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ValueError("not a .docx file") from exc
    root = _parse_xml(document)
    paragraphs = []
    for paragraph in root.iter(_W + "p"):
        parts = []
        for node in paragraph.iter():
            if node.tag == _W + "t":
                parts.append(node.text or "")
            elif node.tag == _W + "tab":
                parts.append("\t")
            elif node.tag in {_W + "br", _W + "cr"}:
                parts.append("\n")
        paragraphs.append("".join(parts))
    return paragraphs


def read_docx(workspace: Path, args: dict) -> str:
    path = _existing_file(workspace, args.get("path", ""))
    paragraphs = _docx_paragraphs(path)
    text = "\n".join(paragraphs).strip()
    if not text:
        return "Error: no text extracted"
    shown = _relative(workspace, path)
    header = f"file: {shown}\nparagraphs: {len(paragraphs)}\ntext:\n"
    if len(header) + len(text) > MAX_TOOL_RESULT:
        text_path = path.with_suffix(".txt")
        text_path.write_text(text, encoding="utf-8")
        hint = f"full text is in {_relative(workspace, text_path)}; use read_file with offset to continue"
    else:
        hint = ""
    return header + cap_result(text, max(MAX_TOOL_RESULT - len(header), 200), hint=hint)


# --- data -----------------------------------------------------------------


def _csv_dialect(sample: str, delimiter):
    if delimiter is not None:
        if not isinstance(delimiter, str) or len(delimiter) != 1:
            raise ValueError("delimiter must be a single character")

        class _Given(csv.excel):
            pass

        _Given.delimiter = delimiter
        return _Given
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        return csv.excel


def inspect_csv(workspace: Path, args: dict) -> str:
    path = _existing_file(workspace, args.get("path", ""))
    preview = _count_arg(args.get("rows"), "rows", PREVIEW_ROWS, 50)
    with path.open(newline="", encoding="utf-8") as handle:
        dialect = _csv_dialect(handle.read(8192), args.get("delimiter"))
        handle.seek(0)
        reader = csv.reader(handle, dialect)
        header = next(reader, None)
        if header is None:
            return "Error: empty file"
        rows = []
        count = 0
        filled = [0] * len(header)
        for row in reader:
            count += 1
            for index, value in enumerate(row[: len(header)]):
                if value.strip():
                    filled[index] += 1
            if len(rows) < preview:
                rows.append(row)
    lines = [
        f"file: {_relative(workspace, path)}",
        f"delimiter: {dialect.delimiter!r}",
        f"rows: {count} (excluding header)",
        f"columns: {len(header)}",
    ]
    for index, name in enumerate(header):
        lines.append(f"  {index + 1}. {name} (filled {filled[index]}/{count})")
    lines.append(f"preview (first {len(rows)} rows):")
    lines.append("  " + " | ".join(header))
    for row in rows:
        lines.append("  " + " | ".join(cell[:60] for cell in row))
    return cap_result("\n".join(lines))


def _describe_json(value, depth: int, max_depth: int, indent: str) -> list[str]:
    if isinstance(value, dict):
        lines = [f"{indent}object with {len(value)} keys"]
        if depth < max_depth:
            for index, (key, item) in enumerate(value.items()):
                if index >= 30:
                    lines.append(f"{indent}  ...[{len(value) - 30} more keys]")
                    break
                described = _describe_json(item, depth + 1, max_depth, indent + "    ")
                lines.append(f"{indent}  {key}: {described[0].strip()}")
                lines.extend(described[1:])
        return lines
    if isinstance(value, list):
        lines = [f"{indent}array of {len(value)}"]
        if value and depth < max_depth:
            described = _describe_json(value[0], depth + 1, max_depth, indent + "    ")
            lines.append(f"{indent}  [0]: {described[0].strip()}")
            lines.extend(described[1:])
        return lines
    if isinstance(value, str):
        shown = value if len(value) <= 60 else value[:57] + "..."
        return [f"{indent}string {shown!r}"]
    if isinstance(value, bool):
        return [f"{indent}boolean {str(value).lower()}"]
    if value is None:
        return [f"{indent}null"]
    return [f"{indent}number {value}"]


def inspect_json(workspace: Path, args: dict) -> str:
    path = _existing_file(workspace, args.get("path", ""))
    max_depth = _count_arg(args.get("max_depth"), "max_depth", 3, 10)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return f"Error: invalid JSON: {exc}"
    lines = [f"file: {_relative(workspace, path)}"]
    lines.extend(_describe_json(data, 0, max_depth, ""))
    return cap_result(
        "\n".join(lines), hint="raise max_depth on a narrower file, or use run_python"
    )


# --- memory ---------------------------------------------------------------


def _memory_path(workspace: Path) -> Path:
    return workspace.resolve() / MEMORY_FILE


def _read_memory(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    if not path.exists() or path.is_dir():
        return entries
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return entries
    key = None
    buffer: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if key is not None:
                entries[key] = "\n".join(buffer).strip()
            key = line[3:].strip()
            buffer = []
        elif key is not None:
            buffer.append(line)
    if key is not None:
        entries[key] = "\n".join(buffer).strip()
    return entries


def _write_memory(path: Path, entries: dict[str, str]) -> None:
    blocks = ["# Memory", ""]
    for key, text in entries.items():
        blocks.append(f"## {key}")
        blocks.append(text)
        blocks.append("")
    path.write_text("\n".join(blocks), encoding="utf-8")


def memory_keys(workspace: Path) -> list[str]:
    return list(_read_memory(_memory_path(workspace)))


def _format_memories(entries: dict[str, str]) -> str:
    return "\n\n".join(f"## {key}\n{text}" for key, text in entries.items())


def remember(workspace: Path, args: dict) -> str:
    key = args.get("key")
    text = args.get("text")
    if not isinstance(key, str) or not key.strip():
        return "Error: key is required"
    if not isinstance(text, str) or not text.strip():
        return "Error: text is required"
    key = " ".join(key.split())
    # Markdown headings would be read back as memory keys, so flatten them.
    text = "\n".join(
        line.lstrip("#").strip() if line.startswith("#") else line
        for line in text.strip().splitlines()
    ).strip()
    path = _memory_path(workspace)
    entries = _read_memory(path)
    verb = "Updated" if key in entries else "Remembered"
    entries[key] = text
    _write_memory(path, entries)
    return f"{verb} '{key}'. {len(entries)} memories stored in {MEMORY_FILE}."


def recall(workspace: Path, args: dict) -> str:
    entries = _read_memory(_memory_path(workspace))
    if not entries:
        return "Memory is empty."
    query = args.get("query")
    if query is None or (isinstance(query, str) and not query.strip()):
        return cap_result(_format_memories(entries))
    if not isinstance(query, str):
        return "Error: query must be a string"
    needle = query.strip().lower()
    matched = {
        key: text
        for key, text in entries.items()
        if needle in key.lower() or needle in text.lower()
    }
    if not matched:
        return f"No memories match '{query.strip()}'."
    return cap_result(_format_memories(matched))


# --- plan -----------------------------------------------------------------


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


# --- prompt helpers -------------------------------------------------------


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


# --- registry -------------------------------------------------------------


TOOL_FNS = {
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "append_file": append_file,
    "delete_file": delete_file,
    "move_file": move_file,
    "list_files": list_files,
    "search_files": search_files,
    "run_shell": run_shell,
    "run_python": run_python,
    "web_search": web_search,
    "read_url": read_url,
    "fetch_url": fetch_url,
    "parse_html": parse_html,
    "read_feed": read_feed,
    "wikipedia_search": wikipedia_search,
    "wikipedia_summary": wikipedia_summary,
    "http_get_json": http_get_json,
    "read_pdf": read_pdf,
    "read_docx": read_docx,
    "inspect_csv": inspect_csv,
    "inspect_json": inspect_json,
    "remember": remember,
    "recall": recall,
    "write_plan": write_plan,
    "update_plan": update_plan,
    "show_plan": show_plan,
}


def execute_tool(name: str, args: dict, workspace: Path) -> str:
    fn = TOOL_FNS.get(name)
    if fn is None:
        return f"Error: Unknown tool '{name}'"
    try:
        return fn(workspace, args)
    except ValueError as exc:
        return f"Error: {exc}"
    except OSError as exc:
        return f"Error: {exc}"


def _tool_result(
    name: str, arguments: str, workspace: Path, registry=None
) -> tuple[dict | None, str]:
    try:
        args = json.loads(arguments)
    except json.JSONDecodeError as exc:
        return None, f"Error: invalid tool arguments: {exc}"
    if not isinstance(args, dict):
        return None, "Error: tool arguments must be an object"
    if registry is None:
        return args, execute_tool(name, args, workspace)
    return args, registry.execute(name, args, workspace)
