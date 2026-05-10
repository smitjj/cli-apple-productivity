#!/usr/bin/env python3
"""Command-line wrapper around AppleProductivityService.

Examples:
  apple_productivity_cli.py mail-accounts list
  apple_productivity_cli.py mail-mailboxes list --account-name "iCloud" --include-counts
  apple_productivity_cli.py mail-messages list --mailbox-name INBOX --limit 5
  apple_productivity_cli.py mail-messages search --query invoice --since 2026-01-01
  apple_productivity_cli.py mail-compose create --to alice@example.com --subject Hi --body "hello"
  apple_productivity_cli.py calendar-events create --calendar-name Work \\
      --summary "Standup" --start-date 2026-05-11T09:00:00 --end-date 2026-05-11T09:30:00
  apple_productivity_cli.py reminders-tasks list --list-name Personal

Output is pretty-printed JSON by default; pass --raw for compact JSON. Errors
print to stderr and the process exits with a non-zero status.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from apple_productivity_service import AppleProductivityService


def _add_action(parser: argparse.ArgumentParser, choices: list) -> None:
    parser.add_argument("action", choices=choices)


def _maybe(arguments: dict, key: str, value: Any) -> None:
    if value is None:
        return
    arguments[key] = value


def _str_list(value):
    if value is None:
        return None
    return [v for v in value if v]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apple-productivity",
        description="CLI for Apple Mail, Calendar, and Reminders via the local MCP service.",
    )
    parser.add_argument("--raw", action="store_true", help="Emit compact JSON instead of pretty-printed.")
    parser.add_argument("--timeout", type=int, default=None, help="Override osascript timeout in seconds.")
    sub = parser.add_subparsers(dest="tool", required=True)

    mail_accounts = sub.add_parser("mail-accounts", help="List Apple Mail accounts.")
    _add_action(mail_accounts, ["list"])

    mail_mailboxes = sub.add_parser("mail-mailboxes", help="List Apple Mail mailboxes.")
    _add_action(mail_mailboxes, ["list"])
    mail_mailboxes.add_argument("--account-name")
    mail_mailboxes.add_argument("--include-counts", action="store_true")

    mail_messages = sub.add_parser("mail-messages", help="Read or modify Apple Mail messages.")
    _add_action(
        mail_messages,
        [
            "list", "get", "search", "move", "delete", "set-read", "set-flag", "open",
            "get-attachment", "get-thread", "get-unsubscribe-link",
            "bulk-set-read", "bulk-set-flag", "bulk-move", "bulk-delete",
        ],
    )
    mail_messages.add_argument("--mailbox-name")
    mail_messages.add_argument("--account-name")
    mail_messages.add_argument("--message-id", type=int)
    mail_messages.add_argument("--query")
    mail_messages.add_argument("--limit", type=int)
    mail_messages.add_argument("--unread-only", action="store_true")
    mail_messages.add_argument("--flagged-only", action="store_true")
    mail_messages.add_argument("--include-source", action="store_true")
    mail_messages.add_argument("--target-mailbox")
    mail_messages.add_argument("--target-account")
    mail_messages.add_argument("--read", choices=["true", "false"])
    mail_messages.add_argument("--flagged", choices=["true", "false"])
    mail_messages.add_argument("--since")
    mail_messages.add_argument("--from-address")
    mail_messages.add_argument("--to-address")
    mail_messages.add_argument("--subject-contains")
    mail_messages.add_argument("--attachment-index", type=int)
    mail_messages.add_argument("--save-to")
    mail_messages.add_argument("--return-inline", action="store_true")
    mail_messages.add_argument("--message-ids", action="append", type=int,
                               help="Repeat for each id: --message-ids 1 --message-ids 2 (bulk-* actions, max 50).")
    mail_messages.add_argument("--dry-run", action="store_true",
                               help="Bulk actions only: report what would change without doing it.")

    mail_compose = sub.add_parser("mail-compose", help="Create, reply, or forward an Apple Mail message.")
    _add_action(mail_compose, ["create", "reply", "forward"])
    mail_compose.add_argument("--message-id", type=int)
    mail_compose.add_argument("--to", action="append")
    mail_compose.add_argument("--cc", action="append")
    mail_compose.add_argument("--bcc", action="append")
    mail_compose.add_argument("--subject")
    mail_compose.add_argument("--body")
    mail_compose.add_argument("--reply-all", action="store_true")
    mail_compose.add_argument("--open-in-mail", action="store_true")
    mail_compose.add_argument("--send-now", action="store_true")

    calendar_calendars = sub.add_parser("calendar-calendars", help="List macOS Calendar calendars.")
    _add_action(calendar_calendars, ["list"])
    calendar_calendars.add_argument("--include-counts", action="store_true")

    calendar_events = sub.add_parser("calendar-events", help="Manage Calendar events.")
    _add_action(calendar_events, ["list", "get", "create", "update", "delete", "open"])
    calendar_events.add_argument("--event-id")
    calendar_events.add_argument("--calendar-name")
    calendar_events.add_argument("--summary")
    calendar_events.add_argument("--location")
    calendar_events.add_argument("--notes")
    calendar_events.add_argument("--start-date")
    calendar_events.add_argument("--end-date")
    calendar_events.add_argument("--date-from")
    calendar_events.add_argument("--date-to")
    calendar_events.add_argument("--search")
    calendar_events.add_argument("--limit", type=int)
    calendar_events.add_argument("--all-day", action="store_true")
    calendar_events.add_argument("--url")
    calendar_events.add_argument("--recurrence", help="RFC 5545 RRULE string, e.g. FREQ=WEEKLY;BYDAY=MO")
    calendar_events.add_argument("--recurrence-rule",
                                 help="EventKit-only: parsed RRULE applied as a structured rule.")
    calendar_events.add_argument("--timezone", help="EventKit-only: IANA timezone name (e.g. Asia/Tokyo).")
    calendar_events.add_argument("--alarm", action="append", type=float, dest="alarms",
                                 help="EventKit-only: alarm offset in seconds (negative = before start). Repeatable.")
    calendar_events.add_argument("--source",
                                 help="EventKit-only: filter calendars by source title (icloud/google/exchange/local).")

    reminders_lists = sub.add_parser("reminders-lists", help="Manage Reminders lists.")
    _add_action(reminders_lists, ["list", "create", "update", "delete"])
    reminders_lists.add_argument("--list-id")
    reminders_lists.add_argument("--name")
    reminders_lists.add_argument("--include-counts", action="store_true")

    reminders_tasks = sub.add_parser("reminders-tasks", help="Manage Reminders tasks.")
    _add_action(
        reminders_tasks,
        ["list", "get", "create", "update", "delete", "complete", "incomplete"],
    )
    reminders_tasks.add_argument("--reminder-id")
    reminders_tasks.add_argument("--title")
    reminders_tasks.add_argument("--list-name")
    reminders_tasks.add_argument("--notes")
    reminders_tasks.add_argument("--due-date")
    reminders_tasks.add_argument("--completed", choices=["true", "false"])
    reminders_tasks.add_argument("--search")
    reminders_tasks.add_argument("--show-completed", action="store_true")
    reminders_tasks.add_argument("--limit", type=int)
    reminders_tasks.add_argument("--priority", type=int, choices=range(0, 10), help="0=none, 1=high, 5=medium, 9=low")
    reminders_tasks.add_argument("--flagged", choices=["true", "false"])
    reminders_tasks.add_argument("--alarm", action="append", type=float, dest="alarms",
                                 help="EventKit-only: alarm offset in seconds. Repeatable.")
    reminders_tasks.add_argument("--source",
                                 help="EventKit-only: filter lists by source title (icloud/google/exchange/local).")
    reminders_tasks.add_argument("--geofence-lat", type=float)
    reminders_tasks.add_argument("--geofence-lon", type=float)
    reminders_tasks.add_argument("--geofence-radius", type=float, default=100.0)
    reminders_tasks.add_argument("--geofence-proximity", choices=["enter", "leave"], default="enter")
    reminders_tasks.add_argument("--geofence-title", default="")

    mail_drafts = sub.add_parser("mail-drafts", help="Manage saved Apple Mail drafts.")
    _add_action(mail_drafts, ["list", "get", "update", "send", "delete"])
    mail_drafts.add_argument("--message-id", type=int)
    mail_drafts.add_argument("--account-name")
    mail_drafts.add_argument("--mailbox-name")
    mail_drafts.add_argument("--subject")
    mail_drafts.add_argument("--body")
    mail_drafts.add_argument("--limit", type=int)

    permissions = sub.add_parser("mail-permissions-check", help="Probe Mail/Calendar/Reminders permissions.")
    permissions.add_argument("action", nargs="?", default="check", choices=["check"])

    return parser


def args_to_payload(namespace: argparse.Namespace) -> tuple:
    """Map argparse Namespace to (tool_name, arguments dict)."""
    tool = namespace.tool
    args: dict = {"action": namespace.action}

    if tool == "mail-accounts":
        return "mail_accounts", args
    if tool == "mail-mailboxes":
        _maybe(args, "account_name", namespace.account_name)
        if namespace.include_counts:
            args["include_counts"] = True
        return "mail_mailboxes", args
    if tool == "mail-messages":
        _maybe(args, "mailbox_name", namespace.mailbox_name)
        _maybe(args, "account_name", namespace.account_name)
        _maybe(args, "message_id", namespace.message_id)
        _maybe(args, "query", namespace.query)
        _maybe(args, "limit", namespace.limit)
        if namespace.unread_only:
            args["unread_only"] = True
        if namespace.flagged_only:
            args["flagged_only"] = True
        if namespace.include_source:
            args["include_source"] = True
        _maybe(args, "target_mailbox", namespace.target_mailbox)
        _maybe(args, "target_account", namespace.target_account)
        if namespace.read is not None:
            args["read"] = namespace.read == "true"
        if namespace.flagged is not None:
            args["flagged"] = namespace.flagged == "true"
        _maybe(args, "since", namespace.since)
        _maybe(args, "from_address", namespace.from_address)
        _maybe(args, "to_address", namespace.to_address)
        _maybe(args, "subject_contains", namespace.subject_contains)
        _maybe(args, "attachment_index", namespace.attachment_index)
        _maybe(args, "save_to", namespace.save_to)
        if namespace.return_inline:
            args["return_inline"] = True
        if namespace.message_ids:
            args["message_ids"] = list(namespace.message_ids)
        if namespace.dry_run:
            args["dry_run"] = True
        return "mail_messages", args
    if tool == "mail-compose":
        _maybe(args, "message_id", namespace.message_id)
        _maybe(args, "to", _str_list(namespace.to))
        _maybe(args, "cc", _str_list(namespace.cc))
        _maybe(args, "bcc", _str_list(namespace.bcc))
        _maybe(args, "subject", namespace.subject)
        _maybe(args, "body", namespace.body)
        if namespace.reply_all:
            args["reply_all"] = True
        if namespace.open_in_mail:
            args["open_in_mail"] = True
        if namespace.send_now:
            args["send_now"] = True
        return "mail_compose", args
    if tool == "calendar-calendars":
        if namespace.include_counts:
            args["include_counts"] = True
        return "calendar_calendars", args
    if tool == "calendar-events":
        _maybe(args, "event_id", namespace.event_id)
        _maybe(args, "calendar_name", namespace.calendar_name)
        _maybe(args, "summary", namespace.summary)
        _maybe(args, "location", namespace.location)
        _maybe(args, "notes", namespace.notes)
        _maybe(args, "start_date", namespace.start_date)
        _maybe(args, "end_date", namespace.end_date)
        _maybe(args, "date_from", namespace.date_from)
        _maybe(args, "date_to", namespace.date_to)
        _maybe(args, "search", namespace.search)
        _maybe(args, "limit", namespace.limit)
        if namespace.all_day:
            args["all_day"] = True
        _maybe(args, "url", namespace.url)
        _maybe(args, "recurrence", namespace.recurrence)
        _maybe(args, "recurrence_rule", namespace.recurrence_rule)
        _maybe(args, "timezone", namespace.timezone)
        if namespace.alarms:
            args["alarms"] = list(namespace.alarms)
        _maybe(args, "source", namespace.source)
        return "calendar_events", args
    if tool == "reminders-lists":
        _maybe(args, "list_id", namespace.list_id)
        _maybe(args, "name", namespace.name)
        if namespace.include_counts:
            args["include_counts"] = True
        return "reminders_lists", args
    if tool == "reminders-tasks":
        _maybe(args, "reminder_id", namespace.reminder_id)
        _maybe(args, "title", namespace.title)
        _maybe(args, "list_name", namespace.list_name)
        _maybe(args, "notes", namespace.notes)
        _maybe(args, "due_date", namespace.due_date)
        if namespace.completed is not None:
            args["completed"] = namespace.completed == "true"
        _maybe(args, "search", namespace.search)
        if namespace.show_completed:
            args["show_completed"] = True
        _maybe(args, "limit", namespace.limit)
        _maybe(args, "priority", namespace.priority)
        if namespace.flagged is not None:
            args["flagged"] = namespace.flagged == "true"
        if namespace.alarms:
            args["alarms"] = list(namespace.alarms)
        _maybe(args, "source", namespace.source)
        if namespace.geofence_lat is not None and namespace.geofence_lon is not None:
            args["geofence"] = {
                "lat": namespace.geofence_lat,
                "lon": namespace.geofence_lon,
                "radius_meters": namespace.geofence_radius,
                "proximity": namespace.geofence_proximity,
                "title": namespace.geofence_title,
            }
        return "reminders_tasks", args
    if tool == "mail-drafts":
        _maybe(args, "message_id", namespace.message_id)
        _maybe(args, "account_name", namespace.account_name)
        _maybe(args, "mailbox_name", namespace.mailbox_name)
        _maybe(args, "subject", namespace.subject)
        _maybe(args, "body", namespace.body)
        _maybe(args, "limit", namespace.limit)
        return "mail_drafts", args
    if tool == "mail-permissions-check":
        return "mail_permissions_check", args
    raise SystemExit(f"Unknown tool: {tool}")


def main(argv=None) -> int:
    parser = build_parser()
    namespace = parser.parse_args(argv)
    tool_name, arguments = args_to_payload(namespace)
    service = AppleProductivityService(timeout_seconds=namespace.timeout)
    try:
        result = service.dispatch(tool_name, arguments)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if namespace.raw:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
