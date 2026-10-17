"""@ file and folder mentions for a coding session.

The composer inserts a path. The coding prompt tells the model to read it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from harnh3x.tools.files import SKIP_DIRS

MENTION_INDEX_MAX = 5000
MENTION_ROWS = 8


@dataclass(frozen=True)
class Mention:
    """A project file or directory, relative to the project root."""

    path: str
    is_dir: bool


def mention_at(text: str, offset: int) -> tuple[int, int, str] | None:
    """Return the active `@` token that ends at `offset`.

    The token is `(start, end, query)`. `@` counts only at the start of the
    text or after whitespace, and an unquoted query stops at whitespace. A
    finished quoted mention such as `@"my file.py"` is not active.
    """
    offset = max(0, min(offset, len(text)))
    if offset == 0:
        return None
    at = text.rfind("@", 0, offset)
    if at < 0 or (at > 0 and not text[at - 1].isspace()):
        return None
    body = text[at + 1 : offset]
    if body.startswith('"'):
        if _closing_quote(body[1:]) is not None:
            return None
        return (at, offset, _unescape(body[1:]))
    if any(char.isspace() for char in body):
        return None
    return (at, offset, body)


def index_project(root: Path, *, limit: int = MENTION_INDEX_MAX) -> list[Mention]:
    """List files and directories under `root`, skipping `SKIP_DIRS`.

    Directory symlinks are listed and not followed. The walk stops at `limit`.
    """
    if limit < 1 or not root.is_dir():
        return []
    found: list[Mention] = []

    def add(path: Path, is_dir: bool) -> bool:
        found.append(Mention(path.relative_to(root).as_posix(), is_dir))
        return len(found) >= limit

    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        children: list[str] = []
        for name in sorted(dirs):
            if name in SKIP_DIRS:
                continue
            path = current_path / name
            linked = path.is_symlink()
            if add(path, True):
                return found
            if not linked:
                children.append(name)
        dirs[:] = children
        for name in sorted(files):
            if add(current_path / name, False):
                return found
    return found


def mention_browse(entries: list[Mention], query: str) -> tuple[str, str]:
    """Return the folder being browsed and the filter typed inside it.

    A slash enters a directory that exists in `entries`. ``src/app`` browses
    ``src`` and filters for ``app``. Text with no slash stays in the project
    root.
    """
    directories = {entry.path for entry in entries if entry.is_dir}
    directory = ""
    rest = query
    while True:
        slash = rest.find("/")
        if slash < 0:
            return directory, rest
        segment = rest[:slash]
        candidate = f"{directory}/{segment}" if directory else segment
        if candidate not in directories:
            return directory, rest
        directory = candidate
        rest = rest[slash + 1 :]


def mention_children(entries: list[Mention], directory: str) -> list[Mention]:
    """Return the files and directories directly inside `directory`."""
    prefix = f"{directory}/" if directory else ""
    children = []
    for entry in entries:
        if prefix and not entry.path.startswith(prefix):
            continue
        name = entry.path[len(prefix) :]
        if name and "/" not in name:
            children.append(entry)
    return children


def replace_mention_query(
    text: str, start: int, end: int, query: str
) -> tuple[str, int]:
    """Replace the active `@` query and leave the cursor in the token."""
    start = max(0, min(start, len(text)))
    end = max(start, min(end, len(text)))
    if any(char.isspace() or char == '"' for char in query):
        escaped = query.replace("\\", "\\\\").replace('"', '\\"')
        body = f'"{escaped}'
    else:
        body = query
    label = f"@{body}"
    updated = text[:start] + label + text[end:]
    return updated, start + len(label)


def filter_mentions(
    entries: list[Mention], query: str, *, limit: int | None = None
) -> list[Mention]:
    """Return the ranked matches for `query`.

    An empty query returns the shallowest paths. Otherwise a match is a
    case-insensitive subsequence of the relative path, ranked by basename
    prefix, basename substring, full-path substring, then any other
    subsequence. Ties break toward the shorter path, then alphabetical.
    `limit` keeps that many matches; a limit below 1 returns nothing.
    """
    if limit is not None and limit < 1:
        return []
    if not query:
        ranked = sorted(entries, key=lambda entry: _alpha(entry))
    else:
        needle = query.casefold()
        scored: list[tuple[tuple[int, int, str, str], Mention]] = []
        for entry in entries:
            rank = _rank(entry, needle)
            if rank is not None:
                scored.append((rank, entry))
        scored.sort(key=lambda item: item[0])
        ranked = [entry for _rank_key, entry in scored]
    if limit is None:
        return ranked
    return ranked[:limit]


def insert_mention(text: str, start: int, end: int, entry: Mention) -> tuple[str, int]:
    """Replace `text[start:end]` with the mention and return the new cursor."""
    start = max(0, min(start, len(text)))
    end = max(start, min(end, len(text)))
    label = _label(entry)
    updated = text[:start] + label + text[end:]
    return updated, start + len(label)


def mention_menu(entries: list[Mention], selected: int) -> str:
    """Render the list. The highlighted row starts with `>`."""
    if not entries:
        return ""
    chosen = min(max(selected, 0), len(entries) - 1)
    lines = []
    for index, entry in enumerate(entries):
        mark = ">" if index == chosen else " "
        suffix = "/" if entry.is_dir else ""
        lines.append(f"{mark} {entry.path}{suffix}")
    return "\n".join(lines)


def mention_window(
    entries: list[Mention],
    selected: int,
    offset: int,
    *,
    rows: int = MENTION_ROWS,
) -> tuple[int, int, str]:
    """Show `rows` lines, scrolling so `selected` stays in view.

    Returns the clamped selection, the index of the first visible row, and
    the rendered menu.
    """
    if not entries or rows < 1:
        return 0, 0, ""
    last = len(entries) - 1
    selected = min(max(selected, 0), last)
    offset = min(max(offset, 0), max(0, len(entries) - rows))
    if selected < offset:
        offset = selected
    elif selected >= offset + rows:
        offset = selected - rows + 1
    visible = entries[offset : offset + rows]
    return selected, offset, mention_menu(visible, selected - offset)


def _label(entry: Mention) -> str:
    shown = entry.path + ("/" if entry.is_dir else "")
    if any(char.isspace() or char == '"' for char in shown):
        escaped = shown.replace("\\", "\\\\").replace('"', '\\"')
        return f'@"{escaped}" '
    return f"@{shown} "


def _unescape(inner: str) -> str:
    characters: list[str] = []
    escaped = False
    for char in inner:
        if escaped:
            characters.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        characters.append(char)
    if escaped:
        characters.append("\\")
    return "".join(characters)


def _closing_quote(inner: str) -> int | None:
    """Index of the unescaped closing quote in `inner`, if it has one."""
    escaped = False
    for index, char in enumerate(inner):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            return index
    return None


def _alpha(entry: Mention) -> tuple[int, str, str]:
    return (entry.path.count("/"), entry.path.casefold(), entry.path)


def _rank(entry: Mention, needle: str) -> tuple[int, int, str, str] | None:
    path = entry.path.casefold()
    if not _subsequence(path, needle):
        return None
    base = path.rsplit("/", 1)[-1]
    if base.startswith(needle):
        band = 0
    elif needle in base:
        band = 1
    elif needle in path:
        band = 2
    else:
        band = 3
    return (band, len(entry.path), path, entry.path)


def _subsequence(path: str, needle: str) -> bool:
    if not needle:
        return True
    index = 0
    for char in path:
        if char == needle[index]:
            index += 1
            if index == len(needle):
                return True
    return False
