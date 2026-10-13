---
name: conventional-commit
description: >-
  Prepares Conventional Commits messages for this repo (release-please), using git diffs and
  agent transcript search for intent. Subjects are changelog-ready: short, outcome-focused
  (what the feat/fix does), not implementation detail. Prefers split commits when changes span
  multiple concerns or chat sessions; proposes commit groups and messages for user approval
  before git add/commit. Use when the user asks to commit, prepare a commit, or write a
  conventional commit message.
---

# Conventional commits (harnh3x)

Use this skill when the user wants to commit working-tree changes with messages that satisfy [release-please](https://github.com/googleapis/release-please) for this repo.

[`release-please-config.json`](../../../release-please-config.json) uses the `python` release type and does not override changelog sections or the versioning strategy. Defaults apply:

- `feat` → minor bump, **Features**
- `fix` → patch bump, **Bug Fixes**
- `feat!:` / `fix!:` or a `BREAKING CHANGE:` footer → major bump
- `perf`, `deps`, and `revert` appear in the changelog when a release is cut, and do not bump the version on their own
- `docs`, `chore`, `refactor`, `test`, `ci`, `build`, and `style` stay out of the changelog and do not bump

Release-please writes the version into `pyproject.toml` and `src/harnh3x/__init__.py` (`extra-files`) on its release PR, and updates [`.release-please-manifest.json`](../../../.release-please-manifest.json). Do not hand-edit those version strings in a feature or fix commit.

Readers of the changelog install and run the `harnh3x` CLI. Subjects should say what that person can do, or what stopped failing.

## Canonical format

```
<type>[(scope)]: <short summary>

[optional body]
```

- **Subject:** imperative mood, no trailing period, aim for ~72 characters.
- **Changelog tone:** For `feat` and `fix` (and `perf` when it will ship with a release), write the subject as the **one-line release note**. State **what the change does** for someone using the CLI, TUI, tools, or config — not how it is implemented. Keep it **short**. Put file paths, flags internals, and step-by-step detail in the body.
- **Body:** explain *why* (intent, trade-offs), not a line-by-line recap of the diff.

**Subject quick test:** Would this sentence read naturally as a bullet in the published changelog? If it sounds like an internal commit log (“add token counter widget to header”, “tighten seatbelt profile generation”) rewrite it to the outward effect (“show context used against the model limit”, “refuse shell when the sandbox cannot be applied”).

| Type | Use for |
|------|---------|
| `feat` | New user-facing behavior |
| `fix` | Bug fix |
| `perf` | Performance improvement |
| `deps` | Dependency updates worth a changelog line |
| `docs` | Documentation only |
| `chore` | Maintenance and tooling noise |
| `refactor`, `test`, `ci`, `build`, `style` | Those concerns, when they are not part of a feat or fix |

**Scopes (optional):** `cli`, `tui`, `agent`, `tools`, `skills`, `sandbox`, `config`, `prompt`, `ci`, `docs`. Match the module or surface that changed. Omit the scope when the change spans several of them for one outcome.

## Workflow

### 1. Gather git context

From repo root:

- `git status` — list modified, staged, untracked paths.
- `git diff` — unstaged changes.
- `git diff --staged` — already staged changes.
- `git log --oneline -5` — recent subjects. History before release-please may be informal; new commits still follow this skill.

### 2. Recover intent from agent transcripts (when useful)

Cursor stores parent chat transcripts under the path mentioned in the system prompt (e.g. `agent-transcripts/<uuid>/<uuid>.jsonl`), **outside** the repo.

Each `agent-transcripts/<uuid>/` directory is typically **one chat session**. Mapping changed files to sessions is the main signal for **splitting commits**.

- Build a list of changed paths from `git status` (relative to repo root).
- Search those path strings across transcript `.jsonl` files.
- Record **which UUID directory** each path appears in (and note paths that appear in **multiple** UUIDs).
- Read small windows around hits to recover the user's goal **per session**.
- If nothing matches or transcripts are unavailable, infer intent from the diff and the current chat; still apply the splitting rules below using cohesion of paths and change types.

### 3. Compose proposal — prefer split commits

**Default bias:** propose **multiple commits** when in doubt. A single commit is acceptable only when every changed file clearly belongs to **one** user goal and **one** narrative (one session, one feat *or* one fix, same area).

**Always split** when either applies:

1. **Multiple sessions:** changed files are tied to **different** transcript UUIDs — **one commit per session** (group files that share the same primary session; if a file shows up in multiple sessions, assign it to the session that matches its main change, or ask the user).
2. **Multiple releasable intents:** more than one independent `feat` and/or independent `fix`, or an unrelated `feat` plus `fix` — **one commit per intent**, unless they are the same small cohesive change.
3. **Unrelated areas:** e.g. TUI behavior, CI, and an unrelated tool change — split by concern.

**Do not split** when:

- Changes are a tight bundle (feature + its tests in `tests/test_harnh3x.py` + docs for that feature only; or a fix and its follow-up in the same module from the same task).

For each proposed commit: **one conventional type**, **one scope** when it fits, **one changelog-ready subject**, body explains **why** for that slice only.

### 4. Present summary for approval (required)

**Do not `git commit` until the user approves.**

Output a clear summary:

1. **Split rationale** (when more than one commit).
2. **Commit 1**:
   - Full proposed message (subject + blank line + body).
   - Bulleted list of paths to `git add` for this commit.
3. **Commit 2, …** — same structure.
4. **Skipped / not staged:** `.env`, `src/.env`, `harnh3x.yaml` with keys, `*.pem`, and other secrets. Call these out explicitly.
5. Ask the user to confirm, adjust the split, or edit wording.

### 5. Execute only after approval

After explicit user approval:

1. `git add` only the agreed paths for the first commit (repeat per commit if splitting).
2. Commit with a heredoc so the body is preserved, e.g.:

```bash
git commit -m "$(cat <<'EOF'
feat(tui): show context used against the model limit

The header reports tokens against the model's window so a long session
is visible before compaction.
EOF
)"
```

3. If multiple commits: stage and commit the next set; repeat.
4. `git status` — confirm clean or report remaining files.

**Safety:** never stage or commit obvious secrets; when unsure, list the file and ask. Do not commit `src/.env`.

## Example

**Proposed commit**

```
feat(cli): resume a past session from its transcript

/resume reloads the workspace transcript so a stopped chat continues
with the same tools and files.
```

**Avoid** an implementation-heavy subject such as `feat(cli): add resume picker, transcript.json reload, and skill replay in Session.resume`.

**Files to stage**

- `src/harnh3x/cli.py`
- `src/harnh3x/agent.py`
- `tests/test_harnh3x.py`

**Skipped**

- `src/.env`

Wait for approval before running `git add` / `git commit`.

**Split example:** one session changes how the TUI shows reasoning; another fixes seatbelt path checks. That is two commits (`feat(tui): …` and `fix(sandbox): …`), each with its own files. A new tool and the tests that cover it stay in one commit.

## References

- Release config: [`release-please-config.json`](../../../release-please-config.json)
- Workflow: [`.github/workflows/release.yml`](../../../.github/workflows/release.yml)
- Install notes: [`README.md`](../../../README.md) (**Install**)
