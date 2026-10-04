import os
import select
import shutil
import sys
import termios
import tty
from contextlib import contextmanager
from dataclasses import dataclass

# Disambiguate modified keys (Kitty protocol). Shift+Enter arrives as CSI 13;2 u
# instead of the same CR as Enter. Pop with CSI < u when the read finishes.
_KITTY_PUSH = "\x1b[>1u"
_KITTY_POP = "\x1b[<u"
_BRACKETED_PASTE_ON = "\x1b[?2004h"
_BRACKETED_PASTE_OFF = "\x1b[?2004l"
_PASTE_END = b"\x1b[201~"


@dataclass(frozen=True)
class Insert:
    text: str


@dataclass(frozen=True)
class Newline:
    pass


@dataclass(frozen=True)
class Submit:
    pass


@dataclass(frozen=True)
class Backspace:
    pass


@dataclass(frozen=True)
class Delete:
    pass


@dataclass(frozen=True)
class Left:
    pass


@dataclass(frozen=True)
class Right:
    pass


@dataclass(frozen=True)
class Up:
    pass


@dataclass(frozen=True)
class Down:
    pass


@dataclass(frozen=True)
class Home:
    pass


@dataclass(frozen=True)
class End:
    pass


@dataclass(frozen=True)
class Eof:
    pass


@dataclass(frozen=True)
class Interrupt:
    pass


@dataclass(frozen=True)
class Ignore:
    pass


@dataclass(frozen=True)
class ToggleThinking:
    pass


@dataclass(frozen=True)
class Frame:
    output: str
    cursor_row: int
    cursor_col: int
    end_row: int
    end_col: int


class Editor:
    def __init__(self) -> None:
        self.text = ""
        self.cursor = 0

    def handle(self, event) -> str | None:
        if isinstance(event, Insert):
            self.text = self.text[: self.cursor] + event.text + self.text[self.cursor :]
            self.cursor += len(event.text)
        elif isinstance(event, Newline):
            return self.handle(Insert("\n"))
        elif isinstance(event, Submit):
            return self.text
        elif isinstance(event, Backspace):
            if self.cursor:
                self.text = self.text[: self.cursor - 1] + self.text[self.cursor :]
                self.cursor -= 1
        elif isinstance(event, Delete):
            if self.cursor < len(self.text):
                self.text = self.text[: self.cursor] + self.text[self.cursor + 1 :]
        elif isinstance(event, Left):
            self.cursor = max(0, self.cursor - 1)
        elif isinstance(event, Right):
            self.cursor = min(len(self.text), self.cursor + 1)
        elif isinstance(event, Up):
            self._vertical(-1)
        elif isinstance(event, Down):
            self._vertical(1)
        elif isinstance(event, Home):
            self.cursor = self.text.rfind("\n", 0, self.cursor) + 1
        elif isinstance(event, End):
            nxt = self.text.find("\n", self.cursor)
            self.cursor = len(self.text) if nxt < 0 else nxt
        elif isinstance(event, Eof):
            if self.text == "":
                raise EOFError
            self.handle(Delete())
        elif isinstance(event, Interrupt):
            raise KeyboardInterrupt
        return None

    def _vertical(self, delta: int) -> None:
        lines = self.text.split("\n")
        offset = 0
        index = 0
        column = 0
        for index, line in enumerate(lines):
            if self.cursor <= offset + len(line):
                column = self.cursor - offset
                break
            offset += len(line) + 1
        target = index + delta
        if target < 0 or target >= len(lines):
            return
        column = min(column, len(lines[target]))
        self.cursor = sum(len(lines[i]) + 1 for i in range(target)) + column


def _params(body: bytes) -> list[list[int]]:
    if not body:
        return []
    groups = []
    for part in body.decode("ascii", "replace").split(";"):
        subs = []
        for piece in part.split(":"):
            if piece == "":
                subs.append(0)
                continue
            try:
                subs.append(int(piece))
            except ValueError:
                subs.append(0)
        groups.append(subs)
    return groups


def _param(groups: list[list[int]], index: int, default: int = 0) -> int:
    if index < len(groups) and groups[index]:
        return groups[index][0]
    return default


def _modifier(groups: list[list[int]]) -> int:
    mods = _param(groups, 1, 1)
    if mods == 0:
        mods = 1
    return mods - 1


def _shifted(groups: list[list[int]]) -> bool:
    return bool(_modifier(groups) & 1)


def _ctrl(groups: list[list[int]]) -> bool:
    return bool(_modifier(groups) & 4)


def _release(groups: list[list[int]]) -> bool:
    if len(groups) > 1 and len(groups[1]) > 1:
        return groups[1][1] == 3
    return False


def _enter_key(groups: list[list[int]]) -> Insert | Newline | Submit | Ignore:
    if _release(groups):
        return Ignore()
    if _shifted(groups):
        return Newline()
    return Submit()


def _kitty(groups: list[list[int]]):
    if _release(groups):
        return Ignore()
    code = _param(groups, 0)
    if code == 116 and _ctrl(groups):
        return ToggleThinking()
    if code != 13:
        return Ignore()
    return _enter_key(groups)


def _modify_other(groups: list[list[int]]):
    key = _param(groups, 2)
    if key != 13:
        return Ignore()
    return _enter_key([[key], [_param(groups, 1, 1)]])


def _csi(body: bytes, final: int):
    groups = _params(body)
    if final == ord("~"):
        code = _param(groups, 0)
        if code == 27:
            return _modify_other(groups)
        if code == 3:
            return Delete()
        if code in (1, 7):
            return Home()
        if code in (4, 8):
            return End()
        return Ignore()
    if final == ord("u"):
        return _kitty(groups)
    if final == ord("A"):
        return Up()
    if final == ord("B"):
        return Down()
    if final == ord("C"):
        return Right()
    if final == ord("D"):
        return Left()
    if final == ord("H"):
        return Home()
    if final == ord("F"):
        return End()
    return Ignore()


def _paste_text(blob: bytes) -> str:
    return blob.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")


def _utf8_width(lead: int) -> int:
    if lead & 0x80 == 0:
        return 1
    if lead & 0xE0 == 0xC0:
        return 2
    if lead & 0xF0 == 0xE0:
        return 3
    if lead & 0xF8 == 0xF0:
        return 4
    return 1


def parse_keys(data: bytes, *, flush: bool = False) -> tuple[list, bytes]:
    events = []
    i = 0
    n = len(data)
    while i < n:
        byte = data[i]
        if byte == 0x1B:
            if i + 1 >= n:
                break
            nxt = data[i + 1]
            if nxt in (ord("\r"), ord("\n")):
                events.append(Newline())
                i += 2
                continue
            if nxt != ord("["):
                if flush:
                    i += 1
                    continue
                break
            j = i + 2
            while j < n and 0x20 <= data[j] <= 0x3F:
                j += 1
            if j >= n:
                break
            if not (0x40 <= data[j] <= 0x7E):
                i += 1
                continue
            body = data[i + 2 : j]
            final = data[j]
            if final == ord("~") and _param(_params(body), 0) == 200:
                end = data.find(_PASTE_END, j + 1)
                if end < 0:
                    if not flush:
                        break
                    events.append(Insert(_paste_text(data[j + 1 :])))
                    i = n
                    break
                events.append(Insert(_paste_text(data[j + 1 : end])))
                i = end + len(_PASTE_END)
                continue
            events.append(_csi(body, final))
            i = j + 1
            continue
        if byte == ord("\r"):
            if i + 1 < n and data[i + 1] == ord("\n"):
                i += 2
            else:
                i += 1
            events.append(Submit())
            continue
        if byte == ord("\n"):
            events.append(Newline())
            i += 1
            continue
        if byte in (0x7F, 0x08):
            events.append(Backspace())
            i += 1
            continue
        if byte == 0x04:
            events.append(Eof())
            i += 1
            continue
        if byte == 0x03:
            events.append(Interrupt())
            i += 1
            continue
        if byte == 0x14:
            events.append(ToggleThinking())
            i += 1
            continue
        if byte < 0x20 and byte != 0x09:
            i += 1
            continue
        width = _utf8_width(byte)
        if i + width > n:
            break
        chunk = data[i : i + width]
        try:
            text = chunk.decode("utf-8")
        except UnicodeDecodeError:
            text = data[i : i + 1].decode("utf-8", "replace")
            width = 1
        events.append(Insert(text))
        i += width
    if flush:
        return events, b""
    return events, data[i:]


def _visual_lines(text: str, prompt: str, width: int) -> list[str]:
    continuation = " " * len(prompt)
    visual: list[str] = []
    for index, line in enumerate(text.split("\n")):
        full = (prompt if index == 0 else continuation) + line
        if full == "":
            visual.append("")
            continue
        start = 0
        while start < len(full):
            visual.append(full[start : start + width])
            start += width
    return visual


def _position(text: str, index: int, prompt: str, width: int) -> tuple[int, int]:
    visual = _visual_lines(text[:index], prompt, width)
    row = len(visual) - 1
    col = len(visual[-1]) if visual else 0
    if col == width:
        return row + 1, 0
    return row, col


def render(text: str, cursor: int, prompt: str, width: int) -> Frame:
    if width <= 0:
        width = 80
    cursor = max(0, min(cursor, len(text)))
    visual = _visual_lines(text, prompt, width)
    chunks: list[str] = []
    for index, line in enumerate(visual):
        chunks.append(line)
        if index != len(visual) - 1 and len(line) < width:
            chunks.append("\n")
    end_row, end_col = _position(text, len(text), prompt, width)
    cursor_row, cursor_col = _position(text, cursor, prompt, width)
    return Frame("".join(chunks), cursor_row, cursor_col, end_row, end_col)


def _move(from_row: int, to_row: int, to_col: int) -> str:
    parts = []
    if to_row > from_row:
        parts.append(f"\x1b[{to_row - from_row}B")
    elif to_row < from_row:
        parts.append(f"\x1b[{from_row - to_row}A")
    parts.append("\r")
    if to_col > 0:
        parts.append(f"\x1b[{to_col}C")
    return "".join(parts)


def _paint(text: str, cursor: int, prompt: str, cursor_row: int) -> tuple[Frame, int]:
    width = shutil.get_terminal_size(fallback=(80, 24)).columns or 80
    frame = render(text, cursor, prompt, width)
    if cursor_row:
        sys.stdout.write(f"\x1b[{cursor_row}A")
    sys.stdout.write("\r\x1b[J")
    sys.stdout.write(frame.output)
    sys.stdout.write(_move(frame.end_row, frame.cursor_row, frame.cursor_col))
    sys.stdout.flush()
    return frame, frame.cursor_row


def read_events(fd: int, pending: bytes) -> tuple[list, bytes]:
    if pending:
        ready, _, _ = select.select([fd], [], [], 0.05)
        if not ready:
            return parse_keys(pending, flush=True)
        chunk = _read_chunk(fd)
        return parse_keys(pending + chunk)
    chunk = _read_chunk(fd)
    return parse_keys(chunk)


@contextmanager
def raw_keys():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    _set_cbreak(fd)
    try:
        _enable_keys()
        try:
            yield fd
        finally:
            _disable_keys()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _edit_loop(prompt: str, on_toggle=None) -> str:
    editor = Editor()
    pending = b""
    cursor_row = 0
    fd = sys.stdin.fileno()
    _, cursor_row = _paint(editor.text, editor.cursor, prompt, cursor_row)
    while True:
        events, pending = read_events(fd, pending)
        submitted = None
        for event in events:
            if isinstance(event, ToggleThinking):
                if on_toggle is not None:
                    cursor_row = on_toggle(cursor_row)
                continue
            submitted = editor.handle(event)
            if submitted is not None:
                break
        frame, cursor_row = _paint(editor.text, editor.cursor, prompt, cursor_row)
        if submitted is not None:
            sys.stdout.write(_move(frame.cursor_row, frame.end_row, frame.end_col))
            sys.stdout.write("\n")
            sys.stdout.flush()
            return submitted


def _read_chunk(fd: int) -> bytes:
    chunk = os.read(fd, 1024)
    if chunk == b"":
        raise EOFError
    return chunk


def _set_cbreak(fd: int) -> None:
    attrs = termios.tcgetattr(fd)
    # ICRNL maps CR to LF, so Enter and Shift+Enter become the same byte.
    attrs[tty.IFLAG] &= ~(termios.ICRNL | termios.IXON)
    attrs[tty.LFLAG] &= ~(termios.ECHO | termios.ICANON)
    attrs[tty.CC][termios.VMIN] = 1
    attrs[tty.CC][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSADRAIN, attrs)


def _enable_keys() -> None:
    sys.stdout.write(_KITTY_PUSH + _BRACKETED_PASTE_ON)
    sys.stdout.flush()


def _disable_keys() -> None:
    sys.stdout.write(_KITTY_POP + _BRACKETED_PASTE_OFF)
    sys.stdout.flush()


def _read_tty(prompt: str, on_toggle=None) -> str:
    try:
        with raw_keys():
            return _edit_loop(prompt, on_toggle)
    except (EOFError, KeyboardInterrupt):
        sys.stdout.write("\n")
        sys.stdout.flush()
        raise


def read_request(prompt: str = "\nYou: ", on_toggle=None) -> str:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return input(prompt).strip()
    head, sep, label = prompt.rpartition("\n")
    if sep:
        sys.stdout.write(head + sep)
        sys.stdout.flush()
    else:
        label = prompt
    return _read_tty(label, on_toggle).strip()
