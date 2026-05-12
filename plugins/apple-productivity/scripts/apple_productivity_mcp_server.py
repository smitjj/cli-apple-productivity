#!/usr/bin/env python3

from __future__ import annotations

import json
import sys

from pathlib import Path

from apple_productivity_registry import KNOWN_TOOLS, mcp_tools
from apple_productivity_service import AppleProductivityService, load_plugin_version, SCRIPT_PATH


SERVER_NAME = "apple-productivity"
SERVER_VERSION = load_plugin_version(SCRIPT_PATH) or "0.0.0"
PROTOCOL_VERSION = "2024-11-05"
TOOLS = mcp_tools()
SERVICE: AppleProductivityService | None = None
USE_FRAMED_STDIO: bool | None = None


def get_service() -> AppleProductivityService:
    global SERVICE
    if SERVICE is None:
        SERVICE = AppleProductivityService()
    return SERVICE


def write_message(message: dict) -> None:
    payload = json.dumps(message, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    if USE_FRAMED_STDIO:
        sys.stdout.buffer.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii"))
        sys.stdout.buffer.write(payload)
    else:
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()


def read_message():
    global USE_FRAMED_STDIO

    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            continue

        if line.startswith(b"{"):
            USE_FRAMED_STDIO = False
            return json.loads(line.decode("utf-8"))

        if line.lower().startswith(b"content-length:"):
            USE_FRAMED_STDIO = True
            headers = {"content-length": line.split(b":", 1)[1].strip().decode("ascii")}
            while True:
                header_line = sys.stdin.buffer.readline()
                if header_line in (b"\r\n", b"\n"):
                    break
                name, value = header_line.decode("utf-8").split(":", 1)
                headers[name.strip().lower()] = value.strip()

            length = int(headers.get("content-length", "0"))
            if length <= 0:
                return None
            body = sys.stdin.buffer.read(length)
            return json.loads(body.decode("utf-8"))

        return None


def success_response(request_id, result) -> None:
    write_message({"jsonrpc": "2.0", "id": request_id, "result": result})


def error_response(request_id, code, message) -> None:
    write_message({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def handle_request(message: dict) -> None:
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params", {})

    if method == "initialize":
        negotiated_protocol = params.get("protocolVersion") or PROTOCOL_VERSION
        success_response(
            request_id,
            {
                "protocolVersion": negotiated_protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                },
            },
        )
        return

    if method == "ping":
        success_response(request_id, {})
        return

    if method == "tools/list":
        success_response(request_id, {"tools": TOOLS})
        return

    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        if tool_name not in KNOWN_TOOLS:
            error_response(request_id, -32601, f"Unknown tool: {tool_name}")
            return
        try:
            result = get_service().dispatch(tool_name, arguments)
        except RuntimeError as exc:
            success_response(
                request_id,
                {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                },
            )
            return
        except Exception as exc:
            success_response(
                request_id,
                {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                },
            )
            return
        success_response(
            request_id,
            {"content": [{"type": "text", "text": json.dumps(result, separators=(",", ":"), ensure_ascii=True)}]},
        )
        return

    if request_id is not None:
        error_response(request_id, -32601, f"Unsupported method: {method}")


def main() -> None:
    while True:
        message = read_message()
        if message is None:
            break
        if message.get("method") == "notifications/initialized":
            continue
        handle_request(message)


if __name__ == "__main__":
    main()
