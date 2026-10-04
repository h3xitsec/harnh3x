# Shell

Run a command with the workspace as the working directory.

## When to Use

- The user asks to run a command or inspect the workspace from the shell

## Available Tools

- `run_shell` — Run a command and return its exit code, stdout, and stderr

## Conventions

- The working directory is the workspace.
- Blocked commands and blocked shell syntax return `Error:`.
- Report the exit status to the user.
