# Harnh3x

A AI harness: an OpenAI-compatible chat loop with tools confined to one workspace. Each session gets its own directory under `~/.harnh3x/workspaces/`, a `transcript.json` you can resume, and a JSONL `session.log`. `--project` grants that same access to one extra folder while the session directory stays put. Facts saved with the memory skill live in `~/.harnh3x/memory.md`, shared by every session.

On a terminal it runs a Textual interface. Piped input and `--answer-only` stay plain stdout.

## Install

Releases are cut from conventional commits on `main`. Install one with pipx:

```bash
pipx install "git+https://github.com/h3xitsec/harnh3x.git@vX.Y.Z"
```

Omit the tag to track the default branch:

```bash
pipx install "git+https://github.com/h3xitsec/harnh3x.git"
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

The client reads `~/.config/harnh3x/harnh3x.yaml`, or `$XDG_CONFIG_HOME/harnh3x/harnh3x.yaml` when that variable is set. A missing file changes nothing, and the process does not create it. [`harnh3x.yaml.example`](harnh3x.yaml.example) lists every setting. If an old `harnh3x.conf` is still there and the YAML file is not, startup exits and tells you to move the settings.

```yaml
api_key: sk-...
api_url: http://127.0.0.1:11434/v1
model: qwen3.5-128k
```

`#` comments are ignored. The same scalar values can be set with environment variables, which override the file when they are non-empty:

```bash
OPENAI_API_KEY=...
OPENAI_BASE_URL=http://127.0.0.1:11434/v1
HARNH3X_MODEL=qwen3.5-128k
```

`python-dotenv` loads a `.env` from the working directory when the process starts, without overriding variables that are already set. Do not commit that file. `--model` overrides the model.

Startup exits with a short error when `api_key` or `api_url` is missing from both the config file and the environment.

`provider` is `openai` by default. Set it to `anthropic` to call Claude's Messages API. The key is then `ANTHROPIC_API_KEY` when that variable is set, otherwise `api_key`. `OPENAI_API_KEY` does not override the Claude key. `api_url` is optional in that mode and defaults to `https://api.anthropic.com`. `max_tokens` (default 8192) is sent on every completion with either provider, including compaction; an OpenAI-compatible server that rejects the parameter is called again with `max_completion_tokens`.

`context_limit` sets the window used for compaction with either provider. When it is unset, Claude uses 200000 and an OpenAI-compatible server is asked through Ollama's `/api/show`; a server without that endpoint leaves the limit unknown and compaction off until `context_limit` is set.

```yaml
provider: anthropic
api_key: sk-ant-...
model: claude-sonnet-4-6
```

Other tunables use the same file: `max_turns`, `compact_at`, `max_tool_result`, `fetch_timeout`, `fetch_max_bytes`, `fetch_max_redirects`, `search_max_results`, `feed_max_items`, `shell_timeout`, `shell_max_timeout`, `shell_max_output`, `list_files_max`, `search_files_max`, `pdf_max_pages`, `preview_rows`, `wikipedia_lang`, and `base_dir`. Each one also accepts `HARNH3X_` plus that name in uppercase, such as `HARNH3X_MAX_TURNS`. `shell_max_output` (default 1000000 bytes) kills a shell or Python command whose combined output passes that size and says so in the result. Startup exits with the setting's name when a count or limit is not a positive integer, when `fetch_max_redirects` is negative, when `compact_at` is not above 0 and at most 1, or when `shell_max_timeout` is below `shell_timeout`. `base_dir` defaults to `~/.harnh3x`: sessions are created under `workspaces/` inside it, and `memory.md` sits next to that directory. `HARNH3X_CONFIG` points at a different file; an empty value skips the file.

MCP servers live in the same file under `mcp`. Each entry needs `command` and may set `args`, `env`, `timeout`, `description`, `include`, and `exclude`. Harnh3x does not install the server binary. The child is started from that command when you load the skill, with a minimal environment plus the `env` block, and it is not wrapped in the Seatbelt sandbox. `unload_skill` drops the tools from the next turn and leaves the process running. A new session or quitting the process stops it.

```yaml
mcp:
  caido:
    command: caido-mcp-server
    args: [serve]
    env:
      CAIDO_URL: http://127.0.0.1:8080
      CAIDO_ACCESS_TOKEN: "..."
```

Install [caido-mcp-server](https://github.com/c0tton-fluff/caido-mcp-server) yourself and put the local Caido access token in `env`. Leave `CAIDO_ALLOW_SENSITIVE_HEADERS` unset unless you need real credential headers in tool output. Another server is another key under `mcp`. Environment variables do not override this block. An MCP skill is omitted when an `http` or `https` URL in its `env` is not accepting connections.

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
poetry run harnh3x --project ~/Github/my-project
poetry run harnh3x --coding --project ~/Github/my-project
poetry run harnh3x --debug
```

`--model` with a name selects that model. `--model` alone lists models from the API: a Textual picker on a tty, a numbered prompt otherwise.

`--project PATH` grants sandboxed read, write, and shell access to an existing directory. The path is expanded and must not be `/` or overlap the session workspace. Relative tool paths and the shell working directory use that folder. Absolute paths may also use the session workspace. Downloads, plans, and the transcript stay in the session directory. The grant is stored in `transcript.json` and restored by `/resume`. A new session in the same process (Ctrl+N) keeps it. Plain output and the header print `Project:` when one is set.

`--coding` requires `--project` and starts a coding session in that folder. The system prompt is a coding agent: read the project before changing it, load the coding skill before editing source, make the smallest change, and confirm the paths. The flag is stored in `transcript.json` and restored by `/resume`. Ctrl+N keeps it. A resumed transcript with no `coding` value clears it. Plain output and the header print `Coding` on the line after `Project:`.

Cursor rules in `<project>/.cursor/rules/**/*.mdc` load only in a coding session. Rules with `alwaysApply: true` are in the system prompt. Other rules are listed there by description. A rule with `globs` is appended once, the first time a file tool uses a matching project path. A rule with only a description is read from that file when the description matches the task.

`--yolo` turns off the macOS Seatbelt sandbox for that process. The header’s first line is `YOLO — sandbox off` in bright yellow, and plain output prints the same warning before the workspace. The agent can read any file and run any command, including `sudo`. Writes, edits, deletes, and moves through the file tools still stay in the workspace, or in the project when `--project` is set. `--answer-only` does not print the warning.

`--debug` shows the system prompt once at the start of the session, and again only when that prompt changes, plus the rest of each completion request. The panels match thinking and tool boxes and use cyan. Each one keeps the last three lines until you click it. Plain output prints the same blocks in cyan. The header and plain output print `Debug` on the line after `Coding`. The flag is not stored in the transcript. Ctrl+N and `/resume` keep it. `--answer-only` hides the panels. The compaction summary call is not shown.

A new session prints its workspace path. Files the agent writes, downloads, and notes all stay in that directory unless `--project` points them at the granted folder.

### Interactive session

Enter submits. Shift+Enter inserts a newline.

| Key | Action |
| --- | --- |
| Ctrl+N | New session in a new workspace |
| Ctrl+Q | Quit and stop the running request |

| Command | Action |
| --- | --- |
| `/help` | Commands and hotkeys. Escape closes the box. |
| `/resume` | List past sessions and restore one |
| `/compact` | Summarize older turns now |
| `/export PATH` | Write one response, or every response, to that markdown file. Icons at the bottom right of a response copy it or write it to a markdown file. |
| `/exit` | Quit and stop the running request |

The header title is Harnh3x, then the workspace, the project when one is set, Coding when the session is a coding session, Debug when `--debug` is set, the model, and how much of the context window the last completion used. The package version sits at the far right of the status bar. With `--yolo` the header’s first line is `YOLO — sandbox off` in bright yellow, and that mark stays for a new session or a resumed transcript. Tool calls for a turn share one box, which keeps the last three lines until you click it. The thinking box keeps the last three lines until you click it. With `--debug`, the first completion adds a cyan System box, and a later completion adds another only when the prompt changed. Every completion adds a cyan Request box. Both collapse the same way. Ctrl+H hides those with the thinking, tool, and compaction boxes. When the history is compacted, the transcript shows a compaction note and then the answer as Markdown.

One-shot on a tty, when the model returned reasoning, waits for Enter before the process exits.

Without a tty (piped input, or `--answer-only`), the same commands work as plain lines. `/help` does not list hotkeys.

## What a turn does

Nothing is loaded at the start. The system prompt names the date, the OS, a one-line listing of the workspace, and any saved memory keys, then a menu of skills. The model calls `load_skill` before it can use that skill's tools, and can keep several skills loaded. `unload_skill` drops one. Only the loaded tools, plus those two, are sent on the next completion.

Tool paths stay inside the session workspace, plus the `--project` directory when one is granted. Shell and Python run with `shell=False`, a short allowlist of environment variables, and a macOS Seatbelt profile that can read and write only that workspace and the project (plus the OS and Python runtime). `HOME` and the temp directory stay on the session workspace. Network is denied for those processes. `rm`, `mv`, `chmod`, and `sudo` are rejected before the process starts. `--yolo` skips that sandbox and those command checks, and lets read tools open any path. Pipes and redirects stay blocked. File mutation tools stay in the workspace or the project.

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
| `transcript.json` | Model, messages, loaded skills, the project path when one is granted, and the last token count. Replaced atomically. |
| `session.log` | One JSON object per line: model turns, tool calls, tool results, and compactions. |
| `plan.md` | Checklist for multi-step work. |

`/resume` lists workspaces that have a user message in the transcript, newest first.

## Skills

| Skill | What it covers |
| --- | --- |
| `file-ops` | Read, write, edit, append, delete, move, list, and search files |
| `shell` | One allowlisted command in the workspace |
| `python` | A short snippet to compute an answer, without saving a file |
| `coding` | Write or edit a source file and run a `.py` script |
| `research` | Web search, fetch and read pages, parse saved HTML, read a feed |
| `reference` | Wikipedia search and summaries |
| `http` | Send HTTP requests and call JSON endpoints |
| `documents` | PDF and Word files in the workspace |
| `data` | CSV and JSON shape, columns, and a short preview |
| `memory` | Remember and recall facts in the shared `memory.md` |
| `tasks` | Write, update, and show a plan in `plan.md` |

Each skill's `SKILL.md` has YAML frontmatter (`name`, `description`, and optional `allowed-tools`) and freeform instructions that are returned when that skill is loaded. The skill name is the folder that contains `SKILL.md`, under `src/harnh3x/skills/<name>/` or `src/harnh3x/skills/<category>/<name>/`.

## Development

```bash
poetry run pytest
poetry run ruff check
poetry run ruff format
```

Limits (turns, compaction threshold, fetch and shell caps, the default model) live in `src/harnh3x/config.py`. A new tool, or a looser path or shell check, needs a matching test under `tests/`, in the `test_*.py` file for that subsystem.
