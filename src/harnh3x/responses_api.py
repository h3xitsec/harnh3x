"""Translate the stored transcript to the OpenAI Responses API.

Chat completions stays the first request. A model moves here only after that
call asks for `/v1/responses`. The loop still sees one assistant message with
`content`, `reasoning`, and `tool_calls`.
"""

import json
from types import SimpleNamespace

from harnh3x import config

_REPLAY_TYPES = {"message", "reasoning", "function_call"}
_ITEM_FIELDS = (
    "id",
    "role",
    "content",
    "phase",
    "status",
    "call_id",
    "name",
    "arguments",
    "summary",
    "encrypted_content",
)
_RESPONSES = "_harnh3x_responses_models"
_REASONING_NONE = "_harnh3x_reasoning_none_models"


def uses(api, model: object) -> bool:
    return _has(api, model, _RESPONSES)


def uses_reasoning_none(api, model: object) -> bool:
    return _has(api, model, _REASONING_NONE)


def remember(api, model: object) -> None:
    _add(api, model, _RESPONSES)
    _discard(api, model, _REASONING_NONE)


def remember_reasoning_none(api, model: object) -> None:
    _add(api, model, _REASONING_NONE)
    _discard(api, model, _RESPONSES)


def asks_for_responses(exc: object) -> bool:
    text = str(exc).lower()
    if "/v1/responses" in text:
        return True
    return "function tools" in text and "reasoning_effort" in text


def route_missing(exc: object) -> bool:
    if getattr(exc, "status_code", None) == 404:
        return True
    text = str(exc).lower()
    if "unknown route" in text or "unknown url" in text:
        return True
    if "no such endpoint" in text or "unsupported endpoint" in text:
        return True
    return "not found" in text and (
        "route" in text or "endpoint" in text or "responses" in text
    )


def stream_turn(api, session):
    """Stream one Responses turn into the assistant message the loop stores."""
    if session.abort.is_set():
        return None
    response = api.responses.create(
        **_payload(
            session.model,
            session.messages,
            session.skills.schemas(),
            stream=True,
        )
    )
    return _consume(session, response)


def complete_text(api, *, model: str, messages: list):
    """One non-streaming Responses call, wrapped for the summary reader."""
    response = api.responses.create(**_payload(model, messages, None, stream=False))
    wrapped = _usage_view(getattr(response, "usage", None))
    wrapped.choices = [
        SimpleNamespace(message=SimpleNamespace(content=_response_text(response)))
    ]
    return wrapped


def _has(api, model: object, name: str) -> bool:
    if not isinstance(model, str) or not model:
        return False
    models = getattr(api, name, None)
    return isinstance(models, set) and model in models


def _add(api, model: object, name: str) -> None:
    if not isinstance(model, str) or not model:
        return
    models = getattr(api, name, None)
    if not isinstance(models, set):
        models = set()
        setattr(api, name, models)
    models.add(model)


def _discard(api, model: object, name: str) -> None:
    if not isinstance(model, str):
        return
    models = getattr(api, name, None)
    if isinstance(models, set):
        models.discard(model)


def _payload(model: str, messages: list, tools, *, stream: bool) -> dict:
    payload = {
        "model": model,
        "input": _to_input(messages),
        "max_output_tokens": config.MAX_TOKENS,
        "store": False,
        "include": ["reasoning.encrypted_content"],
    }
    converted = _to_tools(tools)
    if converted:
        payload["tools"] = converted
    if stream:
        payload["stream"] = True
    return payload


def _to_input(messages: list) -> list:
    items = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role in {"system", "user", "developer"}:
            text = message.get("content")
            if isinstance(text, str) and text:
                items.append({"role": role, "content": text})
            continue
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": _string(message.get("tool_call_id")),
                    "output": _string(message.get("content")),
                }
            )
            continue
        if role == "assistant":
            items.extend(_assistant_items(message))
    return items


def _assistant_items(message: dict) -> list:
    stored = message.get("response_items")
    if isinstance(stored, list):
        replayed = [
            dict(item)
            for item in stored
            if isinstance(item, dict) and item.get("type") in _REPLAY_TYPES
        ]
        if replayed:
            return replayed
    items = []
    content = message.get("content")
    if isinstance(content, str) and content:
        items.append({"role": "assistant", "content": content})
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        call_id = call.get("id")
        items.append(
            {
                "type": "function_call",
                "call_id": call_id if isinstance(call_id, str) else "",
                "name": name,
                "arguments": _arguments(function.get("arguments")),
            }
        )
    return items


def _to_tools(tools) -> list:
    if not tools:
        return []
    converted = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        item = {"type": "function", "name": name}
        description = function.get("description")
        if isinstance(description, str) and description:
            item["description"] = description
        parameters = function.get("parameters")
        if isinstance(parameters, dict):
            item["parameters"] = parameters
        converted.append(item)
    return converted


class _Draft:
    def __init__(self) -> None:
        self.content = ""
        self.reasoning = ""
        self.summary = ""
        self.saw_reasoning_text = False
        self.calls: dict[int, dict[str, str]] = {}
        self.done_items: dict[int, dict] = {}
        self.usage = None


def _consume(session, response):
    output = getattr(response, "output", None)
    if isinstance(output, list):
        if session.abort.is_set():
            return None
        return _from_output(session, output, getattr(response, "usage", None))
    draft = _Draft()
    stopped = False

    def absorb(stream) -> None:
        nonlocal stopped
        for event in stream:
            if session.abort.is_set():
                stopped = True
                break
            _absorb_event(session, draft, event)

    if hasattr(response, "__enter__") and hasattr(response, "__exit__"):
        with response as stream:
            absorb(stream)
    else:
        absorb(response)
    if stopped or session.abort.is_set():
        return None
    _finish_summary(session, draft)
    return _draft_message(draft), _usage_view(draft.usage)


def _from_output(session, output: list, usage):
    draft = _Draft()
    for index, item in enumerate(output):
        data = _item_dict(item)
        if data is not None:
            draft.done_items[index] = data
        kind = _field(item, "type")
        if kind == "reasoning":
            text = _item_reasoning(item)
            if text:
                draft.saw_reasoning_text = True
                draft.reasoning += text
            continue
        if kind == "message":
            draft.content += _output_text(item)
            continue
        if kind == "function_call":
            _store_call(draft, index, item, replace_arguments=True)
    _notify(session, "reasoning", draft.reasoning)
    _notify(session, "content", draft.content)
    return _draft_message(draft), _usage_view(usage)


def _absorb_event(session, draft: _Draft, event) -> None:
    kind = _string(_field(event, "type"))
    if kind == "response.output_text.delta":
        piece = _string(_field(event, "delta"))
        if piece:
            draft.content += piece
            _notify(session, "content", piece)
        return
    if kind == "response.reasoning_text.delta":
        piece = _string(_field(event, "delta"))
        if piece:
            draft.saw_reasoning_text = True
            draft.reasoning += piece
            _notify(session, "reasoning", piece)
        return
    if kind == "response.reasoning_summary_text.delta":
        piece = _string(_field(event, "delta"))
        if piece and not draft.saw_reasoning_text:
            draft.summary += piece
        return
    if kind == "response.output_item.added":
        item = _field(event, "item")
        if _field(item, "type") == "function_call":
            _store_call(draft, _index(event), item, replace_arguments=False)
        return
    if kind == "response.function_call_arguments.delta":
        piece = _string(_field(event, "delta"))
        if piece:
            slot = draft.calls.setdefault(
                _index(event), {"id": "", "name": "", "arguments": ""}
            )
            slot["arguments"] += piece
        return
    if kind == "response.output_item.done":
        _absorb_done(session, draft, event)
        return
    if kind == "response.completed":
        response = _field(event, "response")
        usage = _field(response, "usage") if response is not None else None
        if usage is None:
            usage = _field(event, "usage")
        if usage is not None:
            draft.usage = usage


def _absorb_done(session, draft: _Draft, event) -> None:
    item = _field(event, "item")
    index = _index(event)
    data = _item_dict(item)
    if data is not None:
        draft.done_items[index] = data
    kind = _field(item, "type")
    if kind == "function_call":
        _store_call(draft, index, item, replace_arguments=True)
        return
    if kind == "message" and not draft.content:
        text = _output_text(item)
        if text:
            draft.content = text
            _notify(session, "content", text)
        return
    if kind == "reasoning" and not draft.saw_reasoning_text and not draft.summary:
        draft.summary = _item_reasoning(item)


def _finish_summary(session, draft: _Draft) -> None:
    if draft.saw_reasoning_text or not draft.summary:
        return
    draft.reasoning = draft.summary
    _notify(session, "reasoning", draft.summary)


def _store_call(draft: _Draft, index: int, item, *, replace_arguments: bool) -> None:
    slot = draft.calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
    call_id = _field(item, "call_id")
    if isinstance(call_id, str) and call_id:
        slot["id"] = call_id
    name = _field(item, "name")
    if isinstance(name, str) and name:
        slot["name"] = name
    arguments = _field(item, "arguments")
    text = _arguments(arguments) if arguments is not None else ""
    if text and (replace_arguments or not slot["arguments"]):
        slot["arguments"] = text


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
    items = _replay_items(draft)
    return SimpleNamespace(
        content=draft.content,
        reasoning=draft.reasoning,
        tool_calls=calls or None,
        response_items=items or None,
    )


def _replay_items(draft: _Draft) -> list:
    items = [draft.done_items[index] for index in sorted(draft.done_items)]
    seen = {
        item.get("call_id")
        for item in items
        if item.get("type") == "function_call" and isinstance(item.get("call_id"), str)
    }
    for index in sorted(draft.calls):
        slot = draft.calls[index]
        if slot["id"] in seen or (not slot["id"] and not slot["name"]):
            continue
        items.append(
            {
                "type": "function_call",
                "call_id": slot["id"],
                "name": slot["name"],
                "arguments": slot["arguments"],
            }
        )
    return items


def _usage_view(usage):
    details = getattr(usage, "input_tokens_details", None)
    cached = _count(getattr(details, "cached_tokens", None))
    written = _count(getattr(details, "cache_write_tokens", None))
    prompt_details = None
    if cached is not None:
        prompt_details = SimpleNamespace(cached_tokens=cached)
    return SimpleNamespace(
        usage=SimpleNamespace(
            total_tokens=_count(getattr(usage, "total_tokens", None)),
            prompt_tokens=_count(getattr(usage, "input_tokens", None)),
            cache_read_tokens=cached,
            cache_write_tokens=written,
            prompt_tokens_details=prompt_details,
        )
    )


def _response_text(response) -> str:
    output = getattr(response, "output", None)
    if isinstance(output, list):
        parts = [
            _output_text(item) for item in output if _field(item, "type") == "message"
        ]
        if any(parts):
            return "".join(parts).strip()
    text = getattr(response, "output_text", None)
    if isinstance(text, str):
        return text.strip()
    return ""


def _output_text(item) -> str:
    return _parts_text(_field(item, "content"))


def _item_reasoning(item) -> str:
    text = _parts_text(_field(item, "content"))
    if text:
        return text
    return _parts_text(_field(item, "summary"))


def _parts_text(value) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    return "".join(_part_text(part) for part in value)


def _part_text(part) -> str:
    text = _field(part, "text")
    return text if isinstance(text, str) else ""


def _item_dict(item) -> dict | None:
    if isinstance(item, dict):
        data = {
            key: _jsonable(value) for key, value in item.items() if value is not None
        }
    elif hasattr(item, "model_dump"):
        dumped = item.model_dump(exclude_none=True)
        data = _jsonable(dumped) if isinstance(dumped, dict) else None
    else:
        data = _namespace_dict(item)
    if not isinstance(data, dict) or data.get("type") not in _REPLAY_TYPES:
        return None
    return data


def _namespace_dict(item) -> dict | None:
    kind = getattr(item, "type", None)
    if not isinstance(kind, str):
        return None
    data = {"type": kind}
    for name in _ITEM_FIELDS:
        if not hasattr(item, name):
            continue
        value = getattr(item, name)
        if value is not None:
            data[name] = _jsonable(value)
    return data


def _jsonable(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(exclude_none=True)
        return _jsonable(dumped)
    if hasattr(value, "__dict__"):
        return {
            key: _jsonable(item)
            for key, item in vars(value).items()
            if not key.startswith("_") and item is not None
        }
    return str(value)


def _notify(session, kind: str, text: str) -> None:
    if not isinstance(text, str) or text == "":
        return
    callback = getattr(session, "on_delta", None)
    if callback is None:
        return
    callback(kind, text)
    session._delta_open_line = not text.endswith("\n")


def _index(event) -> int:
    index = _field(event, "output_index")
    if isinstance(index, int) and not isinstance(index, bool):
        return index
    return 0


def _field(obj, name: str):
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _arguments(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return json.dumps(value)
    return ""


def _count(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None
