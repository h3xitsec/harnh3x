import csv
import json
from pathlib import Path

from harnh3x.config import PREVIEW_ROWS
from harnh3x.tools.paths import (
    _count_arg,
    _path_param,
    _readable_file,
    _relative,
    cap_result,
)

SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "inspect_csv",
            "description": "Describe a CSV file: delimiter, columns, row count, and the first rows",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": _path_param("Workspace path to the CSV file"),
                    "rows": {
                        "type": "integer",
                        "description": f"How many rows to preview (default {PREVIEW_ROWS}, max 50)",
                    },
                    "delimiter": {
                        "type": "string",
                        "description": "Field delimiter (default: detected)",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_json",
            "description": "Describe the structure of a JSON file: keys, array lengths, and sample values",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": _path_param("Workspace path to the JSON file"),
                    "max_depth": {
                        "type": "integer",
                        "description": "How many nesting levels to describe (default 3, max 10)",
                    },
                },
                "required": ["path"],
            },
        },
    },
]


def _csv_dialect(sample: str, delimiter):
    if delimiter is not None:
        if not isinstance(delimiter, str) or len(delimiter) != 1:
            raise ValueError("delimiter must be a single character")

        class _Given(csv.excel):
            pass

        _Given.delimiter = delimiter
        return _Given
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        return csv.excel


def inspect_csv(workspace: Path, args: dict) -> str:
    path = _readable_file(workspace, args.get("path", ""))
    preview = _count_arg(args.get("rows"), "rows", PREVIEW_ROWS, 50)
    with path.open(newline="", encoding="utf-8") as handle:
        dialect = _csv_dialect(handle.read(8192), args.get("delimiter"))
        handle.seek(0)
        reader = csv.reader(handle, dialect)
        header = next(reader, None)
        if header is None:
            return "Error: empty file"
        rows = []
        count = 0
        filled = [0] * len(header)
        for row in reader:
            count += 1
            for index, value in enumerate(row[: len(header)]):
                if value.strip():
                    filled[index] += 1
            if len(rows) < preview:
                rows.append(row)
    lines = [
        f"file: {_relative(workspace, path)}",
        f"delimiter: {dialect.delimiter!r}",
        f"rows: {count} (excluding header)",
        f"columns: {len(header)}",
    ]
    for index, name in enumerate(header):
        lines.append(f"  {index + 1}. {name} (filled {filled[index]}/{count})")
    lines.append(f"preview (first {len(rows)} rows):")
    lines.append("  " + " | ".join(header))
    for row in rows:
        lines.append("  " + " | ".join(cell[:60] for cell in row))
    return cap_result("\n".join(lines))


def _describe_json(value, depth: int, max_depth: int, indent: str) -> list[str]:
    if isinstance(value, dict):
        lines = [f"{indent}object with {len(value)} keys"]
        if depth < max_depth:
            for index, (key, item) in enumerate(value.items()):
                if index >= 30:
                    lines.append(f"{indent}  ...[{len(value) - 30} more keys]")
                    break
                described = _describe_json(item, depth + 1, max_depth, indent + "    ")
                lines.append(f"{indent}  {key}: {described[0].strip()}")
                lines.extend(described[1:])
        return lines
    if isinstance(value, list):
        lines = [f"{indent}array of {len(value)}"]
        if value and depth < max_depth:
            described = _describe_json(value[0], depth + 1, max_depth, indent + "    ")
            lines.append(f"{indent}  [0]: {described[0].strip()}")
            lines.extend(described[1:])
        return lines
    if isinstance(value, str):
        shown = value if len(value) <= 60 else value[:57] + "..."
        return [f"{indent}string {shown!r}"]
    if isinstance(value, bool):
        return [f"{indent}boolean {str(value).lower()}"]
    if value is None:
        return [f"{indent}null"]
    return [f"{indent}number {value}"]


def inspect_json(workspace: Path, args: dict) -> str:
    path = _readable_file(workspace, args.get("path", ""))
    max_depth = _count_arg(args.get("max_depth"), "max_depth", 3, 10)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return f"Error: invalid JSON: {exc}"
    lines = [f"file: {_relative(workspace, path)}"]
    lines.extend(_describe_json(data, 0, max_depth, ""))
    return cap_result(
        "\n".join(lines), hint="raise max_depth on a narrower file, or use run_python"
    )


FNS = {
    "inspect_csv": inspect_csv,
    "inspect_json": inspect_json,
}
