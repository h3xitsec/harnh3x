# API

Call public JSON web APIs that need no key.

## When to Use

- The user asks for structured data that a free API serves, such as weather (Open-Meteo), exchange rates, geocoding, or a public GitHub endpoint
- A page is awkward to read but the site offers a JSON endpoint

## Available Tools

- `http_get_json` — GET a URL with optional query `params` and return the parsed JSON

## Conventions

- Only http and https URLs on public hosts are allowed. Keys and credentials cannot be sent, so pick keyless APIs.
- Put query parameters in `params` rather than hand-building the URL.
- Large responses are truncated. Narrow the request with `params`, or save the full response with `fetch_url` from `research` and inspect it with `inspect_json` from `data`.
- Report the values you used and the URL you called. Do not invent fields the response did not contain.
- If a tool returns an error, tell the user the error. Do not guess.
