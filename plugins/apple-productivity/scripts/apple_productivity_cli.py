#!/usr/bin/env python3
"""CLI-first interface for Apple Mail, Calendar, and Reminders.

The CLI and MCP server share the same registry and service core. Low-level
commands map one-to-one to service tools; compound commands reduce agent round
trips by composing those primitives in one warm service process.
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta
import json
import shlex
import sys
from typing import Any, Iterable, Optional

from apple_productivity_registry import ArgumentSpec, TOOL_SPECS, ToolSpec
from apple_productivity_service import AppleProductivityService
from apple_productivity_workflows import (
    run_mail_newsletters_workflow,
    run_mail_triage_workflow,
    summarize_mail_messages,
    summarize_newsletters,
)


PROJECT_NAME = "Apple Productivity CLI"
PROJECT_VERSION = "0.5.6"
PROJECT_REPOSITORY = "https://github.com/smitjj/cli-apple-productivity"
PROJECT_LICENSE = "Apache-2.0"
PROJECT_OWNER = "smitjj"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_PERMISSION = 3
EXIT_NOT_FOUND = 4
EXIT_PLATFORM = 5


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
        description="CLI for Apple Mail, Calendar, and Reminders.",
        epilog=(
            f"{PROJECT_NAME} {PROJECT_VERSION} | {PROJECT_LICENSE} | "
            f"{PROJECT_REPOSITORY}"
        ),
    )
    parser.add_argument("--raw", action="store_true", help="Alias for --compact.")
    parser.add_argument("--compact", action="store_true", help="Emit compact JSON.")
    parser.add_argument("--pretty", action="store_true", help="Emit pretty-printed JSON.")
    parser.add_argument("--timeout", type=int, default=None, help="Override automation timeout in seconds.")
    sub = parser.add_subparsers(dest="command", required=True)

    for spec in TOOL_SPECS:
        tool_parser = sub.add_parser(spec.cli_name, help=spec.description)
        _add_runtime_flags(tool_parser)
        tool_parser.set_defaults(kind="tool", tool_spec=spec)
        if spec.action_required:
            tool_parser.add_argument("action", choices=spec.actions)
        else:
            tool_parser.add_argument("action", nargs="?", default=spec.actions[0], choices=spec.actions)
        for arg in spec.arguments:
            _add_argument(tool_parser, arg)

    batch = sub.add_parser("batch", help="Run JSON calls from a file or stdin in one warm service process.")
    _add_runtime_flags(batch)
    batch.set_defaults(kind="batch")
    batch.add_argument("path", nargs="?", default="-", help="JSON array/JSONL file path, or '-' for stdin.")
    batch.add_argument("--jsonl", action="store_true", help="Emit one response per line.")
    batch.add_argument("--fail-fast", action="store_true", help="Stop after the first failed call.")

    repl = sub.add_parser("repl", help="Run an interactive JSON-call session with one warm service process.")
    _add_runtime_flags(repl)
    repl.set_defaults(kind="repl")
    repl.add_argument("--jsonl", action="store_true", help="Emit {ok,result|error} envelopes, one per response.")
    repl.add_argument("--no-prompt", action="store_true", help="Suppress the interactive prompt.")

    mail = sub.add_parser("mail", help="Compound Mail workflows.")
    mail_sub = mail.add_subparsers(dest="mail_command", required=True)
    triage = mail_sub.add_parser("triage", help="List/search likely triage targets.")
    _add_runtime_flags(triage)
    triage.set_defaults(kind="compound", compound="mail-triage")
    triage.add_argument("--mailbox-name", default="INBOX")
    triage.add_argument("--account-name")
    triage.add_argument("--query")
    triage.add_argument("--since")
    triage.add_argument("--limit", type=int, default=10)
    triage.add_argument("--unread-only", action="store_true")
    triage.add_argument("--flagged-only", action="store_true")

    newsletters = mail_sub.add_parser("newsletters", help="Find newsletter/unsubscribe candidates.")
    _add_runtime_flags(newsletters)
    newsletters.set_defaults(kind="compound", compound="mail-newsletters")
    newsletters.add_argument("--query", default="unsubscribe")
    newsletters.add_argument("--limit", type=int, default=10)
    newsletters.add_argument("--with-links", action="store_true", help="Fetch List-Unsubscribe links for candidates.")

    thread = mail_sub.add_parser("thread", help="Fetch a message thread.")
    _add_runtime_flags(thread)
    thread.set_defaults(kind="compound", compound="mail-thread")
    thread.add_argument("message_id", type=int)
    thread.add_argument("--account-name")
    thread.add_argument("--mailbox-name")
    thread.add_argument("--limit", type=int)

    mail_open = mail_sub.add_parser("open", help="Open a message in Mail.app.")
    _add_runtime_flags(mail_open)
    mail_open.set_defaults(kind="compound", compound="mail-open")
    mail_open.add_argument("message_id", type=int)
    mail_open.add_argument("--account-name")
    mail_open.add_argument("--mailbox-name")

    mail_move = mail_sub.add_parser("move", help="Move a message to another mailbox.")
    _add_runtime_flags(mail_move)
    mail_move.set_defaults(kind="compound", compound="mail-move")
    mail_move.add_argument("message_id", type=int)
    mail_move.add_argument("target_mailbox")
    mail_move.add_argument("--target-account")
    mail_move.add_argument("--account-name")
    mail_move.add_argument("--mailbox-name")
    mail_move.add_argument("--dry-run", action="store_true")

    archive = mail_sub.add_parser("archive", help="Move a message to Archive.")
    _add_runtime_flags(archive)
    archive.set_defaults(kind="compound", compound="mail-archive")
    archive.add_argument("message_id", type=int)
    archive.add_argument("--target-mailbox", default="Archive")
    archive.add_argument("--target-account")
    archive.add_argument("--account-name")
    archive.add_argument("--mailbox-name")
    archive.add_argument("--dry-run", action="store_true")

    calendar = sub.add_parser("calendar", help="Compound Calendar workflows.")
    calendar_sub = calendar.add_subparsers(dest="calendar_command", required=True)
    agenda = calendar_sub.add_parser("agenda", help="List events for a date range.")
    _add_runtime_flags(agenda)
    agenda.set_defaults(kind="compound", compound="calendar-agenda")
    agenda.add_argument("--calendar-name")
    agenda.add_argument("--date-from")
    agenda.add_argument("--date-to")
    agenda.add_argument("--days", type=int, default=1)
    agenda.add_argument("--limit", type=int, default=25)

    day = sub.add_parser("day", help="Compound day-planning workflows.")
    day_sub = day.add_subparsers(dest="day_command", required=True)
    plan = day_sub.add_parser("plan", help="Fetch today's events and open reminders.")
    _add_runtime_flags(plan)
    plan.set_defaults(kind="compound", compound="day-plan")
    plan.add_argument("--date")
    plan.add_argument("--calendar-name")
    plan.add_argument("--list-name")
    plan.add_argument("--limit", type=int, default=25)

    doctor = sub.add_parser("doctor", help="Run permission and platform diagnostics.")
    _add_runtime_flags(doctor)
    doctor.set_defaults(kind="compound", compound="doctor")

    about = sub.add_parser("about", help="Print project, repository, and license metadata.")
    _add_runtime_flags(about)
    about.set_defaults(kind="about")

    completions = sub.add_parser("completions", help="Print shell completion script.")
    completions.set_defaults(kind="completions")
    completions.add_argument("shell", choices=("bash", "zsh"))

    return parser


def _add_runtime_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--raw", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--compact", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--pretty", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=int, default=argparse.SUPPRESS, help=argparse.SUPPRESS)


def _add_argument(parser: argparse.ArgumentParser, arg: ArgumentSpec) -> None:
    kwargs: dict[str, Any] = {"help": arg.help}
    if arg.kind == "boolean":
        kwargs["action"] = "store_true"
    elif arg.kind == "integer":
        kwargs["type"] = int
    elif arg.kind == "number":
        kwargs["type"] = float
    elif arg.kind == "bool_string":
        kwargs["choices"] = ["true", "false"]
    if arg.choices and arg.kind != "bool_string":
        kwargs["choices"] = list(arg.choices)
    if arg.multiple:
        kwargs["action"] = "append"
    if arg.default is not None:
        kwargs["default"] = arg.default
    flags = [arg.flag]
    if arg.name == "alarms":
        flags.insert(0, "--alarm")
    parser.add_argument(*flags, dest=arg.name, **kwargs)


def namespace_to_tool_call(namespace: argparse.Namespace) -> tuple[str, dict]:
    spec: ToolSpec = namespace.tool_spec
    args: dict[str, Any] = {"action": namespace.action}
    for arg in spec.arguments:
        value = getattr(namespace, arg.name, None)
        if arg.kind == "bool_string" and value is not None:
            value = value == "true"
        elif arg.multiple and arg.kind == "string":
            value = _str_list(value)
        _maybe(args, arg.name, value)
    if spec.name == "reminders_tasks":
        lat = args.pop("geofence_lat", None)
        lon = args.pop("geofence_lon", None)
        radius = args.pop("geofence_radius", None)
        proximity = args.pop("geofence_proximity", None)
        title = args.pop("geofence_title", None)
        if lat is not None and lon is not None:
            args["geofence"] = {
                "lat": lat,
                "lon": lon,
                "radius_meters": radius if radius is not None else 100.0,
                "proximity": proximity or "enter",
                "title": title or "",
            }
    return spec.name, args


def run_compound(namespace: argparse.Namespace, service: AppleProductivityService) -> Any:
    compound = namespace.compound
    if compound == "doctor":
        return service.dispatch("mail_permissions_check", {"action": "check"})
    if compound == "mail-thread":
        args = {"action": "get-thread", "message_id": namespace.message_id}
        _maybe(args, "account_name", namespace.account_name)
        _maybe(args, "mailbox_name", namespace.mailbox_name)
        _maybe(args, "limit", namespace.limit)
        return service.dispatch("mail_messages", args)
    if compound == "mail-open":
        args = {"action": "open", "message_id": namespace.message_id}
        _maybe(args, "account_name", namespace.account_name)
        _maybe(args, "mailbox_name", namespace.mailbox_name)
        result = service.dispatch("mail_messages", args)
        return {"workflow": "mail.open", "messageId": namespace.message_id, "result": result}
    if compound in {"mail-move", "mail-archive"}:
        args = {
            "action": "move",
            "message_id": namespace.message_id,
            "target_mailbox": namespace.target_mailbox,
        }
        _maybe(args, "target_account", namespace.target_account)
        _maybe(args, "account_name", namespace.account_name)
        _maybe(args, "mailbox_name", namespace.mailbox_name)
        if namespace.dry_run:
            args["dry_run"] = True
        result = service.dispatch("mail_messages", args)
        return {
            "workflow": "mail.archive" if compound == "mail-archive" else "mail.move",
            "messageId": namespace.message_id,
            "targetMailbox": namespace.target_mailbox,
            "result": result,
        }
    if compound == "mail-triage":
        return run_mail_triage_workflow(
            service,
            {
                "mailbox_name": namespace.mailbox_name,
                "account_name": namespace.account_name,
                "query": namespace.query,
                "since": namespace.since,
                "limit": namespace.limit,
                "unread_only": namespace.unread_only,
                "flagged_only": namespace.flagged_only,
            },
        )
    if compound == "mail-newsletters":
        return run_mail_newsletters_workflow(
            service,
            {
                "query": namespace.query,
                "limit": namespace.limit,
                "with_links": namespace.with_links,
            },
        )
    if compound == "calendar-agenda":
        start = namespace.date_from or date.today().isoformat()
        end = namespace.date_to or (date.fromisoformat(start) + timedelta(days=max(1, namespace.days))).isoformat()
        args = {"action": "list", "date_from": start, "date_to": end, "limit": namespace.limit}
        _maybe(args, "calendar_name", namespace.calendar_name)
        result = service.dispatch("calendar_events", args)
        return {"workflow": "calendar.agenda", "summary": summarize_agenda(result), "result": result}
    if compound == "day-plan":
        target = namespace.date or date.today().isoformat()
        next_day = (date.fromisoformat(target) + timedelta(days=1)).isoformat()
        event_args = {"action": "list", "date_from": target, "date_to": next_day, "limit": namespace.limit}
        _maybe(event_args, "calendar_name", namespace.calendar_name)
        reminder_args = {"action": "list", "show_completed": False, "limit": namespace.limit}
        _maybe(reminder_args, "list_name", namespace.list_name)
        calendar_result = service.dispatch("calendar_events", event_args)
        reminders_result = service.dispatch("reminders_tasks", reminder_args)
        return {
            "workflow": "day.plan",
            "date": target,
            "summary": summarize_day_plan(target, calendar_result, reminders_result),
            "calendar": calendar_result,
            "reminders": reminders_result,
        }
    raise RuntimeError(f"Unknown compound command: {compound}")


def summarize_agenda(result: Any) -> dict:
    events = extract_events(result)
    conflicts = find_conflicts(events)
    return {
        "count": len(events),
        "allDay": sum(1 for item in events if item.get("allDay") is True),
        "timed": sum(1 for item in events if item.get("allDay") is not True),
        "conflicts": conflicts,
        "conflictCount": len(conflicts),
    }


def summarize_day_plan(target_date: str, calendar_result: Any, reminders_result: Any) -> dict:
    reminders = extract_reminders(reminders_result)
    overdue = [
        item for item in reminders
        if item.get("dueDate") and item.get("dueDate")[:10] < target_date and item.get("completed") is not True
    ]
    due_today = [
        item for item in reminders
        if item.get("dueDate") and item.get("dueDate")[:10] == target_date and item.get("completed") is not True
    ]
    return {
        "calendar": summarize_agenda(calendar_result),
        "reminders": {
            "count": len(reminders),
            "overdue": len(overdue),
            "dueToday": len(due_today),
            "flagged": sum(1 for item in reminders if item.get("flagged") is True),
        },
    }


def extract_messages(value: Any) -> list[dict]:
    if isinstance(value, dict):
        messages = value.get("messages")
        if isinstance(messages, list):
            return [item for item in messages if isinstance(item, dict)]
    return []


def extract_events(value: Any) -> list[dict]:
    if isinstance(value, dict):
        events = value.get("events")
        if isinstance(events, list):
            return [item for item in events if isinstance(item, dict)]
    return []


def extract_reminders(value: Any) -> list[dict]:
    if isinstance(value, dict):
        reminders = value.get("reminders")
        if isinstance(reminders, list):
            return [item for item in reminders if isinstance(item, dict)]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def min_compact(values: Iterable[Any]) -> Any:
    candidates = [value for value in values if value]
    return min(candidates) if candidates else None


def max_compact(values: Iterable[Any]) -> Any:
    candidates = [value for value in values if value]
    return max(candidates) if candidates else None


def find_conflicts(events: list[dict]) -> list[dict]:
    timed = [
        item for item in events
        if item.get("allDay") is not True and item.get("startDate") and item.get("endDate")
    ]
    timed.sort(key=lambda item: item.get("startDate") or "")
    conflicts = []
    for left, right in zip(timed, timed[1:]):
        if (left.get("endDate") or "") > (right.get("startDate") or ""):
            conflicts.append(
                {
                    "leftId": left.get("id"),
                    "rightId": right.get("id"),
                    "leftSummary": left.get("summary"),
                    "rightSummary": right.get("summary"),
                    "overlapStart": right.get("startDate"),
                    "overlapEnd": min(left.get("endDate"), right.get("endDate")),
                }
            )
    return conflicts


def parse_calls(text: str) -> list[dict]:
    stripped = text.strip()
    if not stripped:
        return []
    if stripped.startswith("["):
        payload = json.loads(stripped)
        if not isinstance(payload, list):
            raise RuntimeError("Batch JSON array must contain call objects.")
        return payload
    calls = []
    for line in stripped.splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        calls.append(json.loads(candidate))
    return calls


def normalize_call(call: dict) -> tuple[str, dict]:
    if not isinstance(call, dict):
        raise RuntimeError("Each batch call must be an object.")
    tool = call.get("tool") or call.get("tool_name") or call.get("name")
    args = call.get("arguments", call.get("args", {}))
    if not tool or not isinstance(tool, str):
        raise RuntimeError("Each batch call requires a string tool/tool_name/name.")
    if not isinstance(args, dict):
        raise RuntimeError("Batch call arguments/args must be an object.")
    return tool, args


def call_label(call: dict, index: int) -> Any:
    return call.get("id", call.get("call_id", index)) if isinstance(call, dict) else index


def run_batch(namespace: argparse.Namespace, service: AppleProductivityService) -> Any:
    text = sys.stdin.read() if namespace.path == "-" else open(namespace.path, "r", encoding="utf-8").read()
    responses = []
    for index, call in enumerate(parse_calls(text), start=1):
        label = call_label(call, index)
        try:
            tool, args = normalize_call(call)
            responses.append({"index": index, "id": label, "ok": True, "result": service.dispatch(tool, args)})
        except Exception as exc:
            responses.append({"index": index, "id": label, "ok": False, "error": str(exc), "exitCode": classify_error(exc)})
            if getattr(namespace, "fail_fast", False):
                break
    return responses


def run_repl(namespace: argparse.Namespace, service: AppleProductivityService) -> int:
    if not namespace.no_prompt:
        print("apple-productivity repl: enter JSON calls, shell-style CLI commands, help, or exit.", file=sys.stderr)
    parser = build_parser()
    while True:
        if not namespace.no_prompt:
            print("apple-productivity> ", end="", file=sys.stderr, flush=True)
        line = sys.stdin.readline()
        if not line:
            print("", file=sys.stderr)
            return EXIT_OK
        line = line.strip()
        if not line:
            continue
        if line in {"exit", "quit"}:
            return EXIT_OK
        if line == "help":
            print('JSON: {"tool":"mail_accounts","arguments":{"action":"list"}}', file=sys.stderr)
            print("CLI: mail-accounts list", file=sys.stderr)
            continue
        try:
            if line.startswith("{"):
                tool, args = normalize_call(json.loads(line))
                result = service.dispatch(tool, args)
            else:
                repl_args = parser.parse_args(shlex.split(line))
                if getattr(repl_args, "kind", None) == "tool":
                    tool, args = namespace_to_tool_call(repl_args)
                    result = service.dispatch(tool, args)
                elif getattr(repl_args, "kind", None) == "compound":
                    result = run_compound(repl_args, service)
                else:
                    raise RuntimeError("Nested batch/repl commands are not supported inside repl.")
            if namespace.jsonl:
                print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, separators=(",", ":")))
            else:
                print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        except SystemExit:
            print(json.dumps({"ok": False, "error": "invalid repl command"}, separators=(",", ":")))
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc), "exitCode": classify_error(exc)}, separators=(",", ":")))


def classify_error(exc: Exception) -> int:
    text = str(exc).lower()
    if "permission" in text or "not authorized" in text or "access is blocked" in text:
        return EXIT_PERMISSION
    if "not found" in text:
        return EXIT_NOT_FOUND
    if (
        "read-only mode" in text
        or "required" in text
        or "requires" in text
        or "must be" in text
        or "cannot" in text
        or "accepts only" in text
        or "unknown tool" in text
        or "action must" in text
    ):
        return EXIT_USAGE
    return EXIT_PLATFORM


def emit_json(value: Any, *, compact: bool) -> None:
    if compact:
        print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    else:
        print(json.dumps(value, indent=2, ensure_ascii=False))


def project_metadata() -> dict:
    return {
        "name": PROJECT_NAME,
        "version": PROJECT_VERSION,
        "owner": PROJECT_OWNER,
        "repository": PROJECT_REPOSITORY,
        "license": PROJECT_LICENSE,
        "licenseUrl": f"{PROJECT_REPOSITORY}/blob/main/LICENSE",
        "riskNotice": (
            "Runs local macOS automation against Apple Mail, Calendar, and Reminders. "
            "Use at your own risk; prefer --dry-run or APPLE_PRODUCTIVITY_READ_ONLY=1 "
            "for safety-sensitive workflows."
        ),
    }


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = build_parser()
    namespace = parser.parse_args(argv)
    if namespace.kind == "completions":
        print_completion(namespace.shell)
        return EXIT_OK
    if namespace.kind == "about":
        compact = namespace.compact or namespace.raw or not namespace.pretty
        emit_json(project_metadata(), compact=compact)
        return EXIT_OK
    service = AppleProductivityService(timeout_seconds=namespace.timeout)

    if namespace.kind == "repl":
        return run_repl(namespace, service)

    try:
        if namespace.kind == "tool":
            tool_name, arguments = namespace_to_tool_call(namespace)
            result = service.dispatch(tool_name, arguments)
        elif namespace.kind == "compound":
            result = run_compound(namespace, service)
        elif namespace.kind == "batch":
            result = run_batch(namespace, service)
            if namespace.jsonl:
                for item in result:
                    print(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
                return EXIT_OK if all(item.get("ok") for item in result) else EXIT_PLATFORM
        else:
            raise RuntimeError(f"Unknown command kind: {namespace.kind}")
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return classify_error(exc)

    compact = namespace.compact or namespace.raw or not namespace.pretty
    emit_json(result, compact=compact)
    return EXIT_OK


def print_completion(shell: str) -> None:
    commands = " ".join(
        [
            "batch",
            "repl",
            "doctor",
            "mail",
            "calendar",
            "day",
            "about",
            *[spec.cli_name for spec in TOOL_SPECS],
        ]
    )
    mail_commands = "triage newsletters thread open move archive"
    calendar_commands = "agenda"
    day_commands = "plan"
    if shell == "bash":
        print(
            f"""_apple_productivity_complete() {{
  local cur prev
  COMPREPLY=()
  cur="${{COMP_WORDS[COMP_CWORD]}}"
  prev="${{COMP_WORDS[COMP_CWORD-1]}}"
  if [[ $COMP_CWORD -eq 1 ]]; then
    COMPREPLY=( $(compgen -W "{commands}" -- "$cur") )
  elif [[ ${{COMP_WORDS[1]}} == "mail" && $COMP_CWORD -eq 2 ]]; then
    COMPREPLY=( $(compgen -W "{mail_commands}" -- "$cur") )
  elif [[ ${{COMP_WORDS[1]}} == "calendar" && $COMP_CWORD -eq 2 ]]; then
    COMPREPLY=( $(compgen -W "{calendar_commands}" -- "$cur") )
  elif [[ ${{COMP_WORDS[1]}} == "day" && $COMP_CWORD -eq 2 ]]; then
    COMPREPLY=( $(compgen -W "{day_commands}" -- "$cur") )
  fi
}}
complete -F _apple_productivity_complete apple-productivity
"""
        )
        return
    print(
        f"""#compdef apple-productivity
_apple_productivity() {{
  local -a commands mail_commands calendar_commands day_commands
  commands=({commands})
  mail_commands=({mail_commands})
  calendar_commands=({calendar_commands})
  day_commands=({day_commands})
  if (( CURRENT == 2 )); then
    _describe 'command' commands
  elif [[ $words[2] == mail && CURRENT == 3 ]]; then
    _describe 'mail command' mail_commands
  elif [[ $words[2] == calendar && CURRENT == 3 ]]; then
    _describe 'calendar command' calendar_commands
  elif [[ $words[2] == day && CURRENT == 3 ]]; then
    _describe 'day command' day_commands
  fi
}}
_apple_productivity "$@"
"""
    )


if __name__ == "__main__":
    sys.exit(main())
