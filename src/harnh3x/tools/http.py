import hashlib
import http.client
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from harnh3x.config import FETCH_MAX_BYTES, FETCH_MAX_REDIRECTS, FETCH_TIMEOUT
from harnh3x.tools.paths import _path_param, _yolo, cap_result, resolve_in_workspace

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
    {
        "type": "function",
        "function": {
            "name": "http_request",
            "description": (
                "Send an HTTP request and return the status code, headers, and body. "
                "Use for calling APIs. Response bodies are truncated, then the text "
                "result is capped. Redirects are not followed unless follow_redirects "
                "is true."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "enum": [
                            "DELETE",
                            "GET",
                            "HEAD",
                            "OPTIONS",
                            "PATCH",
                            "POST",
                            "PUT",
                        ],
                        "description": "HTTP method",
                    },
                    "url": {
                        "type": "string",
                        "description": "Full http or https URL",
                    },
                    "headers": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": "Request headers",
                    },
                    "params": {
                        "type": "object",
                        "description": "Query string parameters",
                    },
                    "json_body": {
                        "description": (
                            "JSON object or array, not a quoted JSON string. "
                            'Example: {"email": "a@b.c", "password": "secret"}. '
                            "Sets Content-Type to application/json when that header is absent."
                        ),
                        "anyOf": [{"type": "object"}, {"type": "array"}],
                    },
                    "body": {
                        "type": "string",
                        "description": "Raw request body when the payload is not JSON",
                    },
                    "follow_redirects": {
                        "type": "boolean",
                        "description": (
                            "Follow redirects. Each hop is checked. Default false."
                        ),
                    },
                },
                "required": ["method", "url"],
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
ALLOWED_METHODS = frozenset(
    {"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"}
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
    if _yolo.get():
        return
    _vetted_addresses(host)


def _vetted_addresses(host: str) -> list[str]:
    """Resolve host and return its addresses, refusing any that is not global.

    Outside yolo every address must be global; one private answer blocks the
    host. The same check runs when the URL is validated and again when the
    socket is opened, so a DNS answer that changes between the two cannot
    reach a private address.
    """
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ValueError("could not resolve host") from exc
        addresses = []
        for info in infos:
            try:
                ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if info[4][0] not in addresses:
                addresses.append(info[4][0])
        if not addresses:
            raise ValueError("could not resolve host")
    if _yolo.get():
        return addresses
    if any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("blocked host")
    return addresses


def _pinned_create_connection(address, timeout=None, source_address=None):
    """Connect to an address that passed _vetted_addresses just now."""
    host, port = address
    last_error: OSError | None = None
    for ip in _vetted_addresses(host):
        try:
            return socket.create_connection((ip, port), timeout, source_address)
        except OSError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise OSError("could not connect")


class _PinnedConnection:
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = _pinned_create_connection


class _PinnedHTTPConnection(_PinnedConnection, http.client.HTTPConnection):
    pass


class _PinnedHTTPSConnection(_PinnedConnection, http.client.HTTPSConnection):
    pass


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def do_open(self, http_class, req, **http_conn_args):
        return super().do_open(_PinnedHTTPConnection, req, **http_conn_args)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def do_open(self, http_class, req, **http_conn_args):
        return super().do_open(_PinnedHTTPSConnection, req, **http_conn_args)


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


class _ReturnStatus(urllib.request.HTTPErrorProcessor):
    """Leave 3xx, 4xx, and 5xx as responses instead of raising."""

    def http_response(self, request, response):
        return response

    https_response = http_response


class _RedirectOrReturn(urllib.request.HTTPErrorProcessor):
    """Follow 3xx; return every other status as a response."""

    def http_response(self, request, response):
        code = response.status
        if 300 <= code < 400:
            return self.parent.error(
                "http", request, response, code, response.msg, response.info()
            )
        return response

    https_response = http_response


def _open_request(request: urllib.request.Request) -> tuple[int, str, bytes]:
    opener = urllib.request.build_opener(
        _CheckedRedirectHandler,
        urllib.request.ProxyHandler({}),
        _PinnedHTTPHandler,
        _PinnedHTTPSHandler,
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


def _header_map(headers) -> dict[str, str]:
    if headers is None:
        headers = {}
    if not isinstance(headers, dict):
        raise ValueError("headers must be an object")
    cleaned: dict[str, str] = {}
    for key, value in headers.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("header names must be strings")
        if not isinstance(value, str):
            raise ValueError("header values must be strings")
        if any(char in key or char in value for char in "\r\n"):
            raise ValueError("headers must not contain newlines")
        cleaned[key] = value
    return cleaned


def _has_header(headers: dict[str, str], name: str) -> bool:
    target = name.lower()
    return any(key.lower() == target for key in headers)


_JSON_BODY_ERROR = (
    "json_body must be an object or array. "
    'Example: {"email": "a@b.c", "password": "secret"}'
)


def _as_json_body(json_body):
    if isinstance(json_body, str):
        try:
            json_body = json.loads(json_body)
        except json.JSONDecodeError as exc:
            raise ValueError(_JSON_BODY_ERROR) from exc
    if not isinstance(json_body, (dict, list)):
        raise ValueError(_JSON_BODY_ERROR)
    return json_body


def _request_bytes(json_body, body, headers: dict[str, str]) -> bytes | None:
    if json_body is not None and body is not None:
        raise ValueError("json_body and body cannot both be set")
    if json_body is not None:
        json_body = _as_json_body(json_body)
        if not _has_header(headers, "content-type"):
            headers["Content-Type"] = "application/json"
        try:
            encoded = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"json_body must be JSON: {exc}") from exc
        return encoded
    if body is None:
        return None
    if not isinstance(body, str):
        raise ValueError("body must be a string")
    return body.encode("utf-8")


def _http_opener(follow_redirects: bool) -> urllib.request.OpenerDirector:
    shared = (
        urllib.request.ProxyHandler({}),
        _PinnedHTTPHandler,
        _PinnedHTTPSHandler,
    )
    if follow_redirects:
        return urllib.request.build_opener(
            *shared, _CheckedRedirectHandler, _RedirectOrReturn
        )
    return urllib.request.build_opener(*shared, _ReturnStatus)


def _read_capped(response, limit: int) -> tuple[bytes, bool]:
    chunks = []
    total = 0
    while total < limit:
        chunk = response.read(min(65536, limit - total))
        if not chunk:
            return b"".join(chunks), False
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks), bool(response.read(1))


def _decode_body(raw: bytes, encoding: str | None) -> str:
    try:
        return raw.decode(encoding or "utf-8", errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _format_http_result(response, raw: bytes, truncated: bool) -> str:
    content_type = response.headers.get("Content-Type", "")
    media = _media_type(content_type) if content_type else ""
    header_lines = [
        f"{key}: {value}"
        for key, value in sorted(
            response.headers.items(), key=lambda item: item[0].lower()
        )
    ]
    text = _decode_body(raw, response.headers.get_content_charset())
    return (
        f"status_code: {response.status}\n"
        f"reason: {response.reason or ''}\n"
        f"final_url: {response.geturl()}\n"
        f"content_type: {media}\n"
        f"truncated: {str(truncated).lower()}\n"
        f"headers:\n" + "\n".join(header_lines) + f"\nbody:\n{text}"
    )


def _request_hint(method: str) -> str:
    if method == "GET":
        return "narrow the request, or save the full response with fetch_url"
    return "narrow the request"


def http_request(workspace: Path, args: dict) -> str:
    method = args.get("method", "")
    if not isinstance(method, str) or not method.strip():
        raise ValueError("method is required")
    method = method.strip().upper()
    if method not in ALLOWED_METHODS:
        raise ValueError(f"Method not allowed: {method}")
    follow = args.get("follow_redirects", False)
    if not isinstance(follow, bool):
        raise ValueError("follow_redirects must be a boolean")
    url = args.get("url", "")
    if not isinstance(url, str):
        url = ""
    target = _with_params(url, args.get("params"))
    validate_url(target)
    headers = _header_map(args.get("headers"))
    data = _request_bytes(args.get("json_body"), args.get("body"), headers)
    if not _has_header(headers, "user-agent"):
        headers["User-Agent"] = USER_AGENT
    request = urllib.request.Request(target, data=data, headers=headers, method=method)
    opener = _http_opener(follow)
    try:
        response = opener.open(request, timeout=FETCH_TIMEOUT)
        try:
            raw, truncated = _read_capped(response, FETCH_MAX_BYTES)
            rendered = _format_http_result(response, raw, truncated)
        finally:
            response.close()
    except ValueError:
        raise
    except urllib.error.HTTPError as exc:
        detail = exc.reason if isinstance(exc.reason, str) and exc.reason else ""
        raise ValueError(detail or f"http status {exc.code}") from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            raise ValueError("timed out") from exc
        raise ValueError(str(reason)) from exc
    except TimeoutError as exc:
        raise ValueError("timed out") from exc
    return cap_result(rendered, hint=_request_hint(method))


FNS = {
    "fetch_url": fetch_url,
    "http_get_json": http_get_json,
    "http_request": http_request,
}
