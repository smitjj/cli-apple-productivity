#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


CLI_PATH = Path(__file__).with_name("apple_productivity_cli.py")


class CliError(RuntimeError):
    """Raised when a CLI command exits non-zero."""

    def __init__(self, command: list[str], returncode: int, stdout: str, stderr: str) -> None:
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        message = stderr.strip() or stdout.strip() or f"CLI exited {returncode}"
        super().__init__(message)


class PermissionDenied(CliError):
    """Raised when macOS blocks automation; smoke test should skip."""


def run_cli(*args: str, input_text: str | None = None, timeout: int = 90) -> str:
    command = ["python3", str(CLI_PATH), "--timeout", str(timeout), *args]
    completed = subprocess.run(
        command,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        if completed.returncode == 3 or _looks_like_permission_error(completed.stderr + completed.stdout):
            raise PermissionDenied(command, completed.returncode, completed.stdout, completed.stderr)
        raise CliError(command, completed.returncode, completed.stdout, completed.stderr)
    return completed.stdout


def run_cli_json(*args: str, input_text: str | None = None) -> object:
    output = run_cli(*args, input_text=input_text)
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"Expected JSON output from {' '.join(args)}:\n{output}") from exc


def expect_cli_error(args: list[str], must_contain: str, expected_exit: int | None = None) -> None:
    try:
        run_cli(*args)
    except CliError as exc:
        if expected_exit is not None and exc.returncode != expected_exit:
            raise AssertionError(f"Expected exit {expected_exit}, got {exc.returncode}: {exc}") from exc
        text = (exc.stderr + exc.stdout).lower()
        if must_contain.lower() not in text:
            raise AssertionError(f"Expected error containing {must_contain!r}, got: {exc}") from exc
        return
    raise AssertionError(f"Expected CLI command to fail: {' '.join(args)}")


def _looks_like_permission_error(text: str) -> bool:
    lowered = text.lower()
    return (
        "automation permission is blocked" in lowered
        or "not authorized" in lowered
        or "access is blocked" in lowered
        or "(-1743)" in lowered
    )


def test_cli_shape() -> None:
    help_output = run_cli("--help")
    for expected in ("batch", "repl", "doctor", "mail"):
        if expected not in help_output:
            raise AssertionError(f"Expected {expected!r} in CLI help:\n{help_output}")

    compact_output = run_cli("mail-permissions-check")
    if "\n  " in compact_output:
        raise AssertionError(f"Default CLI output should be compact JSON:\n{compact_output}")
    json.loads(compact_output)

    pretty_output = run_cli("mail-permissions-check", "--pretty")
    if "\n  " not in pretty_output:
        raise AssertionError(f"--pretty should emit indented JSON:\n{pretty_output}")
    json.loads(pretty_output)

    expect_cli_error(["mail-messages", "get"], "message_id is required", expected_exit=2)


def test_batch_mode() -> None:
    payload = "\n".join(
        [
            '{"tool":"mail_accounts","arguments":{"action":"list"}}',
            '{"tool":"mail_mailboxes","arguments":{"action":"list"}}',
        ]
    )
    output = run_cli("batch", "--jsonl", input_text=payload)
    lines = [json.loads(line) for line in output.splitlines() if line.strip()]
    if len(lines) != 2:
        raise AssertionError(f"Expected two JSONL batch responses:\n{output}")
    if not all(item.get("ok") for item in lines):
        raise AssertionError(f"Expected all batch calls to succeed:\n{output}")


def test_repl_mode() -> None:
    output = run_cli(
        "repl",
        input_text=(
            "mail-permissions-check\n"
            '{"tool":"mail_accounts","arguments":{"action":"list"}}\n'
            "exit\n"
        ),
    )
    lines = _extract_repl_json(output)
    if len(lines) != 2:
        raise AssertionError(f"Expected two REPL JSON responses:\n{output}")
    if not isinstance(lines[0], dict) or "automation" not in lines[0]:
        raise AssertionError(f"Expected first REPL response to be diagnostics:\n{output}")
    if not isinstance(lines[1], list):
        raise AssertionError(f"Expected second REPL response to be mail account list:\n{output}")


def _extract_repl_json(output: str) -> list[object]:
    values = []
    for line in output.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        object_start = candidate.find("{")
        array_start = candidate.find("[")
        starts = [pos for pos in (object_start, array_start) if pos >= 0]
        if not starts:
            continue
        candidate = candidate[min(starts):]
        try:
            values.append(json.loads(candidate))
        except json.JSONDecodeError:
            continue
    return values


def test_mail_read_paths() -> None:
    accounts_payload = run_cli_json("mail-accounts", "list")
    if not isinstance(accounts_payload, list):
        raise AssertionError(f"mail-accounts should return an array: {accounts_payload!r}")

    mailboxes_payload = run_cli_json("mail-mailboxes", "list")
    if not isinstance(mailboxes_payload, list):
        raise AssertionError(f"mail-mailboxes should return an array: {mailboxes_payload!r}")

    target_mailbox = None
    for entry in mailboxes_payload:
        for mailbox in entry.get("mailboxes", []):
            if mailbox.get("name", "").lower() in {"inbox", "sent"}:
                target_mailbox = (mailbox["account"], mailbox["name"])
                break
        if target_mailbox:
            break

    if target_mailbox:
        list_payload = run_cli_json(
            "mail-messages",
            "list",
            "--account-name",
            target_mailbox[0],
            "--mailbox-name",
            target_mailbox[1],
            "--limit",
            "1",
        )
        if "messages" not in list_payload:
            raise AssertionError(f"mail-messages list should include messages: {list_payload!r}")


def test_mail_negative_paths() -> None:
    expect_cli_error(
        ["mail-messages", "list", "--mailbox-name", "ThisMailboxDoesNotExist__CliSmokeTest", "--limit", "1"],
        "not found",
        expected_exit=4,
    )
    expect_cli_error(
        [
            "mail-messages",
            "get-attachment",
            "--message-id",
            "1",
            "--attachment-index",
            "0",
            "--save-to",
            "/tmp/x",
            "--return-inline",
        ],
        "save_to or return_inline",
        expected_exit=2,
    )
    expect_cli_error(["mail-compose", "create", "--subject", "no recipients"], "recipient", expected_exit=2)


def test_calendar_crud(calendar_name: str) -> None:
    event_id = None
    create_payload = run_cli_json(
        "calendar-events",
        "create",
        "--summary",
        "Codex CLI Smoke Test Event",
        "--start-date",
        "2026-05-10T14:00:00",
        "--end-date",
        "2026-05-10T15:00:00",
        "--calendar-name",
        calendar_name,
    )
    event_id = create_payload.get("id")
    if not event_id or not re.search(r"Calendar::[A-F0-9-]+", event_id):
        raise AssertionError(f"Could not find event id in create payload: {create_payload!r}")
    try:
        get_payload = run_cli_json("calendar-events", "get", "--event-id", event_id)
        if get_payload.get("summary") != "Codex CLI Smoke Test Event":
            raise AssertionError(f"Unexpected calendar get payload: {get_payload!r}")

        update_payload = run_cli_json(
            "calendar-events",
            "update",
            "--event-id",
            event_id,
            "--location",
            "CLI Smoke Test Office",
            "--notes",
            "Updated by CLI smoke test",
        )
        if update_payload.get("location") != "CLI Smoke Test Office":
            raise AssertionError(f"Unexpected calendar update payload: {update_payload!r}")
    finally:
        if event_id:
            try:
                delete_payload = run_cli_json("calendar-events", "delete", "--event-id", event_id)
                if delete_payload.get("deleted") is not True:
                    raise AssertionError(f"Unexpected calendar delete payload: {delete_payload!r}")
            except CliError as exc:
                if "not found" not in str(exc).lower():
                    raise


def test_reminders_crud(list_name: str) -> None:
    reminder_id = None
    create_payload = run_cli_json(
        "reminders-tasks",
        "create",
        "--title",
        "Codex CLI Smoke Test Reminder",
        "--list-name",
        list_name,
    )
    reminder_id = create_payload.get("id")
    if not reminder_id or not reminder_id.startswith("x-apple-reminder://"):
        raise AssertionError(f"Could not find reminder id in create payload: {create_payload!r}")
    try:
        update_payload = run_cli_json(
            "reminders-tasks",
            "update",
            "--reminder-id",
            reminder_id,
            "--notes",
            "Updated by CLI smoke test",
            "--completed",
            "true",
        )
        if update_payload.get("notes") != "Updated by CLI smoke test":
            raise AssertionError(f"Unexpected reminder update payload: {update_payload!r}")
    finally:
        if reminder_id:
            try:
                delete_payload = run_cli_json("reminders-tasks", "delete", "--reminder-id", reminder_id)
                if delete_payload.get("deleted") is not True:
                    raise AssertionError(f"Unexpected reminder delete payload: {delete_payload!r}")
            except CliError as exc:
                if "not found" not in str(exc).lower():
                    raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test the Apple Productivity CLI.")
    parser.add_argument("--calendar-name", default="Calendar")
    parser.add_argument("--reminder-list", default="Personal")
    parser.add_argument("--skip-mail", action="store_true")
    args = parser.parse_args()

    try:
        test_cli_shape()
        test_batch_mode()
        test_repl_mode()
        if not args.skip_mail:
            test_mail_read_paths()
            test_mail_negative_paths()
        test_calendar_crud(args.calendar_name)
        test_reminders_crud(args.reminder_list)
    except PermissionDenied as exc:
        print(f"SKIP: macOS Automation permission denied - {exc}", file=sys.stderr)
        return 0
    print("CLI smoke tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
