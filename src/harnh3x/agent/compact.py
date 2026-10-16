from types import SimpleNamespace

from openai import OpenAI

import harnh3x.agent.client as api_client
from harnh3x.agent.client import _count
from harnh3x.agent.session import Session
from harnh3x.agent.transcript import _save_transcript, write_log
from harnh3x.anthropic_api import AnthropicClient
from harnh3x.config import COMPACT_AT

_COMPACT_PREFIX = "Context compacted:"
_COMPACT_INSTRUCTION = (
    "Summarize this conversation for another assistant who will continue the work. "
    "Keep file paths, decisions, errors, and unfinished work. Be concise."
)


def _is_compact_note(message: object) -> bool:
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    content = message.get("content")
    return isinstance(content, str) and content.startswith(_COMPACT_PREFIX)


def _over_compact_threshold(session: Session) -> bool:
    used = _count(session.context_tokens)
    limit = _count(session.context_limit)
    if used is None or limit is None or limit <= 0:
        return False
    return used >= limit * COMPACT_AT


def _split_for_compact(messages: list) -> tuple[list, list] | None:
    last_user = None
    for index, message in enumerate(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            last_user = index
    if last_user is None:
        return None
    # Keep the system prompt, the current request, a tool result the model has
    # not answered yet, and a finished answer. Earlier tool rounds are compacted.
    protected = {0, last_user}
    last = messages[-1] if messages else None
    if (
        isinstance(last, dict)
        and last.get("role") == "assistant"
        and not last.get("tool_calls")
        and len(messages) - 1 > last_user
    ):
        protected.add(len(messages) - 1)
    elif isinstance(last, dict) and last.get("role") == "tool":
        index = len(messages) - 1
        while (
            index > last_user
            and isinstance(messages[index], dict)
            and messages[index].get("role") == "tool"
        ):
            protected.add(index)
            index -= 1
        if (
            index > last_user
            and isinstance(messages[index], dict)
            and messages[index].get("role") == "assistant"
            and messages[index].get("tool_calls")
        ):
            protected.add(index)
    middle = [
        message for index, message in enumerate(messages) if index not in protected
    ]
    if not middle or all(_is_compact_note(message) for message in middle):
        return None
    tail = [
        message
        for index, message in enumerate(messages)
        if index in protected and index != 0
    ]
    return middle, tail


def _message_text(message: dict) -> str:
    role = message.get("role")
    shown = role if isinstance(role, str) else "message"
    parts = [f"{shown}:"]
    content = message.get("content")
    if isinstance(content, str) and content:
        parts.append(content)
    names = []
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    if names:
        parts.append("tools: " + ", ".join(names))
    return " ".join(parts)


def _render_middle(messages: list, budget: int) -> str:
    text = "\n".join(
        _message_text(message) for message in messages if isinstance(message, dict)
    )
    if budget > 0 and len(text) > budget:
        return text[-budget:]
    return text


def _skill_line(names: list[str]) -> str:
    if not names:
        return ""
    return "Loaded skills: " + ", ".join(names) + "."


def _summary_text(response) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)
    if not isinstance(content, str):
        return ""
    return content.strip()


def _context_chars(messages: list) -> int:
    total = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        for key in ("content", "reasoning"):
            value = message.get(key)
            if isinstance(value, str):
                total += len(value)
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            if isinstance(name, str):
                total += len(name)
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                total += len(arguments)
    return total


def _scaled_tokens(before: int, old_chars: int, new_chars: int) -> int:
    if old_chars <= 0:
        return before
    return max(round(before * new_chars / old_chars), 1)


def _notify_compact(session: Session, phase: str) -> None:
    callback = getattr(session, "on_compact", None)
    if callback is not None:
        callback(phase)


def _compact(session: Session, api, *, force: bool = False) -> bool:
    if session.abort.is_set():
        return False
    if session.context_limit is None:
        limit = api_client.model_context_limit(session.model, api)
        if limit is not None:
            session.context_limit = limit
    if not force and not _over_compact_threshold(session):
        return False
    split = _split_for_compact(session.messages)
    if split is None:
        return False
    middle, tail = split
    loaded = session.skills.loaded_names()
    limit = _count(session.context_limit)
    body = _render_middle(middle, limit * 2 if limit else 0)
    skill_line = _skill_line(loaded)
    if skill_line:
        body = f"{body}\n{skill_line}"
    before_tokens = _count(session.context_tokens)
    old_chars = _context_chars(session.messages)
    _notify_compact(session, "start")
    try:
        try:
            if session.abort.is_set():
                return False
            response = _summary_response(session, api, body)
        except Exception:
            return False
        if session.abort.is_set():
            return False
        summary = _summary_text(response)
        if not summary:
            return False
        content = f"{_COMPACT_PREFIX}\n{summary}"
        if skill_line:
            content = f"{content}\n{skill_line}"
        note = {"role": "user", "content": content}
        if before_tokens is not None:
            note["context_before"] = before_tokens
            note["context_after"] = _scaled_tokens(
                before_tokens,
                old_chars,
                _context_chars([session.messages[0], note, *tail]),
            )
        before = session.context_tokens
        session.messages = [session.messages[0], note, *tail]
        session.sync_prompt()
        # The summary call's usage describes the summary request, not the
        # compacted history, so keep the scaled estimate until the next
        # completion reports real usage.
        if before_tokens is not None:
            session.context_tokens = note["context_after"]
        if session.context_limit is None:
            limit = api_client.model_context_limit(session.model, api)
            if limit is not None:
                session.context_limit = limit
        write_log(
            {"event": "compact", "before": before, "dropped": len(middle)},
            session.workspace,
        )
        return True
    finally:
        _notify_compact(session, "done")


def _summary_response(session: Session, api, body: str):
    messages = [
        {"role": "system", "content": _COMPACT_INSTRUCTION},
        {"role": "user", "content": body},
    ]
    if isinstance(api, AnthropicClient):
        summary, response = api.complete_text(model=session.model, messages=messages)
        response.choices = [SimpleNamespace(message=SimpleNamespace(content=summary))]
        return response
    return api.chat.completions.create(model=session.model, messages=messages)


def compact_session(
    session: Session, client: OpenAI | AnthropicClient | None = None
) -> str:
    api = client or api_client.get_client()
    if _compact(session, api, force=True):
        _save_transcript(session)
        return "Context compacted."
    if _split_for_compact(session.messages) is None:
        return "Nothing to compact."
    return "Could not compact the context."
