---
name: file-ops
description: Read, write, edit, move, list, and search files. Use to inspect the workspace or project, or to change a file.
allowed-tools: read_file write_file edit_file append_file delete_file move_file list_files search_files glob grep
---

## Conventions

- When asked to read a file, give the user the content of the file.
- When asked to write a file, confirm that the file was written successfully but do not repeat the content.
- Prefer `edit_file` over `write_file` for a small change to an existing file.
- When a `read_file` result is cut, keep reading while the answer may be further down.
- Use `glob` to find files by a path pattern such as `**/*.py`, and `grep` to find where text lives. Both skip hidden paths, and inside a git repository they also skip files matched by `.gitignore`.
- Use `list_files` for a directory listing with sizes.
