---
name: reference
description: Look up stable who, what, where, and when facts and definitions on Wikipedia. Use research for recent events or numbers that change.
allowed-tools: wikipedia_search wikipedia_summary
---

## Conventions

- Start with `wikipedia_search` unless you already know the exact article title, then call `wikipedia_summary` with one of the returned titles.
- A disambiguation result is flagged; pick a more specific title and call `wikipedia_summary` again.
- Cite the article URL in your answer.
- Summaries cover the basics only. For details, load `research` and read the full article or other sources.
