---
name: memory
description: Save and recall facts the user wants kept across sessions, or answer a question a saved memory may cover.
allowed-tools: remember recall
---

## Conventions

- Memories live in memory.md in the harnh3x base directory (default ~/.harnh3x). That file is shared by every session and is not inside the workspace.
- Use short, descriptive keys such as `user timezone` or `project deadline`, so the system prompt index stays readable.
- Before answering a question about the user or an earlier session, call `recall` with a relevant word.
- Only save what the user asked you to keep or clearly wants kept. Do not store secrets such as passwords or tokens.
- Confirm what you remembered, and tell the user when nothing matched.
