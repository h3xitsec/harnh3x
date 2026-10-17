---
name: research
description: Search the web and read pages, PDFs, and feeds to answer from cited sources. Use for current, detailed, or latest-news questions.
allowed-tools: web_search read_url fetch_url parse_html read_feed read_file read_pdf
---

## Conventions

- After `web_search`, call `read_url` on the two or three results that look relevant, then answer from what those pages say. Several `read_url` calls can go in the same turn.
- If `read_url` fails on one result, move to the next result instead of answering from the snippet.
- If one search finds nothing useful, rephrase the query once or add a `site` filter before giving up.
- For "latest" or "recent" questions, pass `time` to `web_search`, or use `read_feed` when the site has a feed.
- `read_url` is the default way to read a page. Use `fetch_url` to save a raw JSON, CSV, or PDF file for another tool, and `parse_html` for an HTML file that is already saved.
- When the answer may be further down a cut page, continue with `read_file` or `read_pdf` as the result says.
- Cite the URL of every page you used. Do not invent links, quotes, or facts the pages do not contain.
- Do not paste a raw page. Summarize, quote briefly, and link.
- Confirm the query you searched and the pages you read.
