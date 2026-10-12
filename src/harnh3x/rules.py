from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_FRONT_KEYS = {"description", "alwaysApply", "globs"}
_PATH_ARGS = ("path", "source", "destination")


@dataclass(frozen=True)
class ProjectRule:
    path: str
    description: str
    always_apply: bool
    globs: tuple[str, ...]
    body: str


def load_project_rules(project: Path) -> list[ProjectRule]:
    root = project.resolve()
    rules_dir = root / ".cursor" / "rules"
    if not rules_dir.is_dir():
        return []
    try:
        if not rules_dir.resolve().is_relative_to(root):
            return []
    except OSError:
        return []
    found: list[ProjectRule] = []
    for path in sorted(rules_dir.rglob("*.mdc")):
        rule = _read_rule(root, path)
        if rule is not None:
            found.append(rule)
    return found


def format_rules(rules: list[ProjectRule]) -> str:
    always = [rule for rule in rules if rule.always_apply]
    others = [rule for rule in rules if not rule.always_apply]
    parts: list[str] = []
    if always:
        chunks = ["Project rules (always apply). Follow them for work in the project."]
        for rule in always:
            text = rule.body.strip()
            chunks.append(text or rule.path)
        parts.append("\n\n".join(chunks))
    if others:
        has_globs = any(rule.globs for rule in others)
        has_manual = any(not rule.globs for rule in others)
        lines: list[str] = []
        if has_globs:
            lines.append(
                "Other project rules. A rule with globs is included when a file "
                "tool uses a matching project file."
            )
        if has_manual:
            lines.append(
                "When a rule has no globs, read that .mdc file when its "
                "description matches the task."
            )
        for rule in others:
            label = rule.description or rule.path
            if rule.globs:
                lines.append(f"- {rule.path}: {label} ({', '.join(rule.globs)})")
            else:
                lines.append(f"- {rule.path}: {label}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def glob_matches(relative: str, pattern: str) -> bool:
    path = PurePosixPath(relative)
    if path.full_match(pattern):
        return True
    if pattern.endswith("/**"):
        return path.full_match(pattern + "/*")
    return False


def append_matching_rules(
    project: Path, args: dict | None, result: str, applied: set[str]
) -> str:
    if not isinstance(args, dict):
        return result
    relatives = [
        relative
        for key in _PATH_ARGS
        if (relative := _project_relative(project, args.get(key))) is not None
    ]
    if not relatives:
        return result
    fresh: list[ProjectRule] = []
    for rule in load_project_rules(project):
        if rule.always_apply or not rule.globs or rule.path in applied:
            continue
        if any(
            glob_matches(relative, pattern)
            for relative in relatives
            for pattern in rule.globs
        ):
            fresh.append(rule)
            applied.add(rule.path)
    if not fresh:
        return result
    chunks = ["The following project rules apply to the files you just used:"]
    for rule in fresh:
        chunks.append(rule.body.strip() or rule.path)
    return result + "\n\n" + "\n\n".join(chunks)


def _project_relative(project: Path, raw) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    root = project.resolve()
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    try:
        resolved = path.resolve()
    except OSError:
        return None
    if not resolved.is_relative_to(root) or resolved == root:
        return None
    return resolved.relative_to(root).as_posix()


def _read_rule(root: Path, path: Path) -> ProjectRule | None:
    if not path.is_file():
        return None
    try:
        resolved = path.resolve()
    except OSError:
        return None
    if not resolved.is_relative_to(root):
        return None
    try:
        text = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    parsed = _parse_frontmatter(text)
    if parsed is None:
        return None
    fields, body = parsed
    return ProjectRule(
        path=resolved.relative_to(root).as_posix(),
        description=fields["description"],
        always_apply=fields["alwaysApply"],
        globs=tuple(fields["globs"]),
        body=body,
    )


def _parse_frontmatter(text: str) -> tuple[dict, str] | None:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = index
            break
    if end is None:
        return None
    fields = _parse_fields(lines[1:end])
    if fields is None:
        return None
    body = "\n".join(lines[end + 1 :]).strip("\n")
    return fields, body


def _parse_fields(lines: list[str]) -> dict | None:
    data: dict = {"description": "", "alwaysApply": False, "globs": []}
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            index += 1
            continue
        if line[0] in " \t" or ":" not in line:
            return None
        key, _, raw = line.partition(":")
        key = key.strip()
        raw = raw.strip()
        if key not in _FRONT_KEYS:
            index += 1
            continue
        if raw in {">", "|", ">-", "|-", ">+", "|+"} or (key == "globs" and raw == ""):
            block: list[str] = []
            index += 1
            while index < len(lines) and (
                not lines[index].strip() or lines[index][0] in " \t"
            ):
                block.append(lines[index])
                index += 1
            parsed = _parse_block(key, raw, block)
            if parsed is None:
                return None
            data[key] = parsed
            continue
        if key == "alwaysApply":
            lowered = raw.lower()
            if lowered not in {"true", "false"}:
                return None
            data["alwaysApply"] = lowered == "true"
        elif key == "globs":
            data["globs"] = _split_globs(raw)
        else:
            data["description"] = _unquote(raw)
        index += 1
    return data


def _parse_block(key: str, raw: str, block: list[str]) -> str | list[str] | None:
    if key == "globs":
        globs: list[str] = []
        for line in block:
            stripped = line.strip()
            if not stripped:
                continue
            if not stripped.startswith("-"):
                return None
            globs.append(_unquote(stripped[1:].strip()))
        return globs
    if key == "description" and raw in {">", "|", ">-", "|-", ">+", "|+"}:
        return " ".join(line.strip() for line in block if line.strip())
    return None


def _split_globs(raw: str) -> list[str]:
    text = raw.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    globs = []
    for part in text.split(","):
        item = _unquote(part.strip())
        if item:
            globs.append(item)
    return globs


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value
