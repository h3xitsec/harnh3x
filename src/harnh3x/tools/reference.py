import urllib.parse
from pathlib import Path

from harnh3x.config import SEARCH_MAX_RESULTS, WIKIPEDIA_LANG
from harnh3x.tools.http import get_json
from harnh3x.tools.page import strip_html
from harnh3x.tools.paths import cap_result
from harnh3x.tools.search import _search_limit

SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "wikipedia_search",
            "description": "Search Wikipedia article titles for a topic",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Topic to look up"},
                    "max_results": {
                        "type": "integer",
                        "description": f"How many results to return (default 5, max {SEARCH_MAX_RESULTS})",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wikipedia_summary",
            "description": "Return the summary of a Wikipedia article by its exact title",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Article title"},
                },
                "required": ["title"],
            },
        },
    },
]

WIKIPEDIA_API = "https://{lang}.wikipedia.org/w/api.php"
WIKIPEDIA_SUMMARY = "https://{lang}.wikipedia.org/api/rest_v1/page/summary/{title}"
WIKIPEDIA_PAGE = "https://{lang}.wikipedia.org/wiki/{title}"


def _wiki_slug(title: str) -> str:
    return urllib.parse.quote(title.strip().replace(" ", "_"), safe="")


def wikipedia_search(workspace: Path, args: dict) -> str:
    query = args.get("query", "")
    if not isinstance(query, str) or not query.strip():
        return "Error: query is required"
    limit = _search_limit(args.get("max_results"))
    data = get_json(
        WIKIPEDIA_API.format(lang=WIKIPEDIA_LANG),
        {
            "action": "query",
            "list": "search",
            "format": "json",
            "utf8": 1,
            "srsearch": query.strip(),
            "srlimit": limit,
        },
    )
    hits = []
    if isinstance(data, dict):
        hits = data.get("query", {}).get("search", []) or []
    rows = []
    for index, hit in enumerate(hits[:limit], start=1):
        if not isinstance(hit, dict):
            continue
        title = str(hit.get("title", "")).strip()
        if not title:
            continue
        snippet = strip_html(str(hit.get("snippet", ""))).replace("\n", " ")
        page = WIKIPEDIA_PAGE.format(lang=WIKIPEDIA_LANG, title=_wiki_slug(title))
        rows.append(f"{index}. {title}\n   url: {page}\n   snippet: {snippet}")
    if not rows:
        return "Error: no results"
    rows.append(
        "\nCall wikipedia_summary with one of these titles to read its summary."
    )
    return cap_result("\n".join(rows))


def wikipedia_summary(workspace: Path, args: dict) -> str:
    title = args.get("title", "")
    if not isinstance(title, str) or not title.strip():
        return "Error: title is required"
    url = WIKIPEDIA_SUMMARY.format(lang=WIKIPEDIA_LANG, title=_wiki_slug(title))
    try:
        data = get_json(url)
    except ValueError as exc:
        if "404" in str(exc):
            return f"Error: no Wikipedia article named '{title.strip()}'"
        raise
    if not isinstance(data, dict):
        return "Error: unexpected response"
    extract = str(data.get("extract") or "").strip()
    if not extract:
        return f"Error: no summary for '{title.strip()}'"
    page = ""
    urls = data.get("content_urls")
    if isinstance(urls, dict):
        desktop = urls.get("desktop")
        if isinstance(desktop, dict):
            page = str(desktop.get("page") or "")
    page = page or WIKIPEDIA_PAGE.format(lang=WIKIPEDIA_LANG, title=_wiki_slug(title))
    description = str(data.get("description") or "").strip()
    kind = str(data.get("type") or "")
    note = ""
    if kind == "disambiguation":
        note = "\nnote: this title is a disambiguation page; pick a more specific title"
    return cap_result(
        f"title: {data.get('title', title.strip())}\n"
        f"description: {description}\n"
        f"url: {page}{note}\n"
        f"summary:\n{extract}"
    )


FNS = {
    "wikipedia_search": wikipedia_search,
    "wikipedia_summary": wikipedia_summary,
}
