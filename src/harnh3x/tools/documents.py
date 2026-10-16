import io
import zipfile
from pathlib import Path

import pypdf

from harnh3x.config import FETCH_MAX_BYTES, MAX_TOOL_RESULT, PDF_MAX_PAGES
from harnh3x.tools.paths import (
    _path_param,
    _positive_int,
    _readable_file,
    _relative,
    cap_result,
)
from harnh3x.tools.xml import _parse_xml

SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_pdf",
            "description": f"Extract text from a PDF in the workspace, up to {PDF_MAX_PAGES} pages per call",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": _path_param("Workspace path to the PDF"),
                    "first_page": {
                        "type": "integer",
                        "description": "First page to read, 1-based (default 1)",
                    },
                    "last_page": {
                        "type": "integer",
                        "description": "Last page to read (default: first_page plus the page cap)",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_docx",
            "description": "Extract the paragraphs of a Word .docx file in the workspace",
            "parameters": {
                "type": "object",
                "properties": {"path": _path_param("Workspace path to the .docx file")},
                "required": ["path"],
            },
        },
    },
]


def _pdf_reader(data: bytes) -> pypdf.PdfReader:
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                unlocked = reader.decrypt("")
            except Exception as exc:  # pypdf raises several types here
                raise ValueError("PDF is encrypted") from exc
            if not unlocked:
                raise ValueError("PDF is encrypted")
        len(reader.pages)
    except ValueError:
        raise
    except pypdf.errors.PyPdfError as exc:
        raise ValueError(f"could not read PDF: {exc}") from exc
    return reader


def _pdf_page_text(reader: pypdf.PdfReader, index: int) -> str:
    try:
        return (reader.pages[index].extract_text() or "").strip()
    except pypdf.errors.PyPdfError:
        return ""


def _pdf_title(reader: pypdf.PdfReader) -> str:
    try:
        meta = reader.metadata
    except pypdf.errors.PyPdfError:
        return ""
    title = getattr(meta, "title", None) if meta is not None else None
    return str(title).strip() if title else ""


def read_pdf(workspace: Path, args: dict) -> str:
    path = _readable_file(workspace, args.get("path", ""))
    reader = _pdf_reader(path.read_bytes())
    total = len(reader.pages)
    if total == 0:
        return "Error: PDF has no pages"
    first = _positive_int(args.get("first_page"), "first_page", 1)
    if first > total:
        return f"Error: first_page {first} is past the last page ({total})"
    default_last = min(total, first + PDF_MAX_PAGES - 1)
    last = _positive_int(args.get("last_page"), "last_page", default_last)
    if last < first:
        return "Error: last_page must not be before first_page"
    last = min(last, total, first + PDF_MAX_PAGES - 1)
    chunks = []
    for number in range(first, last + 1):
        chunks.append(f"--- page {number} ---\n{_pdf_page_text(reader, number - 1)}")
    shown = _relative(workspace, path)
    header = f"file: {shown}\ntitle: {_pdf_title(reader)}\npages: {total}\nshowing: {first}-{last}\n"
    hint = (
        f"call read_pdf with first_page={last + 1} to continue"
        if last < total
        else "narrow the page range"
    )
    body = cap_result(
        "\n\n".join(chunks), max(MAX_TOOL_RESULT - len(header), 200), hint=hint
    )
    if last < total and "...[truncated" not in body:
        body += f"\n...[pages {last + 1}-{total} remain; call read_pdf with first_page={last + 1}]"
    return header + body


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx_paragraphs(path: Path) -> list[str]:
    try:
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > FETCH_MAX_BYTES:
                raise ValueError(
                    f"document.xml is {info.file_size} bytes, "
                    f"larger than the {FETCH_MAX_BYTES} byte limit"
                )
            document = archive.read(info)
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ValueError("not a .docx file") from exc
    root = _parse_xml(document)
    paragraphs = []
    for paragraph in root.iter(_W + "p"):
        parts = []
        for node in paragraph.iter():
            if node.tag == _W + "t":
                parts.append(node.text or "")
            elif node.tag == _W + "tab":
                parts.append("\t")
            elif node.tag in {_W + "br", _W + "cr"}:
                parts.append("\n")
        paragraphs.append("".join(parts))
    return paragraphs


def read_docx(workspace: Path, args: dict) -> str:
    path = _readable_file(workspace, args.get("path", ""))
    paragraphs = _docx_paragraphs(path)
    text = "\n".join(paragraphs).strip()
    if not text:
        return "Error: no text extracted"
    shown = _relative(workspace, path)
    header = f"file: {shown}\nparagraphs: {len(paragraphs)}\ntext:\n"
    if len(header) + len(text) > MAX_TOOL_RESULT:
        text_path = path.with_suffix(".txt")
        text_path.write_text(text, encoding="utf-8")
        hint = f"full text is in {_relative(workspace, text_path)}; use read_file with offset to continue"
    else:
        hint = ""
    return header + cap_result(text, max(MAX_TOOL_RESULT - len(header), 200), hint=hint)


FNS = {
    "read_pdf": read_pdf,
    "read_docx": read_docx,
}
