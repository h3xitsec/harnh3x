"""Fixtures and fakes shared by more than one test module."""

import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from harnh3x import config
from harnh3x.config import TRANSCRIPT_FILE


def _banner_text(app) -> str:
    """Header lines, skipping the copy control and hidden rows."""
    lines = []
    for widget_id in (
        "#banner-title",
        "#workspace-path",
        "#banner-project",
        "#banner-coding",
    ):
        found = list(app.query(widget_id))
        if not found or "hidden" in found[0].classes:
            continue
        text = str(found[0].content)
        if text:
            lines.append(text)
    return "\n".join(lines)


def _delta(**fields):
    usage = fields.pop("usage", None)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(**fields))],
        usage=usage,
    )


def _unreachable(kind: str):
    import httpx2
    from openai import APIConnectionError, APITimeoutError

    error = APITimeoutError if kind == "timeout" else APIConnectionError
    return error(
        request=httpx2.Request("POST", "http://127.0.0.1:9/v1/chat/completions")
    )


def _write_transcript(workspace: Path, model: str, messages: list) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    payload = {"model": model, "messages": messages}
    (workspace / TRANSCRIPT_FILE).write_text(json.dumps(payload), encoding="utf-8")


def _past_workspace(root: Path) -> Path:
    workspace = root / "workspaces" / "abcdef01-0000-0000-0000-000000000001"
    _write_transcript(
        workspace,
        "saved-model",
        [
            {"role": "system", "content": "stale"},
            {"role": "user", "content": "fix the notes"},
            {
                "role": "assistant",
                "content": "",
                "reasoning": "alpha\nbeta\ngamma\ndelta",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "load_skill",
                            "arguments": '{"name": "file-ops"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "loaded"},
            {"role": "assistant", "content": "### **Done**\n- notes updated"},
        ],
    )
    return workspace


def _prior_turn(session) -> None:
    session.messages.append({"role": "user", "content": "earlier"})
    session.messages.append({"role": "assistant", "content": "did the earlier work"})


FILE_OPS_LOADED = (
    "[loaded: read_file, write_file, edit_file, append_file, "
    "delete_file, move_file, list_files, search_files, glob, grep]"
)


def _tool_names(schemas):
    return [tool["function"]["name"] for tool in schemas]


def _load_cli():
    path = Path(__file__).resolve().parents[1] / "src" / "harnh3x" / "cli.py"
    spec = importlib.util.spec_from_file_location("harnh3x_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.list_models = lambda: [module.MODEL]
    return module


def _export_messages() -> list[dict]:
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first question"},
        {
            "role": "assistant",
            "content": "draft",
            "reasoning": "hidden thought",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "1", "content": "tool body"},
        {
            "role": "assistant",
            "content": "final alpha",
            "reasoning": "more thought",
        },
        {"role": "user", "content": "second question"},
        {
            "role": "assistant",
            "content": "beta answer",
            "reasoning": "secret thinking",
        },
    ]


def _pdf_bytes(pages):
    objects = []
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(pages)))
    objects.append("<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>")
    objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, text in enumerate(pages):
        content = f"BT /F1 12 Tf 72 712 Td ({text}) Tj ET"
        objects.append(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>"
        )
        objects.append(f"<< /Length {len(content)} >>\nstream\n{content}\nendstream")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for index, obj in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{index} 0 obj\n{obj}\nendobj\n".encode("latin-1"))
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


_FAKE_MCP = str(Path(__file__).resolve().parent / "fake_mcp.py")


def _mcp_server(name="caido", **kwargs):
    env = dict(kwargs.pop("env", ()))
    return config.McpServer(
        name=name,
        command=kwargs.pop("command", sys.executable),
        args=kwargs.pop("args", (_FAKE_MCP,)),
        env=env,
        **kwargs,
    )
