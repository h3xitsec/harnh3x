import argparse
import shutil
import sys

from harness.agent import Session, list_models, run
from harness.config import MODEL
from harness.line_input import (
    Backspace,
    Down,
    Eof,
    Insert,
    Interrupt,
    Newline,
    Submit,
    ToggleThinking,
    Up,
    raw_keys,
    read_events,
    read_request,
)

THINKING_ROWS = 3


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


class ModelMenu:
    def __init__(self, models: list[str]):
        self.models = models
        self.selected = 0
        self.digits = ""
        self.error = ""

    def handle(self, event) -> str | None:
        if isinstance(event, Interrupt):
            raise KeyboardInterrupt
        if isinstance(event, Eof):
            raise EOFError
        if isinstance(event, (Up, Down)):
            self.digits = ""
            self.error = ""
            if isinstance(event, Up):
                self.selected = max(0, self.selected - 1)
            else:
                self.selected = min(len(self.models) - 1, self.selected + 1)
            return None
        if isinstance(event, Backspace):
            self.error = ""
            self.digits = self.digits[:-1]
            self._follow_digits()
            return None
        if isinstance(event, Insert) and event.text.isdigit():
            self.error = ""
            self.digits += event.text
            self._follow_digits()
            return None
        if isinstance(event, (Submit, Newline)):
            if self.digits == "":
                return self.models[self.selected]
            number = int(self.digits)
            if str(number) == self.digits and 1 <= number <= len(self.models):
                return self.models[number - 1]
            self.error = f"Enter a number from 1 to {len(self.models)}."
            self.digits = ""
            return None
        return None

    def render(self) -> str:
        lines = ["Available models:"]
        for index, name in enumerate(self.models):
            mark = ">" if index == self.selected else " "
            lines.append(f"{mark} {index + 1}. {name}")
        if self.error:
            lines.append(self.error)
        elif self.digits:
            lines.append(f"Number: {self.digits}")
        else:
            lines.append("Use up/down and Enter, or type a number.")
        return "\n".join(lines)

    def _follow_digits(self) -> None:
        if self.digits.isdigit() and str(int(self.digits)) == self.digits:
            number = int(self.digits)
            if 1 <= number <= len(self.models):
                self.selected = number - 1


def _paint_menu(text: str, lines_above: int) -> int:
    if lines_above:
        sys.stdout.write(f"\x1b[{lines_above}A")
    sys.stdout.write("\r\x1b[J")
    sys.stdout.write(text)
    sys.stdout.flush()
    return text.count("\n")


def _choose_model_with_keys(models: list[str]) -> str:
    menu = ModelMenu(models)
    pending = b""
    try:
        with raw_keys() as fd:
            drawn = _paint_menu(menu.render(), 0)
            while True:
                events, pending = read_events(fd, pending)
                chosen = None
                for event in events:
                    chosen = menu.handle(event)
                    if chosen is not None:
                        break
                drawn = _paint_menu(menu.render(), drawn)
                if chosen is not None:
                    _paint_menu("", drawn)
                    return chosen
    except (EOFError, KeyboardInterrupt):
        sys.stdout.write("\n")
        sys.stdout.flush()
        raise


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
        return _choose_model_with_keys(models)
    return _choose_model_by_number(models)


def resolve_model(spec: str | None) -> str:
    if spec is None:
        return MODEL
    if spec.strip() == "":
        return choose_model(list_models())
    return spec


def _wrap_lines(text: str, width: int) -> list[str]:
    width = max(width, 1)
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if paragraph == "":
            lines.append("")
            continue
        start = 0
        while start < len(paragraph):
            lines.append(paragraph[start : start + width])
            start += width
    return lines


def _line_limit(width: int) -> int:
    # A line as wide as the terminal wraps before the following newline, so the
    # cursor ends up further down than the newline count and a redraw appends.
    if width <= 1:
        return 1
    return width - 1


def format_thinking(reasoning: str, *, expanded: bool, width: int, hold: bool) -> str:
    limit = _line_limit(width)
    indent = "  " if limit > 2 else ""
    body_limit = max(limit - len(indent), 1)
    wrapped = _wrap_lines(reasoning, body_limit)
    if expanded:
        shown = wrapped
        action = "collapse"
    else:
        shown = wrapped[-THINKING_ROWS:]
        action = "expand"
    hint = f"ctrl+t to {action}"
    if hold:
        hint += " · enter to close"
    header = "\n".join(_wrap_lines(f"Thinking · {hint}", limit))
    body = "\n".join(f"{indent}{line}" for line in shown)
    return f"\x1b[2m{header}\n{body}\x1b[0m"


def reply_reasoning(session, start: int, answer: str) -> str:
    messages = getattr(session, "messages", None)
    if not messages:
        return ""
    parts = []
    for msg in messages[start:]:
        if not isinstance(msg, dict):
            continue
        reasoning = msg.get("reasoning")
        if msg.get("role") == "assistant" and reasoning:
            parts.append(reasoning)
    if parts and parts[-1] == answer:
        parts.pop()
    return "\n\n".join(parts)


def terminal_width() -> int:
    return shutil.get_terminal_size(fallback=(80, 24)).columns or 80


def thinking_rewind(cursor_row: int, block_newlines: int, *, prompt_gap: bool) -> int:
    gap = 1 if prompt_gap else 0
    return cursor_row + gap + block_newlines


class ReplyView:
    def __init__(self):
        self.reasoning = ""
        self.answer = ""
        self.answer_only = False
        self.expanded = False
        self.hold = False
        self.block_newlines = 0

    def render(self) -> str:
        if not self.reasoning:
            text = self.answer if self.answer_only else f"\nAgent: {self.answer}"
            if text.endswith("\n"):
                return text
            return text + "\n"
        width = terminal_width()
        panel = format_thinking(
            self.reasoning,
            expanded=self.expanded,
            width=width,
            hold=self.hold,
        )
        answer = "\n".join(_wrap_lines(f"Agent: {self.answer}", _line_limit(width)))
        return f"\n{panel}\n\n{answer}\n"

    def show(
        self, answer: str, reasoning: str, *, answer_only: bool, hold: bool
    ) -> None:
        self.answer = answer
        self.answer_only = answer_only
        self.expanded = False
        self.hold = hold
        self.reasoning = "" if answer_only else reasoning
        block = self.render()
        self.block_newlines = block.count("\n")
        sys.stdout.write(block)
        sys.stdout.flush()

    def toggle(self, cursor_row: int, *, prompt_gap: bool) -> int:
        if not self.reasoning:
            return cursor_row
        old = self.block_newlines
        self.expanded = not self.expanded
        block = self.render()
        self.block_newlines = block.count("\n")
        up = thinking_rewind(cursor_row, old, prompt_gap=prompt_gap)
        if up:
            sys.stdout.write(f"\x1b[{up}A")
        sys.stdout.write("\r\x1b[J")
        sys.stdout.write(block)
        if prompt_gap:
            sys.stdout.write("\n")
        sys.stdout.flush()
        return 0


def _review_thinking(view: ReplyView) -> None:
    pending = b""
    try:
        with raw_keys() as fd:
            while True:
                events, pending = read_events(fd, pending)
                for event in events:
                    if isinstance(event, ToggleThinking):
                        view.toggle(0, prompt_gap=False)
                    elif isinstance(event, (Submit, Newline, Eof)):
                        return
                    elif isinstance(event, Interrupt):
                        raise KeyboardInterrupt
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        sys.stdout.flush()


def _present(
    view: ReplyView,
    answer: str,
    session,
    start: int,
    *,
    answer_only: bool,
    one_shot: bool,
) -> None:
    reasoning = "" if answer_only else reply_reasoning(session, start, answer)
    hold = one_shot and sys.stdin.isatty() and sys.stdout.isatty() and bool(reasoning)
    view.show(answer, reasoning, answer_only=answer_only, hold=hold)
    if hold:
        _review_thinking(view)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    session = Session(model=resolve_model(args.model))
    show_tools = not args.answer_only
    view = ReplyView()
    if args.query is not None:
        if not args.answer_only:
            print(f"Workspace: {session.workspace}")
            print(f"Model: {session.model}")
        start = len(getattr(session, "messages", []) or [])
        response = run(args.query, session, show_tools=show_tools)
        _present(
            view,
            response,
            session,
            start,
            answer_only=args.answer_only,
            one_shot=True,
        )
        return

    if not args.answer_only:
        print("🤖 File Agent (Shift+Enter for a new line, type 'quit' to exit)")
        print(f"Workspace: {session.workspace}")
        print(f"Model: {session.model}")

    def toggle_thinking(cursor_row: int) -> int:
        return view.toggle(cursor_row, prompt_gap=True)

    while True:
        user_input = read_request(on_toggle=toggle_thinking)
        if user_input.lower() in ("quit", "exit"):
            break
        start = len(session.messages)
        response = run(user_input, session, show_tools=show_tools)
        _present(
            view,
            response,
            session,
            start,
            answer_only=args.answer_only,
            one_shot=False,
        )


if __name__ == "__main__":
    main()
