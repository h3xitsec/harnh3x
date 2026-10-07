import platform
from datetime import datetime
from pathlib import Path

from harness.tools import memory_keys, workspace_listing


def _memory_line(keys: list[str]) -> str:
    if not keys:
        return "No saved memories yet. Load the memory skill to keep facts the user wants remembered."
    return (
        f"Saved memories: {', '.join(keys)}. "
        "Load the memory skill and call recall before answering about them."
    )


def system_prompt(workspace: Path, menu: str, now: datetime | None = None) -> str:
    moment = now or datetime.now().astimezone()
    today = moment.strftime("%A, %B %d, %Y, %H:%M %Z").rstrip()
    return f"""You are a helpful personal assistant.
Today is {today}. The host runs {platform.system()}. Your workspace is {workspace}.
{workspace_listing(workspace)}
{_memory_line(memory_keys(workspace))}
Call load_skill before using a capability. Several skills can stay loaded at once; unload a skill only when you are sure you will not need it again.
A skill name is not a tool. After loading a skill, call one of the tools listed for it.
Use the tool interface. Do not write a tool call as JSON in your reply.
If a tool returns an error, tell the user the error. Do not guess.
Answer from sources you have read: search snippets are not answers, so read the pages that matter before answering, and cite them.
When a job takes several steps, load the tasks skill, write a plan, and work through it.
Always confirm what you did after completing a task.

{menu}"""
