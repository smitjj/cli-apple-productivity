#!/usr/bin/env python3

from __future__ import annotations

import json
import sys

from apple_productivity_service import AppleProductivityService


SERVER_NAME = "apple-productivity"
SERVER_VERSION = "0.5.0"
PROTOCOL_VERSION = "2024-11-05"


TOOLS = [
    {
        "name": "mail_accounts",
        "description": "List Apple Mail accounts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list"]},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "mail_mailboxes",
        "description": "List Apple Mail mailboxes, optionally scoped to an account.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list"]},
                "account_name": {"type": "string"},
                "include_counts": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "mail_messages",
        "description": "Manage Apple Mail messages through grouped actions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "list",
                        "get",
                        "search",
                        "move",
                        "delete",
                        "set-read",
                        "set-flag",
                        "open",
                        "get-attachment",
                        "get-thread",
                        "get-unsubscribe-link",
                        "bulk-set-read",
                        "bulk-set-flag",
                        "bulk-move",
                        "bulk-delete",
                    ],
                },
                "mailbox_name": {"type": "string"},
                "account_name": {"type": "string"},
                "message_id": {"type": "integer"},
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "unread_only": {"type": "boolean"},
                "flagged_only": {"type": "boolean"},
                "include_source": {"type": "boolean"},
                "target_mailbox": {"type": "string"},
                "target_account": {"type": "string"},
                "read": {"type": "boolean"},
                "flagged": {"type": "boolean"},
                "since": {"type": "string"},
                "from_address": {"type": "string"},
                "to_address": {"type": "string"},
                "subject_contains": {"type": "string"},
                "attachment_index": {"type": "integer", "minimum": 0},
                "save_to": {"type": "string"},
                "return_inline": {"type": "boolean"},
                "message_ids": {"type": "array", "items": {"type": "integer"}, "maxItems": 50},
                "dry_run": {"type": "boolean"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
    {
        "name": "mail_compose",
        "description": "Create, reply to, or forward Apple Mail messages.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "reply", "forward"]},
                "message_id": {"type": "integer"},
                "to": {"type": "array", "items": {"type": "string"}},
                "cc": {"type": "array", "items": {"type": "string"}},
                "bcc": {"type": "array", "items": {"type": "string"}},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "reply_all": {"type": "boolean"},
                "open_in_mail": {"type": "boolean"},
                "send_now": {"type": "boolean"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
    {
        "name": "calendar_calendars",
        "description": "List macOS Calendar calendars.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list"]},
                "include_counts": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "calendar_events",
        "description": "List, inspect, create, update, delete, and open Calendar events.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "get", "create", "update", "delete", "open"]},
                "event_id": {"type": "string"},
                "calendar_name": {"type": "string"},
                "summary": {"type": "string"},
                "location": {"type": "string"},
                "notes": {"type": "string"},
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
                "search": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "all_day": {"type": "boolean"},
                "url": {"type": "string"},
                "recurrence": {"type": "string"},
                "recurrence_rule": {"type": "string"},
                "timezone": {"type": "string"},
                "alarms": {"type": "array", "items": {"type": "number"}},
                "source": {"type": "string"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
    {
        "name": "reminders_lists",
        "description": "List and manage macOS Reminders lists.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "create", "update", "delete"]},
                "list_id": {"type": "string"},
                "name": {"type": "string"},
                "include_counts": {"type": "boolean"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
    {
        "name": "reminders_tasks",
        "description": "List and manage macOS Reminders tasks.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "get", "create", "update", "delete", "complete", "incomplete"]},
                "reminder_id": {"type": "string"},
                "title": {"type": "string"},
                "list_name": {"type": "string"},
                "notes": {"type": "string"},
                "due_date": {"type": "string"},
                "completed": {"type": "boolean"},
                "search": {"type": "string"},
                "show_completed": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "priority": {"type": "integer", "minimum": 0, "maximum": 9},
                "flagged": {"type": "boolean"},
                "alarms": {"type": "array", "items": {"type": "number"}},
                "geofence": {
                    "type": "object",
                    "properties": {
                        "lat": {"type": "number"},
                        "lon": {"type": "number"},
                        "radius_meters": {"type": "number"},
                        "proximity": {"type": "string", "enum": ["enter", "leave"]},
                        "title": {"type": "string"},
                    },
                    "required": ["lat", "lon"],
                    "additionalProperties": False,
                },
                "source": {"type": "string"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
    {
        "name": "mail_drafts",
        "description": "List, edit, send, or delete saved Apple Mail drafts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "get", "update", "send", "delete"]},
                "message_id": {"type": "integer"},
                "account_name": {"type": "string"},
                "mailbox_name": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
    {
        "name": "mail_permissions_check",
        "description": "Diagnostic probe of Automation, Full Disk Access, EventKit, and Mail.app state.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["check"]},
            },
            "additionalProperties": False,
        },
    },
]


SERVICE = AppleProductivityService()


def write_message(message: dict) -> None:
    payload = json.dumps(message).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        name, value = line.decode("utf-8").split(":", 1)
        headers[name.strip().lower()] = value.strip()

    length = int(headers.get("content-length", "0"))
    if length <= 0:
        return None
    body = sys.stdin.buffer.read(length)
    return json.loads(body.decode("utf-8"))


def success_response(request_id, result) -> None:
    write_message({"jsonrpc": "2.0", "id": request_id, "result": result})


def error_response(request_id, code, message) -> None:
    write_message({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def handle_request(message: dict) -> None:
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params", {})

    if method == "initialize":
        success_response(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
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
        known_tools = {tool["name"] for tool in TOOLS}
        if tool_name not in known_tools:
            error_response(request_id, -32601, f"Unknown tool: {tool_name}")
            return
        try:
            result = SERVICE.dispatch(tool_name, arguments)
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
            {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=True)}]},
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
