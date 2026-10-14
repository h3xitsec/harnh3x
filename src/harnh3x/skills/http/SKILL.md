---
name: http
description: Send HTTP requests and call JSON endpoints.
allowed-tools: http_get_json http_request
---

## When to Use

- The user asks for structured data that a free API serves, such as weather (Open-Meteo), exchange rates, geocoding, or a public GitHub endpoint
- A page is awkward to read but the site offers a JSON endpoint
- The call needs a method other than GET, a JSON or raw body, or the status and headers
- To search the web or read an article, use `research` instead

## Available Tools

- `http_get_json` — GET a URL with optional query `params` and return the parsed JSON
- `http_request` — Send GET, HEAD, OPTIONS, POST, PUT, PATCH, or DELETE. Returns the status code, headers, and body. Use this when `http_get_json` is too narrow.

## Conventions

- Only http and https URLs on public hosts are allowed. Prefer keyless APIs. Headers may be set; do not invent credentials.
- Put query parameters in `params` rather than hand-building the URL.
- Pass `json_body` as an object or array, not a quoted JSON string. Example: `{"email": "a@b.c", "password": "secret"}`. Use `body` only for text that is not JSON. Do not set both.
- Redirects are not followed unless `follow_redirects` is true.
- Large responses are truncated. Narrow the request with `params`, or for a GET save the full response with `fetch_url` from `research` and inspect it with `inspect_json` from `data`.
- Report the values you used and the URL you called. Do not invent fields the response did not contain.
- If a tool returns an error, tell the user the error. Do not guess. An HTTP error status is a result, not a tool error: report the status and body.
