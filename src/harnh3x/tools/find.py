import os
import sys
from pathlib import Path

from harnh3x.config import (
    LIST_FILES_MAX,
    SEARCH_FILES_MAX,
    SHELL_MAX_OUTPUT,
    SHELL_TIMEOUT,
)
from harnh3x.sandbox import READ_ROOTS
from harnh3x.tools.files import _directory_arg
from harnh3x.tools.paths import _path_param, _project, _text_arg, cap_result
from harnh3x.tools.process import _capture

SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": (
                "Find files with a ripgrep glob such as **/*.js or src/**/*.ts. "
                "Returns matching paths, newest modification time first. Globs "
                "follow .gitignore rules, so *.js matches in any directory. "
                "Hidden files and directories are skipped. Inside a git "
                "repository, files matched by .gitignore are skipped too. "
                ".ignore and .rgignore always apply. include_ignored searches "
                "ignored files and still skips hidden files. "
                f"At most {LIST_FILES_MAX} paths; a longer result says to "
                "narrow the pattern or path."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Ripgrep glob, for example **/*.py or src/**/*.ts",
                    },
                    "path": _path_param("Directory to search (default: the root)"),
                    "include_ignored": {
                        "type": "boolean",
                        "description": (
                            "Also return files matched by .gitignore, .ignore, "
                            "and .rgignore (default false). Hidden files stay skipped."
                        ),
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": (
                "Search file contents with ripgrep. The pattern is a ripgrep "
                "regular expression. Returns path:line:text, and lines longer "
                "than 200 characters are cut. glob is a ripgrep file glob such "
                "as *.py or src/**/*.ts. ignore_case folds case. Hidden files, "
                "directories, and binary files are skipped. Inside a git "
                "repository, files matched by .gitignore are skipped too. "
                ".ignore and .rgignore always apply. include_ignored searches "
                "ignored files and still skips hidden files. "
                f"At most {SEARCH_FILES_MAX} matches; a longer result says to "
                "narrow the pattern or path."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Ripgrep regular expression",
                    },
                    "path": _path_param("Directory to search (default: the root)"),
                    "glob": {
                        "type": "string",
                        "description": "Only search paths matching this ripgrep glob, for example *.py",
                    },
                    "ignore_case": {
                        "type": "boolean",
                        "description": "Case-insensitive match (default false)",
                    },
                    "include_ignored": {
                        "type": "boolean",
                        "description": (
                            "Also search files matched by .gitignore, .ignore, "
                            "and .rgignore (default false). Hidden files stay skipped."
                        ),
                    },
                },
                "required": ["pattern"],
            },
        },
    },
]

_NARROW = "narrow the pattern or path"


def select_rg() -> tuple[Path, Path | None]:
    """Return the rg binary and a file the sandbox must also be allowed to read.

    Prefer a binary already under the sandbox read roots. Otherwise use the
    first rg on PATH and grant that one file, not its directory.
    """
    candidates = _rg_candidates()
    if not candidates:
        raise ValueError("ripgrep (rg) was not found")
    for path in candidates:
        if not _needs_read_grant(path):
            return path, None
    return candidates[0], candidates[0]


def _rg_candidates() -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidate = Path(directory) / "rg"
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        found.append(resolved)
    return found


def _needs_read_grant(path: Path) -> bool:
    for root in (*READ_ROOTS, sys.base_prefix, sys.prefix):
        base = Path(root)
        try:
            if base.exists() and path.is_relative_to(base.resolve()):
                return False
        except OSError:
            continue
    return True


def _include_ignored(args: dict) -> bool:
    return args.get("include_ignored") is True


def _search_target(workspace: Path, root: Path) -> list[str]:
    project = _project.get()
    cwd = (project if project is not None else workspace).resolve()
    resolved = root.resolve()
    if resolved == cwd:
        return []
    try:
        return [resolved.relative_to(cwd).as_posix()]
    except ValueError:
        return [str(resolved)]


def _argv(binary: Path, args: list[str], *, include_ignored: bool) -> list[str]:
    command = [str(binary), "--color", "never", "--no-heading", "--no-config"]
    if include_ignored:
        command.append("--no-ignore")
    command.extend(args)
    return command


def _lines(stdout: str) -> list[str]:
    return [line for line in stdout.splitlines() if line]


def _rg_error(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    message = lines[-1] if lines else "ripgrep failed"
    if message.startswith("error: "):
        message = message.removeprefix("error: ")
    return f"Error: {message}"


def _run(
    workspace: Path,
    binary: Path,
    extra_read: Path | None,
    args: list[str],
    *,
    include_ignored: bool,
) -> tuple[int, str, str, str] | str:
    code, stdout, stderr, note = _capture(
        _argv(binary, args, include_ignored=include_ignored),
        workspace,
        SHELL_TIMEOUT,
        extra_read=extra_read,
    )
    if note == "timeout":
        return f"Error: timed out after {SHELL_TIMEOUT}s"
    if code not in (0, 1):
        return _rg_error(stderr)
    if note == "overflow":
        return f"Error: output exceeded {SHELL_MAX_OUTPUT} bytes; {_NARROW}"
    return code, stdout, stderr, note


def _allowed_paths(
    workspace: Path,
    binary: Path,
    extra_read: Path | None,
    target: list[str],
    *,
    include_ignored: bool,
) -> set[str] | str:
    # A positive -g overrides ignore rules and the hidden-file skip. Intersect
    # with the file list ripgrep would print with those defaults left on.
    result = _run(
        workspace,
        binary,
        extra_read,
        ["--files", *target],
        include_ignored=include_ignored,
    )
    if isinstance(result, str):
        return result
    _code, stdout, _stderr, _note = result
    return set(_lines(stdout))


def _cap(lines: list[str], limit: int, marker: str) -> str:
    if len(lines) > limit:
        extra = len(lines) - limit
        lines = lines[:limit]
        text = "\n".join(lines) + f"\n...[{marker.format(extra=extra)}]"
    else:
        text = "\n".join(lines)
    return cap_result(text, hint=_NARROW)


def glob(workspace: Path, args: dict) -> str:
    pattern = _text_arg(args, "pattern")
    root = _directory_arg(workspace, args.get("path"))
    ignored = _include_ignored(args)
    binary, extra_read = select_rg()
    target = _search_target(workspace, root)
    matched = _run(
        workspace,
        binary,
        extra_read,
        ["--files", "--sortr", "modified", "-g", pattern, *target],
        include_ignored=ignored,
    )
    if isinstance(matched, str):
        return matched
    _code, stdout, _stderr, _note = matched
    paths = _lines(stdout)
    if not paths:
        return "No files found."
    allowed = _allowed_paths(
        workspace, binary, extra_read, target, include_ignored=ignored
    )
    if isinstance(allowed, str):
        return allowed
    paths = [path for path in paths if path in allowed]
    if not paths:
        return "No files found."
    return _cap(paths, LIST_FILES_MAX, f"{{extra}} more entries; {_NARROW}")


def _glob_filter(raw) -> str | None:
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise ValueError("glob must be a string")
    return raw


def grep(workspace: Path, args: dict) -> str:
    pattern = _text_arg(args, "pattern")
    file_glob = _glob_filter(args.get("glob"))
    root = _directory_arg(workspace, args.get("path"))
    ignored = _include_ignored(args)
    binary, extra_read = select_rg()
    target = _search_target(workspace, root)
    command = ["-n", "--max-columns", "200"]
    if args.get("ignore_case") is True:
        command.append("-i")
    if file_glob is not None:
        command.extend(["-g", file_glob])
    command.extend(["-e", pattern, *target])
    matched = _run(workspace, binary, extra_read, command, include_ignored=ignored)
    if isinstance(matched, str):
        return matched
    _code, stdout, _stderr, _note = matched
    lines = _lines(stdout)
    if file_glob is not None and lines:
        allowed = _allowed_paths(
            workspace, binary, extra_read, target, include_ignored=ignored
        )
        if isinstance(allowed, str):
            return allowed
        lines = [line for line in lines if _match_path(line) in allowed]
    if not lines:
        return "No matches."
    return _cap(
        lines,
        SEARCH_FILES_MAX,
        f"stopped at {SEARCH_FILES_MAX} matches; {_NARROW}",
    )


def _match_path(line: str) -> str:
    path, separator, _rest = line.partition(":")
    if not separator:
        return line
    return path


FNS = {
    "glob": glob,
    "grep": grep,
}
