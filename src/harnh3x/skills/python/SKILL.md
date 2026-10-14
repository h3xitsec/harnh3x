---
name: python
description: Run a short Python snippet to compute a value. It does not save a file.
allowed-tools: run_python
---

## When to Use

- The user asks for arithmetic, unit conversion, date math, or statistics
- You need to transform CSV, JSON, or text that is too large or too fiddly to do by hand
- A shell pipeline would be needed but shell syntax is blocked

Do not use this skill to deliver a program the user asked you to write. That is the coding skill: it saves a file and runs it.

## Available Tools

- `run_python` — Run a short Python snippet with the standard library and return exit code, stdout, and stderr. Optional `timeout` and `stdin`.

## Conventions

- The snippet runs in the workspace, so relative paths point at workspace files. The kernel confines the process to that directory: it cannot read or write other paths, and it cannot open network connections.
- Only the standard library is available. Do not import third-party packages.
- Print the result. Nothing is returned unless the snippet prints it.
- Compute instead of estimating: when a number matters, run the code and report the printed value.
- Keep snippets small and self-contained. If a snippet fails, read the traceback, fix it, and run it again.
- This runs the snippet and discards it. If the user wants a script they can keep, load the coding skill and write a file instead.
- Report the exit status and show the output to the user.
