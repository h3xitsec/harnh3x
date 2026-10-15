---
name: shell
description: Run a command with the workspace as the working directory.
allowed-tools: run_shell
---

## When to Use

- The user asks to run a command, a build, a test suite, or a CLI tool
- You need output from a program such as `git`, `ls`, or `wc`

## Conventions

- The working directory is the workspace. The kernel confines the process to that directory: it cannot read or write other paths, and it cannot use the network. A path the program builds itself is still confined.
- One program per call. Pipes, redirects, `;`, `&&`, backticks, and `$` are blocked and return `Error:`; so are `rm`, `mv`, `chmod`, and `sudo`. Use `file-ops` to move or delete files, and `python` for anything that needs a pipeline.
- Pass data to a program through `stdin` instead of a redirect.
- Raise `timeout` for long builds or tests; the cap is enforced by the harness.
- Report the exit status to the user, and the stderr when the command failed.
