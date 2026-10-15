---
name: hardware-diagnosis
description: Perform diagnosis tasks on a system's hardware.
allowed-tools: read_file write_file edit_file append_file delete_file move_file list_files search_files run_shell web_search read_url
---

## When to use

The user asks for computer diagnosis tasks

## Conventions

- It's preferable that `yolo` mode is activated on the session
- Find system's hardware information to know what type of hardware you're working on
- If needed, search the web to find how to fulfill your tasks
- Use run_shell when needed