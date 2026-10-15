---
name: research
description: Search the web, read pages, and follow feeds to answer from sources.
allowed-tools: web_search read_url fetch_url parse_html read_feed
---

## When to Use

- The user asks to search the web, look something up, or find a page
- The user asks to summarize, read, or quote a web page, article, or online PDF
- The user asks for the latest items from a site or blog that offers an RSS or Atom feed

## Conventions

- Snippets are not answers. After `web_search`, call `read_url` on the two or three results that look relevant, then answer from what those pages say. Several `read_url` calls can go in the same turn.
- Cite the URL of every page you used. Do not invent links, quotes, or facts the pages do not contain.
- If `read_url` fails on one result, move to the next result instead of answering from the snippet.
- If one search finds nothing useful, rephrase the query once or add a `site` filter before giving up.
- Long pages are truncated. The result names a `text_file`; use `read_file` from `file-ops` with `offset` to read the rest when the answer may be further down.
- For "latest" or "recent" questions, pass `time` to `web_search` or use `read_feed` when the site has a feed.
- If a tool returns an error, tell the user the error. Do not guess.
- Do not paste a raw page. Summarize, quote briefly, and link.
- Confirm the query you searched and the pages you read.
