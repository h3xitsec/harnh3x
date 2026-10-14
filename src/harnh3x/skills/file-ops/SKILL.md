---
name: file-ops
description: Read, write, edit, move, list, and search files in the workspace.
allowed-tools: read_file write_file edit_file append_file delete_file move_file list_files search_files
---

## When to Use

- The user asks to read, write, edit, rename, or delete a file in the workspace
- You need to find out what is in the workspace or where some text lives

## Available Tools

- `read_file` — Read a file. Pass `offset` and `limit` to page through a long file.
- `write_file` — Create or overwrite a whole file
- `edit_file` — Replace one exact text fragment with another. Set `replace_all` to change every match.
- `append_file` — Add text to the end of a file
- `delete_file` — Delete a file
- `move_file` — Move or rename a file. It will not overwrite.
- `list_files` — List files and directories with sizes. `pattern` filters names, `max_depth` limits nesting.
- `search_files` — Regex search across text files, returning `path:line: text`

## Conventions

- When asked to read a file, give the user the content of the file.
- When asked to write a file, confirm that the file was written successfully but do not repeat the content.
- Prefer `edit_file` over `write_file` for small changes to an existing file, so the rest of the file is untouched. Include enough surrounding text in `old_text` to match exactly once.
- A truncated `read_file` result ends with the next `offset` to pass. Keep reading when the answer may be further down.
- Use `list_files` before guessing a path, and `search_files` to locate text.
- Paths stay inside the workspace. A path outside the workspace returns `Error:`.
