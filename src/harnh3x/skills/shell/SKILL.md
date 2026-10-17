---
name: shell
description: Run one command-line program, such as git, a build, or a test suite. Not for arithmetic or data pipelines; use python.
allowed-tools: run_shell
---

## Conventions

- Raise `timeout` for long builds or tests.
- When shell syntax is blocked, use `file-ops` to move or delete files and `python` for anything that needs a pipeline.
- Report the exit status to the user, and the stderr when the command failed.
