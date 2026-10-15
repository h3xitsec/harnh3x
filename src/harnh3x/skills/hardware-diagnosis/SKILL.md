---
name: hardware-diagnosis
description: Perform diagnosis tasks on a system's hardware.
allowed-tools: read_file write_file edit_file append_file delete_file move_file list_files search_files run_shell web_search read_url
---

## When to use

The user asks for computer diagnosis tasks

## Available tools

- `run_shell` — Run one command and return its exit code, stdout, and stderr. Optional `timeout` in seconds and `stdin` text.
- `web_search` — Return titles, URLs, and snippets for a query. Optional `site` and `time` (d, w, m, y) filters.
- `read_url` — Fetch a URL and return its readable text in one step. Saves the raw file and a `.txt` copy in `downloads/`.
- `read_file` — Read a file. Pass `offset` and `limit` to page through a long file.
- `write_file` — Create or overwrite a whole file
- `edit_file` — Replace one exact text fragment with another. Set `replace_all` to change every match.
- `append_file` — Add text to the end of a file
- `delete_file` — Delete a file
- `move_file` — Move or rename a file. It will not overwrite.
- `list_files` — List files and directories with sizes. `pattern` filters names, `max_depth` limits nesting.
- `search_files` — Regex search across text files, returning `path:line: text`

## Conventions

- It's preferable that `yolo` mode is activated on the session
- Find system's hardware information to know what type of hardware you're working on
- If needed, search the web to find how to fulfill your tasks
- Use run_shell when needed