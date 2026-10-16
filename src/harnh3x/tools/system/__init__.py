"""system_info: a read-only inventory of the host, taken outside the seatbelt."""

import sys
from pathlib import Path

from harnh3x.tools.paths import cap_result
from harnh3x.tools.system.darwin import _darwin
from harnh3x.tools.system.linux import _linux
from harnh3x.tools.system.probe import HostProbe

__all__ = ["FNS", "SCHEMAS", "SECTIONS", "HostProbe", "system_info"]

SECTIONS = ("os", "machine", "cpu", "memory", "storage", "hardware")
_HINT = (
    "call system_info again with one sections value: "
    "os, machine, cpu, memory, storage, or hardware"
)


def system_info(workspace: Path, args: dict, probe: HostProbe | None = None) -> str:
    del workspace
    if sys.platform not in {"darwin", "linux"}:
        return "Error: system_info supports Linux and macOS only"
    chosen = _choose(args.get("sections"))
    if isinstance(chosen, str):
        return chosen
    host = probe if probe is not None else HostProbe()
    notes: list[str] = []
    if sys.platform == "darwin":
        parts = _darwin(host, notes)
    else:
        parts = _linux(host, notes)
    text = _render(parts, chosen, notes)
    return cap_result(text, hint=_HINT)


SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "system_info",
            "description": (
                "Read OS, machine, CPU, memory, storage, and attached hardware "
                "for this Linux or macOS host"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sections": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(SECTIONS)},
                        "description": (
                            "Sections to return. Omit for all of: "
                            + ", ".join(SECTIONS)
                        ),
                    }
                },
            },
        },
    }
]

FNS = {"system_info": system_info}


def _choose(raw: object) -> tuple[str, ...] | str:
    if raw is None:
        return SECTIONS
    if isinstance(raw, str) or not isinstance(raw, list):
        return "Error: sections must be a list"
    if not raw:
        return "Error: sections must be a non-empty list"
    chosen: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            return "Error: sections must be a list of strings"
        if item not in SECTIONS:
            return f"Error: unknown section '{item}'"
        if item not in chosen:
            chosen.append(item)
    return tuple(chosen)


def _render(
    parts: dict[str, list[str]], sections: tuple[str, ...], notes: list[str]
) -> str:
    titles = {
        "os": "OS",
        "machine": "Machine",
        "cpu": "CPU",
        "memory": "Memory",
        "storage": "Storage",
        "hardware": "Hardware",
    }
    blocks = []
    for name in sections:
        rows = parts.get(name) or ["  unavailable"]
        blocks.append(titles[name] + "\n" + "\n".join(rows))
    if notes:
        listed = "\n".join(f"  - {note}" for note in notes)
        blocks.append(f"Notes\n{listed}")
    return "\n\n".join(blocks)
