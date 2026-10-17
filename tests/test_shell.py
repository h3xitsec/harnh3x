"""run_shell and run_python under the seatbelt."""

import io
import os
import re
import threading
from datetime import UTC, datetime
from pathlib import Path

from harnh3x import tools
from harnh3x.config import MAX_TOOL_RESULT
from harnh3x.tools import execute_tool


def test_shell_accepts_stdin_and_timeout(tmp_path):
    result = execute_tool(
        "run_shell", {"command": "cat", "stdin": "from stdin", "timeout": 5}, tmp_path
    )
    assert "exit_code: 0" in result
    assert "stdout:\nfrom stdin\n" in result

    assert (
        execute_tool("run_shell", {"command": "true", "timeout": 0}, tmp_path)
        == "Error: timeout must be a positive integer"
    )
    assert (
        execute_tool("run_shell", {"command": "true", "stdin": 5}, tmp_path)
        == "Error: stdin must be a string"
    )


def test_abort_kills_a_running_shell_child(tmp_path):
    finished = threading.Event()
    holder = {}

    def work():
        holder["result"] = tools.process._run_process(
            ["sleep", "30"], tmp_path, 30, None
        )
        finished.set()

    thread = threading.Thread(target=work)
    thread.start()
    try:
        for _ in range(100):
            if tools.process._children:
                break
            threading.Event().wait(0.05)
        else:
            raise AssertionError("shell child did not start")
        tools.process.kill_children()
        assert finished.wait(timeout=2)
        assert "exit_code: 0" not in holder["result"]
        assert "exit_code:" in holder["result"]
    finally:
        tools.process.kill_children()
        thread.join(timeout=2)


class _FinishedProcess:
    """A Popen stand-in for a child that already exited with no output."""

    pid = 0
    returncode = 0

    def __init__(self, seen=None):
        self.seen = {} if seen is None else seen
        self.stdout = io.BytesIO(b"")
        self.stderr = io.BytesIO(b"")
        self.stdin = None

    def poll(self):
        return 0

    def wait(self, timeout=None):
        self.seen["timeout"] = timeout
        return 0


def test_shell_timeout_is_capped(tmp_path, monkeypatch):
    seen = {}

    monkeypatch.setattr(
        tools.process.subprocess,
        "Popen",
        lambda argv, **kwargs: _FinishedProcess(seen),
    )

    execute_tool("run_shell", {"command": "true", "timeout": 10_000}, tmp_path)
    assert seen["timeout"] == tools.process.SHELL_MAX_TIMEOUT

    execute_tool("run_shell", {"command": "true"}, tmp_path)
    assert seen["timeout"] == tools.process.SHELL_TIMEOUT


def test_run_python_executes_code_in_the_workspace(tmp_path):
    (tmp_path / "data.txt").write_text("3\n4\n")

    result = execute_tool(
        "run_python",
        {
            "code": "import sys\nnums=[int(x) for x in open('data.txt')]\nprint(sum(nums) + int(sys.stdin.read()))",
            "stdin": "5",
        },
        tmp_path,
    )
    assert "status: success" in result
    assert "stdout:\n12\n" in result

    failing = execute_tool("run_python", {"code": "raise SystemExit(3)"}, tmp_path)
    assert "status: error" in failing
    assert "exit_code: 3" in failing

    assert (
        execute_tool("run_python", {"code": "  "}, tmp_path)
        == "Error: code is required"
    )


def test_run_script_executes_a_workspace_file(tmp_path):
    (tmp_path / "dice.py").write_text(
        "import sys\nprint(sum(int(x) for x in sys.argv[1:]))\n"
        "print(sys.stdin.read(), end='')\n"
    )

    result = execute_tool(
        "run_script",
        {"path": "dice.py", "args": ["3", "4"], "stdin": "ok"},
        tmp_path,
    )
    assert "status: success" in result
    assert "stdout:\n7\nok" in result

    assert execute_tool("run_script", {"path": "missing.py"}, tmp_path) == (
        "Error: not found"
    )
    (tmp_path / "pkg").mkdir()
    assert execute_tool("run_script", {"path": "pkg"}, tmp_path) == (
        "Error: is a directory"
    )
    (tmp_path / "notes.md").write_text("hi\n")
    assert execute_tool("run_script", {"path": "notes.md"}, tmp_path) == (
        "Error: path must be a .py file"
    )
    assert (
        execute_tool("run_script", {"path": "dice.py", "args": [1]}, tmp_path)
        == "Error: args must be a list of strings"
    )
    assert "outside workspace" in execute_tool(
        "run_script", {"path": "../dice.py"}, tmp_path
    )


def test_run_python_times_out(tmp_path):
    result = execute_tool(
        "run_python",
        {"code": "import time\ntime.sleep(5)", "timeout": 1},
        tmp_path,
    )
    assert "exit_code: 124" in result
    assert "timed out after 1s" in result


def test_sandbox_blocks_reads_writes_symlinks_and_network(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret-outside")
    (workspace / "inside.txt").write_text("inside-ok\n")
    (workspace / "escape.txt").symlink_to(outside)

    inside = execute_tool(
        "run_python",
        {"code": "print(open('inside.txt').read(), end='')"},
        workspace,
    )
    assert "status: success" in inside
    assert "stdout:\ninside-ok\n" in inside

    denied_sibling = execute_tool(
        "run_python",
        {"code": f"print(open({str(outside)!r}).read())"},
        workspace,
    )
    assert "secret-outside" not in denied_sibling
    assert "Operation not permitted" in denied_sibling

    denied_passwd = execute_tool(
        "run_python",
        {"code": "print(open('/etc/passwd').read())"},
        workspace,
    )
    assert "root:" not in denied_passwd
    assert "Operation not permitted" in denied_passwd

    sibling_out = tmp_path / "pwned.txt"
    write_out = execute_tool(
        "run_python",
        {"code": f"open({str(sibling_out)!r}, 'w').write('pwned')"},
        workspace,
    )
    assert "Operation not permitted" in write_out
    assert not sibling_out.exists()

    linked = execute_tool("run_shell", {"command": "cat escape.txt"}, workspace)
    assert "secret-outside" not in linked
    assert "Operation not permitted" in linked

    networked = execute_tool(
        "run_python",
        {
            "code": "import socket; socket.create_connection(('1.1.1.1', 443), timeout=3)",
            "timeout": 5,
        },
        workspace,
    )
    assert "Operation not permitted" in networked


def test_yolo_reads_outside_the_workspace_and_keeps_writes_inside(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret-outside")
    (workspace / "escape.txt").symlink_to(outside)

    direct = execute_tool("read_file", {"path": str(outside)}, workspace, yolo=True)
    linked = execute_tool("read_file", {"path": "escape.txt"}, workspace, yolo=True)
    blocked = execute_tool(
        "write_file",
        {"path": str(tmp_path / "pwned.txt"), "content": "nope"},
        workspace,
        yolo=True,
    )

    assert direct == "secret-outside"
    assert linked == "secret-outside"
    assert "outside workspace" in blocked
    assert not (tmp_path / "pwned.txt").exists()


def test_yolo_runs_sudo_without_the_seatbelt(tmp_path, monkeypatch):
    seen = {}

    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs["env"]
        seen["shell"] = kwargs["shell"]
        return _FinishedProcess()

    monkeypatch.setattr(tools.process.subprocess, "Popen", fake_popen)

    result = execute_tool("run_shell", {"command": "sudo true"}, tmp_path, yolo=True)

    assert "blocked" not in result
    assert "status: success" in result
    assert seen["argv"] == ["/bin/sh", "-c", "sudo true"]
    assert seen["shell"] is False
    assert seen["env"] == dict(os.environ)
    assert "HARNESS_SANDBOX_PROFILE" not in seen["env"]


def test_yolo_shell_runs_a_pipe_and_a_chain(tmp_path):
    piped = execute_tool(
        "run_shell",
        {"command": "printf hi | tr a-z A-Z"},
        tmp_path,
        yolo=True,
    )
    assert "status: success" in piped
    assert "stdout:\nHI\n" in piped
    assert not (tmp_path / ".shell").exists()

    chained = execute_tool(
        "run_shell", {"command": "true && printf ok"}, tmp_path, yolo=True
    )
    assert "stdout:\nok\n" in chained


def test_shell_blocks_a_pipe(tmp_path, monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("shell ran")

    monkeypatch.setattr(tools.process.subprocess, "Popen", fail)

    result = execute_tool("run_shell", {"command": "printf hi | tr a-z A-Z"}, tmp_path)

    assert "blocked" in result


def test_long_process_output_is_saved_for_read_file(tmp_path):
    code = "print('\\n'.join(f'{i:04d}-' + ('x' * 40) for i in range(400)))"
    result = execute_tool("run_python", {"code": code}, tmp_path)

    assert len(result) <= MAX_TOOL_RESULT
    match = re.search(
        r"full output saved to (\S+); call read_file with path \1 and offset (\d+)",
        result,
    )
    assert match
    path = Path(match.group(1))
    offset = int(match.group(2))
    assert path.is_relative_to((tmp_path / ".shell").resolve())
    saved = path.read_text(encoding="utf-8")
    assert len(saved) > MAX_TOOL_RESULT
    prefix = result.split("\n...[truncated", 1)[0]
    assert prefix.endswith("\n")
    assert saved.startswith(prefix)
    rest = execute_tool("read_file", {"path": str(path), "offset": offset}, tmp_path)
    assert saved[len(prefix) :].splitlines()[0] in rest
    assert "0399-" in saved


def test_process_output_past_the_cap_kills_the_child(tmp_path, monkeypatch):
    monkeypatch.setattr(tools.process, "SHELL_MAX_OUTPUT", 200_000)
    # Without the cap this would run for the whole timeout.
    code = (
        "import sys\n"
        "while True:\n"
        "    sys.stdout.write('y' * 65536)\n"
        "    sys.stdout.flush()\n"
    )
    started = datetime.now(UTC)

    result = execute_tool("run_python", {"code": code, "timeout": 20}, tmp_path)

    assert (datetime.now(UTC) - started).total_seconds() < 10
    assert result.startswith("status: error\nexit_code: ")
    assert "timed out" not in result
    assert "\nnote: output exceeded 200000 bytes; the process was killed\n" in result
    assert result.index("note:") < result.index("stdout:")
    spilled = list((tmp_path / ".shell").glob("*.txt"))
    assert len(spilled) == 1
    assert spilled[0].stat().st_size <= 200_000 + 65536 + 500
    assert not tools.process._children


def test_process_stdin_and_mixed_streams_are_collected(tmp_path):
    code = (
        "import sys\n"
        "data = sys.stdin.read()\n"
        "sys.stdout.write(data.upper())\n"
        "sys.stderr.write('warn\\n')\n"
        "sys.exit(3)\n"
    )
    result = execute_tool("run_python", {"code": code, "stdin": "abc\néß"}, tmp_path)
    assert result == "status: error\nexit_code: 3\nstdout:\nABC\nÉSS\nstderr:\nwarn\n"


def test_yolo_python_reads_outside_the_workspace(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret-outside")

    result = execute_tool(
        "run_python",
        {"code": f"print(open({str(outside)!r}).read(), end='')"},
        workspace,
        yolo=True,
    )

    assert "status: success" in result
    assert "stdout:\nsecret-outside\n" in result
