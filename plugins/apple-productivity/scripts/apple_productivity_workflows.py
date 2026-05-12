#!/usr/bin/env python3
"""Summary-first compound Mail workflows shared by the CLI and MCP server."""

from __future__ import annotations

from typing import Any, Iterable, Optional, Protocol


class MailWorkflowService(Protocol):
    def dispatch(self, tool_name: str, arguments: Optional[dict] = None) -> Any:
        ...


def extract_messages(value: Any) -> list[dict]:
    if isinstance(value, dict):
        messages = value.get("messages")
        if isinstance(messages, list):
            return [item for item in messages if isinstance(item, dict)]
    return []


def min_compact(values: Iterable[Any]) -> Any:
    candidates = [value for value in values if value]
    return min(candidates) if candidates else None


def max_compact(values: Iterable[Any]) -> Any:
    candidates = [value for value in values if value]
    return max(candidates) if candidates else None


def summarize_mail_messages(result: Any) -> dict:
    messages = extract_messages(result)
    unread = sum(1 for item in messages if item.get("read") is False)
    flagged = sum(1 for item in messages if item.get("flagged") is True)
    with_attachments = sum(1 for item in messages if item.get("attachments"))
    return {
        "count": len(messages),
        "unread": unread,
        "flagged": flagged,
        "withAttachments": with_attachments,
        "oldestDateReceived": min_compact(item.get("dateReceived") for item in messages),
        "newestDateReceived": max_compact(item.get("dateReceived") for item in messages),
    }


def summarize_newsletters(candidates: list[dict]) -> dict:
    found = 0
    one_click = 0
    for item in candidates:
        unsubscribe = item.get("unsubscribe") or {}
        if unsubscribe.get("found"):
            found += 1
        if unsubscribe.get("oneClickPost"):
            one_click += 1
    return {"count": len(candidates), "withUnsubscribe": found, "withOneClickPost": one_click}


def _maybe(args: dict, key: str, value: Any) -> None:
    if value is not None:
        args[key] = value


def run_mail_triage_workflow(service: MailWorkflowService, arguments: dict) -> dict:
    limit = int(arguments.get("limit") or 10)
    mailbox_name = arguments.get("mailbox_name") or "INBOX"
    if arguments.get("query") or arguments.get("since"):
        args = {"action": "search", "limit": limit}
        _maybe(args, "query", arguments.get("query"))
        _maybe(args, "since", arguments.get("since"))
        _maybe(args, "account_name", arguments.get("account_name"))
        _maybe(args, "mailbox_name", mailbox_name)
        if arguments.get("unread_only"):
            args["unread_only"] = True
        if arguments.get("flagged_only"):
            args["flagged_only"] = True
        result = service.dispatch("mail_messages", args)
    else:
        args = {"action": "list", "mailbox_name": mailbox_name, "limit": limit}
        _maybe(args, "account_name", arguments.get("account_name"))
        if arguments.get("unread_only"):
            args["unread_only"] = True
        if arguments.get("flagged_only"):
            args["flagged_only"] = True
        result = service.dispatch("mail_messages", args)
    return {
        "workflow": "mail.triage",
        "summary": summarize_mail_messages(result),
        "result": result,
        **({"source": result["source"]} if isinstance(result, dict) and result.get("source") else {}),
    }


def run_mail_newsletters_workflow(service: MailWorkflowService, arguments: dict) -> dict:
    limit = int(arguments.get("limit") or 10)
    query = arguments.get("query") or "unsubscribe"
    search = service.dispatch(
        "mail_messages",
        {"action": "search", "query": query, "limit": limit},
    )
    if not arguments.get("with_links"):
        return {
            "workflow": "mail.newsletters",
            "summary": summarize_mail_messages(search),
            "candidates": search,
            **({"source": search["source"]} if isinstance(search, dict) and search.get("source") else {}),
        }
    enriched = []
    for message in search.get("messages", []):
        item = {"message": message}
        message_id = message.get("id")
        if message_id is not None:
            try:
                item["unsubscribe"] = service.dispatch(
                    "mail_messages",
                    {"action": "get-unsubscribe-link", "message_id": message_id},
                )
            except Exception as exc:
                item["unsubscribeError"] = str(exc)
        enriched.append(item)
    return {
        "workflow": "mail.newsletters",
        "summary": summarize_newsletters(enriched),
        "count": len(enriched),
        "candidates": enriched,
        **({"source": search["source"]} if isinstance(search, dict) and search.get("source") else {}),
    }
