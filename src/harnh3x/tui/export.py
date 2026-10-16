"""Answer export: picking, rendering, and writing markdown, plus the clipboard."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from harnh3x.commands import EXPORT_USAGE, NO_EXPORT
from harnh3x.tui.transcript import conversation_turns

__all__ = [
    "EXPORT_USAGE",
    "NO_EXPORT",
    "answer_preview",
    "copy_response",
    "export_base",
    "exportable_answers",
    "render_export",
    "write_export",
]

_ANSWER_PREVIEW = 60


def exportable_answers(messages: list) -> list[str]:
    answers = []
    for turn in conversation_turns(messages):
        if turn.answer is not None:
            answers.append(turn.answer)
    return answers


def answer_preview(text: str) -> str:
    line = ""
    for raw in text.splitlines():
        stripped = " ".join(raw.split())
        if stripped:
            line = stripped
            break
    if not line:
        return "(empty)"
    if len(line) > _ANSWER_PREVIEW:
        return line[: _ANSWER_PREVIEW - 1] + "…"
    return line


def render_export(answers: list[str]) -> str:
    if len(answers) == 1:
        text = answers[0]
    else:
        text = "\n\n---\n\n".join(answers)
    if not text.endswith("\n"):
        text += "\n"
    return text


def export_base(session) -> Path:
    project = getattr(session, "project", None)
    if project:
        return Path(project)
    return Path.cwd()


def write_export(path: str, text: str, *, base: Path) -> Path:
    raw = path.strip()
    if not raw:
        raise ValueError(EXPORT_USAGE)
    expanded = Path(raw).expanduser()
    target = expanded if expanded.is_absolute() else base / expanded
    target = target.resolve()
    if target.is_dir():
        raise ValueError(f"{target} is a directory.")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    except OSError as exc:
        detail = exc.strerror or str(exc)
        raise ValueError(f"Could not write {target}: {detail}") from exc
    return target


def copy_response(app, text: str) -> None:
    app.copy_to_clipboard(text)
    if sys.platform != "darwin":
        return
    from textual.drivers.headless_driver import HeadlessDriver

    if isinstance(getattr(app, "_driver", None), HeadlessDriver):
        return
    try:
        subprocess.run(
            ["pbcopy"],
            input=text.encode(),
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return
