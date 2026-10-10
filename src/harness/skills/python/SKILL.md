# Python

Run a short Python script for calculations, dates, and data wrangling.

## When to Use

- The user asks for arithmetic, unit conversion, date math, or statistics
- You need to transform CSV, JSON, or text that is too large or too fiddly to do by hand
- A shell pipeline would be needed but shell syntax is blocked

## Available Tools

- `run_python` — Run Python source with the standard library and return exit code, stdout, and stderr. Optional `timeout` and `stdin`.

## Conventions

- The script runs in the workspace, so relative paths point at workspace files. The kernel confines the process to that directory: it cannot read or write other paths, and it cannot open network connections.
- Only the standard library is available. Do not import third-party packages.
- Print the result. Nothing is returned unless the script prints it.
- Compute instead of estimating: when a number matters, run the code and report the printed value.
- Keep scripts small and self-contained. If a script fails, read the traceback, fix the script, and run it again.
- Report the exit status and show the output to the user.
