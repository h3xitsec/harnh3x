import html
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import trafilatura

from harnh3x.config import MAX_TOOL_RESULT
from harnh3x.tools.documents import _pdf_page_text, _pdf_reader, _pdf_title
from harnh3x.tools.http import HTML_TYPES, PDF_TYPE, _write_fetched
from harnh3x.tools.paths import _path_param, cap_result, resolve_readable

SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_url",
            "description": (
                "Fetch a web page, PDF, or text URL and return its readable text in one "
                "step. Saves the raw file and the extracted text in the workspace."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "http or https URL"},
                    "mode": {
                        "type": "string",
                        "description": "article (default) keeps the main content; full keeps all page text",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "parse_html",
            "description": "Read an HTML file already saved in the workspace and return its title and text. Does not download.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": _path_param(
                        "Workspace path to the HTML file saved by fetch_url"
                    ),
                    "mode": {
                        "type": "string",
                        "description": "article (default) keeps the main content; full keeps all page text",
                    },
                },
                "required": ["path"],
            },
        },
    },
]


class _TextParser(HTMLParser):
    _SKIP = frozenset({"script", "style", "noscript", "template"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def strip_html(text: str) -> str:
    parser = _TextParser()
    parser.feed(text)
    parser.close()
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    return "\n".join(line for line in lines if line)


def _html_title(document: bytes) -> str:
    match = re.search(
        rb"<title[^>]*>(.*?)</title>", document, re.IGNORECASE | re.DOTALL
    )
    if not match:
        return ""
    title = html.unescape(match.group(1).decode("utf-8", errors="replace"))
    return " ".join(title.split())


def _article_field(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        return ""
    return value.strip()


def _trafilatura_json(document: bytes, **options) -> dict | None:
    raw = trafilatura.extract(
        document, output_format="json", with_metadata=True, **options
    )
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not _article_field(data, "text"):
        return None
    return data


def _page_mode(raw) -> str:
    if raw is None or raw == "":
        return "article"
    if raw not in {"article", "full"}:
        raise ValueError("mode must be 'article' or 'full'")
    return raw


def extract_page(document: bytes, mode: str = "article") -> dict | None:
    if mode == "article":
        data = _trafilatura_json(document)
        if data is None:
            data = _trafilatura_json(
                document, favor_recall=True, include_tables=True, include_comments=False
            )
        if data is not None:
            return data
    text = strip_html(document.decode("utf-8", errors="replace"))
    if not text:
        return None
    return {"title": _html_title(document), "author": "", "date": "", "text": text}


def _format_article(data: dict) -> str:
    return (
        f"title: {_article_field(data, 'title')}\n"
        f"author: {_article_field(data, 'author')}\n"
        f"date: {_article_field(data, 'date')}\n"
        f"text:\n{_article_field(data, 'text')}"
    )


def parse_html(workspace: Path, args: dict) -> str:
    workspace = workspace.resolve()
    path_arg = args.get("path")
    path = resolve_readable(workspace, path_arg if isinstance(path_arg, str) else "")
    if not path.exists():
        return "Error: not found"
    if path.is_dir():
        return "Error: is a directory"
    mode = _page_mode(args.get("mode"))
    data = extract_page(path.read_bytes(), mode)
    if data is None:
        return "Error: no article text extracted"
    return cap_result(_format_article(data))


def read_url(workspace: Path, args: dict) -> str:
    workspace = workspace.resolve()
    url = args.get("url", "")
    if not isinstance(url, str):
        url = ""
    mode = _page_mode(args.get("mode"))
    path, _status, media = _write_fetched(workspace, url, None)
    body = path.read_bytes()
    saved = path.relative_to(workspace)
    title = author = date = ""
    if media in HTML_TYPES:
        data = extract_page(body, mode)
        if data is None:
            return f"Error: no text extracted; the raw page is saved at {saved}"
        title = _article_field(data, "title")
        author = _article_field(data, "author")
        date = _article_field(data, "date")
        text = _article_field(data, "text")
    elif media == PDF_TYPE:
        reader = _pdf_reader(body)
        title = _pdf_title(reader)
        pages = [_pdf_page_text(reader, i) for i in range(len(reader.pages))]
        text = "\n\n".join(page for page in pages if page)
        if not text:
            return f"Error: no text extracted; the PDF is saved at {saved}"
    else:
        text = body.decode("utf-8", errors="replace").strip()
        if not text:
            return f"Error: empty response; the file is saved at {saved}"
    text_path = path if path.suffix == ".txt" else path.with_suffix(".txt")
    text_path.write_text(text, encoding="utf-8")
    text_file = text_path.relative_to(workspace)
    header = (
        f"url: {url.strip()}\n"
        f"title: {title}\n"
        f"author: {author}\n"
        f"date: {date}\n"
        f"saved: {saved}\n"
        f"text_file: {text_file}\n"
        "text:\n"
    )
    return header + cap_result(
        text,
        max(MAX_TOOL_RESULT - len(header), 200),
        hint=f"full text is in {text_file}; use read_file with offset to continue",
    )


FNS = {
    "read_url": read_url,
    "parse_html": parse_html,
}
