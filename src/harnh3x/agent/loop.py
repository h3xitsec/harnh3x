import json
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
from harnh3x.failures import MAX_TURNS_REACHED
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
    turn = 0

    while True:
        for _ in range(MAX_TURNS):
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

            for position, tc in enumerate(msg.tool_calls):
                if session.abort.is_set():
                    _answer_interrupted(session, msg.tool_calls[position:])
                    return ""
                if show_tools:
                    _show_tool(session, tc, on_tool)
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
                if (
                    tc.function.name in {"load_skill", "unload_skill"}
                    and args is not None
                ):
                    session.sync_prompt()
                done = getattr(session, "on_tool_done", None)
                if show_tools and done is not None:
                    done(tc.function.name, result)
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
            turn += 1

        if session.abort.is_set():
            return ""
        if _another_window(session):
            if session.abort.is_set():
                return ""
            continue
        if session.abort.is_set():
            return ""
        return _close_with(session, MAX_TURNS_REACHED, compact=api)


def _another_window(session: Session) -> bool:
    ask = getattr(session, "confirm_continue", None)
    if not callable(ask) or session.abort.is_set():
        return False
    return ask() is True


def _shown_args(arguments: object) -> object:
    if not isinstance(arguments, str):
        return arguments
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return arguments
    return parsed if isinstance(parsed, dict) else arguments


def _show_tool(session: Session, tc, on_tool) -> None:
    shown = _shown_args(tc.function.arguments)
    if on_tool is not None:
        on_tool(tc.function.name, shown)
        return
    if getattr(session, "_delta_open_line", False):
        print()
        session._delta_open_line = False
    print(f"  🔧 {tc.function.name}({shown})")


INTERRUPTED_RESULT = "Error: interrupted before this tool ran"


def _answer_interrupted(session: Session, tool_calls) -> None:
    # Every tool call the model made needs a result, or the next completion
    # and a later /resume send an assistant turn the API rejects.
    for tc in tool_calls:
        write_log(
            {
                "event": "tool_result",
                "name": tc.function.name,
                "content": INTERRUPTED_RESULT,
            },
            session.workspace,
        )
        session.messages.append(
            {"role": "tool", "tool_call_id": tc.id, "content": INTERRUPTED_RESULT}
        )


def _close_with(session: Session, text: str, compact=None) -> str:
    session.messages.append({"role": "assistant", "content": text})
    write_log(
        {"event": "model", "message": {"role": "assistant", "content": text}},
        session.workspace,
    )
    if compact is not None:
        _compact(session, compact)
    return text
