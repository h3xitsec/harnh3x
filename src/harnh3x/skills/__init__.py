from dataclasses import dataclass
from pathlib import Path

from harnh3x.prompt import YOLO_NOTE
from harnh3x.tools import TOOLS, execute_tool

_YOLO_SKILLS = {"file_ops", "shell", "python"}

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
    "api": ("http_get_json",),
    "documents": ("read_pdf", "read_docx"),
    "data": ("inspect_csv", "inspect_json"),
    "memory": ("remember", "recall"),
    "tasks": ("write_plan", "update_plan", "show_plan"),
}


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    doc: str
    tools: tuple


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


def _load_catalog(
    skills_dir: Path = SKILLS_DIR,
    manifest: dict | None = None,
    tools: list | None = None,
) -> dict[str, Skill]:
    manifest = SKILL_TOOLS if manifest is None else manifest
    tools = TOOLS if tools is None else tools
    catalog: dict[str, Skill] = {}
    found: set[str] = set()
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        name = skill_dir.name
        found.add(name)
        tool_names = manifest.get(name)
        if tool_names is None:
            raise RuntimeError(f"skill '{name}' has no tool manifest")
        doc = skill_md.read_text(encoding="utf-8")
        catalog[name] = Skill(
            name=name,
            description=_blurb(doc),
            doc=doc,
            tools=_tool_schemas(tuple(tool_names), tools),
        )
    missing = set(manifest) - found
    if missing:
        joined = ", ".join(sorted(missing))
        raise RuntimeError(f"skill manifest has no SKILL.md for: {joined}")
    return catalog


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
    def __init__(self, *, yolo: bool = False):
        self._catalog = _CATALOG
        self._active: dict[str, Skill] = {}
        self._yolo = yolo

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
        for name, skill in self._catalog.items():
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
            return f"Skill '{name}' is already loaded."
        skill = self._catalog[name]
        self._active[name] = skill
        tool_names = [tool["function"]["name"] for tool in skill.tools]
        text = (
            f"Loaded skill '{name}' with {len(skill.tools)} tools: "
            f"{', '.join(tool_names)}\n\n"
            f"Documentation:\n{skill.doc}"
        )
        if self._yolo and name in _YOLO_SKILLS:
            text += f"\n\n{YOLO_NOTE}"
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
        self, name: str, args: dict, workspace: Path, *, yolo: bool = False
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
            return f"Error: Tool '{name}' not found. Is the skill loaded?"
        return execute_tool(name, args, workspace, yolo=yolo)
