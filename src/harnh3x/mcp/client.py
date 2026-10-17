import json
import os
import select
import socket
import subprocess
import threading
import time
from collections.abc import Mapping
from urllib.parse import urlparse

from harnh3x.config import McpServer
from harnh3x.tools.paths import cap_result

_PROTOCOL = "2024-11-05"
_BASE_ENV = ("PATH", "HOME", "USER", "LANG", "TMPDIR")
_RESULT_HINT = "narrow the request and call the tool again"
_PAGE_LIMIT = 20
_PROBE_TIMEOUT = 1.0
_SUMMARY_TOOLS = 5


class McpError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def upstream_reachable(server: McpServer, timeout: float = _PROBE_TIMEOUT) -> bool:
    return upstream_problem(server, timeout) is None


def upstream_problem(server: McpServer, timeout: float = _PROBE_TIMEOUT) -> str | None:
    failed = []
    for parsed in _http_targets(server.env):
        if not _port_open(parsed, timeout):
            failed.append(_public_url(parsed))
    if not failed:
        return None
    return "not accepting connections: " + ", ".join(failed)


def _http_targets(env: Mapping[str, str]):
    targets = []
    for value in env.values():
        if not isinstance(value, str):
            continue
        parsed = urlparse(value.strip())
        if parsed.scheme in {"http", "https"}:
            targets.append(parsed)
    return targets


def _public_url(parsed) -> str:
    host = parsed.hostname
    if not host:
        return "invalid URL"
    try:
        port = parsed.port
    except ValueError:
        return f"{parsed.scheme}://{host} (invalid port)"
    if port is None:
        return f"{parsed.scheme}://{host}"
    return f"{parsed.scheme}://{host}:{port}"


def _port_open(parsed, timeout: float) -> bool:
    host = parsed.hostname
    if not host:
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def _first_line(value: object) -> str:
    if not isinstance(value, str):
        return ""
    for line in value.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


def child_env(extra: Mapping[str, str]) -> dict[str, str]:
    env = {}
    for key in _BASE_ENV:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    env.update(extra)
    return env


class McpClient:
    def __init__(self, server: McpServer):
        self.server = server
        self.argv = (server.command, *server.args)
        self._proc: subprocess.Popen | None = None
        self._buf = bytearray()
        self._stderr: list[str] = []
        self._stderr_lock = threading.Lock()
        self._next_id = 0
        self.server_info: dict = {}
        self.instructions = ""
        self.tools: list[dict] = []
        self.resources: list[dict] = []

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def connect(self) -> None:
        self.close()
        self._buf = bytearray()
        self.server_info = {}
        self.instructions = ""
        self.tools = []
        self.resources = []
        try:
            self._spawn()
            result = self._request(
                "initialize",
                {
                    "protocolVersion": _PROTOCOL,
                    "capabilities": {},
                    "clientInfo": {"name": "harnh3x", "version": "0"},
                },
            )
            if isinstance(result, dict):
                instructions = result.get("instructions")
                if isinstance(instructions, str):
                    self.instructions = instructions
                info = result.get("serverInfo")
                if isinstance(info, dict):
                    self.server_info = info
            self._notify("notifications/initialized")
            self.tools = self._pages("tools/list", "tools")
            try:
                self.resources = self._pages("resources/list", "resources")
            except McpError as exc:
                if exc.code == -32601:
                    self.resources = []
                else:
                    raise
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        else:
            proc.wait(timeout=2)

    def documentation(self) -> str:
        parts = []
        if self.server.description:
            parts.append(self.server.description)
        if self.server.instructions.strip():
            parts.append(self.server.instructions.strip())
        if self.instructions:
            parts.append(self.instructions)
        if self.resources:
            lines = ["Resources:"]
            for resource in self.resources:
                if not isinstance(resource, dict):
                    continue
                uri = resource.get("uri")
                if not isinstance(uri, str) or not uri:
                    continue
                description = resource.get("description")
                if isinstance(description, str) and description:
                    lines.append(f"- {uri}: {description}")
                else:
                    lines.append(f"- {uri}")
            if len(lines) > 1:
                parts.append("\n".join(lines))
        return "\n\n".join(parts)

    def summary(self) -> str:
        line = _first_line(self.server_info.get("description"))
        if line:
            return line
        line = _first_line(self.instructions)
        if line:
            return line
        label = (
            _first_line(self.server_info.get("title"))
            or _first_line(self.server_info.get("name"))
            or self.server.name
        )
        version = _first_line(self.server_info.get("version"))
        if version:
            label = f"{label} {version}"
        names = [schema["function"]["name"] for schema in self.schemas()]
        if not names:
            return label
        shown = ", ".join(names[:_SUMMARY_TOOLS])
        if len(names) > _SUMMARY_TOOLS:
            shown += ", ..."
        return f"{label}, {len(names)} tools: {shown}"

    def schemas(self) -> list[dict]:
        chosen = []
        include = self.server.include
        excluded = set(self.server.exclude)
        for tool in self.tools:
            if not isinstance(tool, dict):
                continue
            name = tool.get("name")
            if not isinstance(name, str) or not name:
                continue
            if include is not None and name not in include:
                continue
            if name in excluded:
                continue
            chosen.append(_function_schema(tool, name))
        if self.resources:
            chosen.append(_resource_schema(self.server.name))
        return chosen

    def call(self, name: str, args: dict) -> str:
        try:
            if not self.alive():
                self.connect()
            resource = f"{self.server.name}_read_resource"
            if name == resource:
                uri = args.get("uri")
                if not isinstance(uri, str) or not uri:
                    return "Error: uri is required"
                result = self._request("resources/read", {"uri": uri})
                return _cap(_resource_text(result))
            result = self._request("tools/call", {"name": name, "arguments": args})
            return _tool_text(result)
        except McpError as exc:
            return f"Error: {exc}"

    def _spawn(self) -> None:
        with self._stderr_lock:
            self._stderr.clear()
        try:
            self._proc = subprocess.Popen(
                list(self.argv),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=child_env(self.server.env),
                shell=False,
            )
        except FileNotFoundError:
            raise McpError(f"command not found: {self.server.command}") from None
        except OSError as exc:
            raise McpError(str(exc)) from None
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for raw in proc.stderr:
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                continue
            with self._stderr_lock:
                self._stderr.append(text)
                if len(self._stderr) > 20:
                    del self._stderr[0]

    def _pages(self, method: str, key: str) -> list:
        found = []
        cursor = None
        for _ in range(_PAGE_LIMIT):
            params = {}
            if cursor:
                params["cursor"] = cursor
            result = self._request(method, params)
            if not isinstance(result, dict):
                raise McpError(f"{method} returned no object")
            batch = result.get(key) or []
            if not isinstance(batch, list):
                raise McpError(f"{method} returned no list")
            found.extend(batch)
            cursor = result.get("nextCursor")
            if not isinstance(cursor, str) or not cursor:
                return found
        raise McpError(f"{method} did not finish")

    def _request(self, method: str, params: dict) -> object:
        self._next_id += 1
        pending = self._next_id
        self._send(
            {"jsonrpc": "2.0", "id": pending, "method": method, "params": params}
        )
        deadline = time.monotonic() + self.server.timeout
        while True:
            message = self._read_message(deadline)
            if message.get("method"):
                if "id" in message:
                    self._send(
                        {
                            "jsonrpc": "2.0",
                            "id": message["id"],
                            "error": {"code": -32601, "message": "Method not found"},
                        }
                    )
                continue
            if message.get("id") != pending:
                continue
            if "error" in message:
                error = message["error"] if isinstance(message["error"], dict) else {}
                code = error.get("code")
                text = error.get("message") or "MCP request failed"
                raise McpError(str(text), code if isinstance(code, int) else None)
            return message.get("result")

    def _notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def _send(self, payload: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise McpError(self._closed_message())
        try:
            proc.stdin.write(json.dumps(payload).encode() + b"\n")
            proc.stdin.flush()
        except OSError as exc:
            raise McpError(str(exc)) from None

    def _read_message(self, deadline: float) -> dict:
        while True:
            line = self._read_line(deadline)
            if line is None:
                raise McpError(self._closed_message())
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            if text.startswith("{"):
                return _object(text)
            if text.lower().startswith("content-length:"):
                length = _content_length(text)
                while True:
                    header = self._read_line(deadline)
                    if header is None:
                        raise McpError(self._closed_message())
                    if not header.strip():
                        break
                body = self._read_exact(length, deadline)
                return _object(body.decode("utf-8", "replace"))

    def _read_line(self, deadline: float) -> bytes | None:
        while b"\n" not in self._buf:
            chunk = self._read_some(deadline)
            if chunk is None:
                if self._buf:
                    line = bytes(self._buf)
                    self._buf = bytearray()
                    return line
                return None
            self._buf.extend(chunk)
        line, _, rest = self._buf.partition(b"\n")
        self._buf = bytearray(rest)
        return bytes(line)

    def _read_exact(self, length: int, deadline: float) -> bytes:
        while len(self._buf) < length:
            chunk = self._read_some(deadline)
            if chunk is None:
                raise McpError(self._closed_message())
            self._buf.extend(chunk)
        body = bytes(self._buf[:length])
        self._buf = bytearray(self._buf[length:])
        return body

    def _read_some(self, deadline: float) -> bytes | None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpError("MCP server timed out")
            if proc.poll() is not None:
                waiting = select.select([proc.stdout], [], [], 0)[0]
                if not waiting:
                    return None
            else:
                waiting = select.select([proc.stdout], [], [], min(remaining, 0.2))[0]
                if not waiting:
                    continue
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                return None
            return chunk

    def _closed_message(self) -> str:
        with self._stderr_lock:
            detail = self._stderr[-1] if self._stderr else ""
        if detail:
            return f"MCP server closed: {detail}"
        return "MCP server closed the connection"


def _function_schema(tool: dict, name: str) -> dict:
    description = tool.get("description")
    if not isinstance(description, str):
        description = ""
    parameters = tool.get("inputSchema")
    if not isinstance(parameters, dict):
        parameters = {"type": "object", "properties": {}}
    else:
        parameters = dict(parameters)
        if "type" not in parameters:
            parameters["type"] = "object"
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


def _resource_schema(server: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": f"{server}_read_resource",
            "description": "Read one MCP resource by URI",
            "parameters": {
                "type": "object",
                "properties": {
                    "uri": {"type": "string", "description": "Resource URI"},
                },
                "required": ["uri"],
            },
        },
    }


def _object(text: str) -> dict:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise McpError(f"MCP server sent invalid JSON: {exc}") from None
    if not isinstance(value, dict):
        raise McpError("MCP server sent a non-object message")
    return value


def _content_length(text: str) -> int:
    raw = text.split(":", 1)[1].strip()
    if not raw.isdigit():
        raise McpError("MCP server sent a bad Content-Length")
    return int(raw)


def _tool_text(result: object) -> str:
    if not isinstance(result, dict):
        return _cap("")
    body = _blocks(result.get("content"))
    if result.get("isError"):
        detail = body.strip() or "MCP tool failed"
        if detail.startswith("Error:"):
            return _cap(detail)
        return _cap(f"Error: {detail}")
    return _cap(body)


def _resource_text(result: object) -> str:
    if not isinstance(result, dict):
        return ""
    blocks = []
    contents = result.get("contents")
    if isinstance(contents, list):
        for item in contents:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                blocks.append({"type": "text", "text": item["text"]})
            else:
                blocks.append({"type": "resource"})
    return _blocks(blocks)


def _blocks(content: object) -> str:
    parts = []
    unsupported = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                unsupported.append("unknown")
                continue
            kind = block.get("type")
            if kind == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
                continue
            if kind == "resource":
                resource = block.get("resource")
                if isinstance(resource, dict) and isinstance(resource.get("text"), str):
                    parts.append(resource["text"])
                    continue
                unsupported.append("resource")
                continue
            unsupported.append(str(kind or "unknown"))
    text = "\n".join(parts)
    if unsupported:
        note = "Unsupported content type: " + ", ".join(unsupported)
        text = f"{text}\n{note}" if text else note
    return text


def _cap(text: str) -> str:
    return cap_result(text, hint=_RESULT_HINT)
