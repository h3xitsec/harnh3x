import math
import os
import re
from collections.abc import Mapping
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Alternate models:
# hf.co/unsloth/gpt-oss-20b-GGUF:Q4_K_M
# gpt-oss:20b
# gemma4:12b-it-qat

MEMORY_FILE = "memory.md"
PLAN_FILE = "plan.md"
TRANSCRIPT_FILE = "transcript.json"


class _Setting:
    def __init__(self, attr: str, env: str, default: object, kind: type):
        self.attr = attr
        self.env = env
        self.default = default
        self.kind = kind


_SETTINGS = {
    "api_key": _Setting("API_KEY", "OPENAI_API_KEY", None, str),
    "api_url": _Setting("API_URL", "OPENAI_BASE_URL", None, str),
    "model": _Setting("MODEL", "HARNH3X_MODEL", "qwen3.5-128k", str),
    "max_turns": _Setting("MAX_TURNS", "HARNH3X_MAX_TURNS", 45, int),
    "compact_at": _Setting("COMPACT_AT", "HARNH3X_COMPACT_AT", 0.5, float),
    "max_tool_result": _Setting(
        "MAX_TOOL_RESULT", "HARNH3X_MAX_TOOL_RESULT", 8_000, int
    ),
    "fetch_timeout": _Setting("FETCH_TIMEOUT", "HARNH3X_FETCH_TIMEOUT", 15, int),
    "fetch_max_bytes": _Setting(
        "FETCH_MAX_BYTES", "HARNH3X_FETCH_MAX_BYTES", 2_000_000, int
    ),
    "fetch_max_redirects": _Setting(
        "FETCH_MAX_REDIRECTS", "HARNH3X_FETCH_MAX_REDIRECTS", 5, int
    ),
    "search_max_results": _Setting(
        "SEARCH_MAX_RESULTS", "HARNH3X_SEARCH_MAX_RESULTS", 8, int
    ),
    "feed_max_items": _Setting("FEED_MAX_ITEMS", "HARNH3X_FEED_MAX_ITEMS", 20, int),
    "shell_timeout": _Setting("SHELL_TIMEOUT", "HARNH3X_SHELL_TIMEOUT", 30, int),
    "shell_max_timeout": _Setting(
        "SHELL_MAX_TIMEOUT", "HARNH3X_SHELL_MAX_TIMEOUT", 120, int
    ),
    "list_files_max": _Setting("LIST_FILES_MAX", "HARNH3X_LIST_FILES_MAX", 200, int),
    "search_files_max": _Setting(
        "SEARCH_FILES_MAX", "HARNH3X_SEARCH_FILES_MAX", 100, int
    ),
    "pdf_max_pages": _Setting("PDF_MAX_PAGES", "HARNH3X_PDF_MAX_PAGES", 20, int),
    "preview_rows": _Setting("PREVIEW_ROWS", "HARNH3X_PREVIEW_ROWS", 10, int),
    "wikipedia_lang": _Setting("WIKIPEDIA_LANG", "HARNH3X_WIKIPEDIA_LANG", "en", str),
    "base_dir": _Setting(
        "BASE_DIR",
        "HARNH3X_BASE_DIR",
        "~/.harnh3x",
        str,
    ),
}


def config_location(
    environ: Mapping[str, str] | None = None
) -> tuple[Path | None, bool]:
    env = os.environ if environ is None else environ
    override = env.get("HARNH3X_CONFIG")
    if override is not None:
        if override == "":
            return None, False
        return Path(override).expanduser(), True
    xdg = env.get("XDG_CONFIG_HOME")
    root = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return root / "harnh3x" / "harnh3x.conf", False


def parse_config(text: str, path: Path) -> dict[str, tuple[str, int]]:
    values: dict[str, tuple[str, int]] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise SystemExit(f"{path}:{number}: expected key = value")
        key, raw = stripped.split("=", 1)
        key = key.strip()
        raw = _unquote(raw.strip())
        if not key:
            raise SystemExit(f"{path}:{number}: expected key = value")
        if key not in _SETTINGS:
            raise SystemExit(f"{path}:{number}: unknown setting {key}")
        if raw == "":
            raise SystemExit(f"{path}:{number}: {key} needs a value")
        values[key] = (raw, number)
    return values


def resolve_settings(
    file_values: Mapping[str, tuple[str, int]],
    environ: Mapping[str, str],
    path: Path,
) -> dict[str, object]:
    resolved: dict[str, object] = {}
    for key, spec in _SETTINGS.items():
        env_raw = _present(environ.get(spec.env))
        if env_raw is not None:
            resolved[spec.attr] = _cast(spec.kind, env_raw, spec.env)
            continue
        if key in file_values:
            raw, line = file_values[key]
            resolved[spec.attr] = _cast(spec.kind, raw, f"{path}:{line}")
            continue
        resolved[spec.attr] = spec.default
    return resolved


def require_api_settings() -> None:
    missing: list[str] = []
    if not API_KEY:
        missing.append("api_key (OPENAI_API_KEY)")
    if not API_URL:
        missing.append("api_url (OPENAI_BASE_URL)")
    if not missing:
        return
    path, _explicit = config_location()
    where = "the environment" if path is None else f"{path} or the environment"
    listed = " and ".join(missing)
    pronoun = "it" if len(missing) == 1 else "them"
    raise SystemExit(f"Missing {listed}. Set {pronoun} in {where}.")


def load_settings(environ: Mapping[str, str] | None = None) -> dict[str, object]:
    env = os.environ if environ is None else environ
    path, explicit = config_location(env)
    if path is None:
        return resolve_settings({}, env, Path("harnh3x.conf"))
    if not path.is_file():
        if explicit:
            raise SystemExit(f"{path}: config file not found")
        return resolve_settings({}, env, path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"{path}: {exc.strerror}") from None
    return resolve_settings(parse_config(text, path), env, path)


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _present(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if text == "":
        return None
    return text


def _cast(kind: type, raw: str, origin: str) -> object:
    if kind is int:
        if re.fullmatch(r"[+-]?\d+", raw) is None:
            raise SystemExit(f"{origin}: must be an integer")
        return int(raw)
    if kind is float:
        try:
            value = float(raw)
        except ValueError:
            value = math.nan
        if not math.isfinite(value):
            raise SystemExit(f"{origin}: must be a number")
        return value
    return raw


_loaded = load_settings()
API_KEY = _loaded["API_KEY"]
API_URL = _loaded["API_URL"]
MODEL = _loaded["MODEL"]
MAX_TURNS = _loaded["MAX_TURNS"]
COMPACT_AT = _loaded["COMPACT_AT"]
MAX_TOOL_RESULT = _loaded["MAX_TOOL_RESULT"]
FETCH_TIMEOUT = _loaded["FETCH_TIMEOUT"]
FETCH_MAX_BYTES = _loaded["FETCH_MAX_BYTES"]
FETCH_MAX_REDIRECTS = _loaded["FETCH_MAX_REDIRECTS"]
SEARCH_MAX_RESULTS = _loaded["SEARCH_MAX_RESULTS"]
FEED_MAX_ITEMS = _loaded["FEED_MAX_ITEMS"]
SHELL_TIMEOUT = _loaded["SHELL_TIMEOUT"]
SHELL_MAX_TIMEOUT = _loaded["SHELL_MAX_TIMEOUT"]
LIST_FILES_MAX = _loaded["LIST_FILES_MAX"]
SEARCH_FILES_MAX = _loaded["SEARCH_FILES_MAX"]
PDF_MAX_PAGES = _loaded["PDF_MAX_PAGES"]
PREVIEW_ROWS = _loaded["PREVIEW_ROWS"]
WIKIPEDIA_LANG = _loaded["WIKIPEDIA_LANG"]
BASE_DIR = _loaded["BASE_DIR"]
