import platform
from datetime import datetime
from pathlib import Path

from harnh3x.tools import memory_keys, workspace_listing

_SHELL_YOLO = (
    "run_shell is /bin/sh -c, so pipes, redirects, `;`, `&&`, backticks, and $ run. "
)

_HTTP_YOLO = (
    "HTTP tools may use any host, including localhost and private addresses. "
    "The public-host limit in a skill doc does not apply."
)

YOLO_NOTE = (
    "Yolo mode is on. The seatbelt sandbox is off. "
    "read_file and the other read tools may use any filesystem path. "
    "run_shell and run_python may run any program, including sudo, and may read or write any path. "
    + _SHELL_YOLO
    + "write_file, edit_file, append_file, delete_file, move_file, and run_script stay inside the workspace. "
    + _HTTP_YOLO
)


def yolo_note(project: Path | None = None) -> str:
    if project is None:
        return YOLO_NOTE
    return (
        "Yolo mode is on. The seatbelt sandbox is off. "
        "read_file and the other read tools may use any filesystem path. "
        "run_shell and run_python may run any program, including sudo, and may read or write any path. "
        + _SHELL_YOLO
        + "write_file, edit_file, append_file, delete_file, move_file, and run_script "
        f"stay inside the workspace or the project at {project}. " + _HTTP_YOLO
    )


def _memory_line(keys: list[str]) -> str:
    if not keys:
        return "No saved memories yet. Load the memory skill to keep facts the user wants remembered."
    return (
        f"Saved memories: {', '.join(keys)}. "
        "Load the memory skill and call recall before answering about them."
    )


def system_prompt(
    workspace: Path,
    menu: str,
    now: datetime | None = None,
    *,
    yolo: bool = False,
    project: Path | None = None,
    rules: str = "",
    coding: bool = False,
) -> str:
    moment = now or datetime.now().astimezone()
    today = moment.strftime("%A, %B %d, %Y, %H:%M %Z").rstrip()
    note = f"\n{yolo_note(project)}\n" if yolo else ""
    rules_block = f"{rules}\n" if rules else ""
    if project is None:
        project_line = ""
        code_home = "the workspace"
    else:
        project_line = (
            f" Your project is {project}. Relative paths and the shell working directory use the project."
            " Downloads, plans, and this session's notes stay in the workspace."
        )
        code_home = "the project"
    header = f"""Today is {today}. The host runs {platform.system()}. Your workspace is {workspace}.{project_line}
{workspace_listing(workspace)}
{_memory_line(memory_keys())}"""
    skills = """Call load_skill before using a capability. Several skills can stay loaded at once; unload a skill only when you are sure you will not need it again.
Use the tool interface. Do not write a tool call as JSON in your reply.
If a tool returns an error, tell the user the error. Do not guess."""
    if coding:
        return f"""You are a coding agent working in a granted project.
{header}
Read the project before you change it, and match the code already there.
When the user writes @path, that path is a file or folder in the project; read it before you answer. A trailing slash is a folder.
{skills}
Load the coding skill before you read or change source; it can list, search, edit, and run commands in the project. Use the python skill only to compute an answer.
When a job takes several steps, load the tasks skill, write a plan, and work through it.
Follow the project rules in this prompt. When a listed rule has no globs, read that .mdc file if its description matches the task.
Make the smallest change that does the job. Prefer editing an existing file over rewriting it.
Always confirm what you changed, including the paths.
{note}
{rules_block}{menu}"""
    return f"""You are a helpful personal assistant.
{header}
{skills}
Answer from sources you have read: search snippets are not answers, so read the pages that matter before answering, and cite them.
When a job takes several steps, load the tasks skill, write a plan, and work through it.
When the user asks for a program, script, or code change, load the coding skill and write it to a file in {code_home}. Use the python skill only to compute an answer.
Always confirm what you did after completing a task.
{note}
{rules_block}{menu}"""
