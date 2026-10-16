"""Modal overlays: the export path prompt, the help box, and warning details."""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

_TUI_HELP = """\
Commands
  /help     show commands and hotkeys
  /resume   resume a past session
  /compact  compact context
  /export   write a response to a markdown file
  /exit     quit

Hotkeys
  Shift+Enter  new line
  Ctrl+N       new session
  Ctrl+H       hide or show thinking, tool calls, debug, and compaction boxes
  Ctrl+W       open warning details
  Ctrl+Q       quit and stop the running request"""


class ExportPathScreen(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", priority=True),
    ]
    DEFAULT_CSS = """
    ExportPathScreen {
        align: center middle;
        background: transparent;
    }
    #export-path-box {
        width: 70;
        height: auto;
        border: round ansi_bright_black;
        padding: 1 2;
        background: $surface;
    }
    #export-path {
        width: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="export-path-box"):
            yield Static("Path", markup=False)
            yield Input(placeholder="/path/to/file.md", id="export-path")

    def on_mount(self) -> None:
        box = self.query_one("#export-path-box", Vertical)
        box.border_title = "Export to markdown"
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if text:
            self.dismiss(text)

    def action_cancel(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close", "Close", priority=True),
    ]
    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    #help-box {
        width: auto;
        height: auto;
        border: round ansi_bright_black;
        padding: 1 2;
        background: $surface;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(_TUI_HELP, id="help-box", markup=False)

    def on_mount(self) -> None:
        self.query_one("#help-box", Static).border_title = "Help"

    def action_close(self) -> None:
        self.dismiss()


class WarningScreen(ModalScreen):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close", "Close", priority=True),
    ]
    DEFAULT_CSS = """
    WarningScreen {
        align: center middle;
    }
    #warning-box {
        width: auto;
        height: auto;
        max-width: 80;
        border: round ansi_yellow;
        padding: 1 2;
        background: $surface;
    }
    """

    def __init__(self, warnings: list[tuple[str, str]]) -> None:
        super().__init__()
        self.warnings = warnings

    def compose(self) -> ComposeResult:
        lines = []
        for name, reason in self.warnings:
            lines.append(name)
            lines.append(f"  {reason}")
        yield Static("\n".join(lines), id="warning-box", markup=False)

    def on_mount(self) -> None:
        self.query_one("#warning-box", Static).border_title = "Unavailable"

    def action_close(self) -> None:
        self.dismiss()
