#!/usr/bin/env python3

# Date string contract (enforced here; mirrored in apple_productivity_jxa.js):
#   YYYY-MM-DD                                e.g. "2026-05-10"
#   YYYY-MM-DDTHH:MM:SS                       e.g. "2026-05-10T14:00:00"
#   YYYY-MM-DDTHH:MM:SS(Z|+HH:MM|-HH:MM)      e.g. "2026-05-10T14:00:00Z"
# Milliseconds and other ISO variants are rejected so Python and JXA agree.

from __future__ import annotations

from datetime import datetime
from email.utils import parseaddr
import re

from apple_productivity_registry import TOOL_BY_NAME


SAFE_TEXT_PATTERN = re.compile(r"^[^\x00-\x08\x0B\x0C\x0E-\x1F\x7F]*$")
DATE_ONLY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATETIME_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})?$"
)
EMAIL_LOCAL_PATTERN = re.compile(r"^[^\s@]+$")
EMAIL_DOMAIN_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$")
SEARCH_EMAIL_TOKEN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SEARCH_QUERY_FILLER = frozenset(
    {
        "all",
        "any",
        "email",
        "emails",
        "find",
        "for",
        "from",
        "in",
        "mail",
        "mailbox",
        "mailboxes",
        "message",
        "messages",
        "my",
        "search",
        "show",
        "the",
    }
)


MAIL_MESSAGE_ACTIONS = set(TOOL_BY_NAME["mail_messages"].actions)
MAIL_COMPOSE_ACTIONS = set(TOOL_BY_NAME["mail_compose"].actions)
MAIL_DRAFT_ACTIONS = set(TOOL_BY_NAME["mail_drafts"].actions)
MAIL_ANALYZE_ACTIONS = set(TOOL_BY_NAME["mail_analyze"].actions)
CALENDAR_EVENT_ACTIONS = set(TOOL_BY_NAME["calendar_events"].actions)
REMINDER_LIST_ACTIONS = set(TOOL_BY_NAME["reminders_lists"].actions)
REMINDER_TASK_ACTIONS = set(TOOL_BY_NAME["reminders_tasks"].actions)
BULK_LIMIT = 50


def refine_mail_search_arguments(arguments: dict) -> dict:
    """Promote sender emails embedded in natural-language search queries."""
    if arguments.get("action") != "search":
        return arguments
    refined = dict(arguments)
    query = refined.get("query")
    if refined.get("from_address") or not isinstance(query, str):
        return refined
    emails = SEARCH_EMAIL_TOKEN.findall(query)
    if not emails:
        return refined
    refined["from_address"] = emails[0].lower()
    if _sender_query_is_email_only(query, emails[0]):
        refined.pop("query", None)
    return refined


def _sender_query_is_email_only(query: str, email: str) -> bool:
    lowered = query.strip().lower()
    if lowered == email.lower():
        return True
    residual = SEARCH_EMAIL_TOKEN.sub(" ", lowered)
    residual = re.sub(r"[^a-z ]+", " ", residual)
    tokens = [token for token in residual.split() if token and token not in SEARCH_QUERY_FILLER]
    return not tokens


def validate_tool_arguments(tool_name: str, arguments: dict) -> None:
    validate_registered_contract(tool_name, arguments)
    for key, value in arguments.items():
        validate_value(key, value)

    if tool_name == "mail_accounts":
        validate_action(arguments, set(TOOL_BY_NAME[tool_name].actions), required=False)
        return
    if tool_name == "mail_mailboxes":
        validate_action(arguments, set(TOOL_BY_NAME[tool_name].actions), required=False)
        validate_string(arguments, "account_name")
        validate_boolean(arguments, "include_counts")
        return
    if tool_name == "mail_messages":
        validate_mail_messages(arguments)
        return
    if tool_name == "mail_compose":
        validate_mail_compose(arguments)
        return
    if tool_name == "mail_drafts":
        validate_mail_drafts(arguments)
        return
    if tool_name == "mail_analyze":
        validate_mail_analyze(arguments)
        return
    if tool_name == "mail_permissions_check":
        validate_action(arguments, set(TOOL_BY_NAME[tool_name].actions), required=False)
        return
    if tool_name == "calendar_calendars":
        validate_action(arguments, set(TOOL_BY_NAME[tool_name].actions), required=False)
        validate_boolean(arguments, "include_counts")
        return
    if tool_name == "calendar_events":
        validate_calendar_events(arguments)
        return
    if tool_name == "reminders_lists":
        validate_reminder_lists(arguments)
        return
    if tool_name == "reminders_tasks":
        validate_reminder_tasks(arguments)
        return
    raise RuntimeError(f"Unknown tool: {tool_name}")


def validate_registered_contract(tool_name: str, arguments: dict) -> None:
    spec = TOOL_BY_NAME.get(tool_name)
    if spec is None:
        raise RuntimeError(f"Unknown tool: {tool_name}")

    validate_action(arguments, set(spec.actions), required=spec.action_required)
    allowed_fields = {"action", *(arg.name for arg in spec.arguments)}
    if tool_name == "reminders_tasks":
        allowed_fields.add("geofence")
    extra_fields = sorted(set(arguments) - allowed_fields)
    if extra_fields:
        raise RuntimeError(f"{tool_name} does not accept argument(s): {', '.join(extra_fields)}")
    validate_boolean(arguments, "dry_run")

    for arg in spec.arguments:
        if arg.name not in arguments:
            continue
        validate_registered_bounds(arg.name, arguments[arg.name], arg.minimum, arg.maximum)


def validate_registered_bounds(field: str, value, minimum, maximum) -> None:
    if minimum is None and maximum is None:
        return
    values = value if isinstance(value, list) else [value]
    for item in values:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            continue
        if minimum is not None and item < minimum:
            raise RuntimeError(f"{field} must be at least {minimum}.")
        if maximum is not None and item > maximum:
            raise RuntimeError(f"{field} must be at most {maximum}.")


def validate_mail_messages(arguments: dict) -> None:
    action = validate_action(arguments, MAIL_MESSAGE_ACTIONS)
    if action == "list":
        validate_string(arguments, "mailbox_name", required=True)
        validate_string(arguments, "account_name")
        validate_integer(arguments, "limit")
        validate_integer(arguments, "offset")
        validate_boolean(arguments, "unread_only")
        validate_boolean(arguments, "flagged_only")
        return
    if action == "get":
        validate_integer(arguments, "message_id", required=True)
        validate_string(arguments, "mailbox_name")
        validate_string(arguments, "account_name")
        validate_boolean(arguments, "include_source")
        return
    if action == "search":
        validate_string(arguments, "query", max_length=500)
        validate_string(arguments, "account_name")
        validate_string(arguments, "mailbox_name")
        validate_string(arguments, "from_address", max_length=500)
        validate_string(arguments, "to_address", max_length=500)
        validate_string(arguments, "subject_contains", max_length=500)
        validate_date(arguments, "since")
        validate_integer(arguments, "limit")
        validate_integer(arguments, "offset")
        validate_boolean(arguments, "unread_only")
        validate_boolean(arguments, "flagged_only")
        if not any(
            arguments.get(name)
            for name in (
                "query",
                "from_address",
                "to_address",
                "subject_contains",
                "since",
                "unread_only",
                "flagged_only",
                "mailbox_name",
                "account_name",
            )
        ):
            raise RuntimeError("search requires at least one query or filter.")
        return
    if action == "move":
        validate_integer(arguments, "message_id", required=True)
        validate_string(arguments, "target_mailbox", required=True)
        validate_string(arguments, "target_account")
        validate_string(arguments, "mailbox_name")
        validate_string(arguments, "account_name")
        return
    if action in {"delete", "open"}:
        validate_integer(arguments, "message_id", required=True)
        validate_string(arguments, "mailbox_name")
        validate_string(arguments, "account_name")
        return
    if action == "set-read":
        validate_integer(arguments, "message_id", required=True)
        validate_boolean(arguments, "read", required=True)
        validate_string(arguments, "mailbox_name")
        validate_string(arguments, "account_name")
        return
    if action == "set-flag":
        validate_integer(arguments, "message_id", required=True)
        validate_boolean(arguments, "flagged", required=True)
        validate_string(arguments, "mailbox_name")
        validate_string(arguments, "account_name")
        return
    if action == "get-attachment":
        validate_integer(arguments, "message_id", required=True)
        validate_integer(arguments, "attachment_index", required=True)
        validate_string(arguments, "mailbox_name")
        validate_string(arguments, "account_name")
        validate_string(arguments, "save_to", max_length=4096)
        validate_boolean(arguments, "return_inline")
        save_to = arguments.get("save_to")
        return_inline = arguments.get("return_inline")
        if not save_to and not return_inline:
            raise RuntimeError("get-attachment requires either save_to or return_inline.")
        if save_to and return_inline:
            raise RuntimeError("get-attachment accepts only one of save_to or return_inline.")
        if save_to and not save_to.startswith("/"):
            raise RuntimeError("save_to must be an absolute path.")
        return
    if action == "get-thread":
        validate_integer(arguments, "message_id", required=True)
        validate_string(arguments, "mailbox_name")
        validate_string(arguments, "account_name")
        validate_integer(arguments, "limit")
        return
    if action == "get-unsubscribe-link":
        validate_integer(arguments, "message_id", required=True)
        validate_string(arguments, "mailbox_name")
        validate_string(arguments, "account_name")
        return
    if action in {"bulk-set-read", "bulk-set-flag", "bulk-move", "bulk-delete"}:
        validate_message_id_list(arguments, "message_ids", required=True)
        validate_string(arguments, "mailbox_name")
        validate_string(arguments, "account_name")
        validate_boolean(arguments, "dry_run")
        if action == "bulk-set-read":
            validate_boolean(arguments, "read", required=True)
        elif action == "bulk-set-flag":
            validate_boolean(arguments, "flagged", required=True)
        elif action == "bulk-move":
            validate_string(arguments, "target_mailbox", required=True)
            validate_string(arguments, "target_account")
        return


def validate_mail_drafts(arguments: dict) -> None:
    action = validate_action(arguments, MAIL_DRAFT_ACTIONS)
    if action == "list":
        validate_string(arguments, "account_name")
        validate_integer(arguments, "limit")
        return
    # All other actions need an integer message_id (the draft's id).
    validate_integer(arguments, "message_id", required=True)
    validate_string(arguments, "account_name")
    validate_string(arguments, "mailbox_name")
    if action == "update":
        validate_string(arguments, "subject", max_length=5000)
        validate_string(arguments, "body", max_length=50000)


def validate_mail_analyze(arguments: dict) -> None:
    action = validate_action(arguments, MAIL_ANALYZE_ACTIONS)
    validate_string(arguments, "mailbox_name")
    validate_string(arguments, "account_name")
    validate_string(arguments, "query", max_length=500)
    validate_date(arguments, "since")
    validate_integer(arguments, "limit")
    validate_boolean(arguments, "unread_only")
    validate_boolean(arguments, "flagged_only")
    validate_boolean(arguments, "with_links")
    if action == "newsletters" and arguments.get("with_links") and arguments.get("limit", 10) > 25:
        raise RuntimeError("with_links accepts limit at most 25 per call.")


def validate_message_id_list(arguments: dict, field: str, required: bool = False) -> None:
    values = arguments.get(field)
    if values is None:
        if required:
            raise RuntimeError(f"{field} is required.")
        return
    if not isinstance(values, list) or not values:
        raise RuntimeError(f"{field} must be a non-empty array of integer message ids.")
    if len(values) > BULK_LIMIT:
        raise RuntimeError(f"{field} accepts at most {BULK_LIMIT} ids per call.")
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int):
            raise RuntimeError(f"{field} must contain only integers.")


def validate_mail_compose(arguments: dict) -> None:
    action = validate_action(arguments, MAIL_COMPOSE_ACTIONS)
    validate_email_list(arguments, "to")
    validate_email_list(arguments, "cc")
    validate_email_list(arguments, "bcc")
    validate_string(arguments, "subject", max_length=5000)
    validate_string(arguments, "body", max_length=50000)
    validate_boolean(arguments, "open_in_mail")
    validate_boolean(arguments, "send_now")
    if action in {"reply", "forward"}:
        validate_integer(arguments, "message_id", required=True)
    if action == "reply":
        validate_boolean(arguments, "reply_all")
    if action == "create":
        recipient_count = (
            len(arguments.get("to") or [])
            + len(arguments.get("cc") or [])
            + len(arguments.get("bcc") or [])
        )
        if recipient_count == 0:
            raise RuntimeError("create requires at least one recipient in to, cc, or bcc.")


def validate_calendar_events(arguments: dict) -> None:
    action = validate_action(arguments, CALENDAR_EVENT_ACTIONS)
    if action == "list":
        validate_string(arguments, "calendar_name")
        validate_string(arguments, "search", max_length=500)
        validate_date(arguments, "date_from")
        validate_date(arguments, "date_to")
        validate_integer(arguments, "limit")
        return
    if action in {"get", "delete", "open"}:
        validate_string(arguments, "event_id", required=True)
        return
    if action == "create":
        validate_string(arguments, "summary", required=True, max_length=5000)
        validate_date(arguments, "start_date", required=True)
        validate_date(arguments, "end_date", required=True)
        validate_string(arguments, "calendar_name")
        validate_string(arguments, "location", max_length=5000)
        validate_string(arguments, "notes", max_length=50000)
        validate_boolean(arguments, "all_day")
        validate_string(arguments, "url", max_length=2048)
        validate_string(arguments, "recurrence", max_length=2000)
        validate_string(arguments, "recurrence_rule", max_length=2000)
        validate_string(arguments, "timezone", max_length=200)
        validate_string(arguments, "source", max_length=200)
        validate_alarm_offsets(arguments)
        return
    if action == "update":
        validate_string(arguments, "event_id", required=True)
        validate_string(arguments, "summary", max_length=5000)
        validate_date(arguments, "start_date")
        validate_date(arguments, "end_date")
        validate_string(arguments, "calendar_name")
        validate_string(arguments, "location", max_length=5000)
        validate_string(arguments, "notes", max_length=50000)
        validate_boolean(arguments, "all_day")
        validate_string(arguments, "url", max_length=2048)
        validate_string(arguments, "recurrence", max_length=2000)
        validate_string(arguments, "recurrence_rule", max_length=2000)
        validate_string(arguments, "timezone", max_length=200)
        validate_string(arguments, "source", max_length=200)
        validate_alarm_offsets(arguments)


def validate_reminder_lists(arguments: dict) -> None:
    action = validate_action(arguments, REMINDER_LIST_ACTIONS)
    validate_boolean(arguments, "include_counts")
    if action == "create":
        validate_string(arguments, "name", required=True, max_length=500)
    elif action == "update":
        validate_string(arguments, "list_id", required=True)
        validate_string(arguments, "name", required=True, max_length=500)
    elif action == "delete":
        validate_string(arguments, "list_id", required=True)


def validate_reminder_tasks(arguments: dict) -> None:
    action = validate_action(arguments, REMINDER_TASK_ACTIONS)
    if action == "list":
        validate_string(arguments, "list_name")
        validate_string(arguments, "search", max_length=500)
        validate_boolean(arguments, "show_completed")
        validate_integer(arguments, "limit")
        return
    if action in {"get", "delete", "complete", "incomplete"}:
        validate_string(arguments, "reminder_id", required=True)
        return
    if action == "create":
        validate_string(arguments, "title", required=True, max_length=5000)
        validate_string(arguments, "list_name")
        validate_string(arguments, "notes", max_length=50000)
        validate_date(arguments, "due_date")
        validate_priority(arguments)
        validate_boolean(arguments, "flagged")
        validate_alarm_offsets(arguments)
        validate_geofence(arguments)
        validate_string(arguments, "source", max_length=200)
        return
    if action == "update":
        validate_string(arguments, "reminder_id", required=True)
        validate_string(arguments, "title", max_length=5000)
        validate_string(arguments, "list_name")
        validate_string(arguments, "notes", max_length=50000)
        validate_date(arguments, "due_date")
        validate_boolean(arguments, "completed")
        validate_priority(arguments)
        validate_boolean(arguments, "flagged")
        validate_alarm_offsets(arguments)
        validate_geofence(arguments)
        validate_string(arguments, "source", max_length=200)


def validate_action(arguments: dict, allowed_actions: set, required: bool = True):
    value = arguments.get("action")
    if value is None:
        if required:
            raise RuntimeError("action is required.")
        return None
    if not isinstance(value, str) or value not in allowed_actions:
        allowed = ", ".join(sorted(allowed_actions))
        raise RuntimeError(f"action must be one of: {allowed}")
    return value


def validate_value(key: str, value) -> None:
    if isinstance(value, str):
        if len(value) > 50000:
            raise RuntimeError(f"{key} is too long.")
        if not SAFE_TEXT_PATTERN.match(value):
            raise RuntimeError(f"{key} contains unsupported control characters.")
        return
    if isinstance(value, list):
        if len(value) > 200:
            raise RuntimeError(f"{key} has too many items.")
        for item in value:
            validate_value(key, item)
        return
    if isinstance(value, dict):
        if len(value) > 100:
            raise RuntimeError(f"{key} has too many properties.")
        for nested_key, nested_value in value.items():
            validate_value(f"{key}.{nested_key}", nested_value)


def validate_string(arguments: dict, field: str, required: bool = False, max_length: int = 20000) -> None:
    value = arguments.get(field)
    if value is None:
        if required:
            raise RuntimeError(f"{field} is required.")
        return
    if not isinstance(value, str):
        raise RuntimeError(f"{field} must be a string.")
    if required and not value.strip():
        raise RuntimeError(f"{field} cannot be empty.")
    if len(value) > max_length:
        raise RuntimeError(f"{field} is too long.")


def validate_boolean(arguments: dict, field: str, required: bool = False) -> None:
    value = arguments.get(field)
    if value is None:
        if required:
            raise RuntimeError(f"{field} is required.")
        return
    if not isinstance(value, bool):
        raise RuntimeError(f"{field} must be a boolean.")


def validate_integer(arguments: dict, field: str, required: bool = False) -> None:
    value = arguments.get(field)
    if value is None:
        if required:
            raise RuntimeError(f"{field} is required.")
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"{field} must be an integer.")


def validate_alarm_offsets(arguments: dict) -> None:
    values = arguments.get("alarms")
    if values is None:
        return
    if not isinstance(values, list):
        raise RuntimeError("alarms must be an array of seconds (negative = before, positive = after).")
    if len(values) > 10:
        raise RuntimeError("alarms accepts at most 10 entries.")
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuntimeError("alarms entries must be numbers (offset in seconds).")
        if value < -7 * 24 * 3600 or value > 7 * 24 * 3600:
            raise RuntimeError("alarms entries must be within ±7 days (in seconds).")


def validate_geofence(arguments: dict) -> None:
    value = arguments.get("geofence")
    if value is None:
        return
    if not isinstance(value, dict):
        raise RuntimeError("geofence must be an object {lat, lon, radius_meters?, proximity?, title?}.")
    for key in ("lat", "lon"):
        if key not in value:
            raise RuntimeError(f"geofence is missing required field {key}.")
        if isinstance(value[key], bool) or not isinstance(value[key], (int, float)):
            raise RuntimeError(f"geofence.{key} must be a number.")
    radius = value.get("radius_meters")
    if radius is not None and (isinstance(radius, bool) or not isinstance(radius, (int, float)) or radius <= 0):
        raise RuntimeError("geofence.radius_meters must be a positive number.")
    proximity = value.get("proximity")
    if proximity is not None and proximity not in {"enter", "leave"}:
        raise RuntimeError("geofence.proximity must be 'enter' or 'leave'.")


def validate_priority(arguments: dict) -> None:
    value = arguments.get("priority")
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError("priority must be an integer between 0 and 9.")
    if value < 0 or value > 9:
        raise RuntimeError("priority must be between 0 (none) and 9 (low). 1=high, 5=medium.")


def validate_email_list(arguments: dict, field: str) -> None:
    values = arguments.get(field)
    if values is None:
        return
    if not isinstance(values, list):
        raise RuntimeError(f"{field} must be an array of email addresses.")
    for value in values:
        if not isinstance(value, str) or not is_valid_email(value):
            raise RuntimeError(f"{field} contains an invalid email address: {value}")


def is_valid_email(value: str) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    if any(ch.isspace() for ch in value):
        return False
    _, addr_spec = parseaddr(value)
    if not addr_spec or "@" not in addr_spec:
        return False
    local, _, domain = addr_spec.rpartition("@")
    if not local or not domain:
        return False
    if ".." in local or ".." in domain:
        return False
    if not EMAIL_LOCAL_PATTERN.match(local):
        return False
    if not EMAIL_DOMAIN_PATTERN.match(domain):
        return False
    return True


def validate_date(arguments: dict, field: str, required: bool = False) -> None:
    value = arguments.get(field)
    if value is None:
        if required:
            raise RuntimeError(f"{field} is required.")
        return
    if not isinstance(value, str):
        raise RuntimeError(f"{field} must be a string.")
    parse_date_string(value, field)


def parse_date_string(value: str, field: str) -> None:
    candidate = value.strip()
    if not candidate:
        raise RuntimeError(f"{field} cannot be empty.")
    if DATE_ONLY_PATTERN.match(candidate):
        try:
            datetime.strptime(candidate, "%Y-%m-%d")
        except ValueError as exc:
            raise RuntimeError(_date_error(field)) from exc
        return
    if DATETIME_PATTERN.match(candidate):
        normalized = candidate
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise RuntimeError(_date_error(field)) from exc
        return
    raise RuntimeError(_date_error(field))


def _date_error(field: str) -> str:
    return (
        f"{field} must be YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS, optionally suffixed "
        "with Z or ±HH:MM. Milliseconds and other ISO variants are not accepted."
    )
