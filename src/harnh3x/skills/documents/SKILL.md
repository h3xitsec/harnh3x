---
name: documents
description: Read PDF and Word documents in the workspace.
allowed-tools: read_pdf read_docx
---

## When to Use

- The user asks to read, summarize, or quote a `.pdf` or `.docx` file in the workspace
- A download saved by `fetch_url` is a PDF

## Available Tools

- `read_pdf` — Extract text from a page range of a PDF. Defaults to the first pages; the result says how to continue.
- `read_docx` — Extract the paragraphs of a Word `.docx` file

## Conventions

- Read the whole document before summarizing it. When `read_pdf` reports remaining pages, call it again with the next `first_page`.
- A scanned PDF has no text layer and returns empty pages. Tell the user instead of guessing the content.
- A truncated `read_docx` result names a `.txt` copy; use `read_file` from `file-ops` with `offset` to read the rest.
- Quote page numbers when you cite a PDF.
- If a tool returns an error, tell the user the error. Do not guess.
