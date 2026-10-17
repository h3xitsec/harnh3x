import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from harnh3x.config import SEARCH_MAX_RESULTS
from harnh3x.tools.http import HTML_TYPES, USER_AGENT, _open_request, validate_url
from harnh3x.tools.paths import _count_arg, cap_result

SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web and return titles, URLs, and snippets. Snippets are "
                "not answers: follow up with read_url on the results that matter."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {
                        "type": "integer",
                        "description": f"How many results to return (default 5, max {SEARCH_MAX_RESULTS})",
                    },
                    "site": {
                        "type": "string",
                        "description": "Only return results from this domain, for example docs.python.org",
                    },
                    "time": {
                        "type": "string",
                        "enum": ["d", "w", "m", "y"],
                        "description": "Only return recent pages: d (day), w (week), m (month), y (year)",
                    },
                },
                "required": ["query"],
            },
        },
    },
]

SEARCH_URL = "https://html.duckduckgo.com/html/"
SEARCH_DEFAULT_RESULTS = 5
SEARCH_TIME_FILTERS = {"d", "w", "m", "y"}
SEARCH_HINT = (
    "Snippets are not answers. Call read_url on the results that matter "
    "before answering, and cite the URLs you read."
)


def _class_tokens(attrs) -> set[str]:
    for key, value in attrs:
        if key == "class" and isinstance(value, str):
            return set(value.split())
    return set()


def _unwrap_result_url(href: str) -> str:
    if not isinstance(href, str):
        return ""
    href = href.strip()
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlsplit(href)
    host = (parsed.hostname or "").lower()
    if host.endswith("duckduckgo.com"):
        wrapped = urllib.parse.parse_qs(parsed.query).get("uddg")
        if wrapped and wrapped[0].strip():
            return wrapped[0].strip()
    return href


class _SearchResultParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results: list[tuple[str, str, str]] = []
        self._capture = ""
        self._depth = 0
        self._buf: list[str] = []
        self._href = ""
        self._title = ""
        self._url = ""

    def _flush(self, snippet: str = "") -> None:
        title = " ".join(self._title.split())
        url = self._url.strip()
        if title and url:
            self.results.append((title, url, " ".join(snippet.split())))
        self._title = ""
        self._url = ""

    def handle_starttag(self, tag, attrs):
        classes = _class_tokens(attrs)
        if self._capture:
            self._depth += 1
            return
        if tag == "a" and "result__a" in classes:
            self._flush()
            self._capture = "title"
            self._depth = 1
            self._buf = []
            self._href = ""
            for key, value in attrs:
                if key == "href" and isinstance(value, str):
                    self._href = value
                    break
            return
        if "result__snippet" in classes and self._title:
            self._capture = "snippet"
            self._depth = 1
            self._buf = []

    def handle_endtag(self, tag):
        if not self._capture:
            return
        self._depth -= 1
        if self._depth > 0:
            return
        text = "".join(self._buf)
        if self._capture == "title":
            self._title = text
            self._url = _unwrap_result_url(self._href)
        else:
            self._flush(text)
        self._capture = ""
        self._buf = []

    def handle_data(self, data):
        if self._capture:
            self._buf.append(data)

    def close(self):
        super().close()
        if not self._capture:
            self._flush()


def _parse_search_results(html_text: str) -> list[tuple[str, str, str]]:
    parser = _SearchResultParser()
    parser.feed(html_text)
    parser.close()
    return parser.results


def _search_limit(raw) -> int:
    return _count_arg(raw, "max_results", SEARCH_DEFAULT_RESULTS, SEARCH_MAX_RESULTS)


def _search_html(query: str, recency: str | None = None) -> str:
    validate_url(SEARCH_URL)
    fields = {"q": query}
    if recency:
        fields["df"] = recency
    payload = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        SEARCH_URL,
        data=payload,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    status, media, body = _open_request(request)
    if media not in HTML_TYPES:
        raise ValueError(f"unsupported content type '{media or 'unknown'}'")
    if status < 200 or status >= 300:
        raise ValueError(f"http status {status}")
    return body.decode("utf-8", errors="replace")


def _format_search_results(rows: list[tuple[str, str, str]]) -> str:
    blocks = []
    for index, (title, url, snippet) in enumerate(rows, start=1):
        blocks.append(f"{index}. {title}\n   url: {url}\n   snippet: {snippet}")
    return "\n".join(blocks)


def web_search(workspace: Path, args: dict) -> str:
    query = args.get("query", "")
    if not isinstance(query, str) or not query.strip():
        return "Error: query is required"
    limit = _search_limit(args.get("max_results"))
    query = query.strip()
    site = args.get("site")
    if site is not None and site != "":
        if not isinstance(site, str):
            return "Error: site must be a string"
        query = f"{query} site:{site.strip()}"
    recency = args.get("time")
    if recency is not None and recency != "":
        if recency not in SEARCH_TIME_FILTERS:
            return "Error: time must be one of d, w, m, y"
    else:
        recency = None
    rows = []
    for title, url, snippet in _parse_search_results(_search_html(query, recency)):
        try:
            validate_url(url)
        except ValueError:
            continue
        rows.append((title, url, snippet))
        if len(rows) >= limit:
            break
    if not rows:
        return "Error: no results"
    return cap_result(_format_search_results(rows) + "\n\n" + SEARCH_HINT)


FNS = {
    "web_search": web_search,
}
