"""Tiny stdio MCP server for tests. Speaks newline-delimited JSON-RPC."""

import json
import os
import sys


def emit(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def tool(name: str, description: str) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": {"httpql": {"type": "string"}},
        },
    }


def tool_page(cursor: str | None) -> dict:
    if os.environ.get("FAKE_MCP") == "collide":
        return {"tools": [tool("read_file", "collides")]}
    if not cursor:
        return {
            "tools": [tool("caido_list_requests", "List requests")],
            "nextCursor": "p2",
        }
    return {
        "tools": [
            tool("caido_send_request", "Send"),
            tool("echo_env", "Echo"),
            tool("boom", "Fail"),
            tool("die", "Exit"),
            tool("wide", "Wide"),
            tool("picture", "Picture"),
        ]
    }


def call_result(name: str) -> dict:
    if name == "die":
        sys.stdout.flush()
        os._exit(0)
    if name == "boom":
        return {
            "isError": True,
            "content": [{"type": "text", "text": "nope"}],
        }
    if name == "echo_env":
        body = json.dumps(
            {
                "argv": sys.argv,
                "token": os.environ.get("CAIDO_ACCESS_TOKEN"),
                "openai": os.environ.get("OPENAI_API_KEY"),
            }
        )
        return {"content": [{"type": "text", "text": body}]}
    if name == "wide":
        return {"content": [{"type": "text", "text": "x" * 20000}]}
    if name == "picture":
        return {"content": [{"type": "image", "data": "abc"}]}
    return {
        "content": [
            {"type": "text", "text": "alpha"},
            {"type": "text", "text": "beta"},
        ]
    }


def handle(message: dict) -> None:
    if "id" not in message:
        return
    method = message.get("method")
    pending = message["id"]
    params = message.get("params") or {}
    if method == "initialize":
        emit(
            {
                "jsonrpc": "2.0",
                "id": pending,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "serverInfo": {"name": "fake", "version": "1"},
                    "instructions": "Use HTTPQL.",
                },
            }
        )
        return
    if method == "tools/list":
        emit(
            {
                "jsonrpc": "2.0",
                "id": pending,
                "result": tool_page(params.get("cursor")),
            }
        )
        return
    if method == "resources/list":
        if os.environ.get("FAKE_MCP") == "no-resources":
            emit(
                {
                    "jsonrpc": "2.0",
                    "id": pending,
                    "error": {"code": -32601, "message": "Method not found"},
                }
            )
            return
        emit(
            {
                "jsonrpc": "2.0",
                "id": pending,
                "result": {
                    "resources": [
                        {
                            "uri": "caido://project",
                            "description": "Current project",
                        }
                    ]
                },
            }
        )
        return
    if method == "tools/call":
        emit(
            {
                "jsonrpc": "2.0",
                "id": pending,
                "result": call_result(params.get("name")),
            }
        )
        return
    if method == "resources/read":
        emit(
            {
                "jsonrpc": "2.0",
                "id": pending,
                "result": {
                    "contents": [{"uri": params.get("uri"), "text": "project-body"}]
                },
            }
        )
        return
    emit(
        {
            "jsonrpc": "2.0",
            "id": pending,
            "error": {"code": -32601, "message": "Method not found"},
        }
    )


def main() -> None:
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        handle(json.loads(text))


if __name__ == "__main__":
    main()
