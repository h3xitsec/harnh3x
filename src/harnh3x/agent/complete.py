import json
from types import SimpleNamespace

from openai import APIConnectionError, APIStatusError

from harnh3x import config
from harnh3x.agent.messages import _reasoning_text
from harnh3x.agent.session import Session
from harnh3x.anthropic_api import AnthropicClient


def _notify_delta(session: Session, kind: str, text: str) -> None:
    if not isinstance(text, str) or text == "":
        return
    callback = getattr(session, "on_delta", None)
    if callback is None:
        return
    callback(kind, text)
    session._delta_open_line = not text.endswith("\n")


def _rejects_stream_options(exc: APIStatusError) -> bool:
    param = getattr(exc, "param", None)
    if isinstance(param, str) and "stream_options" in param:
        return True
    text = str(exc).lower()
    return "stream_options" in text or "include_usage" in text


def _rejects_max_tokens(exc: APIStatusError) -> bool:
    param = getattr(exc, "param", None)
    if isinstance(param, str) and param == "max_tokens":
        return True
    text = str(exc).lower()
    return "max_tokens" in text and "max_completion_tokens" in text


def create_completion(api, **kwargs):
    """Call chat.completions.create, retrying without options a server rejects.

    Every call carries max_tokens. A server that rejects stream_options gets
    the call again without it; one that wants max_completion_tokens gets the
    limit under that name. Each retry removes a key, so at most three calls.
    """
    kwargs.setdefault("max_tokens", config.MAX_TOKENS)
    while True:
        try:
            return api.chat.completions.create(**kwargs)
        except APIConnectionError:
            raise
        except APIStatusError as exc:
            if "stream_options" in kwargs and _rejects_stream_options(exc):
                del kwargs["stream_options"]
                continue
            if "max_tokens" in kwargs and _rejects_max_tokens(exc):
                kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
                continue
            raise


def _without_thinking_blocks(messages: list) -> list:
    if not any(
        isinstance(message, dict) and "thinking_blocks" in message
        for message in messages
    ):
        return messages
    cleaned = []
    for message in messages:
        if isinstance(message, dict) and "thinking_blocks" in message:
            cleaned.append(
                {
                    key: value
                    for key, value in message.items()
                    if key != "thinking_blocks"
                }
            )
        else:
            cleaned.append(message)
    return cleaned


def _request_completion(api, **kwargs):
    messages = kwargs.get("messages")
    if isinstance(messages, list):
        kwargs["messages"] = _without_thinking_blocks(messages)
    return create_completion(
        api, stream=True, stream_options={"include_usage": True}, **kwargs
    )


def _debug_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False)


def _debug_args(arguments: object) -> str:
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return arguments
        if isinstance(parsed, dict):
            return json.dumps(parsed, ensure_ascii=False)
        return arguments
    if isinstance(arguments, dict):
        return json.dumps(arguments, ensure_ascii=False)
    return _debug_text(arguments)


def _debug_message(message: dict) -> str:
    role = message.get("role")
    if not isinstance(role, str) or not role:
        role = "message"
    content = _debug_text(message.get("content"))
    if role == "tool":
        call_id = message.get("tool_call_id")
        label = f"tool {call_id}" if isinstance(call_id, str) and call_id else "tool"
        line = f"{label}: {content}".rstrip()
        return line
    lines = [f"{role}: {content}".rstrip()]
    if role == "assistant":
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            if not isinstance(name, str):
                name = ""
            lines.append(f"  {name}({_debug_args(function.get('arguments'))})")
    return "\n".join(lines)


def _format_debug_request(messages: list) -> str:
    blocks = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") == "system":
            continue
        blocks.append(_debug_message(message))
    return "\n\n".join(blocks)


def _emit_debug(session: Session) -> None:
    if not session.debug or session.on_debug is None:
        return
    messages = session.messages
    system = ""
    rest = messages
    if (
        messages
        and isinstance(messages[0], dict)
        and messages[0].get("role") == "system"
    ):
        content = messages[0].get("content")
        system = content if isinstance(content, str) else _debug_text(content)
        rest = messages[1:]
    shown = system if system != session._debug_system else None
    if shown is not None:
        session._debug_system = system
    session.on_debug(shown, _format_debug_request(rest))


def _completion(session: Session, api):
    if session.abort.is_set():
        return None
    if isinstance(api, AnthropicClient):
        message, usage = api.stream_turn(
            session,
            model=session.model,
            messages=session.messages,
            tools=session.skills.schemas(),
        )
        if message is None:
            return None
        return message, usage
    response = _request_completion(
        api,
        model=session.model,
        messages=session.messages,
        tools=session.skills.schemas(),
    )
    return _consume_stream(session, response)


def _complete_message(response):
    choices = getattr(response, "choices", None)
    if not choices:
        return None
    return getattr(choices[0], "message", None)


def _emit_complete(session: Session, message) -> None:
    _notify_delta(session, "reasoning", _reasoning_text(message))
    content = getattr(message, "content", None)
    if isinstance(content, str):
        _notify_delta(session, "content", content)


def _delta_reasoning(delta) -> str:
    for name in ("reasoning", "reasoning_content"):
        value = getattr(delta, name, None)
        if isinstance(value, str) and value:
            return value
    return ""


class _Draft:
    def __init__(self) -> None:
        self.content = ""
        self.reasoning = ""
        self.calls: dict[int, dict[str, str]] = {}
        self.usage = None


def _absorb_chunk(session: Session, draft: _Draft, chunk) -> None:
    usage = getattr(chunk, "usage", None)
    if usage is not None:
        draft.usage = usage
    choices = getattr(chunk, "choices", None) or []
    if not choices:
        return
    delta = getattr(choices[0], "delta", None)
    if delta is None:
        return
    reasoning = _delta_reasoning(delta)
    if reasoning:
        draft.reasoning += reasoning
        _notify_delta(session, "reasoning", reasoning)
    content = getattr(delta, "content", None)
    if isinstance(content, str) and content:
        draft.content += content
        _notify_delta(session, "content", content)
    for call in getattr(delta, "tool_calls", None) or []:
        index = getattr(call, "index", None)
        if not isinstance(index, int) or isinstance(index, bool):
            index = 0
        slot = draft.calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
        call_id = getattr(call, "id", None)
        if isinstance(call_id, str) and call_id:
            slot["id"] += call_id
        function = getattr(call, "function", None)
        if function is None:
            continue
        name = getattr(function, "name", None)
        if isinstance(name, str) and name:
            slot["name"] += name
        arguments = getattr(function, "arguments", None)
        if isinstance(arguments, str) and arguments:
            slot["arguments"] += arguments


def _draft_message(draft: _Draft):
    calls = []
    for index in sorted(draft.calls):
        slot = draft.calls[index]
        if not slot["id"] and not slot["name"] and not slot["arguments"]:
            continue
        calls.append(
            SimpleNamespace(
                id=slot["id"],
                type="function",
                function=SimpleNamespace(
                    name=slot["name"], arguments=slot["arguments"]
                ),
            )
        )
    return SimpleNamespace(
        content=draft.content,
        reasoning=draft.reasoning,
        tool_calls=calls or None,
    )


def _consume_stream(session: Session, response):
    message = _complete_message(response)
    if message is not None:
        _emit_complete(session, message)
        return message, response
    draft = _Draft()

    stopped = False

    def absorb(stream) -> None:
        nonlocal stopped
        for chunk in stream:
            if session.abort.is_set():
                stopped = True
                break
            _absorb_chunk(session, draft, chunk)

    if hasattr(response, "__enter__") and hasattr(response, "__exit__"):
        with response as stream:
            absorb(stream)
    else:
        absorb(response)
    if stopped or session.abort.is_set():
        return None
    return _draft_message(draft), SimpleNamespace(usage=draft.usage)
