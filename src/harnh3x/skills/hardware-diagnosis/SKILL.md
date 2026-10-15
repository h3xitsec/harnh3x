---
name: hardware-diagnosis
description: Read OS, CPU, memory, storage, and attached hardware, and diagnose that hardware.
allowed-tools: system_info read_file write_file edit_file append_file delete_file move_file list_files search_files run_shell web_search read_url
---

## When to use

The user asks for computer diagnosis tasks, or for the OS, model, CPU, memory, storage, or attached hardware.

## Conventions

- Call `system_info` for the inventory. It works with the sandbox on, on Linux and macOS.
- It's preferable that `yolo` mode is activated when a follow-up command must read outside the workspace.
- If needed, search the web to find how to fulfill your tasks
- Use `run_shell` for follow-up diagnosis, not for the inventory
