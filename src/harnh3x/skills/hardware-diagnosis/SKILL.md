---
name: hardware-diagnosis
description: Inventory and diagnose this Linux or macOS host, covering OS, model, CPU, memory, storage, and attached hardware.
allowed-tools: system_info run_shell read_file list_files search_files web_search read_url
---

## Conventions

- Call `system_info` first for the inventory. Pass `sections` to narrow it. It works with the sandbox on.
- Follow up with `run_shell` for a specific check, one diagnostic program at a time.
- When a check fails because the sandbox blocks a path, a device, or the network, tell the user the check needs a session started with `--yolo`. You cannot turn the sandbox off, so do not retry the same command.
- Use `read_file`, `list_files`, and `search_files` for a log or report the user points to.
- Look up an error code, a model number, or a vendor procedure with `web_search`, then `read_url` on the relevant result, and cite the page.
- Report what you observed with its values, then the likely cause and the next step. Do not state a value no tool returned.
