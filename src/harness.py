import argparse
import shutil
import sys

from harness.agent import Session, list_models, run
from harness.config import MODEL
from harness.tui import (
    line_limit,
    pick_model,
    reply_reasoning,
    run_interactive,
    run_review,
    thinking_text,
    wrap_lines,
)


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
        print("🤖 File Agent (Shift+Enter for a new line, type 'exit' or press 'ctrl+q' to exit)")
        print(f"Workspace: {session.workspace}")
        print(f"Model: {session.model}")

    while True:
        try:
            user_input = input("\nYou: ").strip()
        except EOFError:
            break
        if user_input.lower() == "exit":
            break
        start = len(session.messages)
        response = run(user_input, session, show_tools=show_tools)
        reasoning = (
            "" if args.answer_only else reply_reasoning(session, start, response)
        )
        _present(response, reasoning, answer_only=args.answer_only)


if __name__ == "__main__":
    main()
