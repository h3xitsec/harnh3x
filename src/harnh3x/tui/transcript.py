"""Turn a stored transcript into the turns and compaction notes the TUI shows."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from textual.widgets import Static

from harnh3x.agent import _is_compact_note, _visible_answer
from harnh3x.tui.text import displayed_args, reply_reasoning


@dataclass(frozen=True)
class Turn:
    user: str
    tools: list[tuple[str, object]]
    reasoning: str
    answer: str | None
    steps: tuple = ()


def _turn_tools(span: list) -> list[tuple[str, object]]:
    tools: list[tuple[str, object]] = []
    for message in span:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            if not isinstance(name, str):
                name = ""
            tools.append((name, displayed_args(function.get("arguments"))))
    return tools


def _turn_answer(span: list) -> str | None:
    last = None
    for message in span:
        if isinstance(message, dict) and message.get("role") == "assistant":
            last = message
    if last is None or last.get("tool_calls"):
        return None
    return _visible_answer(last)


@dataclass(frozen=True)
class Compaction:
    summary: str
    before: int | None = None
    after: int | None = None


def _token_count(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def compact_summary(message: object) -> str | None:
    if not _is_compact_note(message):
        return None
    content = message.get("content") if isinstance(message, dict) else ""
    if not isinstance(content, str):
        return ""
    rest = content.split("\n", 1)[1] if "\n" in content else ""
    return rest.strip()


def compact_change(before: int | None, after: int | None) -> str | None:
    if before is None or after is None:
        return None
    return f"{before:,} → {after:,}"


def _static_text(widget: Static) -> str:
    body = str(widget.content)
    title = str(widget.border_title or "").strip()
    subtitle = str(widget.border_subtitle or "").strip()
    if not title:
        return body
    header = f"{title}  {subtitle}" if subtitle else title
    if body:
        return f"{header}\n{body}"
    return header


def compact_label(compaction: Compaction) -> str:
    header = "Context compacted"
    change = compact_change(compaction.before, compaction.after)
    if change is not None:
        header = f"{header}  {change}"
    if compaction.summary:
        return f"{header}\n{compaction.summary}"
    return header


def _call_steps(message: dict) -> tuple:
    calls = []
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str):
            name = ""
        calls.append((name, displayed_args(function.get("arguments"))))
    return tuple(calls)


def _assistant_steps(message: dict, answer: str | None, *, is_final: bool) -> list:
    reasoning = message.get("reasoning")
    if not isinstance(reasoning, str):
        reasoning = ""
    content = message.get("content")
    if not isinstance(content, str):
        content = ""
    calls = _call_steps(message)
    steps = []
    if reasoning and (content or calls or reasoning != (answer or "")):
        steps.append(("thinking", reasoning))
    if content:
        steps.append(("answer", content))
    elif not calls and reasoning and reasoning == (answer or ""):
        steps.append(("answer", reasoning))
    elif is_final and not calls and not reasoning and not content and answer:
        steps.append(("answer", answer))
    if calls:
        steps.append(("tools", calls))
    return steps


def _span_steps(span: list, answer: str | None) -> list:
    final = None
    for message in span:
        if (
            isinstance(message, dict)
            and message.get("role") == "assistant"
            and not _call_steps(message)
        ):
            final = message
    steps = []
    number = 0
    for message in span:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        piece = _assistant_steps(message, answer, is_final=message is final)
        if not piece:
            continue
        number += 1
        steps.append(("round", number))
        steps.extend(piece)
    return steps


def transcript_items(messages: list) -> list[tuple]:
    items: list[tuple] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        summary = compact_summary(message)
        if summary is not None and isinstance(message, dict):
            items.append(
                (
                    "compact",
                    Compaction(
                        summary,
                        _token_count(message.get("context_before")),
                        _token_count(message.get("context_after")),
                    ),
                )
            )
            index += 1
            continue
        if not isinstance(message, dict) or message.get("role") != "user":
            index += 1
            continue
        content = message.get("content")
        user = content if isinstance(content, str) else ""
        index += 1
        start = index
        while index < len(messages):
            nxt = messages[index]
            if isinstance(nxt, dict) and nxt.get("role") == "user":
                break
            index += 1
        span = messages[start:index]
        answer = _turn_answer(span)
        reasoning = reply_reasoning(
            SimpleNamespace(messages=span),
            0,
            answer if answer is not None else "",
        )
        items.append(
            (
                "turn",
                Turn(
                    user,
                    _turn_tools(span),
                    reasoning,
                    answer,
                    tuple(_span_steps(span, answer)),
                ),
            )
        )
    return items


def conversation_turns(messages: list) -> list[Turn]:
    return [item for kind, item in transcript_items(messages) if kind == "turn"]
