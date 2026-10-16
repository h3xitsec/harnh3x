"""File tools inside the workspace."""

import re

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
