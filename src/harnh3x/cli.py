import argparse
import shutil
import sys

from harnh3x import __version__
from harnh3x.agent import (
    Session,
    compact_session,
    list_models,
    list_sessions,
    parse_command,
    run,
    session_label,
)
from harnh3x.commands import DETAILS_PLAIN, NO_EXPORT, dispatch, help_lines
from harnh3x.config import MODEL, require_api_settings
from harnh3x.tui import (
    YOLO_PLAIN,
    answer_preview,
    compact_label,
    debug_text,
    export_base,
    exportable_answers,
    line_limit,
    pick_model,
    render_export,
    reply_reasoning,
    run_interactive,
    thinking_text,
    transcript_items,
    wrap_lines,
    write_export,
)

_PLAIN_HELP = "\n".join(help_lines())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="File agent")
    parser.add_argument(
        "--version",
        action="version",
        version=f"harnh3x {__version__}",
    )
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
        "--yolo",
        action="store_true",
        help=(
            "Disable the kernel sandbox. The agent can read any file, "
            "run any command, including sudo, and send HTTP to any host, "
            "including localhost."
        ),
    )
    parser.add_argument(
        "--project",
        metavar="PATH",
        help=(
            "Grant sandboxed read, write, and shell access to this existing "
            "directory. The session workspace stays separate."
        ),
    )
    parser.add_argument(
        "--coding",
        action="store_true",
        help=(
            "Start a coding session in the --project directory. "
            "Uses a coding system prompt and that project's Cursor rules."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help=("Show the system prompt and the request sent on each completion."),
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


def format_debug(title: str, body: str, *, width: int) -> str:
    return "\x1b[36m" + debug_text(title, body, expanded=False, width=width) + "\x1b[0m"


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


def _print_session(session, *, answer_only: bool) -> None:
    if answer_only:
        return
    if getattr(session, "yolo", False):
        print(YOLO_PLAIN)
    print(f"Workspace: {session.workspace}")
    project = getattr(session, "project", None)
    if project:
        print(f"Project: {project}")
    if getattr(session, "coding", False):
        print("Coding")
    if getattr(session, "debug", False):
        print("Debug")
    print(f"Model: {session.model}")


def _print_tools(tools: list[tuple[str, object]]) -> None:
    for name, args in tools:
        print(f"  🔧 {name}({args})")


def _print_turn_heading(number: int) -> None:
    sys.stdout.write(f"\nTurn {number}\n")
    sys.stdout.flush()


def _print_turns(session, *, answer_only: bool) -> None:
    for kind, item in transcript_items(session.messages):
        if kind == "compact":
            print(f"\n{compact_label(item)}")
            continue
        turn = item
        print(f"\nYou: {turn.user}")
        if answer_only:
            if turn.answer is not None:
                _present(turn.answer, "", answer_only=True)
            continue
        for kind, payload in turn.steps:
            if kind == "round":
                _print_turn_heading(payload)
            elif kind == "thinking":
                panel = format_thinking(
                    payload, expanded=False, width=terminal_width(), hold=False
                )
                sys.stdout.write(f"\n{panel}\n")
                sys.stdout.flush()
            elif kind == "tools":
                _print_tools(payload)
            elif kind == "answer":
                _present(payload, "", answer_only=False)


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
    _print_session(session, answer_only=answer_only)
    _print_turns(session, answer_only=answer_only)


def _new_session(session, *, answer_only: bool) -> None:
    session.reset()
    _print_session(session, answer_only=answer_only)


def _choose_export(answers: list[str]) -> list[str] | None:
    if len(answers) == 1:
        return answers
    print("Responses:")
    for index, answer in enumerate(answers):
        print(f"  {index + 1}. {answer_preview(answer)}")
    print("  all  every response")
    while True:
        choice = input("Select a response (Enter cancels): ").strip()
        if choice == "":
            return None
        if choice.lower() == "all":
            return list(answers)
        if choice.isdigit() and 1 <= int(choice) <= len(answers):
            return [answers[int(choice) - 1]]
        print(f"Enter a number from 1 to {len(answers)}, or all.")


def _export_answers(session, rest: str) -> None:
    answers = exportable_answers(getattr(session, "messages", None) or [])
    if not answers:
        print(NO_EXPORT)
        return
    chosen = _choose_export(answers)
    if chosen is None:
        return
    try:
        path = write_export(rest, render_export(chosen), base=export_base(session))
    except ValueError as exc:
        print(exc)
        return
    print(f"Exported to {path}")


def _handle_command(session, name: str, rest: str, *, answer_only: bool) -> bool:
    """Run one slash command at the plain prompt. True means quit."""
    handlers = {
        "help": lambda _rest: print(_PLAIN_HELP),
        "new": lambda _rest: _new_session(session, answer_only=answer_only),
        "resume": lambda _rest: _resume_session(session, answer_only=answer_only),
        "compact": lambda _rest: print(compact_session(session)),
        "export": lambda rest: _export_answers(session, rest),
        "details": lambda _rest: print(DETAILS_PLAIN),
        "exit": lambda _rest: True,
    }
    return dispatch(name, rest, handlers, print) is True


def _print_debug(system: str | None, request: str) -> None:
    width = terminal_width()
    if system is not None:
        sys.stdout.write(f"\n{format_debug('System', system, width=width)}\n")
    sys.stdout.write(f"\n{format_debug('Request', request, width=width)}\n")
    sys.stdout.flush()


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


class _LiveReply:
    def __init__(self, *, answer_only: bool) -> None:
        self.answer_only = answer_only
        self.saw = False
        self.reasoning = ""
        self.content = ""
        self._agent = False

    def __call__(self, kind: str, text: str) -> None:
        self.saw = True
        if kind == "reasoning":
            self.reasoning += text
            if self.answer_only:
                return
            sys.stdout.write(f"\x1b[2m{text}\x1b[0m")
            sys.stdout.flush()
            return
        if kind != "content":
            return
        self.content += text
        if not self.answer_only and not self._agent:
            if self.reasoning and not self.reasoning.endswith("\n"):
                sys.stdout.write("\n")
            sys.stdout.write("\nAgent: ")
            self._agent = True
        sys.stdout.write(text)
        sys.stdout.flush()


def _finish_live(
    live: _LiveReply, answer: str, reasoning: str, *, answer_only: bool
) -> None:
    if not live.saw:
        _present(answer, reasoning, answer_only=answer_only)
        return
    if answer_only:
        if not live.content:
            sys.stdout.write(answer)
        shown = live.content or answer
        if not shown.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
        return
    if live.content:
        if not live.content.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
        return
    if live.reasoning and answer == live.reasoning:
        if not live.reasoning.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
        return
    _present(answer, "", answer_only=False)


def _run_printed(session, text: str, *, show_tools: bool, answer_only: bool) -> None:
    live = _LiveReply(answer_only=answer_only)
    previous = getattr(session, "on_delta", None)
    previous_turn = getattr(session, "on_model_turn", None)
    session.on_delta = live
    if not answer_only:
        session.on_model_turn = _print_turn_heading
    start = len(getattr(session, "messages", []) or [])
    try:
        answer = run(text, session, show_tools=show_tools)
    finally:
        session.on_delta = previous
        session.on_model_turn = previous_turn
    reasoning = "" if answer_only else reply_reasoning(session, start, answer)
    _finish_live(live, answer, reasoning, answer_only=answer_only)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.coding and not args.project:
        raise SystemExit("--coding requires --project")
    require_api_settings()
    try:
        session = Session(
            model=resolve_model(args.model),
            yolo=args.yolo,
            project=args.project,
            coding=args.coding,
            debug=args.debug and not args.answer_only,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    show_tools = not args.answer_only
    use_tui = _tty() and not args.answer_only
    if getattr(session, "debug", False) and not use_tui:
        session.on_debug = _print_debug
    if args.query is not None:
        if use_tui:
            run_interactive(session, run, show_tools=show_tools, initial=args.query)
            return

        _print_session(session, answer_only=args.answer_only)
        _run_printed(
            session,
            args.query,
            show_tools=show_tools,
            answer_only=args.answer_only,
        )
        return

    if use_tui:
        run_interactive(session, run, show_tools=show_tools)
        return

    if not args.answer_only:
        print("🤖 Harnh3x")
        _print_session(session, answer_only=False)

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
        _run_printed(
            session,
            user_input,
            show_tools=show_tools,
            answer_only=args.answer_only,
        )


if __name__ == "__main__":
    main()
