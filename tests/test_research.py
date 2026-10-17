"""Web search, page reading, feeds, and Wikipedia."""

import re
import urllib.parse

from helpers import _pdf_bytes

from harnh3x import tools
from harnh3x.config import MAX_TOOL_RESULT, PDF_MAX_PAGES
from harnh3x.skills import SkillRegistry
from harnh3x.tools import execute_tool

ARTICLE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<title>City Council Approves Transit Plan</title>
<meta name="author" content="Ada Lovelace">
<meta property="article:published_time" content="2026-03-04">
</head>
<body>
<article>
<h1>City Council Approves Transit Plan</h1>
<p>The council voted Tuesday to fund a new bus line through the harbor district. Residents packed the chamber and spoke for two hours.</p>
</article>
</body>
</html>
"""


def test_fetch_url_rejects_blocked_targets(tmp_path):
    for url in ("file:///etc/passwd", "http://127.0.0.1/", "http://169.254.169.254/"):
        result = execute_tool("fetch_url", {"url": url}, tmp_path)
        assert result.startswith("Error:")
        assert "blocked" in result


def test_fetch_redirect_to_private_host_is_blocked():
    handler = tools.http._CheckedRedirectHandler()
    try:
        handler.redirect_request(
            None, None, 302, "Found", None, "http://127.0.0.1/secret"
        )
    except ValueError as exc:
        assert "blocked" in str(exc)
    else:
        raise AssertionError("redirect was allowed")


def test_fetch_url_saves_html_in_workspace(tmp_path, monkeypatch):
    body = b"<html><title>Hi</title><body><p>Hello</p></body></html>"

    def fake_get(url):
        return 200, "text/html", body

    monkeypatch.setattr(tools.http, "_http_get", fake_get)
    result = execute_tool(
        "fetch_url",
        {"url": "https://1.1.1.1/news", "path": "pages/story.html"},
        tmp_path,
    )

    assert "status: success" in result
    assert "path: pages/story.html" in result
    assert (tmp_path / "pages" / "story.html").read_bytes() == body

    outside = execute_tool(
        "fetch_url",
        {"url": "https://1.1.1.1/news", "path": "../secret.html"},
        tmp_path,
    )
    assert "outside workspace" in outside
    assert not (tmp_path.parent / "secret.html").exists()


def test_fetch_url_rejects_unsupported_types(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tools.http,
        "_http_get",
        lambda url: (200, "image/png", b"\x89PNG"),
    )

    result = execute_tool("fetch_url", {"url": "https://1.1.1.1/pic"}, tmp_path)

    assert result.startswith("Error:")
    assert "unsupported content type" in result
    assert list(tmp_path.iterdir()) == []


def test_fetch_url_saves_json_and_pdf_with_matching_extensions(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tools.http, "_http_get", lambda url: (200, "application/json", b'{"a": 1}')
    )
    as_json = execute_tool("fetch_url", {"url": "https://1.1.1.1/data"}, tmp_path)
    assert "content_type: application/json" in as_json
    assert "path: downloads/1.1.1.1-" in as_json
    assert as_json.rstrip().endswith(".json")

    monkeypatch.setattr(
        tools.http, "_http_get", lambda url: (200, "application/pdf", b"%PDF-1.4")
    )
    as_pdf = execute_tool("fetch_url", {"url": "https://1.1.1.1/paper"}, tmp_path)
    assert as_pdf.rstrip().endswith(".pdf")


def test_parse_html_reads_saved_html_and_does_not_download(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_get(url):
        calls["n"] += 1
        return 200, "text/html", ARTICLE_HTML.encode()

    monkeypatch.setattr(tools.http, "_http_get", fake_get)
    saved = execute_tool("fetch_url", {"url": "https://1.1.1.1/news"}, tmp_path)
    path_line = next(line for line in saved.splitlines() if line.startswith("path: "))
    relative = path_line.removeprefix("path: ")

    result = execute_tool("parse_html", {"path": relative}, tmp_path)

    assert calls["n"] == 1
    assert "title: City Council Approves Transit Plan" in result
    assert "harbor district" in result

    ignored = execute_tool(
        "parse_html",
        {
            "path": "https://thehackernews.com/2026/10/mi5-says-chinas-mss-funded-research.html",
            "url": "https://thehackernews.com/2026/10/mi5-says-chinas-mss-funded-research.html",
        },
        tmp_path,
    )
    assert calls["n"] == 1
    assert ignored.startswith("Error:")
    assert not (tmp_path / "fetch_url").exists()


def test_parse_html_extracts_article(tmp_path):
    (tmp_path / "article.html").write_text(ARTICLE_HTML)

    result = execute_tool("parse_html", {"path": "article.html"}, tmp_path)

    assert "title: City Council Approves Transit Plan" in result
    assert "harbor district" in result
    assert "author: Ada Lovelace" in result


def test_parse_html_errors_for_missing_directory_and_empty(tmp_path):
    assert "Error: not found" in execute_tool(
        "parse_html", {"path": "missing.html"}, tmp_path
    )

    (tmp_path / "pages").mkdir()
    assert "Error: is a directory" in execute_tool(
        "parse_html", {"path": "pages"}, tmp_path
    )

    (tmp_path / "empty.html").write_text("<html><head></head><body></body></html>")
    result = execute_tool("parse_html", {"path": "empty.html"}, tmp_path)
    assert result == "Error: no article text extracted"


def _search_page(rows):
    parts = ["<html><body>"]
    for title, href, snippet in rows:
        parts.append(
            "<div class='result'>"
            f"<a class='result__a' href='{href}'>{title}</a>"
            f"<a class='result__snippet'>{snippet}</a>"
            "</div>"
        )
    parts.append("</body></html>")
    return "".join(parts)


def _ddg_href(url):
    return "https://duckduckgo.com/l/?uddg=" + urllib.parse.quote(url, safe="")


def test_web_search_returns_unwrapped_results(tmp_path, monkeypatch):
    page = _search_page(
        [
            ("Alpha Title", _ddg_href("https://1.1.1.1/alpha"), "Alpha snippet"),
            (
                "Beta Title",
                "//duckduckgo.com/l/?uddg="
                + urllib.parse.quote("https://8.8.8.8/beta", safe=""),
                "Beta snippet",
            ),
            ("Secret", _ddg_href("http://127.0.0.1/secret"), "hidden"),
            ("Bad Scheme", "javascript:alert(1)", "nope"),
            ("Gamma Title", "https://9.9.9.9/gamma", "Gamma snippet"),
        ]
    )
    monkeypatch.setattr(tools.search, "_search_html", lambda query, recency=None: page)

    result = execute_tool(
        "web_search",
        {"query": "transit", "max_results": 2},
        tmp_path,
    )

    assert result == (
        "1. Alpha Title\n"
        "   url: https://1.1.1.1/alpha\n"
        "   snippet: Alpha snippet\n"
        "2. Beta Title\n"
        "   url: https://8.8.8.8/beta\n"
        "   snippet: Beta snippet\n"
        "\n" + tools.search.SEARCH_HINT
    )
    assert "127.0.0.1" not in result
    assert "javascript" not in result
    assert "Gamma" not in result


def test_web_search_caps_results_and_rejects_bad_input(tmp_path, monkeypatch):
    rows = [
        (f"Result {index}", f"https://1.1.1.1/{index}", f"snippet {index}")
        for index in range(1, 11)
    ]
    monkeypatch.setattr(
        tools.search, "_search_html", lambda query, recency=None: _search_page(rows)
    )

    capped = execute_tool(
        "web_search",
        {"query": "transit", "max_results": 100},
        tmp_path,
    )
    assert capped.startswith("1. Result 1\n")
    assert "8. Result 8\n" in capped
    assert "9. Result 9" not in capped

    calls = {"n": 0}

    def fail(query, recency=None):
        calls["n"] += 1
        raise AssertionError("search should not run")

    monkeypatch.setattr(tools.search, "_search_html", fail)
    assert execute_tool("web_search", {}, tmp_path) == "Error: query is required"
    assert execute_tool("web_search", {"query": "  "}, tmp_path) == (
        "Error: query is required"
    )
    assert calls["n"] == 0

    bad_limit = execute_tool(
        "web_search",
        {"query": "transit", "max_results": 0},
        tmp_path,
    )
    assert bad_limit == "Error: max_results must be a positive integer"
    assert calls["n"] == 0


def test_web_search_errors_when_nothing_parseable(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tools.search,
        "_search_html",
        lambda query, recency=None: _search_page(
            [("Secret", _ddg_href("http://127.0.0.1/secret"), "hidden")]
        ),
    )
    private_only = execute_tool("web_search", {"query": "transit"}, tmp_path)
    assert private_only == "Error: no results"

    monkeypatch.setattr(
        tools.search,
        "_search_html",
        lambda query, recency=None: "<html><body></body></html>",
    )
    empty = execute_tool("web_search", {"query": "transit"}, tmp_path)
    assert empty == "Error: no results"


def test_web_search_site_and_time_filters(tmp_path, monkeypatch):
    seen = []

    def fake_search(query, recency=None):
        seen.append((query, recency))
        return _search_page([("Alpha", "https://1.1.1.1/alpha", "snippet")])

    monkeypatch.setattr(tools.search, "_search_html", fake_search)

    result = execute_tool(
        "web_search",
        {"query": "asyncio", "site": "docs.python.org", "time": "w"},
        tmp_path,
    )
    assert "url: https://1.1.1.1/alpha" in result
    assert seen == [("asyncio site:docs.python.org", "w")]

    plain = execute_tool("web_search", {"query": "asyncio"}, tmp_path)
    assert "Alpha" in plain
    assert seen[-1] == ("asyncio", None)

    bad = execute_tool("web_search", {"query": "asyncio", "time": "hour"}, tmp_path)
    assert bad == "Error: time must be one of d, w, m, y"
    assert len(seen) == 2


def test_research_skill_gates_web_search(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tools.search,
        "_search_html",
        lambda query, recency=None: _search_page(
            [("Alpha Title", "https://1.1.1.1/alpha", "Alpha snippet")]
        ),
    )
    registry = SkillRegistry()

    blocked = registry.execute("web_search", {"query": "transit"}, tmp_path)
    assert blocked.startswith("Error: 'web_search' is not loaded.")
    assert "hardware-diagnosis, research" in blocked

    loaded = registry.load_skill("research")
    assert loaded.startswith(
        "Loaded skill 'research' with 7 tools: "
        "web_search, read_url, fetch_url, parse_html, read_feed, read_file, read_pdf"
    )
    assert "two or three results" in loaded
    search = next(
        schema
        for schema in registry.schemas()
        if schema["function"]["name"] == "web_search"
    )
    assert "Snippets are not answers" in search["function"]["description"]
    assert (
        "[loaded: web_search, read_url, fetch_url, parse_html, read_feed, "
        "read_file, read_pdf]"
    ) in registry.get_menu()

    result = registry.execute("web_search", {"query": "transit"}, tmp_path)
    assert "url: https://1.1.1.1/alpha" in result

    assert registry.unload_skill("research") == "Unloaded skill 'research'."
    blocked_again = registry.execute("web_search", {"query": "transit"}, tmp_path)
    assert blocked_again == blocked


def test_parse_html_truncates_long_article(tmp_path):
    paragraph = "The council voted to fund the harbor bus line. " * 400
    html = (
        "<html><head><title>Long Story</title></head><body><article>"
        f"<h1>Long Story</h1><p>{paragraph}</p></article></body></html>"
    )
    (tmp_path / "long.html").write_text(html)

    result = execute_tool("parse_html", {"path": "long.html"}, tmp_path)

    assert len(result) <= MAX_TOOL_RESULT
    assert result.endswith("chars omitted]")


def test_read_url_fetches_extracts_and_saves_text(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tools.http,
        "_http_get",
        lambda url: (200, "text/html; charset=utf-8", ARTICLE_HTML.encode()),
    )

    result = execute_tool("read_url", {"url": "https://1.1.1.1/news"}, tmp_path)

    assert result.startswith("url: https://1.1.1.1/news\n")
    assert "title: City Council Approves Transit Plan" in result
    assert "author: Ada Lovelace" in result
    assert "harbor district" in result
    saved = re.search(r"saved: (\S+)", result).group(1)
    text_file = re.search(r"text_file: (\S+)", result).group(1)
    assert saved.startswith("downloads/1.1.1.1-") and saved.endswith(".html")
    assert text_file == saved[: -len(".html")] + ".txt"
    assert (tmp_path / saved).read_bytes() == ARTICLE_HTML.encode()
    assert "harbor district" in (tmp_path / text_file).read_text()

    blocked = execute_tool("read_url", {"url": "http://127.0.0.1/"}, tmp_path)
    assert "blocked" in blocked
    assert (
        execute_tool(
            "read_url", {"url": "https://1.1.1.1/news", "mode": "raw"}, tmp_path
        )
        == "Error: mode must be 'article' or 'full'"
    )


def test_read_url_handles_plain_text_and_truncates_with_a_pointer(
    tmp_path, monkeypatch
):
    body = ("word " * 3000).encode()
    monkeypatch.setattr(tools.http, "_http_get", lambda url: (200, "text/plain", body))

    result = execute_tool("read_url", {"url": "https://1.1.1.1/notes"}, tmp_path)

    assert len(result) <= MAX_TOOL_RESULT
    assert "full text is in downloads/1.1.1.1-" in result
    assert "use read_file with offset to continue" in result
    text_file = re.search(r"text_file: (\S+)", result).group(1)
    assert text_file.endswith(".txt")
    assert (tmp_path / text_file).read_text() == body.decode().strip()


def test_read_url_reads_a_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tools.http,
        "_http_get",
        lambda url: (200, "application/pdf", _pdf_bytes(["Hello PDF"])),
    )

    result = execute_tool("read_url", {"url": "https://1.1.1.1/paper"}, tmp_path)

    assert "Hello PDF" in result
    assert re.search(r"saved: downloads/\S+\.pdf", result)


def test_read_url_stops_a_pdf_at_the_page_cap(tmp_path, monkeypatch):
    pages = [f"Page {number} text" for number in range(1, PDF_MAX_PAGES + 3)]
    monkeypatch.setattr(
        tools.http,
        "_http_get",
        lambda url: (200, "application/pdf", _pdf_bytes(pages)),
    )

    result = execute_tool("read_url", {"url": "https://1.1.1.1/long"}, tmp_path)

    assert f"Page {PDF_MAX_PAGES} text" in result
    assert f"Page {PDF_MAX_PAGES + 1} text" not in result
    marker = (
        f"...[pages {PDF_MAX_PAGES + 1}-{PDF_MAX_PAGES + 2} remain; "
        f"the PDF is saved at downloads/"
    )
    assert marker in result
    assert f"call read_pdf with first_page={PDF_MAX_PAGES + 1}]" in result
    text_file = re.search(r"text_file: (\S+)", result).group(1)
    saved_text = (tmp_path / text_file).read_text(encoding="utf-8")
    assert marker in saved_text
    assert f"Page {PDF_MAX_PAGES + 1} text" not in saved_text


def test_parse_html_full_mode_and_fallback_keep_non_article_text(tmp_path):
    page = (
        "<html><head><title>Docs</title></head><body><nav>Home</nav>"
        "<table><tr><td>Option</td><td>Meaning</td></tr>"
        "<tr><td>-x</td><td>exit</td></tr></table>"
        "<script>var hidden = 1;</script></body></html>"
    )
    (tmp_path / "docs.html").write_text(page)

    article = execute_tool("parse_html", {"path": "docs.html"}, tmp_path)
    assert article.startswith("title: Docs")
    assert "Meaning" in article

    full = execute_tool("parse_html", {"path": "docs.html", "mode": "full"}, tmp_path)
    assert "Option Meaning" in full
    assert "hidden" not in full

    assert (
        execute_tool("parse_html", {"path": "docs.html", "mode": "raw"}, tmp_path)
        == "Error: mode must be 'article' or 'full'"
    )


RSS_FEED = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Harbor News</title>
<item><title>Bus line funded</title><link>https://1.1.1.1/bus</link>
<pubDate>Tue, 06 Oct 2026 10:00:00 GMT</pubDate>
<description><![CDATA[<p>The council <b>voted</b> yes.</p>]]></description></item>
<item><title>Second</title><link>https://1.1.1.1/two</link><description>Two</description></item>
</channel></rss>
"""


ATOM_FEED = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom Blog</title>
<entry><title>Entry One</title><link rel="alternate" href="https://1.1.1.1/one"/>
<updated>2026-10-06T10:00:00Z</updated><summary>First summary</summary></entry>
</feed>
"""


def test_read_feed_parses_rss_and_atom(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tools.http, "_http_get", lambda url: (200, "application/rss+xml", RSS_FEED)
    )
    rss = execute_tool(
        "read_feed", {"url": "https://1.1.1.1/feed", "max_items": 1}, tmp_path
    )
    assert rss.startswith("feed: Harbor News\nentries: 2\n")
    assert (
        "1. Bus line funded\n   link: https://1.1.1.1/bus\n   date: Tue, 06 Oct 2026 10:00:00 GMT\n   summary: The council voted yes."
        in rss
    )
    assert "Second" not in rss
    assert "Call read_url on an entry link" in rss

    monkeypatch.setattr(
        tools.http, "_http_get", lambda url: (200, "application/atom+xml", ATOM_FEED)
    )
    atom = execute_tool("read_feed", {"url": "https://1.1.1.1/atom"}, tmp_path)
    assert "feed: Atom Blog" in atom
    assert "link: https://1.1.1.1/one" in atom
    assert "date: 2026-10-06T10:00:00Z" in atom
    assert "summary: First summary" in atom


def test_read_feed_rejects_bad_xml_and_entities(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tools.http,
        "_http_get",
        lambda url: (200, "text/xml", b"<rss><channel></channel></rss>"),
    )
    assert execute_tool("read_feed", {"url": "https://1.1.1.1/feed"}, tmp_path) == (
        "Error: no feed entries found"
    )

    monkeypatch.setattr(
        tools.http, "_http_get", lambda url: (200, "text/xml", b"<rss><item>")
    )
    assert execute_tool(
        "read_feed", {"url": "https://1.1.1.1/feed"}, tmp_path
    ).startswith("Error: invalid XML")

    bomb = b'<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "y">]><rss><channel><item><title>&x;</title></item></channel></rss>'
    monkeypatch.setattr(tools.http, "_http_get", lambda url: (200, "text/xml", bomb))
    assert "DOCTYPE" in execute_tool(
        "read_feed", {"url": "https://1.1.1.1/feed"}, tmp_path
    )

    assert "blocked" in execute_tool(
        "read_feed", {"url": "http://127.0.0.1/feed"}, tmp_path
    )
