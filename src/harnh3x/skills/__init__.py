import atexit
import re
import weakref
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

from harnh3x.config import McpServer
from harnh3x.mcp.client import McpClient, McpError, upstream_problem
from harnh3x.tools import TOOLS, execute_tool
from harnh3x.tools.scope import scoped

SKILLS_DIR = Path(__file__).resolve().parent

# Budget for a shipped SKILL.md, checked by the test suite.
_METADATA_TOKEN_LIMIT = 100
_BODY_TOKEN_LIMIT = 5000
_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FRONT_KEYS = {
    "name",
    "description",
    "license",
    "compatibility",
    "metadata",
    "allowed-tools",
}


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    doc: str
    tools: tuple
    category: str = ""
    custom: bool = False


def _ordered(found: dict[str, Skill]) -> dict[str, Skill]:
    ordered = sorted(
        found.values(),
        key=lambda skill: (bool(skill.category), skill.category, skill.name),
    )
    return {skill.name: skill for skill in ordered}


def _tool_schemas(names: tuple[str, ...], tools: list) -> tuple:
    schemas = {tool["function"]["name"]: tool for tool in tools}
    chosen = []
    for name in names:
        schema = schemas.get(name)
        if schema is None:
            raise RuntimeError(f"tool '{name}' is missing from TOOLS")
        chosen.append(schema)
    return tuple(chosen)


def _tool_owners(catalog: dict[str, Skill]) -> dict[str, tuple[str, ...]]:
    owners: dict[str, list[str]] = {}
    for name, skill in catalog.items():
        for tool in skill.tools:
            owners.setdefault(tool["function"]["name"], []).append(name)
    return {tool: tuple(names) for tool, names in owners.items()}


def _skill_files(skills_dir: Path) -> list[Path]:
    files = []
    for skill_md in skills_dir.rglob("SKILL.md"):
        if skill_md.parent == skills_dir or not skill_md.is_file():
            continue
        files.append(skill_md)
    return sorted(files, key=lambda path: path.relative_to(skills_dir).as_posix())


def _token_count(text: str) -> int:
    if not text:
        return 0
    return (len(text) + 3) // 4


def _skill_sections(text: str, location: str) -> tuple[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise RuntimeError(f"skill '{location}' has no frontmatter")
    end = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = index
            break
    if end is None:
        raise RuntimeError(f"skill '{location}' frontmatter is not closed")
    raw = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :])
    body = body.removeprefix("\n")
    return raw, body.rstrip("\n")


def _frontmatter(text: str, location: str) -> tuple[dict, str]:
    raw, body = _skill_sections(text, location)
    try:
        loaded = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"skill '{location}' frontmatter is invalid") from exc
    if not isinstance(loaded, dict):
        raise RuntimeError(f"skill '{location}' frontmatter must be a mapping")
    unknown = sorted(set(loaded) - _FRONT_KEYS)
    if unknown:
        joined = ", ".join(unknown)
        raise RuntimeError(f"skill '{location}' has unknown frontmatter key: {joined}")
    return loaded, body


def _required_name(fields: dict, directory: str, location: str) -> str:
    name = fields.get("name")
    if (
        not isinstance(name, str)
        or not 1 <= len(name) <= 64
        or _NAME.fullmatch(name) is None
    ):
        raise RuntimeError(f"skill '{location}' has an invalid name")
    if name != directory:
        raise RuntimeError(
            f"skill name '{name}' does not match directory '{directory}'"
        )
    return name


def _required_description(fields: dict, location: str) -> str:
    description = fields.get("description")
    if not isinstance(description, str):
        raise RuntimeError(f"skill '{location}' description must be a string")
    if not description.strip():
        raise RuntimeError(f"skill '{location}' description is empty")
    if len(description) > 1024:
        raise RuntimeError(f"skill '{location}' description is too long")
    return description


def _optional_text(fields: dict, key: str, location: str, limit: int | None) -> None:
    if key not in fields:
        return
    value = fields[key]
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"skill '{location}' {key} must be a string")
    if limit is not None and len(value) > limit:
        raise RuntimeError(f"skill '{location}' {key} is too long")


def _optional_metadata(fields: dict, location: str) -> None:
    if "metadata" not in fields:
        return
    metadata = fields["metadata"]
    if not isinstance(metadata, dict):
        raise RuntimeError(f"skill '{location}' metadata must be a mapping")
    for key, value in metadata.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise RuntimeError(f"skill '{location}' metadata values must be strings")


def _allowed_tools(fields: dict, location: str) -> tuple[str, ...]:
    if "allowed-tools" not in fields or fields["allowed-tools"] is None:
        return ()
    raw = fields["allowed-tools"]
    if not isinstance(raw, str):
        raise RuntimeError(f"skill '{location}' allowed-tools must be a string")
    names = tuple(raw.split())
    if len(names) != len(set(names)):
        raise RuntimeError(f"skill '{location}' lists a tool more than once")
    return names


def _read_skill(
    skill_md: Path, directory: str, location: str
) -> tuple[str, str, str, tuple[str, ...]]:
    try:
        text = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"skill '{location}' could not be read") from exc
    fields, body = _frontmatter(text, location)
    name = _required_name(fields, directory, location)
    description = _required_description(fields, location)
    _optional_text(fields, "license", location, None)
    _optional_text(fields, "compatibility", location, 500)
    _optional_metadata(fields, location)
    return name, description, body, _allowed_tools(fields, location)


def _load_catalog(
    skills_dir: Path = SKILLS_DIR,
    tools: list | None = None,
) -> dict[str, Skill]:
    tools = TOOLS if tools is None else tools
    found: dict[str, Skill] = {}
    paths: dict[str, str] = {}
    for skill_md in _skill_files(skills_dir):
        skill_dir = skill_md.parent
        relative = skill_dir.relative_to(skills_dir)
        location = relative.as_posix()
        name, description, body, tool_names = _read_skill(
            skill_md, skill_dir.name, location
        )
        if name in paths:
            raise RuntimeError(
                f"duplicate skill '{name}' in {paths[name]} and {location}"
            )
        paths[name] = location
        category = "" if relative.parent == Path(".") else relative.parent.as_posix()
        found[name] = Skill(
            name=name,
            description=description,
            doc=body,
            tools=_tool_schemas(tool_names, tools),
            category=category,
        )
    return _ordered(found)


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

# Registries with MCP processes, closed once at exit. A new session builds a
# new registry, so one handler over a weak set keeps handlers from piling up.
_live_registries: weakref.WeakSet = weakref.WeakSet()
_exit_registered = False


def _register_for_exit(registry: "SkillRegistry") -> None:
    global _exit_registered
    _live_registries.add(registry)
    if not _exit_registered:
        atexit.register(_close_live_registries)
        _exit_registered = True


def _close_live_registries() -> None:
    for registry in list(_live_registries):
        registry.close()


class SkillRegistry:
    def __init__(
        self,
        *,
        yolo: bool = False,
        project: Path | None = None,
        servers: Sequence[McpServer] = (),
        custom_dir: Path | None = None,
    ):
        catalog = dict(_CATALOG)
        self._clients: dict[str, McpClient] = {}
        # Custom skill name -> {MCP tool name: server name}.
        self._remote: dict[str, dict[str, str]] = {}
        self._active: dict[str, Skill] = {}
        self.warnings: list[tuple[str, str]] = []
        for server in servers:
            if server.name in catalog or server.name in {"load_skill", "unload_skill"}:
                raise RuntimeError(
                    f"mcp server '{server.name}' matches a built-in skill"
                )
        for server in servers:
            skill = self._handshake(server)
            if skill is not None:
                catalog[server.name] = skill
        if custom_dir is not None:
            configured = {server.name for server in servers}
            taken = set(catalog) | configured | {"load_skill", "unload_skill"}
            catalog.update(self._load_custom(custom_dir, taken, configured))
        self._catalog = _ordered(catalog)
        self._owners = _tool_owners(self._catalog)
        self._yolo = yolo
        self._project = project
        self._scoped_cache: dict[str, tuple[dict, dict]] = {}
        if self._clients:
            _register_for_exit(self)

    def _handshake(self, server: McpServer) -> Skill | None:
        problem = upstream_problem(server)
        if problem is not None:
            self.warnings.append((server.name, problem))
            return None
        client = McpClient(server)
        try:
            client.connect()
        except McpError as exc:
            self.warnings.append((server.name, str(exc)))
            return None
        schemas = client.schemas()
        collision = self._collision(server.name, schemas)
        if collision is not None:
            client.close()
            self.warnings.append((server.name, collision.removeprefix("Error: ")))
            return None
        self._clients[server.name] = client
        return Skill(
            name=server.name,
            description=server.description or client.summary(),
            doc=client.documentation(),
            tools=tuple(schemas),
            category="mcp",
        )

    def _load_custom(
        self, custom_dir: Path, taken: set[str], configured: set[str]
    ) -> dict[str, Skill]:
        found: dict[str, Skill] = {}
        if not custom_dir.is_dir():
            return found
        for skill_md in _skill_files(custom_dir):
            skill_dir = skill_md.parent
            relative = skill_dir.relative_to(custom_dir)
            location = relative.as_posix()
            try:
                name, description, body, tool_names = _read_skill(
                    skill_md, skill_dir.name, location
                )
                if name in taken or name in found:
                    raise RuntimeError(
                        f"skill '{location}' uses the name '{name}', "
                        "which another skill already has"
                    )
                tools, remote = self._custom_tools(tool_names, location, configured)
            except RuntimeError as exc:
                self.warnings.append((location, str(exc)))
                continue
            category = "custom"
            if relative.parent != Path("."):
                category = f"custom/{relative.parent.as_posix()}"
            found[name] = Skill(
                name=name,
                description=description,
                doc=body,
                tools=tools,
                category=category,
                custom=True,
            )
            if remote:
                self._remote[name] = remote
        return found

    def _custom_tools(
        self, names: tuple[str, ...], location: str, configured: set[str]
    ) -> tuple[tuple, dict[str, str]]:
        builtin = {tool["function"]["name"]: tool for tool in TOOLS}
        chosen: dict[str, dict] = {}
        remote: dict[str, str] = {}
        for entry in names:
            server, colon, tool = entry.partition(":")
            if not colon:
                schema = builtin.get(entry)
                if schema is None:
                    raise RuntimeError(
                        f"skill '{location}' lists unknown tool '{entry}'"
                    )
                chosen.setdefault(entry, schema)
                continue
            if not server or not tool:
                raise RuntimeError(f"skill '{location}' lists invalid tool '{entry}'")
            client = self._clients.get(server)
            if client is None:
                if server in configured:
                    raise RuntimeError(
                        f"skill '{location}' needs MCP server '{server}', "
                        "which is unavailable"
                    )
                raise RuntimeError(
                    f"skill '{location}' lists MCP server '{server}', "
                    "which is not configured"
                )
            offered = client.schemas()
            if tool != "*":
                offered = [s for s in offered if s["function"]["name"] == tool]
                if not offered:
                    raise RuntimeError(
                        f"skill '{location}' lists '{entry}', "
                        f"which MCP server '{server}' does not offer"
                    )
            for schema in offered:
                tool_name = schema["function"]["name"]
                if remote.get(tool_name, server) != server:
                    raise RuntimeError(
                        f"skill '{location}' lists tool '{tool_name}' "
                        "from more than one MCP server"
                    )
                chosen.setdefault(tool_name, schema)
                remote[tool_name] = server
        return tuple(chosen.values()), remote

    def skill_tools(self, name: str) -> tuple[str, ...] | None:
        skill = self._active.get(name) or self._catalog.get(name)
        if skill is None:
            return None
        return tuple(tool["function"]["name"] for tool in skill.tools)

    def close(self) -> None:
        _live_registries.discard(self)
        for client in self._clients.values():
            client.close()

    def loaded_names(self) -> list[str]:
        return sorted(self._active)

    def available_counts(self) -> tuple[int, int, int, int, int]:
        skills = custom = servers = 0
        builtin: set[str] = set()
        remote: set[str] = set()
        for name, skill in self._catalog.items():
            current = self._active.get(name, skill)
            origins = self._remote.get(name, {})
            if skill.category == "mcp":
                servers += 1
            elif skill.custom:
                custom += 1
            else:
                skills += 1
            for tool in current.tools:
                function = tool.get("function") if isinstance(tool, dict) else None
                tool_name = function.get("name") if isinstance(function, dict) else None
                if not isinstance(tool_name, str):
                    continue
                if skill.category == "mcp" or tool_name in origins:
                    remote.add(tool_name)
                else:
                    builtin.add(tool_name)
        return skills, len(builtin), custom, servers, len(remote)

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
        if name in self._clients:
            loaded = self._connect(name, skill)
            if loaded is None:
                return self._connect_error
            skill = loaded
        for server in sorted(set(self._remote.get(name, {}).values())):
            if server not in self._clients:
                return (
                    f"Error: skill '{name}' needs MCP server '{server}', "
                    "which is no longer available."
                )
        self._active[name] = skill
        tool_names = [tool["function"]["name"] for tool in skill.tools]
        return (
            f"Loaded skill '{name}' with {len(skill.tools)} tools: "
            f"{', '.join(tool_names)}\n\n"
            f"Documentation:\n{skill.doc}"
        )

    def unload_skill(self, name: str) -> str:
        if name not in self._active:
            return f"Skill '{name}' is not loaded."
        del self._active[name]
        return f"Unloaded skill '{name}'."

    def schemas(self) -> list[dict]:
        # Skills may share a tool; the API rejects a tool name sent twice.
        active: dict[str, dict] = {}
        for skill in self._active.values():
            for tool in skill.tools:
                name = tool["function"]["name"]
                if name not in active:
                    active[name] = self._scoped(tool)
        return list(_META_TOOLS) + list(active.values())

    def _scoped(self, tool: dict) -> dict:
        name = tool["function"]["name"]
        cached = self._scoped_cache.get(name)
        if cached is None or cached[0] is not tool:
            cached = (tool, scoped(tool, yolo=self._yolo, project=self._project))
            self._scoped_cache[name] = cached
        return cached[1]

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
        server = self._server_for(name)
        if server is not None:
            client = self._clients.get(server)
            if client is None:
                return f"Error: MCP server '{server}' is no longer available"
            return client.call(name, args)
        return execute_tool(name, args, workspace, yolo=yolo, project=project)

    def _connect(self, name: str, skill: Skill) -> Skill | None:
        client = self._clients[name]
        self._connect_error = ""
        try:
            if not client.alive():
                client.connect()
        except McpError as exc:
            self._drop(name, str(exc))
            self._connect_error = f"Error: {exc} Skill '{name}' is no longer available."
            return None
        schemas = client.schemas()
        collision = self._collision(name, schemas)
        if collision is not None:
            self._connect_error = collision
            return None
        return replace(skill, tools=tuple(schemas), doc=client.documentation())

    def _drop(self, name: str, reason: str) -> None:
        client = self._clients.pop(name, None)
        if client is not None:
            client.close()
        self._catalog.pop(name, None)
        self._active.pop(name, None)
        self._catalog = _ordered(self._catalog)
        if not any(item[0] == name for item in self.warnings):
            self.warnings.append((name, reason))

    def _collision(self, skill_name: str, schemas: list[dict]) -> str | None:
        names = [schema["function"]["name"] for schema in schemas]
        if len(names) != len(set(names)):
            return f"Error: tool from '{skill_name}' is listed more than once"
        taken = {tool["function"]["name"] for tool in TOOLS}
        taken.update(("load_skill", "unload_skill"))
        for other, skill in self._active.items():
            if other == skill_name:
                continue
            origins = self._remote.get(other, {})
            for tool in skill.tools:
                tool_name = tool["function"]["name"]
                if origins.get(tool_name) != skill_name:
                    taken.add(tool_name)
        for name in names:
            if name in taken:
                return (
                    f"Error: tool '{name}' from '{skill_name}' "
                    "collides with an existing tool"
                )
        return None

    def _server_for(self, tool: str) -> str | None:
        for name, skill in self._active.items():
            if name in self._clients:
                names = {schema["function"]["name"] for schema in skill.tools}
                if tool in names:
                    return name
                continue
            server = self._remote.get(name, {}).get(tool)
            if server is not None:
                return server
        return None

    def _unknown_tool(self, name: str, args: dict) -> str:
        tools = self.skill_tools(name)
        if tools is None:
            owners = self._owners.get(name)
            if not owners:
                return f"Error: Tool '{name}' not found. Is the skill loaded?"
            if len(owners) == 1:
                return (
                    f"Error: '{name}' is in skill '{owners[0]}', which is not loaded. "
                    f"Call load_skill with name '{owners[0]}', then call {name} again."
                )
            return (
                f"Error: '{name}' is not loaded. It is in skills: {', '.join(owners)}. "
                f"Load the one that fits the task, then call {name} again."
            )
        if not tools:
            return (
                f"Error: '{name}' is a skill, not a tool. "
                f"Call load_skill with name '{name}'."
            )
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
