"""Pure text helpers: wrapping, panel bodies, and the context warning level."""

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


CONTEXT_WARN_SHARE = 0.8


def context_level(used: int | None, limit: int | None, compact_at: float) -> str | None:
    """`warn` near the compaction point, `over` at or past it, else None."""
    if used is None or not limit or limit <= 0 or compact_at <= 0:
        return None
    threshold = limit * compact_at
    if used >= threshold:
        return "over"
    if used >= threshold * CONTEXT_WARN_SHARE:
        return "warn"
    return None


APPLE_TERMINAL = "Apple_Terminal"


def translate_legacy_key(
    key: str, character: str | None, term_program: str | None
) -> str:
    """The key a Terminal.app byte sequence stands for.

    Terminal.app has no kitty keyboard protocol, so Ctrl+H arrives as
    Backspace carrying ``\\x08`` and Option+Backspace (with Meta on) as Ctrl+W
    with no character. A kitty terminal sends a real Ctrl+W with no character
    too, so other terminals pass through unchanged.
    """
    if term_program != APPLE_TERMINAL:
        return key
    if key == "backspace" and character == "\x08":
        return "ctrl+h"
    if key == "ctrl+w" and character is None:
        return "alt+backspace"
    return key


ARG_VALUE_LIMIT = 80


def _clip(text: str, limit: int | None) -> str:
    if limit is None or len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _arg_value(value: object, limit: int | None) -> str:
    if isinstance(value, str) and value and not any(ch.isspace() for ch in value):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    return _clip(text, limit)


def format_call(name: str, args: object, *, limit: int | None = ARG_VALUE_LIMIT) -> str:
    """One call as ``name  key=value``; ``limit=None`` keeps every value whole."""
    if isinstance(args, dict):
        parts = [f"{key}={_arg_value(value, limit)}" for key, value in args.items()]
    elif args is None or args == "":
        parts = []
    else:
        text = args if isinstance(args, str) else repr(args)
        parts = [_clip(" ".join(text.split()), limit)]
    return "  ".join([name, *parts])


def slash_hint(matches: list) -> str:
    if len(matches) == 1:
        command = matches[0]
        return f"/{command.name}  {command.summary} · Tab completes"
    names = "  ".join(f"/{command.name}" for command in matches)
    return f"{names} · Tab completes"


SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def elapsed_text(seconds: float) -> str:
    whole = max(int(seconds), 0)
    if whole < 60:
        return f"{whole}s"
    minutes, rest = divmod(whole, 60)
    return f"{minutes}m {rest:02d}s"


def activity_text(frame: int, phase: str, seconds: float, *, stoppable: bool) -> str:
    parts = [f"{SPINNER[frame % len(SPINNER)]} {phase}", elapsed_text(seconds)]
    if stoppable:
        parts.append("Esc to stop")
    return " · ".join(parts)


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
