import argparse
import shutil
import sys

from harnh3x.agent import (
    Session,
    compact_session,
    list_models,
    list_sessions,
    parse_command,
    run,
    session_label,
)
from harnh3x.config import MODEL
from harnh3x.tui import (
    compact_label,
    conversation_turns,
    line_limit,
    pick_model,
    reply_reasoning,
    run_interactive,
    run_review,
    thinking_text,
    transcript_items,
    wrap_lines,
)

_PLAIN_HELP = """\
Commands
  /help     show commands
  /resume   resume a past session
  /compact  compact context
  /exit     quit"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="File agent")
    parser.add_argument(
        "query",
        nargs="?",
        help="Request to run once, then exit. Omit to start the interactive prompt.",
    )
    parser.add_argument(
        "--answer-only",
        action="store_true",
        help="Print only the final answer, without tool call output",
    )
    parser.add_argument(
        "--model",
        nargs="?",
        const="",
        default=None,
        metavar="MODEL",
        help=(
            "Model to use. Pass a name to select it, or pass the flag alone "
            f"to choose from the API. Defaults to {MODEL}."
        ),
    )
    return parser


def _choose_model_by_number(models: list[str]) -> str:
    print("Available models:")
    for index, name in enumerate(models, start=1):
        print(f"  {index}. {name}")
    while True:
        choice = input("Select a model: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(models):
            return models[int(choice) - 1]
        print(f"Enter a number from 1 to {len(models)}.")


def choose_model(models: list[str]) -> str:
    if not models:
        raise SystemExit("No models available from the API.")
    if sys.stdin.isatty() and sys.stdout.isatty():
        return pick_model(models)
    return _choose_model_by_number(models)


def resolve_model(spec: str | None) -> str:
    if spec is None:
        return MODEL
    if spec.strip() == "":
        return choose_model(list_models())
    return spec


def format_thinking(reasoning: str, *, expanded: bool, width: int, hold: bool) -> str:
    return (
        "\x1b[2m"
        + thinking_text(reasoning, expanded=expanded, width=width, hold=hold)
        + "\x1b[0m"
    )


def terminal_width() -> int:
    return shutil.get_terminal_size(fallback=(80, 24)).columns or 80


def format_reply(
    answer: str,
    reasoning: str,
    *,
    answer_only: bool,
    hold: bool,
    width: int,
    expanded: bool = False,
) -> str:
    if answer_only or not reasoning:
        text = answer if answer_only else f"\nAgent: {answer}"
        if text.endswith("\n"):
            return text
        return text + "\n"
    panel = format_thinking(reasoning, expanded=expanded, width=width, hold=hold)
    answer_text = "\n".join(wrap_lines(f"Agent: {answer}", line_limit(width)))
    return f"\n{panel}\n\n{answer_text}\n"


def _tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _print_tools(tools: list[tuple[str, object]]) -> None:
    for name, args in tools:
        print(f"  🔧 {name}({args})")


def _print_turns(session, *, answer_only: bool) -> None:
    for kind, item in transcript_items(session.messages):
        if kind == "compact":
            print(f"\n{compact_label(item)}")
            continue
        turn = item
        print(f"\nYou: {turn.user}")
        if not answer_only:
            _print_tools(turn.tools)
        if turn.answer is None:
            continue
        reasoning = "" if answer_only else turn.reasoning
        _present(turn.answer, reasoning, answer_only=answer_only)


def _choose_session(sessions: list):
    print("Past sessions:")
    for index, info in enumerate(sessions):
        print(f"  {session_label(info, index)}")
    while True:
        choice = input("Select a session (Enter cancels): ").strip()
        if choice == "":
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(sessions):
            return sessions[int(choice) - 1]
        print(f"Enter a number from 1 to {len(sessions)}.")


def _resume_session(session, *, answer_only: bool) -> None:
    sessions = list_sessions()
    if not sessions:
        print("No past sessions.")
        return
    chosen = _choose_session(sessions)
    if chosen is None:
        return
    try:
        session.resume(chosen.workspace)
    except ValueError as exc:
        print(exc)
        return
    if not answer_only:
        print(f"Workspace: {session.workspace}")
        print(f"Model: {session.model}")
    _print_turns(session, answer_only=answer_only)


def _handle_command(session, name: str, rest: str, *, answer_only: bool) -> bool:
    if name == "help" and rest == "":
        print(_PLAIN_HELP)
        return False
    if name == "help":
        print("Usage: /help")
        return False
    if name == "resume" and rest == "":
        _resume_session(session, answer_only=answer_only)
        return False
    if name == "resume":
        print("Usage: /resume")
        return False
    if name == "compact" and rest == "":
        print(compact_session(session))
        return False
    if name == "compact":
        print("Usage: /compact")
        return False
    if name == "exit" and rest == "":
        return True
    if name == "exit":
        print("Usage: /exit")
        return False
    shown = f"/{name}" if name else "/"
    print(f"Unknown command: {shown}")
    return False


def _present(answer: str, reasoning: str, *, answer_only: bool) -> None:
    sys.stdout.write(
        format_reply(
            answer,
            reasoning,
            answer_only=answer_only,
            hold=False,
            width=terminal_width(),
        )
    )
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    session = Session(model=resolve_model(args.model))
    show_tools = not args.answer_only
    use_tui = _tty() and not args.answer_only
    if args.query is not None:
        if use_tui:
            tools: list[tuple[str, object]] = []

            def on_tool(name: str, args_text: object) -> None:
                tools.append((name, args_text))

            start = len(getattr(session, "messages", []) or [])
            response = run(
                args.query,
                session,
                show_tools=show_tools,
                on_tool=on_tool,
            )
            reasoning = reply_reasoning(session, start, response)
            if reasoning:
                run_review(session, args.query, tools, reasoning, response)
                return
            print(f"Workspace: {session.workspace}")
            print(f"Model: {session.model}")
            _print_tools(tools)
            _present(response, "", answer_only=False)
            return

        if not args.answer_only:
            print(f"Workspace: {session.workspace}")
            print(f"Model: {session.model}")
        start = len(getattr(session, "messages", []) or [])
        response = run(args.query, session, show_tools=show_tools)
        reasoning = (
            "" if args.answer_only else reply_reasoning(session, start, response)
        )
        _present(response, reasoning, answer_only=args.answer_only)
        return

    if use_tui:
        run_interactive(session, run, show_tools=show_tools)
        return

    if not args.answer_only:
        print("🤖 Harnh3x")
        print(f"Workspace: {session.workspace}")
        print(f"Model: {session.model}")

    while True:
        try:
            user_input = input("\nYou: ").strip()
        except EOFError:
            break
        command = parse_command(user_input)
        if command is not None:
            if _handle_command(session, *command, answer_only=args.answer_only):
                break
            continue
        start = len(session.messages)
        response = run(user_input, session, show_tools=show_tools)
        reasoning = (
            "" if args.answer_only else reply_reasoning(session, start, response)
        )
        _present(response, reasoning, answer_only=args.answer_only)


if __name__ == "__main__":
    main()
