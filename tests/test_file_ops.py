"""File tools inside the workspace."""

import os
import re
import subprocess

from harnh3x.config import MAX_TOOL_RESULT, SEARCH_FILES_MAX
from harnh3x.tools import execute_tool


def test_read_file_pages_with_offset_and_limit(tmp_path):
    (tmp_path / "lines.txt").write_text("".join(f"line {n}\n" for n in range(1, 11)))

    page = execute_tool(
        "read_file", {"path": "lines.txt", "offset": 3, "limit": 2}, tmp_path
    )
    assert (
        page
        == "line 3\nline 4\n\n...[showing lines 3-4 of 10; call read_file with offset=5 to continue]"
    )

    tail = execute_tool("read_file", {"path": "lines.txt", "offset": 9}, tmp_path)
    assert tail == "line 9\nline 10\n"

    assert execute_tool("read_file", {"path": "lines.txt", "offset": 11}, tmp_path) == (
        "Error: offset 11 is past the last line (10)"
    )
    assert execute_tool("read_file", {"path": "lines.txt", "offset": 0}, tmp_path) == (
        "Error: offset must be a positive integer"
    )


def test_read_file_long_file_cuts_on_a_line_and_names_the_next_offset(tmp_path):
    line = "x" * 100 + "\n"
    (tmp_path / "big.txt").write_text(line * 200)

    result = execute_tool("read_file", {"path": "big.txt"}, tmp_path)

    assert len(result) <= MAX_TOOL_RESULT
    match = re.search(
        r"showing lines 1-(\d+) of 200; call read_file with offset=(\d+)", result
    )
    assert match
    shown = int(match.group(1))
    assert int(match.group(2)) == shown + 1
    assert result.startswith(line * shown)


def test_edit_file_replaces_exactly_one_match(tmp_path):
    (tmp_path / "notes.md").write_text("alpha beta alpha\n")

    ambiguous = execute_tool(
        "edit_file",
        {"path": "notes.md", "old_text": "alpha", "new_text": "gamma"},
        tmp_path,
    )
    assert ambiguous.startswith("Error: old_text matches 2 places")
    assert (tmp_path / "notes.md").read_text() == "alpha beta alpha\n"

    one = execute_tool(
        "edit_file",
        {"path": "notes.md", "old_text": "beta", "new_text": "delta"},
        tmp_path,
    )
    assert one == "Replaced 1 occurrence(s) in notes.md"
    assert (tmp_path / "notes.md").read_text() == "alpha delta alpha\n"

    everything = execute_tool(
        "edit_file",
        {"path": "notes.md", "old_text": "alpha", "new_text": "z", "replace_all": True},
        tmp_path,
    )
    assert everything == "Replaced 2 occurrence(s) in notes.md"
    assert (tmp_path / "notes.md").read_text() == "z delta z\n"

    missing = execute_tool(
        "edit_file",
        {"path": "notes.md", "old_text": "nope", "new_text": "x"},
        tmp_path,
    )
    assert missing == "Error: old_text not found in notes.md"
    assert "outside workspace" in execute_tool(
        "edit_file",
        {"path": "../notes.md", "old_text": "a", "new_text": "b"},
        tmp_path,
    )
    assert "Error: not found" == execute_tool(
        "edit_file",
        {"path": "missing.md", "old_text": "a", "new_text": "b"},
        tmp_path,
    )


def test_append_file_creates_and_extends(tmp_path):
    first = execute_tool(
        "append_file", {"path": "log/out.txt", "content": "one\n"}, tmp_path
    )
    second = execute_tool(
        "append_file", {"path": "log/out.txt", "content": "two\n"}, tmp_path
    )

    assert first.startswith("Appended 4 bytes")
    assert second.startswith("Appended 4 bytes")
    assert (tmp_path / "log" / "out.txt").read_text() == "one\ntwo\n"
    assert "outside workspace" in execute_tool(
        "append_file", {"path": "../x.txt", "content": "no"}, tmp_path
    )
    assert execute_tool("append_file", {"path": "a.txt"}, tmp_path) == (
        "Error: content is required"
    )


def test_move_file_renames_without_overwriting(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "taken.txt").write_text("taken")
    (tmp_path / "dir").mkdir()

    moved = execute_tool(
        "move_file", {"source": "a.txt", "destination": "sub/b.txt"}, tmp_path
    )
    assert moved == "Moved a.txt to sub/b.txt"
    assert not (tmp_path / "a.txt").exists()
    assert (tmp_path / "sub" / "b.txt").read_text() == "a"

    assert (
        execute_tool(
            "move_file", {"source": "sub/b.txt", "destination": "taken.txt"}, tmp_path
        )
        == "Error: destination exists"
    )
    assert (tmp_path / "taken.txt").read_text() == "taken"
    assert (
        execute_tool("move_file", {"source": "dir", "destination": "dir2"}, tmp_path)
        == "Error: is a directory"
    )
    assert (
        execute_tool("move_file", {"source": "missing", "destination": "x"}, tmp_path)
        == "Error: not found"
    )
    outside = execute_tool(
        "move_file", {"source": "sub/b.txt", "destination": "../escaped.txt"}, tmp_path
    )
    assert "outside workspace" in outside
    assert (tmp_path / "sub" / "b.txt").exists()


def test_list_files_shows_tree_with_sizes_and_filters(tmp_path):
    (tmp_path / "a.md").write_text("hello")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print(1)\n")
    (tmp_path / "src" / "deep").mkdir()
    (tmp_path / "src" / "deep" / "x.md").write_text("x")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref")

    listing = execute_tool("list_files", {}, tmp_path)
    assert "src/" in listing
    assert "a.md (5 bytes)" in listing
    assert "src/main.py (9 bytes)" in listing
    assert "src/deep/x.md (1 bytes)" in listing
    assert "HEAD" not in listing

    only_md = execute_tool("list_files", {"pattern": "*.md"}, tmp_path)
    assert "a.md" in only_md
    assert "x.md" in only_md
    assert "main.py" not in only_md
    assert "src/\n" not in only_md and not only_md.endswith("src/")

    shallow = execute_tool("list_files", {"max_depth": 1}, tmp_path)
    assert "src/" in shallow
    assert "main.py" not in shallow

    assert execute_tool("list_files", {"path": "a.md"}, tmp_path) == (
        "Error: not a directory"
    )
    assert execute_tool("list_files", {"path": "nope"}, tmp_path) == "Error: not found"
    assert "outside workspace" in execute_tool("list_files", {"path": ".."}, tmp_path)
    (tmp_path / "empty").mkdir()
    assert execute_tool("list_files", {"path": "empty"}, tmp_path) == "No files found."


def test_delete_file_removes_a_file_and_refuses_a_directory(tmp_path):
    (tmp_path / "gone.txt").write_text("x")

    removed = execute_tool("delete_file", {"path": "gone.txt"}, tmp_path)

    assert removed.startswith("Deleted ")
    assert removed.endswith("gone.txt")
    assert not (tmp_path / "gone.txt").exists()
    assert (
        execute_tool("delete_file", {"path": "gone.txt"}, tmp_path)
        == "Error: not found"
    )
    (tmp_path / "dir").mkdir()
    assert execute_tool("delete_file", {"path": "dir"}, tmp_path) == (
        "Error: is a directory"
    )


def test_list_files_rejects_a_bad_pattern_and_names_the_overflow(tmp_path, monkeypatch):
    monkeypatch.setattr("harnh3x.tools.files.LIST_FILES_MAX", 2)
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text("x")

    assert (
        execute_tool("list_files", {"pattern": 1}, tmp_path)
        == "Error: pattern must be a string"
    )
    listing = execute_tool("list_files", {}, tmp_path)
    assert "a.txt" in listing
    assert "b.txt" in listing
    assert "c.txt" not in listing
    assert "[1 more entries; narrow with path or pattern]" in listing


def test_search_files_reports_path_line_and_text(tmp_path):
    (tmp_path / "a.py").write_text("import os\nTODO: fix\n")
    (tmp_path / "b.txt").write_text("todo later\n")
    (tmp_path / "bin.dat").write_bytes(b"\xff\xfeTODO")

    result = execute_tool("search_files", {"pattern": "TODO"}, tmp_path)
    assert result == "a.py:2: TODO: fix"

    loose = execute_tool(
        "search_files", {"pattern": "todo", "ignore_case": True}, tmp_path
    )
    assert "a.py:2: TODO: fix" in loose
    assert "b.txt:1: todo later" in loose

    globbed = execute_tool(
        "search_files",
        {"pattern": "todo", "ignore_case": True, "glob": "*.txt"},
        tmp_path,
    )
    assert globbed == "b.txt:1: todo later"

    assert execute_tool("search_files", {"pattern": "zzz"}, tmp_path) == "No matches."
    assert execute_tool("search_files", {"pattern": "("}, tmp_path).startswith(
        "Error: invalid pattern"
    )
    assert execute_tool("search_files", {}, tmp_path) == "Error: pattern is required"
    assert "outside workspace" in execute_tool(
        "search_files", {"pattern": "x", "path": ".."}, tmp_path
    )


def test_search_files_stops_at_the_match_cap(tmp_path):
    (tmp_path / "many.txt").write_text("hit\n" * (SEARCH_FILES_MAX + 20))

    result = execute_tool("search_files", {"pattern": "hit"}, tmp_path)

    assert result.count("many.txt:") == SEARCH_FILES_MAX
    assert f"stopped at {SEARCH_FILES_MAX} matches" in result


def test_write_file_checks_python_syntax(tmp_path):
    written = execute_tool(
        "write_file",
        {"path": "dice.py", "content": "print(1 + 1)\n"},
        tmp_path,
    )
    assert written.startswith("Wrote ")
    assert "Syntax error" not in written
    assert (tmp_path / "dice.py").read_text() == "print(1 + 1)\n"

    note = execute_tool(
        "write_file",
        {"path": "src/broken.py", "content": "def (\n"},
        tmp_path,
    )
    assert note.startswith("Wrote ")
    assert "Syntax error: line 1:" in note
    assert (tmp_path / "src" / "broken.py").read_text() == "def (\n"

    text = execute_tool(
        "write_file", {"path": "notes.md", "content": "def (\n"}, tmp_path
    )
    assert "Syntax error" not in text
    assert (tmp_path / "notes.md").read_text() == "def (\n"


def test_edit_file_reports_python_syntax(tmp_path):
    (tmp_path / "dice.py").write_text("print(1)\n")

    edited = execute_tool(
        "edit_file",
        {"path": "dice.py", "old_text": "print(1)", "new_text": "print(2)"},
        tmp_path,
    )
    assert edited == "Replaced 1 occurrence(s) in dice.py"
    assert (tmp_path / "dice.py").read_text() == "print(2)\n"

    broken = execute_tool(
        "edit_file",
        {"path": "dice.py", "old_text": "print(2)", "new_text": "def ("},
        tmp_path,
    )
    assert broken.startswith(
        "Replaced 1 occurrence(s) in dice.py\nSyntax error: line 1:"
    )
    assert (tmp_path / "dice.py").read_text() == "def (\n"


def test_outline_python_maps_classes_functions_and_line_spans(tmp_path):
    (tmp_path / "store.py").write_text(
        '"""notes"""\n'
        "\n"
        "class Store(Base, Mixin):\n"
        "    def __init__(self, path: str) -> None:\n"
        "        self.path = path\n"
        "\n"
        "    @property\n"
        "    def get(self, key: str):\n"
        "        return key\n"
        "\n"
        "    async def refresh(self):\n"
        "        def helper():\n"
        "            return 1\n"
        "        return helper\n"
        "\n"
        "def load(path):\n"
        "    return path\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    def main():\n"
        "        return load(None)\n"
    )

    result = execute_tool("outline_python", {"path": "store.py"}, tmp_path)

    assert result == (
        "store.py\n"
        "  class Store(Base, Mixin)  3-14\n"
        "    def __init__(self, path: str) -> None  4-5\n"
        "    def get(self, key: str)  7-9  @property\n"
        "    async def refresh(self)  11-14\n"
        "      def helper()  12-13\n"
        "  def load(path)  16-17\n"
        "  def main()  20-21"
    )


def test_outline_python_maps_a_directory_and_keeps_a_syntax_error(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "ok.py").write_text("def alpha():\n    return 1\n")
    (pkg / "broken.py").write_text("def (\n")
    (pkg / "notes.txt").write_text("def not_python():\n    return 1\n")
    cache = pkg / "__pycache__"
    cache.mkdir()
    (cache / "cached.py").write_text("def cached():\n    return 1\n")

    result = execute_tool("outline_python", {"path": "pkg"}, tmp_path)

    assert result == (
        "pkg/broken.py\n"
        "  syntax error: line 1: invalid syntax\n"
        "pkg/ok.py\n"
        "  def alpha()  1-2"
    )
    assert execute_tool("outline_python", {"path": "pkg/broken.py"}, tmp_path) == (
        "Error: syntax error: line 1: invalid syntax"
    )


def test_outline_python_refuses_a_missing_or_non_python_path(tmp_path):
    (tmp_path / "notes.txt").write_text("x")

    assert execute_tool("outline_python", {"path": "missing.py"}, tmp_path) == (
        "Error: not found"
    )
    assert execute_tool("outline_python", {"path": "notes.txt"}, tmp_path) == (
        "Error: not a Python file"
    )
    assert "outside workspace" in execute_tool(
        "outline_python", {"path": ".."}, tmp_path
    )


def test_outline_python_reads_outside_the_workspace_when_yolo(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "secret.py"
    outside.write_text("def secret():\n    return 1\n")

    blocked = execute_tool("outline_python", {"path": str(outside)}, workspace)
    opened = execute_tool(
        "outline_python", {"path": str(outside)}, workspace, yolo=True
    )

    assert "outside workspace" in blocked
    assert "def secret()  1-2" in opened


def test_outline_python_names_how_to_narrow_past_the_file_cap(tmp_path, monkeypatch):
    monkeypatch.setattr("harnh3x.tools.outline.OUTLINE_MAX_FILES", 1)
    (tmp_path / "a.py").write_text("def a():\n    return 1\n")
    (tmp_path / "b.py").write_text("def b():\n    return 1\n")

    result = execute_tool("outline_python", {"path": "."}, tmp_path)

    assert "def a()  1-2" in result
    assert "b.py" not in result
    assert "[1 more files; narrow path or lower max_depth]" in result


def test_outline_python_names_a_file_with_no_definitions(tmp_path):
    (tmp_path / "empty.py").write_text("import os\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "notes.txt").write_text("hi")

    assert execute_tool("outline_python", {"path": "empty.py"}, tmp_path) == (
        "empty.py\n  no classes or functions"
    )
    assert execute_tool("outline_python", {"path": "pkg"}, tmp_path) == (
        "No Python files found."
    )


def test_outline_python_stops_at_max_depth(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "top.py").write_text("def top():\n    return 1\n")
    nested = tmp_path / "pkg" / "sub"
    nested.mkdir()
    (nested / "deep.py").write_text("def deep():\n    return 1\n")

    result = execute_tool("outline_python", {"path": "pkg", "max_depth": 1}, tmp_path)

    assert "def top()  1-2" in result
    assert "deep.py" not in result


def test_outline_python_skips_a_large_or_non_utf8_file(tmp_path, monkeypatch):
    monkeypatch.setattr("harnh3x.tools.outline.FETCH_MAX_BYTES", 4)
    (tmp_path / "big.py").write_text("def big():\n    return 1\n")

    assert execute_tool("outline_python", {"path": "big.py"}, tmp_path) == (
        "big.py\n  skipped: larger than 4 bytes"
    )

    monkeypatch.setattr("harnh3x.tools.outline.FETCH_MAX_BYTES", 10_000_000)
    (tmp_path / "bad.py").write_bytes(b"\xff\xfe")
    assert execute_tool("outline_python", {"path": "bad.py"}, tmp_path) == (
        "Error: not utf-8"
    )
    (tmp_path / "ok.py").write_text("def ok():\n    return 1\n")
    listed = execute_tool("outline_python", {"path": "."}, tmp_path)
    assert "bad.py\n  skipped: not utf-8" in listed
    assert "def ok()  1-2" in listed


def test_outline_python_keeps_defs_inside_try_and_match(tmp_path):
    (tmp_path / "flow.py").write_text(
        "class Bare:\n"
        "    pass\n"
        "\n"
        "try:\n"
        "    def in_try():\n"
        "        return 1\n"
        "except ValueError:\n"
        "    def in_except():\n"
        "        return 2\n"
        "\n"
        "def choose(value):\n"
        "    match value:\n"
        "        case 1:\n"
        "            def in_case():\n"
        "                return 3\n"
    )

    result = execute_tool("outline_python", {"path": "flow.py"}, tmp_path)

    assert result == (
        "flow.py\n"
        "  class Bare  1-2\n"
        "  def in_try()  5-6\n"
        "  def in_except()  8-9\n"
        "  def choose(value)  11-15\n"
        "    def in_case()  14-15"
    )


def test_outline_python_notes_an_unreadable_file_in_a_directory(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    blocked = pkg / "blocked.py"
    blocked.write_text("def blocked():\n    return 1\n")
    blocked.chmod(0)
    (pkg / "ok.py").write_text("def ok():\n    return 1\n")
    try:
        result = execute_tool("outline_python", {"path": "pkg"}, tmp_path)
    finally:
        blocked.chmod(0o644)

    assert "def ok()  1-2" in result
    assert "blocked.py\n  skipped:" in result


def test_outline_python_clips_a_long_signature(tmp_path):
    (tmp_path / "long.py").write_text(f"def f({'a' * 100}):\n    return 1\n")

    line = execute_tool("outline_python", {"path": "long.py"}, tmp_path).splitlines()[1]

    assert line.endswith("  1-2")
    assert len(line.removesuffix("  1-2").strip()) == 80
    assert line.removesuffix("  1-2").strip().endswith("...")


def _git_init(path) -> None:
    subprocess.run(
        ["git", "init", "-q"],
        cwd=path,
        check=True,
        capture_output=True,
    )


def test_glob_matches_path_globs_and_sorts_by_mtime(tmp_path):
    (tmp_path / "old.js").write_text("old\n")
    (tmp_path / "new.js").write_text("new\n")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "x.js").write_text("nested\n")
    source = tmp_path / "src" / "pkg"
    source.mkdir(parents=True)
    (tmp_path / "src" / "a.ts").write_text("export {}\n")
    (source / "b.ts").write_text("export {}\n")
    (tmp_path / "other.ts").write_text("export {}\n")
    os.utime(tmp_path / "old.js", (1_000, 1_000))
    os.utime(tmp_path / "new.js", (3_000, 3_000))
    os.utime(nested / "x.js", (2_000, 2_000))

    listing = execute_tool("glob", {"pattern": "**/*.js"}, tmp_path)

    assert [line for line in listing.splitlines() if line.endswith(".js")] == [
        "new.js",
        "nested/x.js",
        "old.js",
    ]
    typescript = execute_tool("glob", {"pattern": "src/**/*.ts"}, tmp_path)
    assert "src/a.ts" in typescript
    assert "src/pkg/b.ts" in typescript
    assert "other.ts" not in typescript
    (source / "c.js").write_text("deep\n")
    assert execute_tool("glob", {"pattern": "*.js", "path": "src"}, tmp_path) == (
        "src/pkg/c.js"
    )
    assert execute_tool("glob", {}, tmp_path) == "Error: pattern is required"
    assert execute_tool("glob", {"pattern": "*.js", "path": "old.js"}, tmp_path) == (
        "Error: not a directory"
    )
    assert "outside workspace" in execute_tool(
        "glob", {"pattern": "*.js", "path": ".."}, tmp_path
    )


def test_glob_names_the_overflow(tmp_path, monkeypatch):
    monkeypatch.setattr("harnh3x.tools.find.LIST_FILES_MAX", 2)
    for name in ("a.js", "b.js", "c.js"):
        (tmp_path / name).write_text("x\n")

    listing = execute_tool("glob", {"pattern": "*.js"}, tmp_path)

    assert listing.count(".js") == 2
    assert "1 more entries; narrow the pattern or path" in listing


def test_grep_reports_matches_and_filters_files(tmp_path):
    (tmp_path / "a.py").write_text("import os\nTODO: fix\n")
    (tmp_path / "b.txt").write_text("todo later\n")
    (tmp_path / "bin.dat").write_bytes(b"TODO\x00rest\n")

    result = execute_tool("grep", {"pattern": "TODO"}, tmp_path)
    assert result == "a.py:2:TODO: fix"

    loose = execute_tool("grep", {"pattern": "todo", "ignore_case": True}, tmp_path)
    assert "a.py:2:TODO: fix" in loose
    assert "b.txt:1:todo later" in loose
    assert "bin.dat" not in loose

    globbed = execute_tool(
        "grep",
        {"pattern": "todo", "ignore_case": True, "glob": "*.txt"},
        tmp_path,
    )
    assert globbed == "b.txt:1:todo later"
    assert execute_tool("grep", {"pattern": "zzz"}, tmp_path) == "No matches."
    assert execute_tool("grep", {"pattern": "("}, tmp_path).startswith("Error:")
    assert execute_tool("grep", {}, tmp_path) == "Error: pattern is required"
    assert execute_tool("grep", {"pattern": "x", "glob": 1}, tmp_path) == (
        "Error: glob must be a string"
    )
    assert "outside workspace" in execute_tool(
        "grep", {"pattern": "x", "path": ".."}, tmp_path
    )


def test_grep_stops_at_the_match_cap(tmp_path, monkeypatch):
    monkeypatch.setattr("harnh3x.tools.find.SEARCH_FILES_MAX", 2)
    (tmp_path / "many.txt").write_text("hit\n" * 5)

    result = execute_tool("grep", {"pattern": "hit"}, tmp_path)

    assert result.count("many.txt:") == 2
    assert "stopped at 2 matches; narrow the pattern or path" in result


def test_glob_and_grep_skip_gitignored_and_hidden_files(tmp_path):
    (tmp_path / "visible.txt").write_text("visible-token\n")
    (tmp_path / "secret.txt").write_text("secret-token\n")
    (tmp_path / ".secret").write_text("hidden-token\n")
    (tmp_path / ".gitignore").write_text("secret.txt\n")
    _git_init(tmp_path)

    listing = execute_tool("glob", {"pattern": "*.txt"}, tmp_path)
    assert "visible.txt" in listing
    assert "secret.txt" not in listing

    found = execute_tool("grep", {"pattern": "token"}, tmp_path)
    assert "visible-token" in found
    assert "secret-token" not in found
    assert "hidden-token" not in found

    ignored = execute_tool(
        "grep",
        {"pattern": "token", "glob": "*.txt", "include_ignored": True},
        tmp_path,
    )
    assert "secret-token" in ignored
    assert "hidden-token" not in ignored

    listed = execute_tool(
        "glob", {"pattern": "**/*", "include_ignored": True}, tmp_path
    )
    assert "secret.txt" in listed
    assert ".secret" not in listed
    assert ".git/" not in listed
    assert ".git\n" not in listed and not listed.endswith(".git")


def test_gitignore_is_not_applied_outside_a_repository(tmp_path):
    (tmp_path / "secret.txt").write_text("secret-token\n")
    (tmp_path / ".gitignore").write_text("secret.txt\n")

    assert "secret.txt" in execute_tool("grep", {"pattern": "secret-token"}, tmp_path)
    assert "secret.txt" in execute_tool("glob", {"pattern": "*.txt"}, tmp_path)


def test_grep_reads_outside_the_workspace_when_yolo(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "out"
    outside.mkdir()
    (outside / "note.txt").write_text("outside-token\n")

    blocked = execute_tool(
        "grep", {"pattern": "outside-token", "path": str(outside)}, workspace
    )
    found = execute_tool(
        "grep",
        {"pattern": "outside-token", "path": str(outside)},
        workspace,
        yolo=True,
    )

    assert "outside workspace" in blocked
    assert "outside-token" in found


def test_glob_and_grep_use_the_project_directory(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    (workspace / "notes.py").write_text("workspace-only\n")
    (project / "app.py").write_text("project-only\n")

    listed = execute_tool("glob", {"pattern": "*.py"}, workspace, project=project)
    found = execute_tool("grep", {"pattern": "only"}, workspace, project=project)

    assert "app.py" in listed
    assert "notes.py" not in listed
    assert "project-only" in found
    assert "workspace-only" not in found


def test_missing_rg_is_an_error(tmp_path, monkeypatch):
    empty = tmp_path / "bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    assert execute_tool("glob", {"pattern": "*.py"}, tmp_path) == (
        "Error: ripgrep (rg) was not found"
    )
    assert execute_tool("grep", {"pattern": "x"}, tmp_path) == (
        "Error: ripgrep (rg) was not found"
    )


def test_select_rg_prefers_a_read_root_and_grants_an_outside_binary(
    tmp_path, monkeypatch
):
    from harnh3x.sandbox import READ_ROOTS
    from harnh3x.tools import find

    inside_dir = tmp_path / "root" / "bin"
    inside_dir.mkdir(parents=True)
    inside = inside_dir / "rg"
    inside.write_text("#!/bin/sh\n")
    inside.chmod(0o755)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside = outside_dir / "rg"
    outside.write_text("#!/bin/sh\n")
    outside.chmod(0o755)
    monkeypatch.setattr(find, "READ_ROOTS", (*READ_ROOTS, str(tmp_path / "root")))
    monkeypatch.setenv("PATH", f"{outside_dir}{os.pathsep}{inside_dir}")

    chosen, grant = find.select_rg()

    assert chosen == inside.resolve()
    assert grant is None
    monkeypatch.setenv("PATH", str(outside_dir))
    chosen, grant = find.select_rg()
    assert chosen == outside.resolve()
    assert grant == outside.resolve()


def test_extra_read_grants_one_rg_file(tmp_path):
    from harnh3x import sandbox

    workspace = tmp_path / "ws"
    workspace.mkdir()
    binary = tmp_path / "outside" / "rg"
    binary.parent.mkdir()
    binary.write_bytes(b"#!/bin/sh\n")
    resolved = str(binary.resolve())

    profile = sandbox.seatbelt_profile(workspace, extra_read=binary)
    policy = sandbox.linux_policy(workspace, None, "aarch64", extra_read=binary)

    assert f'(literal "{resolved}")' in profile
    assert f'(subpath "{binary.parent.resolve()}")' not in profile
    assert resolved in policy["read"]
    assert str(binary.parent.resolve()) not in policy["read"]
    if sandbox.sys.platform == "darwin":
        _argv, env = sandbox.sandboxed_argv(["true"], workspace, extra_read=binary)
        assert f'(literal "{resolved}")' in env[sandbox.PROFILE_ENV]
