def _reasoning_text(msg) -> str:
    for name in ("reasoning", "reasoning_content"):
        value = getattr(msg, name, None)
        if isinstance(value, str) and value:
            return value
    return ""


def assistant_message(msg) -> dict:
    stored = {"role": "assistant", "content": msg.content}
    reasoning = _reasoning_text(msg)
    if reasoning:
        stored["reasoning"] = reasoning
    if msg.tool_calls:
        stored["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in msg.tool_calls
        ]
    blocks = getattr(msg, "thinking_blocks", None)
    if isinstance(blocks, list):
        copied = [
            dict(block)
            for block in blocks
            if isinstance(block, dict) and block.get("type")
        ]
        if copied:
            stored["thinking_blocks"] = copied
    return stored


def _visible_answer(stored: dict) -> str:
    content = stored.get("content") or ""
    if content:
        return content
    reasoning = stored.get("reasoning") or ""
    if reasoning:
        return reasoning
    return "The model returned an empty answer."


def _log_preview(value):
    if isinstance(value, str) and len(value) > 500:
        return value[:500] + "...[truncated]"
    if isinstance(value, dict):
        return {key: _log_preview(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_log_preview(item) for item in value]
    return value
