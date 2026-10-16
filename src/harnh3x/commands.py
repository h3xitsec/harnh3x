"""The slash commands shared by the plain prompt and the Textual composer.

`parse_command` in `harnh3x.agent` splits the typed text into a name and
the rest of the line. This module says which names exist, which of them
take an argument, what their help and usage lines are, and routes a parsed
command to the caller's handler.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

EXPORT_USAGE = "Usage: /export /path/to/file.md"
NO_EXPORT = "No responses to export."


@dataclass(frozen=True)
class Command:
    name: str
    summary: str
    takes_argument: bool = False
    usage: str | None = None

    @property
    def usage_text(self) -> str:
        return self.usage or f"Usage: /{self.name}"


COMMANDS: tuple[Command, ...] = (
    Command("help", "show commands"),
    Command("resume", "resume a past session"),
    Command("compact", "compact context"),
    Command(
        "export",
        "write a response to a markdown file",
        takes_argument=True,
        usage=EXPORT_USAGE,
    ),
    Command("exit", "quit"),
)

_BY_NAME = {command.name: command for command in COMMANDS}


def help_lines(*, hotkeys: bool = False) -> list[str]:
    """The Commands block of the help text.

    `hotkeys` says the surrounding help also lists hotkeys, which the
    `/help` line then mentions.
    """
    width = max(len(command.name) for command in COMMANDS) + 2
    lines = ["Commands"]
    for command in COMMANDS:
        summary = command.summary
        if command.name == "help" and hotkeys:
            summary = f"{summary} and hotkeys"
        lines.append(f"  /{command.name:<{width}}{summary}")
    return lines


def dispatch(
    name: str,
    rest: str,
    handlers: Mapping[str, Callable[[str], object]],
    note: Callable[[str], None],
) -> object:
    """Run the handler for a parsed command.

    An unknown name, a missing argument, or an unexpected one goes to
    `note` as a message and returns None. Otherwise the handler's result is
    returned.
    """
    command = _BY_NAME.get(name)
    if command is None:
        shown = f"/{name}" if name else "/"
        note(f"Unknown command: {shown}")
        return None
    if command.takes_argument != bool(rest):
        note(command.usage_text)
        return None
    return handlers[command.name](rest)
