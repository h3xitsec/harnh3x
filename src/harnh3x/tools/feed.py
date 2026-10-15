import xml.etree.ElementTree as ET
from pathlib import Path

from harnh3x.config import FEED_MAX_ITEMS
from harnh3x.tools.http import _checked_get, validate_url
from harnh3x.tools.page import strip_html
from harnh3x.tools.paths import _count_arg, cap_result
from harnh3x.tools.xml import _parse_xml

SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_feed",
            "description": "Read an RSS or Atom feed and return its latest entries",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Feed URL"},
                    "max_items": {
                        "type": "integer",
                        "description": f"How many entries to return (default 10, max {FEED_MAX_ITEMS})",
                    },
                },
                "required": ["url"],
            },
        },
    },
]


def _local(tag) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, *names: str) -> str:
    for child in element:
        if _local(child.tag) in names:
            if child.text and child.text.strip():
                return child.text.strip()
            href = child.get("href")
            if href and _local(child.tag) == "link":
                return href.strip()
    return ""


def _entry_link(element: ET.Element) -> str:
    fallback = ""
    for child in element:
        if _local(child.tag) != "link":
            continue
        href = child.get("href")
        if href:
            rel = child.get("rel", "alternate")
            if rel == "alternate":
                return href.strip()
            fallback = fallback or href.strip()
        elif child.text and child.text.strip():
            return child.text.strip()
    return fallback


def _feed_entries(root: ET.Element) -> tuple[str, list[dict]]:
    if _local(root.tag) == "feed":
        title = _child_text(root, "title")
    else:
        channel = next((c for c in root.iter() if _local(c.tag) == "channel"), root)
        title = _child_text(channel, "title")
    entries = []
    for element in root.iter():
        if _local(element.tag) not in {"item", "entry"}:
            continue
        summary = _child_text(element, "description", "summary", "content", "encoded")
        entries.append(
            {
                "title": " ".join(_child_text(element, "title").split()),
                "link": _entry_link(element),
                "date": _child_text(element, "pubDate", "published", "updated", "date"),
                "summary": strip_html(summary).replace("\n", " ")[:300],
            }
        )
    return title, entries


def read_feed(workspace: Path, args: dict) -> str:
    url = args.get("url", "")
    limit = _count_arg(args.get("max_items"), "max_items", 10, FEED_MAX_ITEMS)
    validate_url(url)
    _status, _media, body = _checked_get(url)
    root = _parse_xml(body)
    feed_title, entries = _feed_entries(root)
    if not entries:
        return "Error: no feed entries found"
    lines = [f"feed: {feed_title}", f"entries: {len(entries)}", ""]
    for index, entry in enumerate(entries[:limit], start=1):
        lines.append(f"{index}. {entry['title']}")
        lines.append(f"   link: {entry['link']}")
        lines.append(f"   date: {entry['date']}")
        lines.append(f"   summary: {entry['summary']}")
    lines.append("")
    lines.append("Call read_url on an entry link to read the full item.")
    return cap_result("\n".join(lines))


FNS = {
    "read_feed": read_feed,
}
