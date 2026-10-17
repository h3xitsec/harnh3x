---
name: coding
description: Write, edit, and run source files, or change code in the project. Use when the user wants a program, a script, or a code change.
allowed-tools: read_file write_file edit_file list_files search_files outline_python run_script run_shell
---

## Conventions

- A request for a program, script, or code change is a file. Do not answer with only a code block.
- Find the files involved with `list_files` and `search_files`, read the code around a change before editing it, and match its style.
- Call `outline_python` on a `.py` file or a directory before reading a whole module, then `read_file` that span (`offset` is the start line, `limit` is end - start + 1).
- Use `write_file` for a new file and `edit_file` for a change to an existing one.
- Run what you wrote. `run_script` runs a `.py` file with only the standard library. Run project code that needs its own dependencies, or another language, through `run_shell` with the project's own tool, such as `pytest`, `npm test`, or `cargo test`.
- Fix a reported syntax error before running. If a run fails, read the error, fix the file, and run it again.
- The final answer names the paths, what changed, and the run result. Do not paste the full source unless the user asked to see it.
- A one-off calculation that should not leave a file belongs to the `python` skill.
