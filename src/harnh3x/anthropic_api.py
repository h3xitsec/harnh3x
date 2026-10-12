import json
from types import SimpleNamespace

from anthropic import APIConnectionError, APIStatusError, APITimeoutError, Anthropic

import harnh3x.config as config

_THINKING = {"thinking", "redacted_thinking"}


class CompletionError(Exception):
    pass


class AnthropicClient:
    def __init__(self, sdk):
        self.sdk = sdk

    def stream_turn(self, session, *, model: str, messages: list, tools: list):
        payload = _request(model, messages, tools)

        def run():
            return _assemble(_iter_events(self.sdk.messages.stream(**payload)), session)

        message, usage = _call(run)
        return message, SimpleNamespace(usage=usage)

    def complete_text(
        self, *, model: str, messages: list
    ) -> tuple[str, SimpleNamespace]:
        payload = _request(model, messages, None)

        def run():
            return self.sdk.messages.create(**payload)

        response = _call(run)
        return _message_text(response), SimpleNamespace(
            usage=_usage_namespace(response)
        )

    def list_model_ids(self) -> list[str]:
        try:
            page = self.sdk.models.list()
        except APITimeoutError:
            raise SystemExit("The API request timed out.") from None
        except APIConnectionError:
            raise SystemExit("Could not reach the API.") from None
        except APIStatusError as exc:
            raise SystemExit(status_message(exc)) from None
        items = getattr(page, "data", page)
        names = set()
        for model in items or []:
            model_id = getattr(model, "id", None)
            if isinstance(model_id, str) and model_id:
                names.add(model_id)
        return sorted(names)


def to_anthropic(messages: list, tools: list | None = None) -> dict:
    system_parts: list[str] = []
    pending: list[dict] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "system":
            text = message.get("content")
            if isinstance(text, str) and text:
                system_parts.append(text)
            continue
        if role == "tool":
            _append(
                pending,
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": _string(message.get("tool_call_id")),
                        "content": _string(message.get("content")),
                    }
                ],
            )
            continue
        if role == "user":
            text = message.get("content")
            if isinstance(text, str) and text:
                _append(pending, "user", [{"type": "text", "text": text}])
            continue
        if role == "assistant":
            blocks = _assistant_blocks(message)
            if blocks:
                _append(pending, "assistant", blocks)
    payload: dict = {"messages": [_flatten(item) for item in pending]}
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)
    converted = _convert_tools(tools)
    if converted:
        payload["tools"] = converted
    return payload


def _request(model: str, messages: list, tools: list | None) -> dict:
    payload = to_anthropic(messages, tools)
    payload["model"] = model
    payload["max_tokens"] = config.MAX_TOKENS
    return payload


def _append(pending: list[dict], role: str, blocks: list[dict]) -> None:
    if pending and pending[-1]["role"] == role:
        pending[-1]["content"].extend(blocks)
        return
    pending.append({"role": role, "content": list(blocks)})


def _flatten(item: dict) -> dict:
    content = item["content"]
    if len(content) == 1 and content[0].get("type") == "text":
        return {"role": item["role"], "content": content[0]["text"]}
    return {"role": item["role"], "content": content}


def _assistant_blocks(message: dict) -> list[dict]:
    blocks: list[dict] = []
    for block in message.get("thinking_blocks") or []:
        if isinstance(block, dict) and block.get("type") in _THINKING:
            blocks.append(dict(block))
    content = message.get("content")
    if isinstance(content, str) and content:
        blocks.append({"type": "text", "text": content})
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            function = {}
        blocks.append(
            {
                "type": "tool_use",
                "id": _string(call.get("id")),
                "name": _string(function.get("name")),
                "input": _object_input(function.get("arguments")),
            }
        )
    return blocks


def _object_input(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    return {}


def _convert_tools(tools: list | None) -> list[dict]:
    converted = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        schema = function.get("parameters")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        item = {"name": name, "input_schema": schema}
        description = function.get("description")
        if isinstance(description, str) and description:
            item["description"] = description
        converted.append(item)
    return converted


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def status_message(exc: APIStatusError) -> str:
    text = _error_text(getattr(exc, "body", None))
    if not text:
        text = _plain_error(str(exc))
    if not text:
        text = "the API rejected the request"
    return f"The API rejected the request: {text}"


def _error_text(body: object) -> str:
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            return " ".join(message.split())
    message = body.get("message")
    if isinstance(message, str) and message.strip():
        return " ".join(message.split())
    return ""


def _plain_error(raw: str) -> str:
    text = raw.strip()
    if not text or text.lower().startswith("error code:"):
        return ""
    return " ".join(text.split())


def _call(fn):
    try:
        return fn()
    except APITimeoutError:
        raise CompletionError("The API request timed out.") from None
    except APIConnectionError:
        raise CompletionError("Could not reach the API.") from None
    except APIStatusError as exc:
        raise CompletionError(status_message(exc)) from None


def _iter_events(response):
    if hasattr(response, "__enter__") and hasattr(response, "__exit__"):
        with response as stream:
            yield from stream
        return
    yield from response


class _Slot:
    def __init__(self, kind: str):
        self.kind = kind
        self.text = ""
        self.signature = ""
        self.partial_json = ""
        self.call_id = ""
        self.name = ""
        self.initial_input = None
        self.data = None
        self.saw_json = False


def _assemble(events, session) -> tuple[SimpleNamespace, SimpleNamespace | None]:
    slots: dict[int, _Slot] = {}
    input_tokens = None
    output_tokens = None
    for event in events:
        kind = _field(event, "type")
        if kind == "message_start":
            usage = _field(_field(event, "message"), "usage")
            incoming = _count(_field(usage, "input_tokens"))
            if incoming is not None:
                input_tokens = incoming
            continue
        if kind == "message_delta":
            usage = _field(event, "usage")
            incoming = _count(_field(usage, "input_tokens"))
            outgoing = _count(_field(usage, "output_tokens"))
            if incoming is not None:
                input_tokens = incoming
            if outgoing is not None:
                output_tokens = outgoing
            continue
        if kind == "content_block_start":
            index = _index(event)
            slots[index] = _start_slot(_field(event, "content_block"), session)
            continue
        if kind == "content_block_delta":
            index = _index(event)
            slot = slots.setdefault(index, _Slot("text"))
            _apply_delta(slot, _field(event, "delta"), session)
    return _draft(slots), _usage_total(input_tokens, output_tokens)


def _start_slot(block, session) -> _Slot:
    kind = _field(block, "type")
    if kind == "thinking":
        slot = _Slot("thinking")
        slot.text = _string(_field(block, "thinking"))
        slot.signature = _string(_field(block, "signature"))
        _notify(session, "reasoning", slot.text)
        return slot
    if kind == "redacted_thinking":
        slot = _Slot("redacted_thinking")
        slot.data = _field(block, "data")
        return slot
    if kind == "tool_use":
        slot = _Slot("tool_use")
        slot.call_id = _string(_field(block, "id"))
        slot.name = _string(_field(block, "name"))
        slot.initial_input = _field(block, "input")
        return slot
    slot = _Slot("text")
    slot.text = _string(_field(block, "text"))
    _notify(session, "content", slot.text)
    return slot


def _apply_delta(slot: _Slot, delta, session) -> None:
    kind = _field(delta, "type")
    if kind == "thinking_delta":
        slot.kind = "thinking"
        piece = _string(_field(delta, "thinking"))
        slot.text += piece
        _notify(session, "reasoning", piece)
        return
    if kind == "signature_delta":
        slot.kind = "thinking"
        slot.signature += _string(_field(delta, "signature"))
        return
    if kind == "text_delta":
        slot.kind = "text"
        piece = _string(_field(delta, "text"))
        slot.text += piece
        _notify(session, "content", piece)
        return
    if kind == "input_json_delta":
        slot.kind = "tool_use"
        slot.saw_json = True
        slot.partial_json += _string(_field(delta, "partial_json"))


def _draft(slots: dict[int, _Slot]) -> SimpleNamespace:
    thinking = []
    content = []
    calls = []
    for index in sorted(slots):
        slot = slots[index]
        if slot.kind == "thinking" and (slot.text or slot.signature):
            thinking.append(
                {
                    "type": "thinking",
                    "thinking": slot.text,
                    "signature": slot.signature,
                }
            )
            continue
        if slot.kind == "redacted_thinking":
            block = {"type": "redacted_thinking"}
            if slot.data is not None:
                block["data"] = slot.data
            thinking.append(block)
            continue
        if slot.kind == "text":
            content.append(slot.text)
            continue
        if slot.kind == "tool_use":
            arguments = slot.partial_json
            if not slot.saw_json:
                arguments = _seed_arguments(slot.initial_input)
            calls.append(
                SimpleNamespace(
                    id=slot.call_id,
                    type="function",
                    function=SimpleNamespace(name=slot.name, arguments=arguments),
                )
            )
    return SimpleNamespace(
        content="".join(content),
        reasoning="".join(
            block["thinking"] for block in thinking if "thinking" in block
        ),
        thinking_blocks=thinking or None,
        tool_calls=calls or None,
    )


def _seed_arguments(initial: object) -> str:
    if isinstance(initial, dict) and initial:
        return json.dumps(initial)
    if isinstance(initial, str):
        return initial
    return ""


def _message_text(response) -> str:
    parts = []
    for block in _field(response, "content") or []:
        if _field(block, "type") != "text":
            continue
        text = _field(block, "text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts)


def _usage_namespace(response) -> SimpleNamespace | None:
    usage = _field(response, "usage")
    return _usage_total(
        _count(_field(usage, "input_tokens")),
        _count(_field(usage, "output_tokens")),
    )


def _usage_total(incoming: int | None, outgoing: int | None) -> SimpleNamespace | None:
    if incoming is None and outgoing is None:
        return None
    return SimpleNamespace(total_tokens=(incoming or 0) + (outgoing or 0))


def _notify(session, kind: str, text: str) -> None:
    if not isinstance(text, str) or text == "":
        return
    callback = getattr(session, "on_delta", None)
    if callback is None:
        return
    callback(kind, text)
    session._delta_open_line = not text.endswith("\n")


def _index(event) -> int:
    index = _field(event, "index")
    if isinstance(index, int) and not isinstance(index, bool):
        return index
    return 0


def _field(obj, name: str):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _count(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None
