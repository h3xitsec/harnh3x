import hashlib
import ipaddress
import json
import os
import re
import shlex
import socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import trafilatura

from harness.config import (
    FETCH_MAX_BYTES,
    FETCH_MAX_REDIRECTS,
    FETCH_TIMEOUT,
    MAX_TOOL_RESULT,
)

DENIED_COMMANDS = {"rm", "mv", "chmod", "sudo"}
# shell=False does not interpret these; refuse them so a model cannot smuggle a chain or redirect.
SHELL_META = set("><|&;`$")

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file inside the workspace",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file inside the workspace (creates or overwrites)",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file"},
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a file inside the workspace",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Run a command in the workspace and return its exit code, stdout, and stderr",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Command to run"}
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch an HTTP or HTTPS page and save the HTML in the workspace. Returns the saved path, not the page body.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "http or https URL to fetch",
                    },
                    "path": {
                        "type": "string",
                        "description": "Optional workspace path to save the HTML",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "parse_html",
            "description": "Read an HTML file already saved in the workspace and return its title and text. Does not download.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace path to the HTML file saved by fetch_url",
                    },
                },
                "required": ["path"],
            },
        },
    },
]


def cap_result(text: str, limit: int = MAX_TOOL_RESULT) -> str:
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    marker = f"\n...[truncated, {omitted} chars omitted]"
    keep = limit - len(marker)
    while keep >= 0 and len(text) - keep != omitted:
        omitted = len(text) - keep
        marker = f"\n...[truncated, {omitted} chars omitted]"
        keep = limit - len(marker)
    if keep < 0:
        return f"...[truncated, {len(text)} chars omitted]"[:limit]
    return text[:keep] + marker


def resolve_in_workspace(workspace: Path, raw: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("path is required")
    root = workspace.resolve()
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("outside workspace")
    return resolved


def _shell_env() -> dict:
    keep = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "LOGNAME")
    return {key: os.environ[key] for key in keep if key in os.environ}


def prepare_shell(command: str, workspace: Path) -> list[str]:
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command is required")
    if any(ch in command for ch in SHELL_META):
        raise ValueError("blocked shell syntax")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"invalid command: {exc}") from exc
    if not argv:
        raise ValueError("command is required")
    program = Path(argv[0]).name
    if program in DENIED_COMMANDS:
        raise ValueError(f"blocked command '{program}'")
    for arg in argv:
        if ".." in Path(arg).parts:
            raise ValueError("blocked path '..'")
        if Path(arg).is_absolute():
            resolve_in_workspace(workspace, arg)
    return argv


def _format_shell(exit_code: int, stdout: str, stderr: str) -> str:
    status = "success" if exit_code == 0 else "error"
    return cap_result(
        f"status: {status}\nexit_code: {exit_code}\nstdout:\n{stdout}\nstderr:\n{stderr}"
    )


def read_file(workspace: Path, args: dict) -> str:
    path = resolve_in_workspace(workspace, args.get("path", ""))
    if not path.exists():
        return "Error: not found"
    if path.is_dir():
        return "Error: is a directory"
    return cap_result(path.read_text(encoding="utf-8"))


def write_file(workspace: Path, args: dict) -> str:
    path = resolve_in_workspace(workspace, args.get("path", ""))
    content = args.get("content")
    if not isinstance(content, str):
        return "Error: content is required"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode("utf-8")
    path.write_bytes(data)
    return f"Wrote {len(data)} bytes to {path}"


def delete_file(workspace: Path, args: dict) -> str:
    path = resolve_in_workspace(workspace, args.get("path", ""))
    if not path.exists():
        return "Error: not found"
    if path.is_dir():
        return "Error: is a directory"
    path.unlink()
    return f"Deleted {path}"


HTML_TYPES = {"text/html", "application/xhtml+xml"}


def validate_url(url: str):
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url is required")
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("blocked url scheme")
    if parsed.username or parsed.password:
        raise ValueError("blocked url credentials")
    if not parsed.hostname:
        raise ValueError("url host is required")
    _assert_public_host(parsed.hostname)
    return parsed


def _assert_public_host(host: str) -> None:
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ValueError("could not resolve host") from exc
        addresses = []
        for info in infos:
            try:
                addresses.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue
        if not addresses:
            raise ValueError("could not resolve host")
    for ip in addresses:
        if not ip.is_global:
            raise ValueError("blocked host")


def default_download_path(url: str, host: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", host.lower()).strip("-") or "page"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return f"downloads/{slug}-{digest}.html"


def _media_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def _read_limited(response, limit: int) -> bytes:
    chunks = []
    total = 0
    while True:
        chunk = response.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ValueError("response too large")
        chunks.append(chunk)
    return b"".join(chunks)


class _CheckedRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = FETCH_MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _http_get(url: str) -> tuple[int, str, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": "harness/0.1"})
    opener = urllib.request.build_opener(
        _CheckedRedirectHandler,
        urllib.request.ProxyHandler({}),
    )
    try:
        with opener.open(request, timeout=FETCH_TIMEOUT) as response:
            content_type = response.headers.get("Content-Type", "")
            media = _media_type(content_type)
            if media not in HTML_TYPES:
                raise ValueError(f"unsupported content type '{media or 'unknown'}'")
            body = _read_limited(response, FETCH_MAX_BYTES)
            return response.status, media, body
    except ValueError:
        raise
    except urllib.error.HTTPError as exc:
        raise ValueError(f"http status {exc.code}") from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            raise ValueError("timed out") from exc
        raise ValueError(str(reason)) from exc
    except TimeoutError as exc:
        raise ValueError("timed out") from exc


def _write_fetched(workspace: Path, url: str, path_arg) -> tuple[Path, int, str]:
    parsed = validate_url(url)
    if path_arg is None:
        relative = default_download_path(url.strip(), parsed.hostname or "page")
    else:
        relative = path_arg
    path = resolve_in_workspace(workspace, relative)
    status, content_type, body = _http_get(url.strip())
    media = _media_type(content_type)
    if status < 200 or status >= 300:
        raise ValueError(f"http status {status}")
    if media not in HTML_TYPES:
        raise ValueError(f"unsupported content type '{content_type or 'unknown'}'")
    if len(body) > FETCH_MAX_BYTES:
        raise ValueError("response too large")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path, status, media


def fetch_url(workspace: Path, args: dict) -> str:
    workspace = workspace.resolve()
    url = args.get("url", "")
    if not isinstance(url, str):
        url = ""
    path_arg = args["path"] if "path" in args and args["path"] is not None else None
    path, status, content_type = _write_fetched(workspace, url, path_arg)
    return (
        f"status: success\n"
        f"http_status: {status}\n"
        f"content_type: {content_type}\n"
        f"bytes: {path.stat().st_size}\n"
        f"path: {path.relative_to(workspace)}"
    )


def _article_field(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        return ""
    return value.strip()


def _format_article(html: bytes) -> str:
    raw = trafilatura.extract(
        html,
        output_format="json",
        with_metadata=True,
    )
    if not raw:
        return "Error: no article text extracted"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return "Error: no article text extracted"
    if not isinstance(data, dict):
        return "Error: no article text extracted"
    text = _article_field(data, "text")
    if not text:
        return "Error: no article text extracted"
    return (
        f"title: {_article_field(data, 'title')}\n"
        f"author: {_article_field(data, 'author')}\n"
        f"date: {_article_field(data, 'date')}\n"
        f"text:\n{text}"
    )


def parse_html(workspace: Path, args: dict) -> str:
    workspace = workspace.resolve()
    path_arg = args.get("path")
    path = resolve_in_workspace(
        workspace, path_arg if isinstance(path_arg, str) else ""
    )
    if not path.exists():
        return "Error: not found"
    if path.is_dir():
        return "Error: is a directory"
    return cap_result(_format_article(path.read_bytes()))


def run_shell(workspace: Path, args: dict) -> str:
    argv = prepare_shell(args.get("command", ""), workspace)
    try:
        completed = subprocess.run(
            argv,
            shell=False,
            cwd=workspace,
            env=_shell_env(),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        return _format_shell(124, stdout, stderr or "timed out after 30s")
    return _format_shell(completed.returncode, completed.stdout, completed.stderr)


TOOL_FNS = {
    "read_file": read_file,
    "write_file": write_file,
    "delete_file": delete_file,
    "run_shell": run_shell,
    "fetch_url": fetch_url,
    "parse_html": parse_html,
}


def execute_tool(name: str, args: dict, workspace: Path) -> str:
    fn = TOOL_FNS.get(name)
    if fn is None:
        return f"Error: Unknown tool '{name}'"
    try:
        return fn(workspace, args)
    except ValueError as exc:
        return f"Error: {exc}"
    except OSError as exc:
        return f"Error: {exc}"


def _tool_result(
    name: str, arguments: str, workspace: Path, registry=None
) -> tuple[dict | None, str]:
    try:
        args = json.loads(arguments)
    except json.JSONDecodeError as exc:
        return None, f"Error: invalid tool arguments: {exc}"
    if not isinstance(args, dict):
        return None, "Error: tool arguments must be an object"
    if registry is None:
        return args, execute_tool(name, args, workspace)
    return args, registry.execute(name, args, workspace)
