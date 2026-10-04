# File Operations

Read, write, and delete files in the workspace.

## When to Use

- The user asks to read, write, or delete a file in the workspace

## Available Tools

- `read_file` — Read a file and return its contents
- `write_file` — Create or overwrite a file
- `delete_file` — Delete a file

## Conventions

- When asked to read a file, always give the user the content of the file.
- When asked to write a file, confirm that the file was written successfully but do not give the user the content of the file.
- Use `delete_file` to remove a file.
- Paths stay inside the workspace. A path outside the workspace returns `Error:`.
