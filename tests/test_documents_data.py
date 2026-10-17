"""PDF, DOCX, CSV, and JSON tools."""

import io
import json
import zipfile

from helpers import _pdf_bytes
from pypdf import PdfWriter

from harnh3x import tools
from harnh3x.config import MAX_TOOL_RESULT, PDF_MAX_PAGES
from harnh3x.tools import execute_tool


def test_read_pdf_pages_through_the_document(tmp_path):
    pages = [f"Page {n} text" for n in range(1, PDF_MAX_PAGES + 3)]
    (tmp_path / "doc.pdf").write_bytes(_pdf_bytes(pages))

    first = execute_tool("read_pdf", {"path": "doc.pdf"}, tmp_path)
    assert f"pages: {PDF_MAX_PAGES + 2}" in first
    assert f"showing: 1-{PDF_MAX_PAGES}" in first
    assert "--- page 1 ---\nPage 1 text" in first
    assert f"Page {PDF_MAX_PAGES + 1} text" not in first
    assert f"call read_pdf with first_page={PDF_MAX_PAGES + 1}" in first

    rest = execute_tool(
        "read_pdf", {"path": "doc.pdf", "first_page": PDF_MAX_PAGES + 1}, tmp_path
    )
    assert f"showing: {PDF_MAX_PAGES + 1}-{PDF_MAX_PAGES + 2}" in rest
    assert f"Page {PDF_MAX_PAGES + 2} text" in rest
    assert "remain" not in rest

    one = execute_tool(
        "read_pdf", {"path": "doc.pdf", "first_page": 2, "last_page": 2}, tmp_path
    )
    assert "showing: 2-2" in one
    assert "Page 2 text" in one
    assert "Page 3 text" not in one

    assert execute_tool(
        "read_pdf", {"path": "doc.pdf", "first_page": 99}, tmp_path
    ) == (f"Error: first_page 99 is past the last page ({PDF_MAX_PAGES + 2})")
    assert (
        execute_tool(
            "read_pdf", {"path": "doc.pdf", "first_page": 3, "last_page": 2}, tmp_path
        )
        == "Error: last_page must not be before first_page"
    )
    (tmp_path / "bad.pdf").write_bytes(b"not a pdf")
    assert execute_tool("read_pdf", {"path": "bad.pdf"}, tmp_path).startswith(
        "Error: could not read PDF"
    )
    assert "outside workspace" in execute_tool(
        "read_pdf", {"path": "../x.pdf"}, tmp_path
    )


def test_read_pdf_rejects_encrypted_and_empty_documents(tmp_path):
    empty = io.BytesIO()
    PdfWriter().write(empty)
    (tmp_path / "empty.pdf").write_bytes(empty.getvalue())
    assert (
        execute_tool("read_pdf", {"path": "empty.pdf"}, tmp_path)
        == "Error: PDF has no pages"
    )

    locked = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.encrypt("secret")
    writer.write(locked)
    (tmp_path / "locked.pdf").write_bytes(locked.getvalue())
    assert (
        execute_tool("read_pdf", {"path": "locked.pdf"}, tmp_path)
        == "Error: PDF is encrypted"
    )


def _docx_bytes(paragraphs):
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    document = f'<?xml version="1.0"?><w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>'
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", document)
    return out.getvalue()


def _docx_document(body: str) -> bytes:
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    document = (
        f'<?xml version="1.0"?><w:document xmlns:w="{ns}">'
        f"<w:body>{body}</w:body></w:document>"
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", document)
    return out.getvalue()


def test_read_docx_keeps_tabs_and_breaks_and_rejects_empty_text(tmp_path):
    body = (
        "<w:p><w:r><w:t>Hello</w:t><w:tab/><w:t>there</w:t>"
        "<w:br/><w:t>next</w:t></w:r></w:p>"
    )
    (tmp_path / "memo.docx").write_bytes(_docx_document(body))

    result = execute_tool("read_docx", {"path": "memo.docx"}, tmp_path)

    assert result == "file: memo.docx\nparagraphs: 1\ntext:\nHello\tthere\nnext"
    (tmp_path / "empty.docx").write_bytes(_docx_document("<w:p></w:p>"))
    assert (
        execute_tool("read_docx", {"path": "empty.docx"}, tmp_path)
        == "Error: no text extracted"
    )


def test_read_docx_returns_paragraphs(tmp_path):
    (tmp_path / "memo.docx").write_bytes(_docx_bytes(["Title line", "Body text here"]))

    result = execute_tool("read_docx", {"path": "memo.docx"}, tmp_path)
    assert result == "file: memo.docx\nparagraphs: 2\ntext:\nTitle line\nBody text here"

    (tmp_path / "bad.docx").write_bytes(b"zip? no")
    assert (
        execute_tool("read_docx", {"path": "bad.docx"}, tmp_path)
        == "Error: not a .docx file"
    )
    assert "outside workspace" in execute_tool(
        "read_docx", {"path": "../m.docx"}, tmp_path
    )


def test_read_docx_truncation_saves_a_text_copy(tmp_path):
    paragraphs = [f"Paragraph {n} " + "word " * 40 for n in range(100)]
    (tmp_path / "long.docx").write_bytes(_docx_bytes(paragraphs))

    result = execute_tool("read_docx", {"path": "long.docx"}, tmp_path)

    assert len(result) <= MAX_TOOL_RESULT
    assert "full text is in long.txt" in result
    assert (tmp_path / "long.txt").read_text().startswith("Paragraph 0")


def test_read_docx_refuses_an_oversized_document_part(tmp_path, monkeypatch):
    monkeypatch.setattr(tools.documents, "FETCH_MAX_BYTES", 2_000)
    # The member size from the zip directory is checked before any bytes
    # are inflated.
    (tmp_path / "bomb.docx").write_bytes(_docx_bytes(["x" * 5_000]))

    result = execute_tool("read_docx", {"path": "bomb.docx"}, tmp_path)

    assert result.startswith("Error: document.xml is ")
    assert "larger than the 2000 byte limit" in result
    assert not (tmp_path / "bomb.txt").exists()

    (tmp_path / "ok.docx").write_bytes(_docx_bytes(["fits"]))
    assert "fits" in execute_tool("read_docx", {"path": "ok.docx"}, tmp_path)


def test_inspect_csv_describes_columns_and_previews_rows(tmp_path):
    (tmp_path / "sales.csv").write_text(
        "city;amount;note\nParis;10;\nLyon;20;late\nNice;;\n"
    )

    result = execute_tool("inspect_csv", {"path": "sales.csv", "rows": 2}, tmp_path)

    assert "file: sales.csv" in result
    assert "delimiter: ';'" in result
    assert "rows: 3 (excluding header)" in result
    assert "columns: 3" in result
    assert "1. city (filled 3/3)" in result
    assert "2. amount (filled 2/3)" in result
    assert "3. note (filled 1/3)" in result
    assert "preview (first 2 rows):" in result
    assert "  Paris | 10 | " in result
    assert "Nice" not in result

    forced = execute_tool(
        "inspect_csv", {"path": "sales.csv", "delimiter": ","}, tmp_path
    )
    assert "columns: 1" in forced
    assert (
        execute_tool("inspect_csv", {"path": "sales.csv", "delimiter": ";;"}, tmp_path)
        == "Error: delimiter must be a single character"
    )
    (tmp_path / "empty.csv").write_text("")
    assert (
        execute_tool("inspect_csv", {"path": "empty.csv"}, tmp_path)
        == "Error: empty file"
    )


def test_inspect_json_describes_structure(tmp_path):
    data = {
        "items": [{"id": 1, "name": "a", "tags": ["x"]}],
        "count": 1,
        "ok": False,
        "none": None,
    }
    (tmp_path / "data.json").write_text(json.dumps(data))

    result = execute_tool("inspect_json", {"path": "data.json"}, tmp_path)

    assert result.splitlines()[0] == "file: data.json"
    assert "object with 4 keys" in result
    assert "items: array of 1" in result
    assert "[0]: object with 3 keys" in result
    assert "id: number 1" in result
    assert "name: string 'a'" in result
    assert "tags: array of 1" in result
    assert "count: number 1" in result
    assert "ok: boolean false" in result
    assert "none: null" in result

    shallow = execute_tool(
        "inspect_json", {"path": "data.json", "max_depth": 1}, tmp_path
    )
    assert "items: array of 1" in shallow
    assert "[0]:" not in shallow

    (tmp_path / "bad.json").write_text("{nope")
    assert execute_tool("inspect_json", {"path": "bad.json"}, tmp_path).startswith(
        "Error: invalid JSON"
    )
