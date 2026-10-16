"""Path confinement and tool result shaping."""

from harnh3x.config import MAX_TOOL_RESULT
from harnh3x.tools import execute_tool


def test_path_escape_is_rejected(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")

    for name, args in (
        ("read_file", {"path": str(outside)}),
        ("read_file", {"path": "../secret.txt"}),
        ("write_file", {"path": "../pwned.txt", "content": "nope"}),
        ("write_file", {"path": str(tmp_path / "pwned.txt"), "content": "nope"}),
        ("delete_file", {"path": str(outside)}),
        ("delete_file", {"path": "../secret.txt"}),
    ):
        result = execute_tool(name, args, workspace)
        assert "outside workspace" in result

    assert outside.read_text() == "secret"
    assert not (tmp_path / "pwned.txt").exists()


def test_read_file_rejects_symlink_escape(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    (workspace / "escape.txt").symlink_to(outside)

    result = execute_tool("read_file", {"path": "escape.txt"}, workspace)

    assert "outside workspace" in result
    assert "secret" not in result


def test_denied_shell_command_is_not_executed(tmp_path):
    target = tmp_path / "poem.txt"
    target.write_text("keep")

    result = execute_tool("run_shell", {"command": "rm poem.txt"}, tmp_path)

    assert "blocked" in result
    assert target.read_text() == "keep"


def test_shell_success_with_empty_stdout(tmp_path):
    result = execute_tool("run_shell", {"command": "true"}, tmp_path)

    assert "status: success" in result
    assert "exit_code: 0" in result
    assert "stdout:\n\nstderr:\n" in result


def test_tool_result_truncation_keeps_a_suffix(tmp_path):
    (tmp_path / "big.txt").write_text("a" * (MAX_TOOL_RESULT + 500))

    result = execute_tool("read_file", {"path": "big.txt"}, tmp_path)

    assert len(result) <= MAX_TOOL_RESULT
    assert "chars omitted" in result
    assert result.endswith("]")
