"""Modal overlays: the export path prompt, the help box, and warning details."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from harnh3x.commands import help_lines

_HOTKEYS = """\
Hotkeys
  Enter        send
  Shift+Enter  new line (Ctrl+J where Shift+Enter sends Enter)
  Up/Down      previous or next prompt
  Tab          complete a slash command
  Esc          stop the running request
  Ctrl+N       new session
  Ctrl+H       hide or show thinking, tool calls, debug, and compaction boxes
  Ctrl+W       open warning details
  Ctrl+Q       quit and stop the running request"""

_TERMINAL_APP = """\
Terminal.app
  Option+Backspace deletes a word only when "Use Option as Meta key" is on
  (Settings, Profiles, Keyboard). If "Delete sends Control-H" is on,
  Backspace toggles details; /details works in any terminal."""

_TUI_HELP = (
    "\n".join(help_lines(hotkeys=True)) + "\n\n" + _HOTKEYS + "\n\n" + _TERMINAL_APP
)

CLOSE_HINT = "Escape to close"

_FRAME_CSS = """
    {screen} {{
        align: center middle;
    }}
    #{frame} {{
        width: auto;
        max-width: 90%;
        height: auto;
        max-height: 90%;
        border: round {border};
        padding: 1 2;
        background: $surface;
    }}
    #{frame} Static {{
        width: auto;
    }}
"""


def export_filename(now: datetime | None = None) -> str:
    moment = now or datetime.now()
    return f"answer-{moment.strftime('%Y%m%d-%H%M')}.md"


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
    #export-base {
        color: $text-muted;
    }
    """

    def __init__(self, base: Path | None = None, *, now: datetime | None = None):
        super().__init__()
        self.base = base
        self.default = export_filename(now)

    def compose(self) -> ComposeResult:
        with Vertical(id="export-path-box"):
            yield Static("Path", markup=False)
            yield Input(
                value=self.default,
                placeholder="/path/to/file.md",
                id="export-path",
                select_on_focus=True,
            )
            if self.base is not None:
                yield Static(
                    f"Relative paths go to {self.base}",
                    id="export-base",
                    markup=False,
                )

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
    DEFAULT_CSS = _FRAME_CSS.format(
        screen="HelpScreen", frame="help-frame", border="ansi_bright_black"
    )

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-frame"):
            yield Static(_TUI_HELP, id="help-box", markup=False)

    def on_mount(self) -> None:
        frame = self.query_one("#help-frame", VerticalScroll)
        frame.border_title = "Help"
        frame.border_subtitle = CLOSE_HINT

    def action_close(self) -> None:
        self.dismiss()


class WarningScreen(ModalScreen):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close", "Close", priority=True),
    ]
    DEFAULT_CSS = _FRAME_CSS.format(
        screen="WarningScreen", frame="warning-frame", border="ansi_yellow"
    )

    def __init__(self, warnings: list[tuple[str, str]]) -> None:
        super().__init__()
        self.warnings = warnings

    def compose(self) -> ComposeResult:
        lines = []
        for name, reason in self.warnings:
            lines.append(name)
            lines.append(f"  {reason}")
        with VerticalScroll(id="warning-frame"):
            yield Static("\n".join(lines), id="warning-box", markup=False)

    def on_mount(self) -> None:
        frame = self.query_one("#warning-frame", VerticalScroll)
        frame.border_title = "Unavailable"
        frame.border_subtitle = CLOSE_HINT

    def action_close(self) -> None:
        self.dismiss()
