import json
import os
import re
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

import harnh3x.anthropic_api as anthropic_api
import harnh3x.config as config
from harnh3x.anthropic_api import AnthropicClient, CompletionError, status_message
from harnh3x.config import (
    COMPACT_AT,
    MAX_TURNS,
    MODEL,
    TRANSCRIPT_FILE,
)
from harnh3x.prompt import system_prompt
from harnh3x.rules import append_matching_rules, format_rules, load_project_rules
from harnh3x.skills import SkillRegistry
from harnh3x.tools import _tool_result

_client = None


def get_client() -> OpenAI | AnthropicClient:
    global _client
    if _client is None:
        kwargs = {}
        if config.API_KEY:
            kwargs["api_key"] = config.API_KEY
        if config.API_URL:
            kwargs["base_url"] = config.API_URL
        if config.PROVIDER == "anthropic":
            _client = AnthropicClient(anthropic_api.Anthropic(**kwargs))
        else:
            _client = OpenAI(**kwargs)
    return _client


def _api_failure(exc: APIConnectionError) -> str:
    if isinstance(exc, APITimeoutError):
        return "The API request timed out."
    return "Could not reach the API."


def list_models(client: OpenAI | AnthropicClient | None = None) -> list[str]:
    api = client or get_client()
    if isinstance(api, AnthropicClient):
        return api.list_model_ids()
    try:
        names = {model.id for model in api.models.list() if getattr(model, "id", None)}
    except APIConnectionError as exc:
        raise SystemExit(_api_failure(exc)) from None
    except APIStatusError as exc:
        raise SystemExit(status_message(exc)) from None
    return sorted(names)


_SHOW_TIMEOUT = 3
_NUM_CTX = re.compile(r"(?m)^num_ctx\s+(\d+)\s*$")


def _show_url(client) -> str | None:
    base = getattr(client, "base_url", None)
    if base is None:
        return None
    text = str(base).strip().rstrip("/")
    if not text:
        return None
    if text.endswith("/v1"):
        text = text[: -len("/v1")]
    return f"{text}/api/show"


def _context_limit_from_show(payload: object) -> int | None:
    if not isinstance(payload, dict):
        return None
    parameters = payload.get("parameters")
    if isinstance(parameters, str):
        match = _NUM_CTX.search(parameters)
        if match:
            return int(match.group(1))
    info = payload.get("model_info")
    if not isinstance(info, dict):
        return None
    lengths = [
        value
        for key, value in info.items()
        if isinstance(key, str)
        and key.endswith(".context_length")
        and isinstance(value, int)
        and not isinstance(value, bool)
    ]
    if not lengths:
        return None
    return max(lengths)


def model_context_limit(
    model: str, client: OpenAI | AnthropicClient | None = None
) -> int | None:
    api = client or get_client()
    if isinstance(api, AnthropicClient):
        configured = _count(config.CONTEXT_LIMIT)
        if configured is not None:
            return configured
        return config.ANTHROPIC_CONTEXT_LIMIT
    url = _show_url(api)
    if url is None:
        return None
    body = json.dumps({"model": model}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_SHOW_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, urllib.error.URLError):
        return None
    return _context_limit_from_show(payload)


def _count(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _stored_context_tokens(data: dict) -> int | None:
    return _count(data.get("context_tokens"))


def _remember_context(session, response, client) -> None:
    usage = getattr(response, "usage", None)
    total = _count(getattr(usage, "total_tokens", None))
    if total is not None:
        session.context_tokens = total
    if session.context_limit is None:
        limit = model_context_limit(session.model, client)
        if limit is not None:
            session.context_limit = limit


def _expand(value: str | Path) -> Path:
    return Path(value).expanduser()


def _workspaces_dir() -> Path:
    return _expand(config.BASE_DIR).resolve() / "workspaces"


def _session_workspace(workspace: Path | str | None) -> Path:
    if workspace is not None:
        raw = _expand(workspace)
    else:
        env = os.environ.get("WORKSPACE")
        raw = _expand(env) if env else _workspaces_dir() / str(uuid.uuid4())
    path = raw.resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _bind_project(raw: Path | str, workspace: Path) -> Path:
    path = _expand(raw).resolve()
    if path == Path("/") or any(char in str(path) for char in '()"\\\n\x00'):
        raise ValueError("project path is not allowed")
    if not path.is_dir():
        raise ValueError(f"project must be an existing directory: {path}")
    root = workspace.resolve()
    if path.is_relative_to(root) or root.is_relative_to(path):
        raise ValueError("project must not overlap the session workspace")
    return path


def _stored_project(data: dict, workspace: Path) -> Path | None:
    if "project" not in data or data["project"] is None:
        return None
    raw = data["project"]
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Cannot resume: invalid project")
    try:
        return _bind_project(raw, workspace)
    except ValueError as exc:
        raise ValueError(f"Cannot resume: {exc}") from exc


def _stored_coding(data: dict, project: Path | None) -> bool:
    if "coding" not in data:
        return False
    raw = data["coding"]
    if raw is False:
        return False
    if raw is True:
        if project is None:
            raise ValueError("Cannot resume: coding session has no project")
        return True
    raise ValueError("Cannot resume: invalid coding")


class Session:
    def __init__(
        self,
        workspace: Path | None = None,
        model: str | None = None,
        *,
        yolo: bool = False,
        project: Path | str | None = None,
        coding: bool = False,
    ):
        self.workspace = _session_workspace(workspace)
        self.model = model or MODEL
        self.yolo = yolo
        self.project = (
            None if project is None else _bind_project(project, self.workspace)
        )
        if coding and self.project is None:
            raise ValueError("--coding requires --project")
        self.coding = coding
        self.skills = SkillRegistry(yolo=yolo, project=self.project)
        self.applied_rules: set[str] = set()
        self.context_tokens: int | None = None
        self.context_limit: int | None = None
        self.on_compact: Callable[[str], None] | None = None
        self.messages: list[dict] = [self._system_message()]

    def _system_message(self) -> dict:
        rules = ""
        if self.coding:
            rules = format_rules(load_project_rules(self.project))
        return {
            "role": "system",
            "content": system_prompt(
                self.workspace,
                self.skills.get_menu(),
                yolo=self.yolo,
                project=self.project,
                rules=rules,
                coding=self.coding,
            ),
        }

    def reset(self) -> None:
        self.workspace = _session_workspace(_workspaces_dir() / str(uuid.uuid4()))
        if self.project is not None:
            self.project = _bind_project(self.project, self.workspace)
        self.skills = SkillRegistry(yolo=self.yolo, project=self.project)
        self.applied_rules = set()
        self.context_tokens = None
        self.messages = [self._system_message()]

    def sync_prompt(self) -> None:
        self.messages[0]["content"] = self._system_message()["content"]

    def resume(self, workspace: Path) -> None:
        path = _expand(workspace).resolve()
        data = _read_transcript(path / TRANSCRIPT_FILE)
        self.workspace = path
        self.project = _stored_project(data, path)
        self.coding = _stored_coding(data, self.project)
        self.model = data["model"]
        self.messages = data["messages"]
        self.context_tokens = _stored_context_tokens(data)
        self.context_limit = None
        self.skills = SkillRegistry(yolo=self.yolo, project=self.project)
        self.applied_rules = set()
        _replay_skills(self)
        _load_saved_skills(self, data)
        self.sync_prompt()


_PREVIEW_LIMIT = 60


@dataclass(frozen=True)
class SessionInfo:
    workspace: Path
    model: str
    updated: float
    preview: str


def parse_command(text: str) -> tuple[str, str] | None:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    body = stripped[1:].strip()
    if not body:
        return ("", "")
    name, _, rest = body.partition(" ")
    return name.lower(), rest.strip()


def session_label(info: SessionInfo, index: int) -> str:
    moment = datetime.fromtimestamp(info.updated)
    when = moment.strftime("%b %d %H:%M")
    short = info.workspace.name[:8]
    preview = info.preview if info.preview else "(empty)"
    return f"{index + 1}. {short}  {when}  {info.model}  {preview}"


def list_sessions(root: Path | str | None = None) -> list[SessionInfo]:
    base = _expand(root).resolve() if root is not None else _workspaces_dir()
    if not base.is_dir():
        return []
    found: list[SessionInfo] = []
    for child in base.iterdir():
        if not child.is_dir():
            continue
        info = _session_info(child)
        if info is not None:
            found.append(info)
    found.sort(key=lambda item: item.updated, reverse=True)
    return found


def _preview(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= _PREVIEW_LIMIT:
        return collapsed
    return collapsed[: _PREVIEW_LIMIT - 3] + "..."


def _first_preview(messages: list) -> str | None:
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            text = content if isinstance(content, str) else ""
            return _preview(text)
    return None


def _read_transcript(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Cannot read {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Cannot read {path}: expected an object")
    model = data.get("model")
    messages = data.get("messages")
    if not isinstance(model, str) or not model:
        raise ValueError(f"Cannot read {path}: missing model")
    if not isinstance(messages, list) or not messages:
        raise ValueError(f"Cannot read {path}: missing messages")
    first = messages[0]
    if not isinstance(first, dict) or first.get("role") != "system":
        raise ValueError(f"Cannot read {path}: missing system message")
    for message in messages:
        if not isinstance(message, dict) or "role" not in message:
            raise ValueError(f"Cannot read {path}: invalid message")
    return data


def _session_info(workspace: Path) -> SessionInfo | None:
    path = workspace / TRANSCRIPT_FILE
    if not path.is_file():
        return None
    try:
        data = _read_transcript(path)
    except ValueError:
        return None
    preview = _first_preview(data["messages"])
    if preview is None:
        return None
    return SessionInfo(
        workspace=workspace.resolve(),
        model=data["model"],
        updated=path.stat().st_mtime,
        preview=preview,
    )


def _skill_name(arguments: object) -> str | None:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return None
    if not isinstance(arguments, dict):
        return None
    name = arguments.get("name", "")
    return name if isinstance(name, str) else None


def _replay_skills(session: Session) -> None:
    for message in session.messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            skill = _skill_name(function.get("arguments"))
            if skill is None:
                continue
            if name == "load_skill":
                session.skills.load_skill(skill)
            elif name == "unload_skill":
                session.skills.unload_skill(skill)


def _load_saved_skills(session: Session, data: dict) -> None:
    names = data.get("skills")
    if not isinstance(names, list):
        return
    active = set(session.skills.loaded_names())
    for name in names:
        if isinstance(name, str) and name not in active:
            session.skills.load_skill(name)
            active.add(name)


def _save_transcript(session: Session) -> None:
    path = session.workspace / TRANSCRIPT_FILE
    payload = {"model": session.model, "messages": session.messages}
    if _count(session.context_tokens) is not None:
        payload["context_tokens"] = session.context_tokens
    loaded = session.skills.loaded_names()
    if loaded:
        payload["skills"] = loaded
    if session.project is not None:
        payload["project"] = str(session.project)
    if session.coding:
        payload["coding"] = True
    temporary = path.with_name(f".{TRANSCRIPT_FILE}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def write_log(event: dict, workspace: Path) -> None:
    path = workspace / "session.log"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


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
    try:
        return api.chat.completions.create(
            stream=True,
            stream_options={"include_usage": True},
            **kwargs,
        )
    except APIConnectionError:
        raise
    except APIStatusError as exc:
        if not _rejects_stream_options(exc):
            raise
        return api.chat.completions.create(stream=True, **kwargs)


def _completion(session: Session, api):
    if isinstance(api, AnthropicClient):
        return api.stream_turn(
            session,
            model=session.model,
            messages=session.messages,
            tools=session.skills.schemas(),
        )
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

    def absorb(stream) -> None:
        for chunk in stream:
            _absorb_chunk(session, draft, chunk)

    if hasattr(response, "__enter__") and hasattr(response, "__exit__"):
        with response as stream:
            absorb(stream)
    else:
        absorb(response)
    return _draft_message(draft), SimpleNamespace(usage=draft.usage)


def _compact(session: Session, api, *, force: bool = False) -> bool:
    if session.context_limit is None:
        limit = model_context_limit(session.model, api)
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
            response = _summary_response(session, api, body)
        except Exception:
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
        _remember_context(session, response, api)
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
    api = client or get_client()
    if _compact(session, api, force=True):
        _save_transcript(session)
        return "Context compacted."
    if _split_for_compact(session.messages) is None:
        return "Nothing to compact."
    return "Could not compact the context."


def run(
    user_message: str,
    session: Session,
    client: OpenAI | AnthropicClient | None = None,
    show_tools: bool = True,
    on_tool: Callable[[str, object], None] | None = None,
) -> str:
    session.messages.append({"role": "user", "content": user_message})
    try:
        return _complete(session, client, show_tools, on_tool)
    finally:
        _save_transcript(session)


def _complete(
    session: Session,
    client: OpenAI | AnthropicClient | None,
    show_tools: bool,
    on_tool: Callable[[str, object], None] | None,
) -> str:
    api = client or get_client()

    for turn in range(MAX_TURNS):
        write_log({"event": "turn", "turn": turn}, session.workspace)
        _compact(session, api)
        try:
            msg, usage = _completion(session, api)
        except APIConnectionError as exc:
            return _close_with(session, _api_failure(exc))
        except APIStatusError as exc:
            return _close_with(session, status_message(exc))
        except CompletionError as exc:
            return _close_with(session, str(exc))
        stored = assistant_message(msg)
        session.messages.append(stored)
        _remember_context(session, usage, api)
        write_log(
            {"event": "model", "turn": turn, "message": _log_preview(stored)},
            session.workspace,
        )
        if not msg.tool_calls:
            _compact(session, api)
            return _visible_answer(stored)

        for tc in msg.tool_calls:
            args, result = _tool_result(
                tc.function.name,
                tc.function.arguments,
                session.workspace,
                session.skills,
                yolo=session.yolo,
                project=session.project,
            )
            if session.coding:
                result = append_matching_rules(
                    session.project, args, result, session.applied_rules
                )
            if tc.function.name in {"load_skill", "unload_skill"} and args is not None:
                session.sync_prompt()
            if show_tools:
                shown = args if args is not None else tc.function.arguments
                if on_tool is not None:
                    on_tool(tc.function.name, shown)
                else:
                    if getattr(session, "_delta_open_line", False):
                        print()
                        session._delta_open_line = False
                    print(f"  🔧 {tc.function.name}({shown})")
            write_log(
                {
                    "event": "tool_call",
                    "name": tc.function.name,
                    "args": _log_preview(
                        args if args is not None else tc.function.arguments
                    ),
                },
                session.workspace,
            )
            write_log(
                {
                    "event": "tool_result",
                    "name": tc.function.name,
                    "content": result,
                },
                session.workspace,
            )
            session.messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": result}
            )

    return _close_with(session, "Max turns reached.", compact=api)


def _close_with(session: Session, text: str, compact=None) -> str:
    session.messages.append({"role": "assistant", "content": text})
    write_log(
        {"event": "model", "message": {"role": "assistant", "content": text}},
        session.workspace,
    )
    if compact is not None:
        _compact(session, compact)
    return text
