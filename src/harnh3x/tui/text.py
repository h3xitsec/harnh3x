"""Pure text helpers: wrapping, panel bodies, and the status context line."""

from __future__ import annotations

import json

from textual.widgets import Static

THINKING_ROWS = 3

YOLO_PLAIN = "YOLO — sandbox off. This session can read any file and run any command, including sudo."


def line_limit(width: int) -> int:
    # A line as wide as the terminal wraps before the following newline.
    if width <= 1:
        return 1
    return width - 1


def wrap_lines(text: str, width: int) -> list[str]:
    width = max(width, 1)
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if paragraph == "":
            lines.append("")
            continue
        start = 0
        while start < len(paragraph):
            lines.append(paragraph[start : start + width])
            start += width
    return lines


def thinking_hint(*, expanded: bool, hold: bool, click: bool = False) -> str:
    parts = []
    if click:
        action = "collapse" if expanded else "expand"
        parts.append(f"click to {action}")
    if hold:
        parts.append("enter to close")
    return " · ".join(parts)


def _thinking_wrapped(reasoning: str, *, width: int) -> tuple[list[str], str]:
    limit = line_limit(width)
    indent = "  " if limit > 2 else ""
    body_limit = max(limit - len(indent), 1)
    return wrap_lines(reasoning, body_limit), indent


def thinking_overflows(reasoning: str, *, width: int) -> bool:
    wrapped, _indent = _thinking_wrapped(reasoning, width=width)
    return len(wrapped) > THINKING_ROWS


def thinking_shown(reasoning: str, *, expanded: bool, width: int) -> list[str]:
    wrapped, indent = _thinking_wrapped(reasoning, width=width)
    shown = wrapped if expanded else wrapped[-THINKING_ROWS:]
    return [f"{indent}{line}" for line in shown]


def thinking_text(reasoning: str, *, expanded: bool, width: int, hold: bool) -> str:
    hint = thinking_hint(expanded=expanded, hold=hold)
    title = f"Thinking · {hint}" if hint else "Thinking"
    return debug_text(title, reasoning, expanded=expanded, width=width)


def debug_text(title: str, body: str, *, expanded: bool, width: int) -> str:
    limit = line_limit(width)
    header = "\n".join(wrap_lines(title, limit))
    shown = "\n".join(thinking_shown(body, expanded=expanded, width=width))
    return f"{header}\n{shown}"


def context_line(used: int | None, limit: int | None) -> str | None:
    if used is None and limit is None:
        return None
    used_text = f"{used:,}" if used is not None else "—"
    if limit is None:
        return f"Context: {used_text}"
    return f"Context: {used_text} / {limit:,}"


def displayed_args(arguments: object) -> object:
    if isinstance(arguments, dict):
        return arguments
    if not isinstance(arguments, str):
        return arguments
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return arguments
    if isinstance(parsed, dict):
        return parsed
    return arguments


def _panel_inner_width(widget: Static, outer: int) -> int:
    gutter = widget.styles.gutter
    horizontal = gutter.left + gutter.right
    if horizontal == 0:
        horizontal = 4
    return max(outer - horizontal, 1)


def reply_reasoning(session, start: int, answer: str) -> str:
    messages = getattr(session, "messages", None)
    if not messages:
        return ""
    parts = []
    for msg in messages[start:]:
        if not isinstance(msg, dict):
            continue
        reasoning = msg.get("reasoning")
        if msg.get("role") == "assistant" and reasoning:
            parts.append(reasoning)
    if parts and parts[-1] == answer:
        parts.pop()
    return "\n\n".join(parts)
