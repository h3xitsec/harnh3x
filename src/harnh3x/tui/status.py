"""The status bar model: segments built from session state, laid out as badges."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from textual.content import Content

from harnh3x.tui.text import context_level
from harnh3x.tui.theme import (
    BACKGROUND,
    CYAN,
    ERROR,
    FOREGROUND,
    PRIMARY,
    SECONDARY,
    WARNING,
    YELLOW,
)

MODEL_STYLE = f"bold {BACKGROUND} on {PRIMARY}"
BADGE_STYLE = f"{FOREGROUND} on {SECONDARY}"
CONTEXT_STYLES = {
    None: BADGE_STYLE,
    "warn": f"{BACKGROUND} on {WARNING}",
    "over": f"bold {FOREGROUND} on {ERROR}",
}
YOLO_STYLE = f"bold {BACKGROUND} on {YELLOW}"
DEBUG_STYLE = f"{BACKGROUND} on {CYAN}"
WARNING_STYLE = f"bold {FOREGROUND} on {ERROR}"
VERSION_STYLE = SECONDARY


@dataclass(frozen=True)
class StatusState:
    model: str | None = None
    used: int | None = None
    limit: int | None = None
    compact_at: float = 0.0
    counts: tuple[int, int, int, int, int] | None = None
    warnings: tuple[tuple[str, str], ...] = ()
    yolo: bool = False
    debug: bool = False
    version: str | None = None


@dataclass(frozen=True)
class Segment:
    key: str
    text: str
    style: str
    side: Literal["left", "right"] = "left"
    priority: int = 0
    action: str | None = None

    def badge(self) -> Content:
        return Content.styled(f" {self.text} ", self.style)


def compact_count(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 10_000:
        return f"{n / 1000:.1f}k"
    if n < 1_000_000:
        return f"{round(n / 1000)}k"
    return f"{n / 1_000_000:.1f}M"


def context_text(used: int | None, limit: int | None) -> str | None:
    if used is None and limit is None:
        return None
    used_text = compact_count(used) if used is not None else "—"
    if limit is None:
        return f"ctx {used_text}"
    text = f"ctx {used_text}/{compact_count(limit)}"
    if used is not None and limit > 0:
        text += f" {round(used * 100 / limit)}%"
    return text


def _model(state: StatusState) -> Segment | None:
    if not state.model:
        return None
    return Segment("model", state.model, MODEL_STYLE, priority=90, action="model")


def _context(state: StatusState) -> Segment | None:
    text = context_text(state.used, state.limit)
    if text is None:
        return None
    level = context_level(state.used, state.limit, state.compact_at)
    return Segment("context", text, CONTEXT_STYLES[level], priority=80)


def _yolo(state: StatusState) -> Segment | None:
    if not state.yolo:
        return None
    return Segment("yolo", "YOLO", YOLO_STYLE, priority=100)


def _debug(state: StatusState) -> Segment | None:
    if not state.debug:
        return None
    return Segment("debug", "DEBUG", DEBUG_STYLE, priority=60)


def _skills(state: StatusState) -> Segment | None:
    if state.counts is None:
        return None
    skills, _tools, custom, _servers, _mcp_tools = state.counts
    text = f"skills {skills}+{custom}" if custom else f"skills {skills}"
    return Segment("skills", text, BADGE_STYLE, "right", 30, action="skills")


def _tools(state: StatusState) -> Segment | None:
    if state.counts is None:
        return None
    text = f"tools {state.counts[1]}"
    return Segment("tools", text, BADGE_STYLE, "right", 30, action="skills")


def _mcp(state: StatusState) -> Segment | None:
    if state.counts is None:
        return None
    _skills, _tools, _custom, servers, mcp_tools = state.counts
    if not servers:
        return None
    tools = "1 tool" if mcp_tools == 1 else f"{mcp_tools} tools"
    return Segment("mcp", f"mcp {servers} · {tools}", BADGE_STYLE, "right", 40)


def _warnings(state: StatusState) -> Segment | None:
    if not state.warnings:
        return None
    return Segment(
        "warnings",
        f"⚠ {len(state.warnings)}",
        WARNING_STYLE,
        "right",
        95,
        action="warnings",
    )


def _version(state: StatusState) -> Segment | None:
    if not state.version:
        return None
    return Segment("version", state.version, VERSION_STYLE, "right", 10)


SEGMENT_BUILDERS: tuple[Callable[[StatusState], Segment | None], ...] = (
    _model,
    _context,
    _yolo,
    _debug,
    _skills,
    _tools,
    _mcp,
    _warnings,
    _version,
)


def build_segments(state: StatusState) -> list[Segment]:
    segments = []
    for builder in SEGMENT_BUILDERS:
        segment = builder(state)
        if segment is not None:
            segments.append(segment)
    return segments


def _group_width(segments: list[Segment]) -> int:
    if not segments:
        return 0
    badges = sum(segment.badge().cell_length for segment in segments)
    return badges + len(segments) - 1


def _fits(segments: list[Segment], width: int) -> bool:
    left = [segment for segment in segments if segment.side == "left"]
    right = [segment for segment in segments if segment.side == "right"]
    gap = 1 if left and right else 0
    return _group_width(left) + gap + _group_width(right) <= width


def layout_segments(
    segments: list[Segment], width: int
) -> tuple[Content, list[tuple[int, int, Segment]]]:
    """The bar at `width` cells, and the cell range each shown segment covers.

    The lowest-priority segment is dropped until the rest fit; on a tie the
    later one goes first. Right-side segments are aligned to the right edge.
    """
    kept = list(segments)
    while kept and not _fits(kept, width):
        lowest = min(reversed(kept), key=lambda segment: segment.priority)
        kept.remove(lowest)
    left = [segment for segment in kept if segment.side == "left"]
    right = [segment for segment in kept if segment.side == "right"]
    pieces: list[Content] = []
    hits: list[tuple[int, int, Segment]] = []
    column = 0

    def place(group: list[Segment]) -> None:
        nonlocal column
        for index, segment in enumerate(group):
            if index:
                pieces.append(Content(" "))
                column += 1
            badge = segment.badge()
            hits.append((column, column + badge.cell_length, segment))
            pieces.append(badge)
            column += badge.cell_length

    place(left)
    fill = width - _group_width(left) - _group_width(right)
    if right and fill > 0:
        pieces.append(Content(" " * fill))
        column += fill
    place(right)
    return Content.assemble(*pieces), hits
