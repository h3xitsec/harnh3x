# Harnh3x

A AI harness: an OpenAI-compatible chat loop with tools confined to one workspace. Each session gets its own directory under `~/.harnh3x/workspaces/`, a `transcript.json` you can resume, and a JSONL `session.log`. Facts saved with the memory skill live in `~/.harnh3x/memory.md`, shared by every session.

On a terminal it runs a Textual interface. Piped input and `--answer-only` stay plain stdout.

## Install

Releases are cut from conventional commits on `main`. Install one with pipx:

```bash
pipx install "git+https://github.com/h3xitsec/harn3x.git@vX.Y.Z"
```

Omit the tag to track the default branch:

```bash
pipx install "git+https://github.com/h3xitsec/harn3x.git"
```

## Requirements

- Python 3.14
- [Poetry](https://python-poetry.org/)
- An OpenAI-compatible chat API (OpenAI, Ollama, or another server that speaks the same protocol), or Claude via the Anthropic Messages API
- macOS, if you want the agent to run shell commands or Python. Those tools use Seatbelt and refuse to start when the sandbox cannot be applied.

## Setup

```bash
poetry install
```

The client reads `~/.config/harnh3x/harnh3x.conf`, or `$XDG_CONFIG_HOME/harnh3x/harnh3x.conf` when that variable is set. A missing file changes nothing, and the process does not create it. [`harnh3x.conf.example`](harnh3x.conf.example) lists every settings.

```
api_key = sk-...
api_url = http://127.0.0.1:11434/v1
model = qwen3.5-128k
```

Blank lines and `#` comments are ignored. The same values can be set with environment variables, which override the file when they are non-empty:

```bash
OPENAI_API_KEY=...
OPENAI_BASE_URL=http://127.0.0.1:11434/v1
HARNH3X_MODEL=qwen3.5-128k
```

`python-dotenv` loads a `.env` from the working directory when the process starts, without overriding variables that are already set. Do not commit that file. `--model` overrides the model.

Startup exits with a short error when `api_key` or `api_url` is missing from both the config file and the environment.

`provider` is `openai` by default. Set it to `anthropic` to call Claude's Messages API. The key is then `ANTHROPIC_API_KEY` when that variable is set, otherwise `api_key`. `OPENAI_API_KEY` does not override the Claude key. `api_url` is optional in that mode and defaults to `https://api.anthropic.com`. `max_tokens` (default 8192) is sent on every Messages call, including compaction. `context_limit` sets the window used for compaction; when it is unset, Claude uses 200000.

```
provider = anthropic
api_key = sk-ant-...
model = claude-sonnet-4-6
```

Other tunables use the same file: `max_turns`, `compact_at`, `max_tool_result`, `fetch_timeout`, `fetch_max_bytes`, `fetch_max_redirects`, `search_max_results`, `feed_max_items`, `shell_timeout`, `shell_max_timeout`, `list_files_max`, `search_files_max`, `pdf_max_pages`, `preview_rows`, `wikipedia_lang`, and `base_dir`. Each one also accepts `HARNH3X_` plus that name in uppercase, such as `HARNH3X_MAX_TURNS`. `base_dir` defaults to `~/.harnh3x`: sessions are created under `workspaces/` inside it, and `memory.md` sits next to that directory. `HARNH3X_CONFIG` points at a different file; an empty value skips the file.

The built-in model is `qwen3.5-128k` when nothing else sets it. Point `api_url` or `OPENAI_BASE_URL` at whatever server hosts it.

## Usage

From the repo root:

```bash
poetry run harnh3x
```

That starts an interactive session. Omit a query to keep talking; pass one to run a single turn and exit.

```bash
poetry run harnh3x "Summarize the CSV in this workspace"
poetry run harnh3x --answer-only "What is 17 * 43?"
poetry run harnh3x --model gpt-4.1 "List the files here"
poetry run harnh3x --model
poetry run harnh3x --yolo
```

`--model` with a name selects that model. `--model` alone lists models from the API: a Textual picker on a tty, a numbered prompt otherwise.

`--yolo` turns off the macOS Seatbelt sandbox for that process. The header’s first line is `YOLO — sandbox off` in bright yellow, and plain output prints the same warning before the workspace. The agent can read any file and run any command, including `sudo`. Writes, edits, deletes, and moves through the file tools still stay in the workspace. `--answer-only` does not print the warning.

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

The header title is Harnh3x followed by the package version, then the workspace, the model, and how much of the context window the last completion used. With `--yolo` the header’s first line is `YOLO — sandbox off` in bright yellow, and that mark stays for a new session or a resumed transcript. Tool calls for a turn share one box. The thinking box keeps the last three lines until you expand it. When the history is compacted, the transcript shows a compaction note and then the answer as Markdown.

One-shot on a tty, when the model returned reasoning, waits for Enter before the process exits.

Without a tty (piped input, or `--answer-only`), the same commands work as plain lines. `/help` does not list hotkeys.

## What a turn does

Nothing is loaded at the start. The system prompt names the date, the OS, a one-line listing of the workspace, and any saved memory keys, then a menu of skills. The model calls `load_skill` before it can use that skill's tools, and can keep several skills loaded. `unload_skill` drops one. Only the loaded tools, plus those two, are sent on the next completion.

Tool paths stay inside the session workspace. Shell and Python run with `shell=False`, a short allowlist of environment variables, and a macOS Seatbelt profile that can read and write only that workspace (plus the OS and Python runtime). Network is denied for those processes. `rm`, `mv`, `chmod`, and `sudo` are rejected before the process starts. `--yolo` skips that sandbox and those command checks, and lets read tools open any path. Pipes and redirects stay blocked. File mutation tools stay in the workspace.

Web and API tools run in the parent process. They accept only public `http` and `https` URLs, follow redirects with the same check, and cap body size and time.

When context use reaches half the model's limit, older turns are summarized into one note. The system prompt, the current user message, and the latest unanswered tool result or finished answer stay put. `/compact` forces that summary.

`~/.harnh3x/` (or `base_dir`) holds:

| Path | Role |
| --- | --- |
| `memory.md` | Facts saved with the memory skill, shared across sessions. Keys are listed in the system prompt. |
| `workspaces/<id>/` | One directory per session. |

Each session directory holds:

| File | Role |
| --- | --- |
| `transcript.json` | Model, messages, loaded skills, and the last token count. Replaced atomically. |
| `session.log` | One JSON object per line: model turns, tool calls, tool results, and compactions. |
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
| `memory` | Remember and recall facts in the shared `memory.md` |
| `tasks` | Write, update, and show a plan in `plan.md` |

Per-skill conventions live in `src/harnh3x/skills/<name>/SKILL.md` and are returned when that skill is loaded.

## Development

```bash
poetry run pytest
poetry run black src tests
```

Limits (turns, compaction threshold, fetch and shell caps, the default model) live in `src/harnh3x/config.py`. A new tool, or a looser path or shell check, needs a matching test in `tests/test_harnh3x.py`.
