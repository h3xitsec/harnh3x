import json
import os
import uuid
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from harness.config import MAX_TURNS, MODEL, WORKSPACES_DIR
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

    def sync_prompt(self) -> None:
        self.messages[0]["content"] = system_prompt(
            self.workspace, self.skills.get_menu()
        )


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
) -> str:
    session.messages.append({"role": "user", "content": user_message})
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
                print(
                    f"  🔧 {tc.function.name}({args if args is not None else tc.function.arguments})"
                )
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
