from pathlib import Path

from harnh3x import config
from harnh3x.config import MEMORY_FILE
from harnh3x.tools.paths import cap_result

SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": f"Save a fact under a key in {MEMORY_FILE} so later sessions can recall it",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Short name for the memory, for example 'user timezone'",
                    },
                    "text": {"type": "string", "description": "What to remember"},
                },
                "required": ["key", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall",
            "description": f"Return saved memories from {MEMORY_FILE}, optionally filtered by a search word",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Word or phrase to match in keys and text; omit for everything",
                    },
                },
            },
        },
    },
]


def _memory_path() -> Path:
    return Path(config.BASE_DIR).expanduser().resolve() / MEMORY_FILE


def _read_memory(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    if not path.exists() or path.is_dir():
        return entries
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return entries
    key = None
    buffer: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if key is not None:
                entries[key] = "\n".join(buffer).strip()
            key = line[3:].strip()
            buffer = []
        elif key is not None:
            buffer.append(line)
    if key is not None:
        entries[key] = "\n".join(buffer).strip()
    return entries


def _write_memory(path: Path, entries: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    blocks = ["# Memory", ""]
    for key, text in entries.items():
        blocks.append(f"## {key}")
        blocks.append(text)
        blocks.append("")
    path.write_text("\n".join(blocks), encoding="utf-8")


def memory_keys() -> list[str]:
    return list(_read_memory(_memory_path()))


def _format_memories(entries: dict[str, str]) -> str:
    return "\n\n".join(f"## {key}\n{text}" for key, text in entries.items())


def remember(workspace: Path, args: dict) -> str:
    key = args.get("key")
    text = args.get("text")
    if not isinstance(key, str) or not key.strip():
        return "Error: key is required"
    if not isinstance(text, str) or not text.strip():
        return "Error: text is required"
    key = " ".join(key.split())
    # Markdown headings would be read back as memory keys, so flatten them.
    text = "\n".join(
        line.lstrip("#").strip() if line.startswith("#") else line
        for line in text.strip().splitlines()
    ).strip()
    path = _memory_path()
    entries = _read_memory(path)
    verb = "Updated" if key in entries else "Remembered"
    entries[key] = text
    _write_memory(path, entries)
    return f"{verb} '{key}'. {len(entries)} memories stored in {MEMORY_FILE}."


def recall(workspace: Path, args: dict) -> str:
    entries = _read_memory(_memory_path())
    if not entries:
        return "Memory is empty."
    query = args.get("query")
    if query is None or (isinstance(query, str) and not query.strip()):
        return cap_result(_format_memories(entries))
    if not isinstance(query, str):
        return "Error: query must be a string"
    needle = query.strip().lower()
    matched = {
        key: text
        for key, text in entries.items()
        if needle in key.lower() or needle in text.lower()
    }
    if not matched:
        return f"No memories match '{query.strip()}'."
    return cap_result(_format_memories(matched))


FNS = {
    "remember": remember,
    "recall": recall,
}
