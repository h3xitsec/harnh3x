# Coding

Write a program to a workspace file, edit it, and run it.

## When to Use

- The user asks for a script, a program, or a change to existing code
- You need to save source they can run again, not just print a result

## Available Tools

- `read_source` — Read a source file. Pass `offset` and `limit` to page through a long file.
- `write_source` — Create or overwrite a whole source file. A `.py` file is syntax-checked.
- `edit_source` — Replace one exact text fragment with another. Set `replace_all` to change every match.
- `run_script` — Run a `.py` file and return exit code, stdout, and stderr. Optional `args`, `timeout`, and `stdin`.

## Conventions

- A request for a script, program, or code change is a file in the workspace. Do not answer with only a code block.
- Write the program with `write_source`, then run it with `run_script`. If it fails, fix it with `edit_source` and run it again.
- Prefer `edit_source` over `write_source` for a small change to an existing file, so the rest of the file is untouched. Include enough surrounding text in `old_text` to match exactly once.
- A `.py` syntax error is reported after the write or edit, and the file is still saved. Fix that error before running.
- The script runs in the workspace, so relative paths point at workspace files. The kernel confines the process to that directory: it cannot read or write other paths, and it cannot open network connections. Only the standard library is available.
- `args` are command-line strings. There is no shell.
- The final answer names the path, what the program does, and the run result. Do not paste the full source unless the user asked to see it.
- Load `file_ops` to list or search the workspace. A one-off calculation that should not leave a file stays on the `python` skill.
