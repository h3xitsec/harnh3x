import os
import shlex
import signal
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from harnh3x.config import MAX_TOOL_RESULT, SHELL_MAX_TIMEOUT, SHELL_TIMEOUT
from harnh3x.sandbox import sandboxed_argv
from harnh3x.tools.files import edit_file, read_file, write_file
from harnh3x.tools.paths import (
    _count_arg,
    _path_param,
    _project,
    _yolo,
    cap_result,
    resolve_in_workspace,
)

SCHEMAS = [
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
                "Run a short Python snippet in the workspace with the standard library "
                "and return its exit code, stdout, and stderr. Use it to compute a "
                "number, date, or transformation. It does not save a file; to deliver "
                "a program, use the coding skill."
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
            "name": "read_source",
            "description": (
                "Read a source file inside the workspace. Long files are paged: the "
                "result ends with the next offset to pass to continue reading."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": _path_param("Path to the source file"),
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
            "name": "write_source",
            "description": (
                "Write a source file inside the workspace (creates or overwrites). "
                "A .py file is checked for syntax; a syntax error is reported and "
                "the file is still written."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": _path_param("Path to the source file"),
                    "content": {"type": "string", "description": "Source to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_source",
            "description": (
                "Replace an exact text fragment in a workspace source file. old_text "
                "must match exactly once unless replace_all is true. A .py file is "
                "checked for syntax after the edit."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": _path_param("Path to the source file"),
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
            "name": "run_script",
            "description": (
                "Run a .py file in the workspace with the standard library and return "
                "its exit code, stdout, and stderr. Optional args are command-line "
                "arguments, not a shell command."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": _path_param("Path to the .py file"),
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Command-line arguments passed to the script",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": f"Seconds before the script is killed (default {SHELL_TIMEOUT}, max {SHELL_MAX_TIMEOUT})",
                    },
                    "stdin": {
                        "type": "string",
                        "description": "Text to feed to the script's standard input",
                    },
                },
                "required": ["path"],
            },
        },
    },
]

DENIED_COMMANDS = {"rm", "mv", "chmod", "sudo"}
# shell=False does not interpret these; refuse them so a model cannot smuggle a chain or redirect.
SHELL_META = set("><|&;`$")


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


def read_source(workspace: Path, args: dict) -> str:
    return read_file(workspace, args, tool_name="read_source")


def write_source(workspace: Path, args: dict) -> str:
    result = write_file(workspace, args)
    if result.startswith("Error:"):
        return result
    path = resolve_in_workspace(workspace, args.get("path", ""))
    return result + _python_syntax_note(path)


def edit_source(workspace: Path, args: dict) -> str:
    result = edit_file(workspace, args)
    if result.startswith("Error:"):
        return result
    path = resolve_in_workspace(workspace, args.get("path", ""))
    return result + _python_syntax_note(path)


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
    if not _yolo.get():
        if program in DENIED_COMMANDS:
            raise ValueError(f"blocked command '{program}'")
        for arg in argv:
            if ".." in Path(arg).parts:
                raise ValueError("blocked path '..'")
            if Path(arg).is_absolute():
                resolve_in_workspace(workspace, arg)
    return argv


def _spill_shell(workspace: Path, text: str) -> Path:
    directory = workspace / ".shell"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{uuid.uuid4().hex}.txt"
    path.write_text(text, encoding="utf-8")
    return path.resolve()


def _shell_hint(path: Path, offset: int) -> str:
    return (
        f"full output saved to {path}; "
        f"call read_file with path {path} and offset {offset}"
    )


def _truncate_shell(text: str, workspace: Path) -> str:
    if len(text) <= MAX_TOOL_RESULT:
        return text
    path = _spill_shell(workspace, text)
    lines = text.splitlines(keepends=True)
    chosen = ""
    marker = ""
    for count in range(len(lines) + 1):
        prefix = "".join(lines[:count])
        omitted = len(text) - len(prefix)
        candidate = (
            f"\n...[truncated, {omitted} chars omitted; {_shell_hint(path, count + 1)}]"
        )
        if len(prefix) + len(candidate) > MAX_TOOL_RESULT:
            break
        chosen = prefix
        marker = candidate
    if chosen:
        return chosen + marker
    return cap_result(text, hint=_shell_hint(path, 1))


def _format_shell(exit_code: int, stdout: str, stderr: str, workspace: Path) -> str:
    status = "success" if exit_code == 0 else "error"
    text = f"status: {status}\nexit_code: {exit_code}\nstdout:\n{stdout}\nstderr:\n{stderr}"
    return _truncate_shell(text, workspace)


def _timeout_arg(raw) -> int:
    return _count_arg(raw, "timeout", SHELL_TIMEOUT, SHELL_MAX_TIMEOUT)


def _stdin_arg(raw) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError("stdin must be a string")
    return raw


_children: set[subprocess.Popen] = set()
_children_lock = threading.Lock()


def _track(proc: subprocess.Popen) -> None:
    with _children_lock:
        _children.add(proc)


def _untrack(proc: subprocess.Popen) -> None:
    with _children_lock:
        _children.discard(proc)


def _kill_group(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        try:
            proc.kill()
        except OSError:
            return


def kill_children() -> None:
    with _children_lock:
        procs = list(_children)
    for proc in procs:
        _kill_group(proc)


def _run_process(
    argv: list[str], workspace: Path, timeout: int, stdin: str | None
) -> str:
    project = _project.get()
    cwd = project if project is not None else workspace
    if _yolo.get():
        command = argv
        env = os.environ.copy()
    else:
        try:
            command, env = sandboxed_argv(argv, workspace, project)
        except ValueError as exc:
            return f"Error: {exc}"
    proc = subprocess.Popen(
        command,
        shell=False,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE if stdin is not None else None,
        text=True,
        start_new_session=True,
    )
    _track(proc)
    try:
        try:
            stdout, stderr = proc.communicate(input=stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            stdout, stderr = proc.communicate()
            return _format_shell(
                124, stdout or "", stderr or f"timed out after {timeout}s", workspace
            )
        return _format_shell(
            proc.returncode or 0, stdout or "", stderr or "", workspace
        )
    finally:
        _untrack(proc)


def run_shell(workspace: Path, args: dict) -> str:
    command = args.get("command", "")
    timeout = _timeout_arg(args.get("timeout"))
    stdin = _stdin_arg(args.get("stdin"))
    if _yolo.get():
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command is required")
        argv = ["/bin/sh", "-c", command]
    else:
        argv = prepare_shell(command, workspace)
    return _run_process(argv, workspace, timeout, stdin)


def run_python(workspace: Path, args: dict) -> str:
    code = args.get("code")
    if not isinstance(code, str) or not code.strip():
        return "Error: code is required"
    timeout = _timeout_arg(args.get("timeout"))
    stdin = _stdin_arg(args.get("stdin"))
    argv = [sys.executable, "-I", "-c", code]
    return _run_process(argv, workspace, timeout, stdin)


def run_script(workspace: Path, args: dict) -> str:
    raw = args.get("path", "")
    path = resolve_in_workspace(workspace, raw if isinstance(raw, str) else "")
    if not path.exists():
        return "Error: not found"
    if path.is_dir():
        return "Error: is a directory"
    if path.suffix != ".py":
        return "Error: path must be a .py file"
    script_args = args.get("args")
    if script_args is None:
        script_args = []
    if not isinstance(script_args, list) or any(
        not isinstance(item, str) for item in script_args
    ):
        return "Error: args must be a list of strings"
    timeout = _timeout_arg(args.get("timeout"))
    stdin = _stdin_arg(args.get("stdin"))
    argv = [sys.executable, "-I", str(path), *script_args]
    return _run_process(argv, workspace, timeout, stdin)


FNS = {
    "run_shell": run_shell,
    "run_python": run_python,
    "read_source": read_source,
    "write_source": write_source,
    "edit_source": edit_source,
    "run_script": run_script,
}
