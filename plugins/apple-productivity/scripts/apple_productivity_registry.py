#!/usr/bin/env python3
"""Shared command/action registry for the Apple Productivity transports.

The registry is intentionally declarative: it describes the low-level service
tools once, then the CLI and MCP server derive their help text, argparse flags,
and JSON schemas from the same source. Validation and behavior still live in
``shared_validation`` and ``AppleProductivityService``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class ArgumentSpec:
    name: str
    kind: str = "string"
    help: str = ""
    multiple: bool = False
    choices: Optional[tuple[Any, ...]] = None
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    default: Any = None

    @property
    def flag(self) -> str:
        return "--" + self.name.replace("_", "-")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    cli_name: str
    description: str
    actions: tuple[str, ...]
    arguments: tuple[ArgumentSpec, ...] = field(default_factory=tuple)
    action_required: bool = True

    def action_schema(self) -> dict:
        schema = {"type": "string", "enum": list(self.actions)}
        return schema


GLOBAL_ARGUMENTS = {
    "account_name": ArgumentSpec("account_name", help="Apple Mail account name."),
    "mailbox_name": ArgumentSpec("mailbox_name", help="Apple Mail mailbox name."),
    "message_id": ArgumentSpec("message_id", "integer", help="Apple Mail message id."),
    "message_ids": ArgumentSpec(
        "message_ids",
        "integer",
        help="Repeat for each id. Bulk actions accept at most 50 ids.",
        multiple=True,
    ),
    "limit": ArgumentSpec("limit", "integer", help="Maximum number of items to return.", minimum=1, maximum=100),
    "include_counts": ArgumentSpec("include_counts", "boolean", help="Include item counts."),
    "query": ArgumentSpec("query", help="Search query."),
    "unread_only": ArgumentSpec("unread_only", "boolean", help="Only include unread messages."),
    "flagged_only": ArgumentSpec("flagged_only", "boolean", help="Only include flagged messages."),
    "include_source": ArgumentSpec("include_source", "boolean", help="Include raw message source when supported."),
    "target_mailbox": ArgumentSpec("target_mailbox", help="Target mailbox name."),
    "target_account": ArgumentSpec("target_account", help="Target account name."),
    "read": ArgumentSpec("read", "bool_string", choices=("true", "false"), help="Read state."),
    "flagged": ArgumentSpec("flagged", "bool_string", choices=("true", "false"), help="Flagged state."),
    "since": ArgumentSpec("since", help="Lower date bound."),
    "from_address": ArgumentSpec("from_address", help="Sender address filter."),
    "to_address": ArgumentSpec("to_address", help="Recipient address filter."),
    "subject_contains": ArgumentSpec("subject_contains", help="Subject substring filter."),
    "attachment_index": ArgumentSpec("attachment_index", "integer", help="Attachment index.", minimum=0),
    "save_to": ArgumentSpec("save_to", help="Absolute path where an attachment should be saved."),
    "return_inline": ArgumentSpec("return_inline", "boolean", help="Return attachment bytes inline as base64."),
    "dry_run": ArgumentSpec("dry_run", "boolean", help="Report what would change without mutating state."),
    "with_links": ArgumentSpec(
        "with_links",
        "boolean",
        help="Fetch List-Unsubscribe links for newsletter candidates.",
    ),
    "to": ArgumentSpec("to", help="Recipient address. Repeatable.", multiple=True),
    "cc": ArgumentSpec("cc", help="CC address. Repeatable.", multiple=True),
    "bcc": ArgumentSpec("bcc", help="BCC address. Repeatable.", multiple=True),
    "subject": ArgumentSpec("subject", help="Mail subject or event summary."),
    "body": ArgumentSpec("body", help="Mail body."),
    "reply_all": ArgumentSpec("reply_all", "boolean", help="Reply to all recipients."),
    "open_in_mail": ArgumentSpec("open_in_mail", "boolean", help="Open the message in Mail.app."),
    "send_now": ArgumentSpec("send_now", "boolean", help="Send immediately instead of saving/opening a draft."),
    "event_id": ArgumentSpec("event_id", help="Calendar event id."),
    "calendar_name": ArgumentSpec("calendar_name", help="Calendar name."),
    "summary": ArgumentSpec("summary", help="Event summary."),
    "location": ArgumentSpec("location", help="Event location."),
    "notes": ArgumentSpec("notes", help="Notes."),
    "start_date": ArgumentSpec("start_date", help="Start date/time."),
    "end_date": ArgumentSpec("end_date", help="End date/time."),
    "date_from": ArgumentSpec("date_from", help="Start of date range."),
    "date_to": ArgumentSpec("date_to", help="End of date range."),
    "search": ArgumentSpec("search", help="Search string."),
    "all_day": ArgumentSpec("all_day", "boolean", help="Create/update as all-day event."),
    "url": ArgumentSpec("url", help="Event URL."),
    "recurrence": ArgumentSpec("recurrence", help="RFC 5545 RRULE string."),
    "recurrence_rule": ArgumentSpec("recurrence_rule", help="EventKit structured RRULE string."),
    "timezone": ArgumentSpec("timezone", help="IANA timezone name."),
    "alarms": ArgumentSpec("alarms", "number", help="Alarm offset in seconds. Repeatable.", multiple=True),
    "source": ArgumentSpec("source", help="Calendar/reminder source filter."),
    "list_id": ArgumentSpec("list_id", help="Reminders list id."),
    "name": ArgumentSpec("name", help="Reminders list name."),
    "reminder_id": ArgumentSpec("reminder_id", help="Reminder id."),
    "title": ArgumentSpec("title", help="Reminder title."),
    "list_name": ArgumentSpec("list_name", help="Reminders list name."),
    "due_date": ArgumentSpec("due_date", help="Reminder due date."),
    "completed": ArgumentSpec("completed", "bool_string", choices=("true", "false"), help="Completion state."),
    "show_completed": ArgumentSpec("show_completed", "boolean", help="Include completed reminders."),
    "priority": ArgumentSpec("priority", "integer", help="0=none, 1=high, 5=medium, 9=low.", minimum=0, maximum=9),
    "geofence_lat": ArgumentSpec("geofence_lat", "number", help="Geofence latitude."),
    "geofence_lon": ArgumentSpec("geofence_lon", "number", help="Geofence longitude."),
    "geofence_radius": ArgumentSpec("geofence_radius", "number", help="Geofence radius in meters.", default=100.0),
    "geofence_proximity": ArgumentSpec("geofence_proximity", choices=("enter", "leave"), help="Geofence trigger.", default="enter"),
    "geofence_title": ArgumentSpec("geofence_title", help="Geofence display title.", default=""),
}


TOOL_SPECS = (
    ToolSpec("mail_accounts", "mail-accounts", "List Apple Mail accounts.", ("list",), action_required=False),
    ToolSpec(
        "mail_mailboxes",
        "mail-mailboxes",
        "List Apple Mail mailboxes.",
        ("list",),
        (GLOBAL_ARGUMENTS["account_name"], GLOBAL_ARGUMENTS["include_counts"]),
        action_required=False,
    ),
    ToolSpec(
        "mail_messages",
        "mail-messages",
        "Read or modify Apple Mail messages.",
        (
            "list", "get", "search", "move", "delete", "set-read", "set-flag", "open",
            "get-attachment", "get-thread", "get-unsubscribe-link",
            "bulk-set-read", "bulk-set-flag", "bulk-move", "bulk-delete",
        ),
        tuple(GLOBAL_ARGUMENTS[name] for name in (
            "mailbox_name", "account_name", "message_id", "query", "limit", "unread_only",
            "flagged_only", "include_source", "target_mailbox", "target_account", "read",
            "flagged", "since", "from_address", "to_address", "subject_contains",
            "attachment_index", "save_to", "return_inline", "message_ids", "dry_run",
        )),
    ),
    ToolSpec(
        "mail_compose",
        "mail-compose",
        "Create, reply to, or forward an Apple Mail message.",
        ("create", "reply", "forward"),
        tuple(GLOBAL_ARGUMENTS[name] for name in (
            "message_id", "to", "cc", "bcc", "subject", "body", "reply_all", "open_in_mail", "send_now",
            "dry_run",
        )),
    ),
    ToolSpec(
        "calendar_calendars",
        "calendar-calendars",
        "List macOS Calendar calendars.",
        ("list",),
        (GLOBAL_ARGUMENTS["include_counts"],),
        action_required=False,
    ),
    ToolSpec(
        "calendar_events",
        "calendar-events",
        "List, inspect, create, update, delete, and open Calendar events.",
        ("list", "get", "create", "update", "delete", "open"),
        tuple(GLOBAL_ARGUMENTS[name] for name in (
            "event_id", "calendar_name", "summary", "location", "notes", "start_date",
            "end_date", "date_from", "date_to", "search", "limit", "all_day", "url",
            "recurrence", "recurrence_rule", "timezone", "alarms", "source", "dry_run",
        )),
    ),
    ToolSpec(
        "reminders_lists",
        "reminders-lists",
        "List and manage macOS Reminders lists.",
        ("list", "create", "update", "delete"),
        tuple(GLOBAL_ARGUMENTS[name] for name in ("list_id", "name", "include_counts", "dry_run")),
    ),
    ToolSpec(
        "reminders_tasks",
        "reminders-tasks",
        "List and manage macOS Reminders tasks.",
        ("list", "get", "create", "update", "delete", "complete", "incomplete"),
        tuple(GLOBAL_ARGUMENTS[name] for name in (
            "reminder_id", "title", "list_name", "notes", "due_date", "completed",
            "search", "show_completed", "limit", "priority", "flagged", "alarms",
            "source", "geofence_lat", "geofence_lon", "geofence_radius",
            "geofence_proximity", "geofence_title", "dry_run",
        )),
    ),
    ToolSpec(
        "mail_drafts",
        "mail-drafts",
        "List, edit, send, or delete saved Apple Mail drafts.",
        ("list", "get", "update", "send", "delete"),
        tuple(GLOBAL_ARGUMENTS[name] for name in (
            "message_id", "account_name", "mailbox_name", "subject", "body", "limit", "dry_run",
        )),
    ),
    ToolSpec(
        "mail_analyze",
        "mail-analyze",
        "Summary-first Mail triage and newsletter/unsubscribe analysis.",
        ("triage", "newsletters"),
        tuple(GLOBAL_ARGUMENTS[name] for name in (
            "mailbox_name", "account_name", "query", "since", "limit", "unread_only",
            "flagged_only", "with_links",
        )),
    ),
    ToolSpec(
        "mail_permissions_check",
        "mail-permissions-check",
        "Probe Mail, Calendar, Reminders, Automation, and Full Disk Access permissions.",
        ("check",),
        action_required=False,
    ),
)


TOOL_BY_NAME = {spec.name: spec for spec in TOOL_SPECS}
TOOL_BY_CLI_NAME = {spec.cli_name: spec for spec in TOOL_SPECS}
KNOWN_TOOLS = set(TOOL_BY_NAME)


def mcp_tools() -> list[dict]:
    return [mcp_tool_schema(spec) for spec in TOOL_SPECS]


def mcp_tool_schema(spec: ToolSpec) -> dict:
    properties = {"action": spec.action_schema()}
    for arg in spec.arguments:
        if arg.name.startswith("geofence_"):
            continue
        properties[arg.name] = json_schema_for_argument(arg)
    if spec.name == "reminders_tasks":
        properties["geofence"] = {
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
        }
    schema = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if spec.action_required:
        schema["required"] = ["action"]
    return {
        "name": spec.name,
        "description": spec.description,
        "inputSchema": schema,
    }


def json_schema_for_argument(arg: ArgumentSpec) -> dict:
    if arg.multiple:
        item = json_schema_for_argument(ArgumentSpec(arg.name, arg.kind, choices=arg.choices))
        schema = {"type": "array", "items": item}
        if arg.name == "message_ids":
            schema["maxItems"] = 50
        return schema
    if arg.kind in {"integer", "number"}:
        schema = {"type": arg.kind}
        if arg.minimum is not None:
            schema["minimum"] = arg.minimum
        if arg.maximum is not None:
            schema["maximum"] = arg.maximum
        return schema
    if arg.kind == "boolean" or arg.kind == "bool_string":
        return {"type": "boolean"}
    schema = {"type": "string"}
    if arg.choices:
        schema["enum"] = list(arg.choices)
    return schema
