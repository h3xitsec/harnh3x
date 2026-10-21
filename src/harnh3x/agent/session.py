import json
import threading
import uuid
from collections.abc import Callable
from pathlib import Path

from harnh3x import config
from harnh3x.agent.client import _stored_context_tokens
from harnh3x.agent.transcript import (
    _bind_project,
    _expand,
    _read_transcript,
    _session_workspace,
    _stored_coding,
    _stored_project,
    _workspaces_dir,
)
from harnh3x.config import CUSTOM_SKILLS_DIR, MODEL, TRANSCRIPT_FILE
from harnh3x.prompt import system_prompt
from harnh3x.rules import format_rules, load_project_rules
from harnh3x.skills import SkillRegistry


class Session:
    def __init__(
        self,
        workspace: Path | None = None,
        model: str | None = None,
        *,
        yolo: bool = False,
        project: Path | str | None = None,
        coding: bool = False,
        debug: bool = False,
    ):
        self.workspace = _session_workspace(workspace)
        self.model = model or MODEL
        self.yolo = yolo
        self.debug = debug
        self.on_debug: Callable[[str | None, str], None] | None = None
        self._debug_system: str | None = None
        self.project = (
            None if project is None else _bind_project(project, self.workspace)
        )
        if coding and self.project is None:
            raise ValueError("coding session requires a project directory")
        self.coding = coding
        self.skills = self._skills()
        self.applied_rules: set[str] = set()
        self.context_tokens: int | None = None
        self.context_limit: int | None = None
        self.on_compact: Callable[[str], None] | None = None
        self.on_tool_done: Callable[[str, str], None] | None = None
        self.on_model_turn: Callable[[int], None] | None = None
        self.on_subagent_start: Callable[[str, str], None] | None = None
        self.on_subagent_turn: Callable[[int], None] | None = None
        self.on_subagent_delta: Callable[[str, str], None] | None = None
        self.on_subagent_tool: Callable[[str, object], None] | None = None
        self.on_subagent_tool_done: Callable[[str, str], None] | None = None
        self.confirm_continue: Callable[[], bool] | None = None
        self.abort = threading.Event()
        self.messages: list[dict] = [self._system_message()]

    def _system_message(self) -> dict:
        rules = ""
        if self.coding:
            rules = format_rules(load_project_rules(self.project))
        return {
            "role": "system",
            "content": system_prompt(
                self.workspace,
                self.skills.get_menu(),
                yolo=self.yolo,
                project=self.project,
                rules=rules,
                coding=self.coding,
            ),
        }

    def _skills(self) -> SkillRegistry:
        return SkillRegistry(
            yolo=self.yolo,
            project=self.project,
            servers=config.MCP_SERVERS,
            custom_dir=_expand(config.BASE_DIR).resolve() / CUSTOM_SKILLS_DIR,
        )

    def reset(self) -> None:
        self.workspace = _session_workspace(_workspaces_dir() / str(uuid.uuid4()))
        if self.project is not None:
            self.project = _bind_project(self.project, self.workspace)
        self.skills.close()
        self.skills = self._skills()
        self.applied_rules = set()
        self.context_tokens = None
        self._debug_system = None
        self.abort.clear()
        self.messages = [self._system_message()]

    def sync_prompt(self) -> None:
        self.messages[0]["content"] = self._system_message()["content"]

    def resume(self, workspace: Path) -> None:
        path = _expand(workspace).resolve()
        data = _read_transcript(path / TRANSCRIPT_FILE)
        self.workspace = path
        self.project = _stored_project(data, path)
        self.coding = _stored_coding(data, self.project)
        self.model = data["model"]
        self.messages = data["messages"]
        self.context_tokens = _stored_context_tokens(data)
        self.context_limit = None
        self.skills.close()
        self.skills = self._skills()
        self.applied_rules = set()
        self._debug_system = None
        self.abort.clear()
        _replay_skills(self)
        _load_saved_skills(self, data)
        self.sync_prompt()


def _skill_name(arguments: object) -> str | None:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return None
    if not isinstance(arguments, dict):
        return None
    name = arguments.get("name", "")
    return name if isinstance(name, str) else None


def _replay_skills(session: Session) -> None:
    for message in session.messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            skill = _skill_name(function.get("arguments"))
            if skill is None:
                continue
            if name == "load_skill":
                session.skills.load_skill(skill)
            elif name == "unload_skill":
                session.skills.unload_skill(skill)


def _load_saved_skills(session: Session, data: dict) -> None:
    names = data.get("skills")
    if not isinstance(names, list):
        return
    active = set(session.skills.loaded_names())
    for name in names:
        if isinstance(name, str) and name not in active:
            session.skills.load_skill(name)
            active.add(name)
