---
name: data
description: Inspect a CSV or JSON file's columns, types, and row count, before computing on it with python.
allowed-tools: inspect_csv inspect_json
---

## Conventions

- Inspect before computing. Use the column names and types you see here when you write a `run_python` snippet.
- For totals, filters, or grouping, load the `python` skill.
- Report the row count, the columns, and anything odd such as empty columns.
