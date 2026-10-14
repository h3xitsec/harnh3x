---
name: data
description: Inspect CSV and JSON files before working with them.
allowed-tools: inspect_csv inspect_json
---

## When to Use

- The user asks what a CSV or JSON file contains, its columns, or how many rows it has
- You are about to write Python against a data file and need its shape first

## Available Tools

- `inspect_csv` — Delimiter, columns with fill counts, row count, and a preview of the first rows
- `inspect_json` — Structure of a JSON document: keys, array lengths, and sample values

## Conventions

- Inspect before computing. Use the column names and types you see here when you write a `run_python` script.
- The preview is a sample, not the data. For totals, filters, or grouping, use the `python` skill.
- Report the row count, the columns, and anything odd such as empty columns.
- If a tool returns an error, tell the user the error. Do not guess.
