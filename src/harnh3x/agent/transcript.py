from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from harnh3x import config
from harnh3x.agent.client import _count
from harnh3x.config import TRANSCRIPT_FILE

if TYPE_CHECKING:
    from harnh3x.agent.session import Session


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
