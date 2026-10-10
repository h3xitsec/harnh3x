# Harnh3x

A file agent: an OpenAI-compatible chat loop with tools confined to one workspace. Each session gets its own directory under `workspaces/`, a `transcript.json` you can resume, and a JSONL `session.log`.

On a terminal it runs a Textual interface. Piped input and `--answer-only` stay plain stdout.

## Requirements

- Python 3.14
- [Poetry](https://python-poetry.org/)
- An OpenAI-compatible chat API (OpenAI, Ollama, or another server that speaks the same protocol)
- macOS, if you want the agent to run shell commands or Python. Those tools use Seatbelt and refuse to start when the sandbox cannot be applied.

## Setup

```bash
poetry install
```

The client reads the usual OpenAI environment variables. `python-dotenv` loads a `.env` from the working directory when the process starts. Do not commit that file.

```bash
OPENAI_API_KEY=...
OPENAI_BASE_URL=http://127.0.0.1:11434/v1   # optional; omit for api.openai.com
```

The default model is `qwen3.5-128k` in `src/harnh3x/config.py`. Point `OPENAI_BASE_URL` at whatever server hosts it, or pass `--model`.

## Usage

From the repo root:

```bash
poetry run python src/harnh3x.py
```

That starts an interactive session. Omit a query to keep talking; pass one to run a single turn and exit.

```bash
poetry run python src/harnh3x.py "Summarize the CSV in this workspace"
poetry run python src/harnh3x.py --answer-only "What is 17 * 43?"
poetry run python src/harnh3x.py --model gpt-4.1 "List the files here"
poetry run python src/harnh3x.py --model
```

`--model` with a name selects that model. `--model` alone lists models from the API: a Textual picker on a tty, a numbered prompt otherwise.

A new session prints its workspace path. Files the agent writes, downloads, and notes all stay in that directory.

### Interactive session

Enter submits. Shift+Enter inserts a newline.

| Key | Action |
| --- | --- |
| Ctrl+N | New session in a new workspace |
| Ctrl+T | Expand or collapse the thinking box |
| Ctrl+Q | Quit |

| Command | Action |
| --- | --- |
| `/help` | Commands and hotkeys. Escape closes the box. |
| `/resume` | List past sessions and restore one |
| `/compact` | Summarize older turns now |
| `/exit` | Quit |

The header shows the workspace, the model, and how much of the context window the last completion used. Tool calls for a turn share one box. The thinking box keeps the last three lines until you expand it. When the history is compacted, the transcript shows a compaction note and then the answer as Markdown.

One-shot on a tty, when the model returned reasoning, waits for Enter before the process exits.

Without a tty (piped input, or `--answer-only`), the same commands work as plain lines. `/help` does not list hotkeys.

## What a turn does

Nothing is loaded at the start. The system prompt names the date, the OS, a one-line listing of the workspace, and any saved memory keys, then a menu of skills. The model calls `load_skill` before it can use that skill's tools, and can keep several skills loaded. `unload_skill` drops one. Only the loaded tools, plus those two, are sent on the next completion.

Tool paths stay inside the session workspace. Shell and Python run with `shell=False`, a short allowlist of environment variables, and a macOS Seatbelt profile that can read and write only that workspace (plus the OS and Python runtime). Network is denied for those processes. `rm`, `mv`, `chmod`, and `sudo` are rejected before the process starts.

Web and API tools run in the parent process. They accept only public `http` and `https` URLs, follow redirects with the same check, and cap body size and time.

When context use reaches half the model's limit, older turns are summarized into one note. The system prompt, the current user message, and the latest unanswered tool result or finished answer stay put. `/compact` forces that summary.

Each session directory holds:

| File | Role |
| --- | --- |
| `transcript.json` | Model, messages, loaded skills, and the last token count. Replaced atomically. |
| `session.log` | One JSON object per line: model turns, tool calls, tool results, and compactions. |
| `memory.md` | Facts saved with the memory skill. Keys are listed in the system prompt. |
| `plan.md` | Checklist for multi-step work. |

`/resume` lists workspaces that have a user message in the transcript, newest first.

## Skills

| Skill | What it covers |
| --- | --- |
| `file_ops` | Read, write, edit, append, delete, move, list, and search files |
| `shell` | One allowlisted command in the workspace |
| `python` | A short snippet to compute an answer, without saving a file |
| `coding` | Write or edit a source file and run a `.py` script |
| `research` | Web search, fetch and read pages, parse saved HTML, read a feed |
| `reference` | Wikipedia search and summaries |
| `api` | GET a public JSON API |
| `documents` | PDF and Word files in the workspace |
| `data` | CSV and JSON shape, columns, and a short preview |
| `memory` | Remember and recall facts in `memory.md` |
| `tasks` | Write, update, and show a plan in `plan.md` |

Per-skill conventions live in `src/harnh3x/skills/<name>/SKILL.md` and are returned when that skill is loaded.

## Development

```bash
poetry run pytest
poetry run black src tests
```

Limits (turns, compaction threshold, fetch and shell caps, the default model) live in `src/harnh3x/config.py`. A new tool, or a looser path or shell check, needs a matching test in `tests/test_harness.py`.
