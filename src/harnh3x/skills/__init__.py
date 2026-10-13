from dataclasses import dataclass
from pathlib import Path

from harnh3x.prompt import project_note, yolo_note
from harnh3x.tools import TOOLS, execute_tool

_YOLO_SKILLS = {"file_ops", "shell", "python", "http", "research", "reference"}
_PROJECT_SKILLS = {"file_ops", "shell", "python", "coding"}

SKILLS_DIR = Path(__file__).resolve().parent

SKILL_TOOLS = {
    "file_ops": (
        "read_file",
        "write_file",
        "edit_file",
        "append_file",
        "delete_file",
        "move_file",
        "list_files",
        "search_files",
    ),
    "shell": ("run_shell",),
    "python": ("run_python",),
    "coding": ("read_source", "write_source", "edit_source", "run_script"),
    "research": ("web_search", "read_url", "fetch_url", "parse_html", "read_feed"),
    "reference": ("wikipedia_search", "wikipedia_summary"),
    "http": ("http_get_json", "http_request"),
    "documents": ("read_pdf", "read_docx"),
    "data": ("inspect_csv", "inspect_json"),
    "memory": ("remember", "recall"),
    "tasks": ("write_plan", "update_plan", "show_plan"),
    "sql-injection": ("http_request",),
}


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    doc: str
    tools: tuple
    category: str = ""


def _blurb(doc: str) -> str:
    for line in doc.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        return stripped
    return ""


def _tool_schemas(names: tuple[str, ...], tools: list) -> tuple:
    schemas = {tool["function"]["name"]: tool for tool in tools}
    chosen = []
    for name in names:
        schema = schemas.get(name)
        if schema is None:
            raise RuntimeError(f"tool '{name}' is missing from TOOLS")
        chosen.append(schema)
    return tuple(chosen)


def _skill_files(skills_dir: Path) -> list[Path]:
    files = []
    for skill_md in skills_dir.rglob("SKILL.md"):
        if skill_md.parent == skills_dir or not skill_md.is_file():
            continue
        files.append(skill_md)
    return sorted(files, key=lambda path: path.relative_to(skills_dir).as_posix())


def _load_catalog(
    skills_dir: Path = SKILLS_DIR,
    manifest: dict | None = None,
    tools: list | None = None,
) -> dict[str, Skill]:
    manifest = SKILL_TOOLS if manifest is None else manifest
    tools = TOOLS if tools is None else tools
    found: dict[str, Skill] = {}
    paths: dict[str, str] = {}
    for skill_md in _skill_files(skills_dir):
        skill_dir = skill_md.parent
        relative = skill_dir.relative_to(skills_dir)
        name = skill_dir.name
        location = relative.as_posix()
        if name in paths:
            raise RuntimeError(
                f"duplicate skill '{name}' in {paths[name]} and {location}"
            )
        paths[name] = location
        tool_names = manifest.get(name)
        if tool_names is None:
            raise RuntimeError(f"skill '{name}' has no tool manifest")
        category = "" if relative.parent == Path(".") else relative.parent.as_posix()
        doc = skill_md.read_text(encoding="utf-8")
        found[name] = Skill(
            name=name,
            description=_blurb(doc),
            doc=doc,
            tools=_tool_schemas(tuple(tool_names), tools),
            category=category,
        )
    missing = set(manifest) - set(found)
    if missing:
        joined = ", ".join(sorted(missing))
        raise RuntimeError(f"skill manifest has no SKILL.md for: {joined}")
    ordered = sorted(
        found.values(),
        key=lambda skill: (bool(skill.category), skill.category, skill.name),
    )
    return {skill.name: skill for skill in ordered}


_CATALOG = _load_catalog()


def _meta_tool(name: str, description: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill name from the menu",
                    }
                },
                "required": ["name"],
            },
        },
    }


_META_TOOLS = (
    _meta_tool("load_skill", "Load a skill by name to activate its tools"),
    _meta_tool("unload_skill", "Unload a skill to free its tools"),
)


class SkillRegistry:
    def __init__(self, *, yolo: bool = False, project: Path | None = None):
        self._catalog = _CATALOG
        self._active: dict[str, Skill] = {}
        self._yolo = yolo
        self._project = project

    def skill_tools(self, name: str) -> tuple[str, ...] | None:
        skill = self._catalog.get(name)
        if skill is None:
            return None
        return tuple(tool["function"]["name"] for tool in skill.tools)

    def loaded_names(self) -> list[str]:
        return sorted(self._active)

    def get_menu(self) -> str:
        lines = [
            "Available skills (use load_skill to activate).",
            "A skill name is not a tool. After loading, call only the tools listed for that skill.",
        ]
        category = ""
        for name, skill in self._catalog.items():
            if skill.category != category:
                category = skill.category
                if category:
                    lines.append(f"{category}:")
            status = ""
            if name in self._active:
                listed = ", ".join(self.skill_tools(name) or ())
                status = f" [loaded: {listed}]"
            lines.append(f"- {name}: {skill.description}{status}")
        return "\n".join(lines)

    def load_skill(self, name: str) -> str:
        if not isinstance(name, str) or name not in self._catalog:
            shown = name if isinstance(name, str) else ""
            return f"Error: Unknown skill '{shown}'. Check the skill menu."
        if name in self._active:
            tools = ", ".join(self.skill_tools(name) or ())
            return f"Skill '{name}' is already loaded. Call one of: {tools}."
        skill = self._catalog[name]
        self._active[name] = skill
        tool_names = [tool["function"]["name"] for tool in skill.tools]
        text = (
            f"Loaded skill '{name}' with {len(skill.tools)} tools: "
            f"{', '.join(tool_names)}\n\n"
            f"Documentation:\n{skill.doc}"
        )
        if self._yolo and name in _YOLO_SKILLS:
            text += f"\n\n{yolo_note(self._project)}"
        if self._project is not None and name in _PROJECT_SKILLS:
            text += f"\n\n{project_note(self._project)}"
        return text

    def unload_skill(self, name: str) -> str:
        if name not in self._active:
            return f"Skill '{name}' is not loaded."
        del self._active[name]
        return f"Unloaded skill '{name}'."

    def schemas(self) -> list[dict]:
        active = []
        for skill in self._active.values():
            active.extend(skill.tools)
        return list(_META_TOOLS) + active

    def _skill_name(self, args: dict) -> str:
        name = args.get("name", "")
        return name if isinstance(name, str) else ""

    def execute(
        self,
        name: str,
        args: dict,
        workspace: Path,
        *,
        yolo: bool = False,
        project: Path | None = None,
    ) -> str:
        if name == "load_skill":
            return self.load_skill(self._skill_name(args))
        if name == "unload_skill":
            return self.unload_skill(self._skill_name(args))
        loaded = {
            tool["function"]["name"]
            for skill in self._active.values()
            for tool in skill.tools
        }
        if name not in loaded:
            return self._unknown_tool(name, args)
        return execute_tool(name, args, workspace, yolo=yolo, project=project)

    def _unknown_tool(self, name: str, args: dict) -> str:
        tools = self.skill_tools(name)
        if tools is None:
            return f"Error: Tool '{name}' not found. Is the skill loaded?"
        listed = ", ".join(tools)
        hinted = ""
        if isinstance(args, dict):
            keys = [key for key in args if isinstance(key, str) and key in tools]
            if len(keys) == 1:
                hinted = f" Call {keys[0]} directly, with its own parameters."
        if name in self._active:
            return (
                f"Error: '{name}' is a skill, not a tool. It is already loaded. "
                f"Call one of: {listed}.{hinted}"
            )
        return (
            f"Error: '{name}' is a skill, not a tool. "
            f"Call load_skill with name '{name}', then call one of: {listed}.{hinted}"
        )
