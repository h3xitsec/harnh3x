import json
import os
import uuid
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from harness.config import LOG_PATH, MAX_TURNS, MODEL, WORKSPACES_DIR
from harness.prompt import system_prompt
from harness.tools import TOOLS, _tool_result

load_dotenv()

_client = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI()
    return _client


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
    def __init__(self, workspace: Path | None = None, log_path: str | None = None):
        self.workspace = _session_workspace(workspace)
        self.log_path = (
            log_path if log_path is not None else str(self.workspace / LOG_PATH)
        )
        self.messages: list[dict] = [
            {"role": "system", "content": system_prompt(self.workspace)}
        ]


def write_log(event: dict, log_path: str = LOG_PATH) -> None:
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def assistant_message(msg) -> dict:
    stored = {"role": "assistant", "content": msg.content}
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


def _log_preview(value):
    if isinstance(value, str) and len(value) > 500:
        return value[:500] + "...[truncated]"
    if isinstance(value, dict):
        return {key: _log_preview(item) for key, item in value.items()}
    return value


def run(user_message: str, session: Session, client: OpenAI | None = None) -> str:
    session.messages.append({"role": "user", "content": user_message})
    api = client or get_client()

    for turn in range(MAX_TURNS):
        write_log({"event": "turn", "turn": turn}, session.log_path)
        response = api.chat.completions.create(
            model=MODEL, messages=session.messages, tools=TOOLS
        )
        msg = response.choices[0].message
        stored = assistant_message(msg)
        session.messages.append(stored)
        write_log({"event": "model", "turn": turn, "message": stored}, session.log_path)
        if not msg.tool_calls:
            return msg.content or ""

        for tc in msg.tool_calls:
            args, result = _tool_result(
                tc.function.name, tc.function.arguments, session.workspace
            )
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
                session.log_path,
            )
            write_log(
                {
                    "event": "tool_result",
                    "name": tc.function.name,
                    "content": result,
                },
                session.log_path,
            )
            session.messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": result}
            )

    text = "Max turns reached."
    session.messages.append({"role": "assistant", "content": text})
    write_log(
        {"event": "model", "message": {"role": "assistant", "content": text}},
        session.log_path,
    )
    return text
