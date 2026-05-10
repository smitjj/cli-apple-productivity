#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


SERVER_PATH = Path(__file__).with_name("apple_productivity_mcp_server.py")


class ToolError(RuntimeError):
    """Raised when a tools/call response carries isError: true."""


class PermissionDenied(ToolError):
    """Raised when the OS blocks automation; smoke test should skip."""


def call_tool(name: str, arguments: dict, request_id: int = 2) -> str:
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "smoke-test", "version": "0.1.0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    ]
    payload = b""
    for message in requests:
        body = json.dumps(message).encode("utf-8")
        payload += b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body

    completed = subprocess.run(
        ["python3", str(SERVER_PATH)],
        input=payload,
        capture_output=True,
        timeout=45,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.decode("utf-8") or completed.stdout.decode("utf-8"))
    output = completed.stdout.decode("utf-8")
    error_match = re.search(r'"isError": true.*?"text": "(.*?)"', output, re.DOTALL)
    if error_match:
        message = error_match.group(1)
        if "automation permission is blocked" in message or "(-1743)" in message:
            raise PermissionDenied(message)
        raise ToolError(message)
    return output


def extract_tool_text(output: str) -> str:
    messages = parse_mcp_messages(output)
    text_values = []
    for message in messages:
        result = message.get("result", {})
        for item in result.get("content", []):
            if item.get("type") == "text":
                text_values.append(item.get("text", ""))
    if not text_values:
        raise AssertionError(f"Could not find MCP text payload in output:\n{output}")
    return text_values[-1]


def parse_mcp_messages(output: str) -> list:
    messages = []
    cursor = 0
    marker = "Content-Length:"
    while True:
        start = output.find(marker, cursor)
        if start == -1:
            break
        header_end = output.find("\r\n\r\n", start)
        if header_end == -1:
            break
        header = output[start:header_end]
        length = int(header.split(":", 1)[1].strip())
        body_start = header_end + 4
        body = output[body_start:body_start + length]
        messages.append(json.loads(body))
        cursor = body_start + length
    return messages


def assert_contains(output: str, expected: str) -> None:
    payload_text = extract_tool_text(output)
    if expected not in payload_text:
        raise AssertionError(f"Expected to find {expected!r} in payload:\n{payload_text}")


def expect_error(name: str, arguments: dict, request_id: int, must_contain: str) -> None:
    """Call tool expecting it to fail; assert the error message includes a hint."""
    try:
        call_tool(name, arguments, request_id)
    except ToolError as exc:
        if must_contain.lower() not in str(exc).lower():
            raise AssertionError(
                f"Expected error containing {must_contain!r}, got: {exc}"
            ) from exc
        return
    raise AssertionError(f"Expected {name} {arguments!r} to fail but it succeeded.")


def test_mail_read_paths() -> None:
    accounts_output = call_tool("mail_accounts", {"action": "list"}, 10)
    assert_contains(accounts_output, '"name":')
    mailboxes_output = call_tool("mail_mailboxes", {"action": "list"}, 11)
    assert_contains(mailboxes_output, '"mailboxes":')

    accounts_payload = json.loads(extract_tool_text(accounts_output))
    if not accounts_payload:
        return
    first_account = accounts_payload[0].get("name")
    if not first_account:
        return
    mailboxes_payload = json.loads(extract_tool_text(mailboxes_output))
    target_mailbox = None
    for entry in mailboxes_payload:
        for mailbox in entry.get("mailboxes", []):
            if mailbox.get("name", "").lower() in {"inbox", "sent"}:
                target_mailbox = (mailbox["account"], mailbox["name"])
                break
        if target_mailbox:
            break
    if target_mailbox:
        list_output = call_tool(
            "mail_messages",
            {
                "action": "list",
                "account_name": target_mailbox[0],
                "mailbox_name": target_mailbox[1],
                "limit": 1,
            },
            12,
        )
        assert_contains(list_output, '"messages":')


def test_mail_negative_paths() -> None:
    expect_error(
        "mail_messages",
        {"action": "list", "mailbox_name": "ThisMailboxDoesNotExist__SmokeTest", "limit": 1},
        15,
        "not found",
    )
    expect_error(
        "mail_messages",
        {
            "action": "get-attachment",
            "message_id": 1,
            "attachment_index": 0,
            "save_to": "/tmp/x",
            "return_inline": True,
        },
        16,
        "save_to or return_inline",
    )
    expect_error(
        "mail_compose",
        {"action": "create", "subject": "no recipients"},
        17,
        "recipient",
    )


def test_calendar_crud(calendar_name: str) -> None:
    create_output = call_tool(
        "calendar_events",
        {
            "action": "create",
            "summary": "Codex Smoke Test Event",
            "start_date": "2026-05-10T14:00:00",
            "end_date": "2026-05-10T15:00:00",
            "calendar_name": calendar_name,
        },
        20,
    )
    payload_text = extract_tool_text(create_output)
    match = re.search(r'Calendar::[A-F0-9-]+', payload_text)
    if not match:
        raise AssertionError(f"Could not find event id in output:\n{create_output}")
    event_id = match.group(0)

    get_output = call_tool("calendar_events", {"action": "get", "event_id": event_id}, 21)
    assert_contains(get_output, "Codex Smoke Test Event")

    update_output = call_tool(
        "calendar_events",
        {
            "action": "update",
            "event_id": event_id,
            "location": "Smoke Test Office",
            "notes": "Updated by smoke test",
        },
        22,
    )
    assert_contains(update_output, "Smoke Test Office")

    delete_output = call_tool("calendar_events", {"action": "delete", "event_id": event_id}, 23)
    assert_contains(delete_output, '"deleted": true')

    expect_error(
        "calendar_events",
        {"action": "get", "event_id": "no-separator-here"},
        24,
        "calendar event ids must include both",
    )


def test_reminders_crud(list_name: str) -> None:
    create_output = call_tool(
        "reminders_tasks",
        {"action": "create", "title": "Codex Smoke Test Reminder", "list_name": list_name},
        30,
    )
    payload_text = extract_tool_text(create_output)
    match = re.search(r'x-apple-reminder://[A-F0-9-]+', payload_text)
    if not match:
        raise AssertionError(f"Could not find reminder id in output:\n{create_output}")
    reminder_id = match.group(0)

    update_output = call_tool(
        "reminders_tasks",
        {
            "action": "update",
            "reminder_id": reminder_id,
            "notes": "Updated by smoke test",
            "completed": True,
        },
        31,
    )
    assert_contains(update_output, "Updated by smoke test")

    delete_output = call_tool(
        "reminders_tasks",
        {"action": "delete", "reminder_id": reminder_id},
        32,
    )
    assert_contains(delete_output, '"deleted": true')


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test the Apple Productivity Codex plugin.")
    parser.add_argument("--calendar-name", default="Calendar")
    parser.add_argument("--reminder-list", default="Personal")
    parser.add_argument("--skip-mail", action="store_true")
    args = parser.parse_args()

    try:
        if not args.skip_mail:
            test_mail_read_paths()
            test_mail_negative_paths()
        test_calendar_crud(args.calendar_name)
        test_reminders_crud(args.reminder_list)
    except PermissionDenied as exc:
        print(f"SKIP: macOS Automation permission denied — {exc}", file=sys.stderr)
        return 0
    print("Smoke tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
