from collections.abc import Callable

from openai import APIConnectionError, APIStatusError, OpenAI

import harnh3x.agent.client as api_client
from harnh3x.agent.client import _api_failure, _remember_context
from harnh3x.agent.compact import _compact
from harnh3x.agent.complete import _completion, _emit_debug
from harnh3x.agent.messages import _log_preview, _visible_answer, assistant_message
from harnh3x.agent.session import Session
from harnh3x.agent.transcript import _save_transcript, write_log
from harnh3x.anthropic_api import AnthropicClient, CompletionError, status_message
from harnh3x.config import MAX_TURNS
from harnh3x.rules import append_matching_rules
from harnh3x.tools import _tool_result


def run(
    user_message: str,
    session: Session,
    client: OpenAI | AnthropicClient | None = None,
    show_tools: bool = True,
    on_tool: Callable[[str, object], None] | None = None,
) -> str:
    session.messages.append({"role": "user", "content": user_message})
    try:
        if session.abort.is_set():
            return ""
        return _complete(session, client, show_tools, on_tool)
    finally:
        _save_transcript(session)


def _complete(
    session: Session,
    client: OpenAI | AnthropicClient | None,
    show_tools: bool,
    on_tool: Callable[[str, object], None] | None,
) -> str:
    api = client or api_client.get_client()

    for turn in range(MAX_TURNS):
        if session.abort.is_set():
            return ""
        write_log({"event": "turn", "turn": turn}, session.workspace)
        _compact(session, api)
        if session.abort.is_set():
            return ""
        _emit_debug(session)
        try:
            completed = _completion(session, api)
        except APIConnectionError as exc:
            return _close_with(session, _api_failure(exc))
        except APIStatusError as exc:
            return _close_with(session, status_message(exc))
        except CompletionError as exc:
            return _close_with(session, str(exc))
        if completed is None or session.abort.is_set():
            return ""
        msg, usage = completed
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
            if session.abort.is_set():
                return ""
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
