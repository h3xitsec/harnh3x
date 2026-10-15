---
name: reference
description: Look up encyclopedic facts on Wikipedia.
allowed-tools: wikipedia_search wikipedia_summary
---

## When to Use

- The user asks who, what, where, or when about a person, place, event, or concept
- You need a quick, stable definition before a deeper web search

## Conventions

- Start with `wikipedia_search` unless you already know the exact article title, then call `wikipedia_summary` with one of the returned titles.
- A disambiguation result is flagged; pick a more specific title and call `wikipedia_summary` again.
- Cite the article URL in your answer.
- Summaries cover the basics only. For recent events, numbers that change, or details, use the `research` skill to read the full article or other sources.
- If a tool returns an error, tell the user the error. Do not guess.
