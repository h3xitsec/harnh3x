import hashlib
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from harnh3x.config import FETCH_MAX_BYTES, FETCH_MAX_REDIRECTS, FETCH_TIMEOUT
from harnh3x.tools.paths import _path_param, cap_result, resolve_in_workspace

SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": (
                "Download an http or https URL (HTML, text, JSON, XML, or PDF) into the "
                "workspace. Returns the saved path, not the body."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "http or https URL to fetch",
                    },
                    "path": _path_param("Optional workspace path to save the file"),
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_get_json",
            "description": "GET a public JSON API and return the parsed response",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "http or https URL"},
                    "params": {
                        "type": "object",
                        "description": "Query parameters to add to the URL",
                    },
                },
                "required": ["url"],
            },
        },
    },
]

HTML_TYPES = {"text/html", "application/xhtml+xml"}
JSON_TYPES = {"application/json", "application/ld+json", "text/json"}
XML_TYPES = {
    "application/xml",
    "text/xml",
    "application/rss+xml",
    "application/atom+xml",
}
PDF_TYPE = "application/pdf"
# Media types fetch_url saves, with the file extension used for the default path.
FETCH_TYPES = {
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "application/json": ".json",
    "application/ld+json": ".json",
    "text/json": ".json",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "application/rss+xml": ".xml",
    "application/atom+xml": ".xml",
    PDF_TYPE: ".pdf",
}
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
ACCEPT_HEADER = (
    "text/html,application/xhtml+xml,application/json,application/xml,"
    "text/plain,application/pdf;q=0.9,*/*;q=0.5"
)


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


def default_download_path(url: str, host: str, extension: str = ".html") -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", host.lower()).strip("-") or "page"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return f"downloads/{slug}-{digest}{extension}"


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


def _open_request(request: urllib.request.Request) -> tuple[int, str, bytes]:
    opener = urllib.request.build_opener(
        _CheckedRedirectHandler,
        urllib.request.ProxyHandler({}),
    )
    try:
        with opener.open(request, timeout=FETCH_TIMEOUT) as response:
            media = _media_type(response.headers.get("Content-Type", ""))
            if media not in FETCH_TYPES:
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


def _http_get(url: str) -> tuple[int, str, bytes]:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": ACCEPT_HEADER}
    )
    return _open_request(request)


def _checked_get(url: str, allowed: set[str] | None = None) -> tuple[int, str, bytes]:
    status, content_type, body = _http_get(url.strip())
    media = _media_type(content_type)
    if status < 200 or status >= 300:
        raise ValueError(f"http status {status}")
    if media not in FETCH_TYPES or (allowed is not None and media not in allowed):
        raise ValueError(f"unsupported content type '{content_type or 'unknown'}'")
    if len(body) > FETCH_MAX_BYTES:
        raise ValueError("response too large")
    return status, media, body


def _write_fetched(workspace: Path, url: str, path_arg) -> tuple[Path, int, str]:
    parsed = validate_url(url)
    path = None
    if path_arg is not None:
        path = resolve_in_workspace(workspace, path_arg, include_project=False)
    status, media, body = _checked_get(url)
    if path is None:
        relative = default_download_path(
            url.strip(), parsed.hostname or "page", FETCH_TYPES[media]
        )
        path = resolve_in_workspace(workspace, relative, include_project=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path, status, media


def fetch_url(workspace: Path, args: dict) -> str:
    workspace = workspace.resolve()
    url = args.get("url", "")
    if not isinstance(url, str):
        url = ""
    path_arg = args["path"] if "path" in args and args["path"] is not None else None
    path, status, media = _write_fetched(workspace, url, path_arg)
    return (
        f"status: success\n"
        f"http_status: {status}\n"
        f"content_type: {media}\n"
        f"bytes: {path.stat().st_size}\n"
        f"path: {path.relative_to(workspace)}"
    )


def _with_params(url: str, params) -> str:
    if params is None:
        return url
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    parts = urllib.parse.urlsplit(url.strip())
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    for key, value in params.items():
        if isinstance(value, bool):
            value = "true" if value else "false"
        elif value is None:
            value = ""
        elif not isinstance(value, (str, int, float)):
            raise ValueError(f"param '{key}' must be a string or number")
        query.append((str(key), str(value)))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def get_json(url: str, params=None):
    target = _with_params(url, params)
    validate_url(target)
    _status, _media, body = _checked_get(target, JSON_TYPES)
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON response: {exc}") from exc


def http_get_json(workspace: Path, args: dict) -> str:
    url = args.get("url", "")
    if not isinstance(url, str) or not url.strip():
        return "Error: url is required"
    data = get_json(url, args.get("params"))
    return cap_result(
        json.dumps(data, indent=2, ensure_ascii=False),
        hint="narrow the request with params, or save the full response with fetch_url",
    )


FNS = {
    "fetch_url": fetch_url,
    "http_get_json": http_get_json,
}
