---
name: http
description: Call public JSON APIs (weather, exchange rates, geocoding, GitHub) or send custom HTTP requests. Not for reading articles; use research.
allowed-tools: http_get_json http_request
---

## Conventions

- Prefer keyless APIs such as Open-Meteo. Headers may be set; do not invent credentials.
- A page that is awkward to read may have a JSON endpoint; try it.
- Large responses are truncated. Narrow the request with `params`, or for a GET save the full response with `fetch_url` from `research` and inspect it with `inspect_json` from `data`.
- Report the values you used and the URL you called. Do not invent fields the response did not contain.
- Report an HTTP error status and its body to the user.
