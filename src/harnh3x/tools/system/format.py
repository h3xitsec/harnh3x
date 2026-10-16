"""Parsers and formatters shared by the Darwin and Linux reports."""

import re
from pathlib import Path

from harnh3x.tools.system.probe import HostProbe

_PLACEHOLDERS = {
    "",
    "none",
    "system product name",
    "to be filled by o.e.m.",
    "default string",
    "not specified",
    "not available",
}
_KV = re.compile(r"^(\s*)([^:\n]+):\s*(.*?)\s*$")
_BYTES = re.compile(r"\(([0-9,]+) bytes\)")


class _Node:
    def __init__(self, name: str) -> None:
        self.name = name
        self.fields: dict[str, str] = {}
        self.children: list[_Node] = []


def _note(notes: list[str], text: str | None) -> None:
    if text and text not in notes:
        notes.append(text)


def _command(
    probe: HostProbe,
    argv: list[str],
    timeout: int,
    notes: list[str],
    *,
    quiet: bool = False,
) -> str | None:
    text, note = probe.run(argv, timeout)
    if not quiet:
        _note(notes, note)
    return text


def _optional(
    probe: HostProbe, argv: list[str], timeout: int, notes: list[str]
) -> str | None:
    if probe.which(Path(argv[0]).name) is None:
        return None
    return _command(probe, argv, timeout, notes)


def _field(label: str, value: str | None) -> str | None:
    if not value:
        return None
    return f"  {label}: {value}"


def _rows(*fields: str | None) -> list[str]:
    return [field for field in fields if field]


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.replace("\x00", "").strip()
    if text.lower() in _PLACEHOLDERS:
        return None
    return text


def _size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B" or abs(value - round(value)) < 0.05:
                return f"{round(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{num} B"


def _uptime(seconds: int) -> str:
    days, rem = divmod(max(seconds, 0), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def _capacity(text: str) -> str:
    match = _BYTES.search(text)
    if match:
        return _size(int(match.group(1).replace(",", "")))
    return text


def _parse_tree(text: str) -> list[_Node]:
    roots: list[_Node] = []
    stack: list[tuple[int, _Node]] = []
    for line in text.splitlines():
        match = _KV.match(line)
        if not match:
            continue
        indent = len(match.group(1))
        key = match.group(2).strip()
        value = match.group(3).strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if not value:
            node = _Node(key)
            if stack:
                stack[-1][1].children.append(node)
            else:
                roots.append(node)
            stack.append((indent, node))
        elif stack:
            stack[-1][1].fields[key] = value
    return roots


def _section(roots: list[_Node], name: str) -> _Node | None:
    for node in roots:
        if node.name == name:
            return node
    return None


def _descendants(node: _Node) -> list[_Node]:
    found: list[_Node] = []
    for child in node.children:
        found.append(child)
        found.extend(_descendants(child))
    return found


def _deep(node: _Node, key: str) -> str | None:
    if key in node.fields:
        return node.fields[key]
    for child in node.children:
        found = _deep(child, key)
        if found:
            return found
    return None


def _pairs(text: str) -> dict[str, str]:
    found = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        found[key.strip()] = value.strip()
    return found
