# Memory

Keep facts across sessions in the workspace's memory.md.

## When to Use

- The user tells you something to remember: a preference, a name, a deadline, a project detail
- The user asks what you remember, or a question that a saved memory may answer
- The system prompt lists saved memories that look relevant to the request

## Available Tools

- `remember` — Save text under a short key. Saving the same key again replaces the earlier text.
- `recall` — Return saved memories, all of them or only those matching a word

## Conventions

- Use short, descriptive keys such as `user timezone` or `project deadline`, so the system prompt index stays readable.
- Before answering a question about the user or an earlier session, call `recall` with a relevant word.
- Only save what the user asked you to keep or clearly wants kept. Do not store secrets such as passwords or tokens.
- Confirm what you remembered, and tell the user when nothing matched.
