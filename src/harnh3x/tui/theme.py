"""The Dracula palette, Toad's default theme.

Textual's built-in ``dracula`` theme supplies the roles used in CSS
(``$primary``, ``$success``, ``$accent``, and the rest). The values here are
the same published colors, for the status bar and for checks that a widget
resolved to one of them.
"""

from rich.terminal_theme import TerminalTheme

THEME = "dracula"

PRIMARY = "#BD93F9"
SECONDARY = "#6272A4"
ACCENT = "#FF79C6"
WARNING = "#FFB86C"
ERROR = "#FF5555"
SUCCESS = "#50FA7B"
CYAN = "#8BE9FD"
YELLOW = "#F1FA8C"
BACKGROUND = "#282A36"
FOREGROUND = "#F8F8F2"

ANSI_THEME = TerminalTheme(
    background=(0x28, 0x2A, 0x36),
    foreground=(0xF8, 0xF8, 0xF2),
    normal=[
        (0x21, 0x22, 0x2C),
        (0xFF, 0x55, 0x55),
        (0x50, 0xFA, 0x7B),
        (0xF1, 0xFA, 0x8C),
        (0xBD, 0x93, 0xF9),
        (0xFF, 0x79, 0xC6),
        (0x8B, 0xE9, 0xFD),
        (0xF8, 0xF8, 0xF2),
    ],
    bright=[
        (0x62, 0x72, 0xA4),
        (0xFF, 0x6E, 0x6E),
        (0x69, 0xFF, 0x94),
        (0xFF, 0xFF, 0xA5),
        (0xD6, 0xAC, 0xFF),
        (0xFF, 0x92, 0xDF),
        (0xA4, 0xFF, 0xFF),
        (0xFF, 0xFF, 0xFF),
    ],
)
