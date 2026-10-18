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

MCP servers live in the same file under `mcp`. Each entry needs `command` and may set `args`, `env`, `timeout`, `description`, `instructions`, `include`, and `exclude`. `description` is the skill's menu line; without it, the menu uses the description or instructions the server sends, or else its name, version, and tool list. `instructions` is your own usage notes for that server; when the skill loads, they come after `description` and before the instructions the server sends. Harnh3x does not install the server binary. The child is started from that command when a session starts, with a minimal environment plus the `env` block, and it is not wrapped in the Seatbelt sandbox. A server that fails to start, or whose tool names collide with a built-in tool, is left out of the menu with a warning. `unload_skill` drops the tools from the next turn and leaves the process running. A new session or quitting the process stops it.

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

The header title is Harnh3x, then the workspace, the project when one is set, and Coding when the session is a coding session. The status bar at the bottom is a row of colored badges. On the left are the model, the context the last completion used against the model's limit (for example `ctx 5k/262k 2%`, yellow near the compaction point and red at or past it), `YOLO` with `--yolo`, and `DEBUG` with `--debug`. On the right are the built-in skills, with `+N` for the [custom skills](#custom-skills) (`skills 12+1`), the tools, the MCP servers and their tools when any are connected (`mcp 1 · 67 tools`), a red `⚠ N` badge when an MCP server or custom skill is unavailable, and the package version. Click the skills or tools badge to see which skills are loaded and which are not, with the tools each one provides. Click the model badge to switch to another model the API lists; the conversation continues with that model, and its context limit is looked up again. On a narrow terminal the least important badges are dropped first: the version, then the skill and tool counts, then MCP, then `DEBUG`, then the context. YOLO and the warning badge are dropped last. YOLO stays for a new session or a resumed transcript. Tool calls for a turn share one box, which keeps the last three lines until you click it. Each model completion is labeled `Turn N`, starting at 1, with a line under the label. The thinking box keeps the last three lines until you click it. With `--debug`, the first completion adds a cyan System box, and a later completion adds another only when the prompt changed. Every completion adds a cyan Request box. Both collapse the same way. Ctrl+H hides those with the thinking, tool, and compaction boxes. When the history is compacted, the transcript shows a compaction note and then the answer as Markdown.

One-shot on a tty, when the model returned reasoning, waits for Enter before the process exits.

Without a tty (piped input, or `--answer-only`), the same commands work as plain lines. `/help` does not list hotkeys.

## What a turn does

Nothing is loaded at the start. The system prompt names the date, the OS, a one-line listing of the workspace, and any saved memory keys, then a menu of skills. The model calls `load_skill` before it can use that skill's tools, and can keep several skills loaded. `unload_skill` drops one. Only the loaded tools, plus those two, are sent on the next completion.

Tool paths stay inside the session workspace, plus the `--project` directory when one is granted. Shell and Python run with `shell=False`, a short allowlist of environment variables, and a macOS Seatbelt profile that can read and write only that workspace and the project (plus the OS and Python runtime). `HOME` and the temp directory stay on the session workspace. Network is denied for those processes. `rm`, `mv`, `chmod`, and `sudo` are rejected before the process starts. `--yolo` skips that sandbox and those command checks, and lets read tools open any path. Pipes and redirects stay blocked. File mutation tools stay in the workspace or the project.

Web and API tools run in the parent process. They accept only public `http` and `https` URLs, follow redirects with the same check, and cap body size and time.

When context use reaches half the model's limit, older turns are summarized into one note. The system prompt, the current user message, and the latest unanswered tool result or finished answer stay put. `/compact` forces that summary.

An interactive session that uses up `max_turns` asks before stopping, and each yes grants another window of that many completions. Declining, or a run that is not interactive, ends with `Max turns reached.`

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
| `hardware-diagnosis` | Inventory and diagnose this Linux or macOS host |

Each skill's `SKILL.md` has YAML frontmatter (`name`, `description`, and optional `allowed-tools`) and freeform instructions that are returned when that skill is loaded. The skill name is the folder that contains `SKILL.md`, under `src/harnh3x/skills/<name>/` or `src/harnh3x/skills/<category>/<name>/`.

## Tools

These are the built-in tool names a skill can list in `allowed-tools`. Paths, shell commands, and HTTP hosts follow the sandbox rules above, whichever skill a tool is loaded through.

| Tool | Built-in skills | What it does |
| --- | --- | --- |
| `read_file` | file-ops, coding, documents, research, hardware-diagnosis | Read a text file, paged by line |
| `write_file` | file-ops, coding | Write a file, creating parent directories and replacing existing content |
| `edit_file` | file-ops, coding | Replace an exact text fragment in a file |
| `append_file` | file-ops | Append to the end of a file, creating it if missing |
| `delete_file` | file-ops | Delete a file |
| `move_file` | file-ops | Move or rename a file without overwriting |
| `list_files` | file-ops, coding, hardware-diagnosis | List files and directories with sizes |
| `search_files` | file-ops, coding, hardware-diagnosis | Search UTF-8 text files with a regular expression |
| `outline_python` | coding | Outline the classes and functions of a `.py` file or a directory |
| `run_shell` | shell, coding, hardware-diagnosis | Run one command and return its exit code, stdout, and stderr |
| `run_python` | python | Run a short standard-library Python snippet |
| `run_script` | coding | Run a `.py` file with arguments |
| `system_info` | hardware-diagnosis | Report the OS, machine, CPU, memory, storage, and attached hardware |
| `web_search` | research, hardware-diagnosis | Search the web for titles, URLs, and snippets |
| `read_url` | research, hardware-diagnosis | Fetch a page, PDF, or text URL and return its readable text |
| `fetch_url` | research | Download a URL into the workspace |
| `parse_html` | research | Extract the title and text of a saved HTML file |
| `read_feed` | research | Read the latest entries of an RSS or Atom feed |
| `wikipedia_search` | reference | Search Wikipedia article titles |
| `wikipedia_summary` | reference | Summarize a Wikipedia article by its exact title |
| `http_get_json` | http | GET a JSON API and return the parsed response |
| `http_request` | http | Send any HTTP request and return the status, headers, and body |
| `read_pdf` | documents, research | Extract text from a PDF |
| `read_docx` | documents | Extract the paragraphs of a Word `.docx` file |
| `inspect_csv` | data | Describe a CSV file's delimiter, columns, row count, and first rows |
| `inspect_json` | data | Describe a JSON file's keys, array lengths, and sample values |
| `remember` | memory | Save a fact under a key in `memory.md` |
| `recall` | memory | Return saved memories, optionally filtered by a word |
| `write_plan` | tasks | Write a numbered checklist to `plan.md` |
| `update_plan` | tasks | Mark a plan step done, todo, or skipped |
| `show_plan` | tasks | Show the current plan and how many steps are done |

Tools from an MCP server in your config are named `server:tool` in `allowed-tools`, where `server` is the key under `mcp` and `tool` is the name the server reports, such as `caido:caido_list_requests`. `server:*` takes every tool the server exposes after its `include` and `exclude` filters, plus `server_read_resource` when the server has resources. The names a server reports are listed when its skill loads, and in its own documentation.

## Custom Skills

You can add your own skills under `~/.harnh3x/skills/` (`<base_dir>/skills/` when `base_dir` is set). Each skill is a folder holding one `SKILL.md`, either `skills/<name>/SKILL.md` or, to group them, `skills/<category>/<name>/SKILL.md` at any depth. Nothing is created for you, and a missing directory changes nothing.

```markdown
---
name: web-triage
description: Triage captured HTTP traffic and note findings. Use when the user asks to review requests in Caido or summarize suspicious endpoints.
allowed-tools: caido:caido_list_requests caido:caido_send_request write_file read_file
---

## Workflow

- List recent requests with `caido_list_requests` before replaying any of them.
- Write findings to `findings.md` with `write_file`, one heading per endpoint.
- The final answer names the endpoints and the file path.
```

The file starts with YAML frontmatter between two `---` lines, using the same strict rules as the built-in skills:

| Key | Rule |
| --- | --- |
| `name` | Required. Lowercase letters, digits, and single hyphens (`web-triage`), at most 64 characters, and the same as the folder name. It must not match a built-in skill, an MCP server under `mcp`, or another custom skill. |
| `description` | Required. A non-empty string of at most 1024 characters. It is the skill's menu line, and the only thing the model sees before loading, so say what the skill does and when to use it. |
| `allowed-tools` | Optional. Space-separated tool names: built-in names from [Tools](#tools), `server:tool` for one MCP tool, or `server:*` for every tool of that server. A name may appear once. Without it, the skill is instructions only. |
| `license` | Optional non-empty string. |
| `compatibility` | Optional non-empty string of at most 500 characters. |
| `metadata` | Optional mapping of string keys to string values. |

Any other key makes the file malformed. Everything after the frontmatter is freeform Markdown, returned to the model when the skill is loaded. Put the workflow and reporting conventions there. A tool's own limits already travel with its schema.

Custom skills appear in the menu under `custom:` (or `custom/<category>:`), after the built-in skills and before `mcp:`. The model loads and unloads them like any other skill, and a custom skill can share tools with built-in and MCP skills. The status bar counts them separately, as the `+N` in the skills badge (`skills 12+1`).

A malformed skill is skipped and the rest still load. That covers bad frontmatter, an unknown tool, an MCP server that is not configured or is unavailable when the session starts, an MCP tool that server does not offer, and a name that is already taken. Each skipped skill adds to the count in the red `⚠ N` status-bar badge, and Ctrl+W, or a click on that badge, opens the reasons. If an MCP server stops during a session, the custom skills that use it report that the server is no longer available.

Skills are read when a session starts, so after editing a `SKILL.md`, press Ctrl+N or `/resume` to pick up the change.

## Development

```bash
poetry run pytest
poetry run ruff check
poetry run ruff format
```

Limits (turns, compaction threshold, fetch and shell caps, the default model) live in `src/harnh3x/config.py`. A new tool, or a looser path or shell check, needs a matching test under `tests/`, in the `test_*.py` file for that subsystem.
