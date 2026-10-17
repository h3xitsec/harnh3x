"""HTTP tools and the SSRF guard."""

import http.client
import json
import socket
import urllib.error
import urllib.parse
import urllib.request

import pytest

from harnh3x import tools
from harnh3x.tools import execute_tool
from harnh3x.tools.http import validate_url


def _json_get(payloads):
    seen = []

    def fake_get(url):
        seen.append(url)
        for prefix, payload in payloads:
            if url.startswith(prefix):
                return (
                    200,
                    "application/json; charset=utf-8",
                    json.dumps(payload).encode(),
                )
        raise AssertionError(url)

    return fake_get, seen

def test_validate_url_without_url_raises_error():
    with pytest.raises(ValueError, match="url is required"):
        validate_url(None)
    with pytest.raises(ValueError, match="url is required"):
        validate_url("")
    with pytest.raises(ValueError, match="url is required"):
        validate_url(123)
    with pytest.raises(ValueError, match="url is required"):
        validate_url(True)

def test_validate_url_with_invalid_url_raises_error():
    with pytest.raises(ValueError, match="url host is required"):
        validate_url("https://:80/path")
    with pytest.raises(ValueError, match="blocked url scheme"):
        validate_url("file:///etc/passwd")
    with pytest.raises(ValueError, match="blocked url scheme"):
        validate_url("ftp://127.0.0.1/path")
    with pytest.raises(ValueError, match="blocked url credentials"):
        validate_url("http://user:pass@1.1.1.1/path")

def test_wikipedia_search_lists_titles_with_urls(tmp_path, monkeypatch):
    payload = {
        "query": {
            "search": [
                {
                    "title": "Ada Lovelace",
                    "snippet": "English <span class='x'>mathematician</span>",
                },
                {"title": "Lovelace Medal", "snippet": "award"},
            ]
        }
    }
    fake_get, seen = _json_get([("https://en.wikipedia.org/w/api.php", payload)])
    monkeypatch.setattr(tools.http, "_http_get", fake_get)

    result = execute_tool("wikipedia_search", {"query": "ada lovelace"}, tmp_path)

    assert (
        "1. Ada Lovelace\n   url: https://en.wikipedia.org/wiki/Ada_Lovelace\n   snippet: English mathematician"
        in result
    )
    assert "2. Lovelace Medal" in result
    assert "wikipedia_summary" in result
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(seen[0]).query)
    assert query["srsearch"] == ["ada lovelace"]
    assert query["srlimit"] == ["5"]
    assert (
        execute_tool("wikipedia_search", {"query": " "}, tmp_path)
        == "Error: query is required"
    )


def test_wikipedia_search_skips_hits_without_a_title(tmp_path, monkeypatch):
    payload = {
        "query": {
            "search": [
                "skip",
                {"title": "  ", "snippet": "blank"},
                {"title": "Ada Lovelace", "snippet": "math"},
            ]
        }
    }
    fake_get, _seen = _json_get([("https://en.wikipedia.org/w/api.php", payload)])
    monkeypatch.setattr(tools.http, "_http_get", fake_get)

    result = execute_tool("wikipedia_search", {"query": "ada"}, tmp_path)

    assert "3. Ada Lovelace\n" in result
    assert "snippet: math" in result
    assert "snippet: blank" not in result
    assert "\n1. " not in result


def test_wikipedia_summary_returns_extract_and_flags_missing(tmp_path, monkeypatch):
    payload = {
        "title": "Ada Lovelace",
        "description": "English mathematician",
        "extract": "Ada Lovelace was an English mathematician.",
        "content_urls": {
            "desktop": {"page": "https://en.wikipedia.org/wiki/Ada_Lovelace"}
        },
    }
    fake_get, _seen = _json_get(
        [("https://en.wikipedia.org/api/rest_v1/page/summary/Ada_Lovelace", payload)]
    )
    monkeypatch.setattr(tools.http, "_http_get", fake_get)

    result = execute_tool("wikipedia_summary", {"title": "Ada Lovelace"}, tmp_path)
    assert result == (
        "title: Ada Lovelace\n"
        "description: English mathematician\n"
        "url: https://en.wikipedia.org/wiki/Ada_Lovelace\n"
        "summary:\nAda Lovelace was an English mathematician."
    )

    def missing(url):
        raise ValueError("http status 404")

    monkeypatch.setattr(tools.http, "_http_get", missing)
    assert execute_tool("wikipedia_summary", {"title": "Nope Nope"}, tmp_path) == (
        "Error: no Wikipedia article named 'Nope Nope'"
    )
    assert execute_tool("wikipedia_summary", {}, tmp_path) == "Error: title is required"

    monkeypatch.setattr(
        tools.http, "_http_get", lambda url: (200, "application/json", b"[]")
    )
    assert (
        execute_tool("wikipedia_summary", {"title": "Ada"}, tmp_path)
        == "Error: unexpected response"
    )

    monkeypatch.setattr(
        tools.http,
        "_http_get",
        lambda url: (200, "application/json", b'{"title": "Bare", "extract": "  "}'),
    )
    assert execute_tool("wikipedia_summary", {"title": "Bare"}, tmp_path) == (
        "Error: no summary for 'Bare'"
    )

    monkeypatch.setattr(
        tools.http,
        "_http_get",
        lambda url: (
            200,
            "application/json",
            b'{"title": "Mercury", "extract": "Many things.", "type": "disambiguation"}',
        ),
    )
    ambiguous = execute_tool("wikipedia_summary", {"title": "Mercury"}, tmp_path)
    assert "summary:\nMany things." in ambiguous
    assert "note: this title is a disambiguation page" in ambiguous


def test_http_get_json_adds_params_and_rejects_non_json(tmp_path, monkeypatch):
    fake_get, seen = _json_get([("https://1.1.1.1/api", {"temp": 21.5, "ok": True})])
    monkeypatch.setattr(tools.http, "_http_get", fake_get)

    result = execute_tool(
        "http_get_json",
        {
            "url": "https://1.1.1.1/api?units=metric",
            "params": {"lat": 45.5, "city": "Montréal", "dry": True},
        },
        tmp_path,
    )
    assert json.loads(result) == {"temp": 21.5, "ok": True}
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(seen[0]).query)
    assert query == {
        "units": ["metric"],
        "lat": ["45.5"],
        "city": ["Montréal"],
        "dry": ["true"],
    }

    monkeypatch.setattr(
        tools.http, "_http_get", lambda url: (200, "text/html", b"<html></html>")
    )
    html = execute_tool("http_get_json", {"url": "https://1.1.1.1/page"}, tmp_path)
    assert html.startswith("Error: unsupported content type")

    monkeypatch.setattr(
        tools.http, "_http_get", lambda url: (200, "application/json", b"{nope")
    )
    assert execute_tool(
        "http_get_json", {"url": "https://1.1.1.1/api"}, tmp_path
    ).startswith("Error: invalid JSON response")
    assert "blocked" in execute_tool(
        "http_get_json", {"url": "http://10.0.0.1/api"}, tmp_path
    )
    assert (
        execute_tool(
            "http_get_json", {"url": "https://1.1.1.1/api", "params": ["a"]}, tmp_path
        )
        == "Error: params must be an object"
    )


class _FakeHTTPResponse:
    def __init__(self, code, reason, body, url, headers):
        self.code = code
        self.status = code
        self.reason = reason
        self.msg = reason
        self.url = url
        self._body = body
        self.headers = http.client.HTTPMessage()
        for key, value in headers.items():
            self.headers[key] = value

    def read(self, amt=-1):
        if amt is None or amt < 0:
            data, self._body = self._body, b""
            return data
        data, self._body = self._body[:amt], self._body[amt:]
        return data

    def info(self):
        return self.headers

    def geturl(self):
        return self.url

    def close(self):
        return None


def _install_http(monkeypatch, replies):
    seen = []
    pending = list(replies)

    def http_open(self, req):
        seen.append(req)
        reply = pending.pop(0)
        return _FakeHTTPResponse(
            reply["code"],
            reply.get("reason", "OK"),
            reply.get("body", b""),
            req.full_url,
            reply.get("headers", {}),
        )

    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", http_open)
    return seen


def test_http_request_blocks_targets_and_bad_arguments(tmp_path):
    for url in (
        "file:///etc/passwd",
        "http://user:pass@1.1.1.1/",
        "http://127.0.0.1/",
        "http://10.0.0.1/",
    ):
        result = execute_tool("http_request", {"method": "GET", "url": url}, tmp_path)
        assert result.startswith("Error:")
        assert "blocked" in result
    assert (
        execute_tool(
            "http_request", {"method": "TRACE", "url": "http://1.1.1.1/"}, tmp_path
        )
        == "Error: Method not allowed: TRACE"
    )
    assert (
        execute_tool(
            "http_request",
            {"method": "GET", "url": "http://1.1.1.1/", "params": ["a"]},
            tmp_path,
        )
        == "Error: params must be an object"
    )
    assert (
        execute_tool(
            "http_request",
            {
                "method": "POST",
                "url": "http://1.1.1.1/",
                "json_body": {"a": 1},
                "body": "raw",
            },
            tmp_path,
        )
        == "Error: json_body and body cannot both be set"
    )
    assert (
        execute_tool("http_request", {"url": "http://1.1.1.1/"}, tmp_path)
        == "Error: method is required"
    )
    assert (
        execute_tool(
            "http_request",
            {"method": "GET", "url": "http://1.1.1.1/", "follow_redirects": "yes"},
            tmp_path,
        )
        == "Error: follow_redirects must be a boolean"
    )


def test_http_request_maps_timeout_and_resolution_failure(tmp_path, monkeypatch):
    def timed_out(self, request, timeout=None):
        raise urllib.error.URLError(TimeoutError())

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", timed_out)
    assert (
        execute_tool(
            "http_request", {"method": "GET", "url": "http://1.1.1.1/"}, tmp_path
        )
        == "Error: timed out"
    )

    def unresolvable(*args, **kwargs):
        raise socket.gaierror(8, "nodename nor servname provided")

    monkeypatch.setattr(tools.http.socket, "getaddrinfo", unresolvable)
    assert (
        execute_tool(
            "http_request", {"method": "GET", "url": "http://example.com/"}, tmp_path
        )
        == "Error: could not resolve host"
    )


def _dns(monkeypatch, *answers):
    """Make getaddrinfo answer each resolution from the given addresses."""
    pending = list(answers)

    def getaddrinfo(host, port, *args, **kwargs):
        if not pending:
            raise AssertionError("unexpected DNS lookup")
        answer = pending.pop(0)
        addresses = answer if isinstance(answer, list) else [answer]
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 0))
            for address in addresses
        ]

    monkeypatch.setattr(tools.http.socket, "getaddrinfo", getaddrinfo)
    return pending


def test_http_tools_block_a_dns_answer_that_flips_before_connecting(
    tmp_path, monkeypatch
):
    def no_socket(*args, **kwargs):
        raise AssertionError("socket opened")

    monkeypatch.setattr(tools.http.socket, "create_connection", no_socket)

    _dns(monkeypatch, "1.1.1.1", "127.0.0.1")
    result = execute_tool(
        "http_request", {"method": "GET", "url": "http://rebind.test/"}, tmp_path
    )
    assert result == "Error: blocked host"

    _dns(monkeypatch, "1.1.1.1", "169.254.169.254")
    result = execute_tool("fetch_url", {"url": "http://rebind.test/"}, tmp_path)
    assert result == "Error: blocked host"
    assert not (tmp_path / "downloads").exists()


def test_pinned_connection_uses_the_vetted_address(monkeypatch):
    seen = []

    def create_connection(address, timeout=None, source_address=None):
        seen.append((address, timeout, source_address))
        if address[0] == "1.0.0.1":
            raise OSError("unreachable")
        return "sock"

    monkeypatch.setattr(tools.http.socket, "create_connection", create_connection)

    _dns(monkeypatch, ["1.0.0.1", "1.1.1.1"])
    assert tools.http._pinned_create_connection(("dns.test", 443), 5, None) == "sock"
    assert seen == [
        (("1.0.0.1", 443), 5, None),
        (("1.1.1.1", 443), 5, None),
    ]

    seen.clear()
    _dns(monkeypatch, ["1.1.1.1", "10.0.0.5"])
    with pytest.raises(ValueError, match="blocked host"):
        tools.http._pinned_create_connection(("dns.test", 80), 5, None)
    assert seen == []

    _dns(monkeypatch, ["10.0.0.5"])
    token = tools.paths._yolo.set(True)
    try:
        assert tools.http._pinned_create_connection(("dns.test", 80), 5, None) == "sock"
    finally:
        tools.paths._yolo.reset(token)
    assert seen == [(("10.0.0.5", 80), 5, None)]

    _dns(monkeypatch, ["1.0.0.1"])
    with pytest.raises(OSError, match="unreachable"):
        tools.http._pinned_create_connection(("dns.test", 80), 5, None)


def test_http_openers_pin_every_connection():
    for opener in (tools.http._http_opener(True), tools.http._http_opener(False)):
        kinds = {type(handler) for handler in opener.handlers}
        assert tools.http._PinnedHTTPHandler in kinds
        assert tools.http._PinnedHTTPSHandler in kinds
        assert urllib.request.HTTPHandler not in kinds
        assert urllib.request.HTTPSHandler not in kinds
    connection = tools.http._PinnedHTTPSConnection("dns.test")
    assert connection._create_connection is tools.http._pinned_create_connection
    assert connection.host == "dns.test"


def test_http_request_posts_json_and_returns_status(tmp_path, monkeypatch):
    seen = _install_http(
        monkeypatch,
        [
            {
                "code": 404,
                "reason": "Not Found",
                "body": b"missing",
                "headers": {"Content-Type": "application/json"},
            }
        ],
    )

    result = execute_tool(
        "http_request",
        {
            "method": "post",
            "url": "http://1.1.1.1/api",
            "params": {"q": "1"},
            "json_body": {"name": "Ada"},
            "headers": {"X-Test": "yes"},
        },
        tmp_path,
    )

    assert not result.startswith("Error:")
    assert "status_code: 404" in result
    assert "reason: Not Found" in result
    assert "final_url: http://1.1.1.1/api?q=1" in result
    assert "content_type: application/json" in result
    assert "truncated: false" in result
    assert "body:\nmissing" in result
    request = seen[0]
    assert request.get_method() == "POST"
    assert request.data == b'{"name": "Ada"}'
    assert request.headers["Content-type"] == "application/json"
    assert request.headers["User-agent"] == tools.http.USER_AGENT
    assert request.headers["X-test"] == "yes"
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
    assert query == {"q": ["1"]}

    seen = _install_http(
        monkeypatch,
        [
            {
                "code": 401,
                "reason": "Unauthorized",
                "body": b"Invalid email or password.",
                "headers": {"Content-Type": "text/html"},
            }
        ],
    )
    as_string = execute_tool(
        "http_request",
        {
            "method": "POST",
            "url": "http://1.1.1.1/login",
            "json_body": ('{"email": "test@example.com", "password": "password123"}'),
        },
        tmp_path,
    )
    assert "status_code: 401" in as_string
    assert seen[-1].data == (
        b'{"email": "test@example.com", "password": "password123"}'
    )
    assert seen[-1].headers["Content-type"] == "application/json"
    rejected = execute_tool(
        "http_request",
        {"method": "POST", "url": "http://1.1.1.1/login", "json_body": "not-json"},
        tmp_path,
    )
    assert rejected.startswith("Error: json_body must be an object or array.")
    assert '{"email": "a@b.c", "password": "secret"}' in rejected


def test_http_request_redirects_and_truncates(tmp_path, monkeypatch):
    stayed_seen = _install_http(
        monkeypatch,
        [
            {
                "code": 302,
                "reason": "Found",
                "body": b"stay",
                "headers": {
                    "Location": "http://127.0.0.1/secret",
                    "Content-Type": "text/plain",
                },
            }
        ],
    )
    stayed = execute_tool(
        "http_request", {"method": "GET", "url": "http://1.1.1.1/go"}, tmp_path
    )
    assert "status_code: 302" in stayed
    assert "final_url: http://1.1.1.1/go" in stayed
    assert "body:\nstay" in stayed
    assert len(stayed_seen) == 1

    followed_seen = _install_http(
        monkeypatch,
        [
            {
                "code": 302,
                "reason": "Found",
                "body": b"go",
                "headers": {"Location": "http://127.0.0.1/secret"},
            }
        ],
    )
    blocked = execute_tool(
        "http_request",
        {
            "method": "GET",
            "url": "http://1.1.1.1/go",
            "follow_redirects": True,
        },
        tmp_path,
    )
    assert blocked.startswith("Error:")
    assert "blocked" in blocked
    assert len(followed_seen) == 1

    monkeypatch.setattr(tools.http, "FETCH_MAX_BYTES", 9000)
    _install_http(
        monkeypatch,
        [
            {
                "code": 200,
                "reason": "OK",
                "body": b"y" * 9001,
                "headers": {"Content-Type": "text/plain"},
            }
        ],
    )
    truncated = execute_tool(
        "http_request", {"method": "GET", "url": "http://1.1.1.1/big"}, tmp_path
    )
    assert "truncated: true" in truncated
    assert "...[truncated," in truncated
    assert "save the full response with fetch_url" in truncated


def test_http_request_yolo_allows_localhost(tmp_path, monkeypatch):
    _install_http(
        monkeypatch,
        [
            {
                "code": 200,
                "reason": "OK",
                "body": b"local",
                "headers": {"Content-Type": "text/plain"},
            }
        ],
    )
    result = execute_tool(
        "http_request",
        {"method": "GET", "url": "http://127.0.0.1/local"},
        tmp_path,
        yolo=True,
    )
    assert "status_code: 200" in result
    assert "body:\nlocal" in result
    assert "blocked" in execute_tool(
        "http_request",
        {"method": "GET", "url": "http://127.0.0.1/local"},
        tmp_path,
    )


def test_yolo_http_allows_localhost_and_private_hosts(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tools.http,
        "_http_get",
        lambda url: (200, "application/json", b'{"ok": true}'),
    )
    for url in (
        "http://localhost/local",
        "http://127.0.0.1/local",
        "http://10.0.0.1/local",
    ):
        fetched = execute_tool("fetch_url", {"url": url}, tmp_path, yolo=True)
        assert "status: success" in fetched
    got = execute_tool(
        "http_get_json", {"url": "http://127.0.0.1/api"}, tmp_path, yolo=True
    )
    assert json.loads(got) == {"ok": True}
    assert "blocked" in execute_tool(
        "fetch_url", {"url": "http://127.0.0.1/local"}, tmp_path
    )
    assert "blocked" in execute_tool(
        "http_get_json", {"url": "http://10.0.0.1/api"}, tmp_path
    )
    scheme = execute_tool(
        "fetch_url", {"url": "file:///etc/passwd"}, tmp_path, yolo=True
    )
    assert scheme.startswith("Error:")
    assert "blocked" in scheme
