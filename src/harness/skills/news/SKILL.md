# News

Summarize a news article from its URL.

## When to Use

- The user asks to summarize a news article

## Available Tools

- `fetch_url` — Save the article HTML in the workspace
- `parse_html` — Read a saved HTML file and return its title and text

## Conventions

- To summarize a news article, call `fetch_url` with the article URL, then `parse_html` with the `path` from that result.
- `parse_html` reads a workspace HTML file. It does not download.
- Summarize only the text `parse_html` returns.
- If a tool returns an error, tell the user the error. Do not guess or invent the article.
- Do not paste the raw page.
- Confirm the URL you fetched and the summary.
