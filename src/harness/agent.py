import json
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from harness.config import MAX_TURNS, MODEL, TRANSCRIPT_FILE, WORKSPACES_DIR
from harness.prompt import system_prompt
from harness.skills import SkillRegistry
from harness.tools import _tool_result

load_dotenv()

_client = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI()
    return _client


def list_models(client: OpenAI | None = None) -> list[str]:
    api = client or get_client()
    names = {model.id for model in api.models.list() if getattr(model, "id", None)}
    return sorted(names)


def _session_workspace(workspace: Path | None) -> Path:
    if workspace is not None:
        raw = workspace
    else:
        env = os.environ.get("WORKSPACE")
        raw = env if env else Path(WORKSPACES_DIR) / str(uuid.uuid4())
    path = Path(raw).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


class Session:
    def __init__(self, workspace: Path | None = None, model: str | None = None):
        self.workspace = _session_workspace(workspace)
        self.model = model or MODEL
        self.skills = SkillRegistry()
        self.messages: list[dict] = [
            {
                "role": "system",
                "content": system_prompt(self.workspace, self.skills.get_menu()),
            }
        ]

    def reset(self) -> None:
        self.workspace = _session_workspace(Path(WORKSPACES_DIR) / str(uuid.uuid4()))
        self.skills = SkillRegistry()
        self.messages = [
            {
                "role": "system",
                "content": system_prompt(self.workspace, self.skills.get_menu()),
            }
        ]

    def sync_prompt(self) -> None:
        self.messages[0]["content"] = system_prompt(
            self.workspace, self.skills.get_menu()
        )

    def resume(self, workspace: Path) -> None:
        path = Path(workspace).resolve()
        data = _read_transcript(path / TRANSCRIPT_FILE)
        self.workspace = path
        self.model = data["model"]
        self.messages = data["messages"]
        self.skills = SkillRegistry()
        _replay_skills(self)
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


def list_sessions(root: Path | None = None) -> list[SessionInfo]:
    base = Path(root) if root is not None else Path(WORKSPACES_DIR)
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


def _save_transcript(session: Session) -> None:
    path = session.workspace / TRANSCRIPT_FILE
    payload = {"model": session.model, "messages": session.messages}
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
    return value


def run(
    user_message: str,
    session: Session,
    client: OpenAI | None = None,
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
    client: OpenAI | None,
    show_tools: bool,
    on_tool: Callable[[str, object], None] | None,
) -> str:
    api = client or get_client()

    for turn in range(MAX_TURNS):
        write_log({"event": "turn", "turn": turn}, session.workspace)
        response = api.chat.completions.create(
            model=session.model,
            messages=session.messages,
            tools=session.skills.schemas(),
        )
        msg = response.choices[0].message
        stored = assistant_message(msg)
        session.messages.append(stored)
        write_log(
            {"event": "model", "turn": turn, "message": _log_preview(stored)},
            session.workspace,
        )
        if not msg.tool_calls:
            return _visible_answer(stored)

        for tc in msg.tool_calls:
            args, result = _tool_result(
                tc.function.name,
                tc.function.arguments,
                session.workspace,
                session.skills,
            )
            if tc.function.name in {"load_skill", "unload_skill"} and args is not None:
                session.sync_prompt()
            if show_tools:
                shown = args if args is not None else tc.function.arguments
                if on_tool is not None:
                    on_tool(tc.function.name, shown)
                else:
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

    text = "Max turns reached."
    session.messages.append({"role": "assistant", "content": text})
    write_log(
        {"event": "model", "message": {"role": "assistant", "content": text}},
        session.workspace,
    )
    return text
