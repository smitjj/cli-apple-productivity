#!/usr/bin/env python3

from __future__ import annotations

import json
import logging
import os
import select
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote

from apple_productivity_registry import KNOWN_TOOLS
from apple_productivity_workflows import (
    run_mail_classify_workflow,
    run_mail_newsletters_workflow,
    run_mail_triage_workflow,
    summarize_automation_classification,
)
from shared_validation import (
    normalize_mail_mailbox_scope,
    refine_mail_search_arguments,
    validate_tool_arguments,
)

try:
    from apple_productivity_mail_index import (
        MailIndexReader,
        MailIndexUnavailable,
        account_url_hints_from_accounts_map,
    )
except Exception:  # pragma: no cover - module is pure-python; only fails on broken install
    MailIndexReader = None  # type: ignore
    MailIndexUnavailable = RuntimeError  # type: ignore
    account_url_hints_from_accounts_map = None  # type: ignore

try:
    from apple_productivity_eventkit import EventKitBackend, open_default as _open_eventkit
except Exception:  # pragma: no cover
    EventKitBackend = None  # type: ignore
    _open_eventkit = None  # type: ignore


SCRIPT_PATH = Path(__file__).with_name("apple_productivity_jxa.js")
DEFAULT_TIMEOUT_SECONDS = 30
TIMEOUT_ENV_VAR = "APPLE_PRODUCTIVITY_TIMEOUT_SECONDS"
LOG_PATH_ENV_VAR = "APPLE_PRODUCTIVITY_LOG"
PERSISTENT_ENV_VAR = "APPLE_PRODUCTIVITY_PERSISTENT_JXA"
READ_ONLY_ENV_VAR = "APPLE_PRODUCTIVITY_READ_ONLY"
MAIL_INDEX_ENV_VAR = "APPLE_PRODUCTIVITY_MAIL_INDEX"
EVENTKIT_ENV_VAR = "APPLE_PRODUCTIVITY_EVENTKIT"

# Mail actions that target a single message by id and benefit from the scope
# cache. `move` is excluded because the cached entry must be updated to the
# new mailbox after success — handled separately below.
SCOPED_MAIL_ACTIONS = {
    "get",
    "delete",
    "set-read",
    "set-flag",
    "open",
    "get-attachment",
    "get-thread",
    "get-unsubscribe-link",
}


class MessageScopeCache:
    """Bounded message_id -> (account_name, mailbox_name) cache.

    Lives for the lifetime of one service instance — that is, one MCP server
    process or one CLI invocation. The cache lets repeated targeted actions
    against the same message skip the global mailbox scan in JXA.
    """

    MAX_ENTRIES = 256

    def __init__(self) -> None:
        self._entries: dict = {}

    def remember(self, message_id, account, mailbox) -> None:
        if message_id is None or mailbox is None:
            return
        if message_id in self._entries:
            del self._entries[message_id]
        elif len(self._entries) >= self.MAX_ENTRIES:
            self._entries.pop(next(iter(self._entries)))
        self._entries[message_id] = (account, mailbox)

    def get(self, message_id):
        return self._entries.get(message_id)

    def forget(self, message_id) -> None:
        self._entries.pop(message_id, None)

    def __len__(self) -> int:
        return len(self._entries)


class JxaWorkerError(RuntimeError):
    """Raised when the persistent JXA worker fails. Treated as fatal for the
    current call; the caller can retry on a fresh worker."""


class PersistentJxaWorker:
    """Long-lived osascript subprocess fed length-prefixed JSON requests.

    Avoids paying the ~150–250 ms osascript cold-start cost on every tool call.
    Each request and response is framed as ``"<byte-count>\\n<JSON body>"`` —
    matches the protocol implemented in ``runServer()`` inside the JXA file.

    Thread-safe (one in-flight call at a time via internal lock). On timeout or
    pipe error the worker is killed and respawned for the next call.
    """

    HANDSHAKE_TIMEOUT = 5.0

    def __init__(self, script_path: Path, timeout_seconds: int) -> None:
        self._script_path = script_path
        self._timeout = timeout_seconds
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._req_id = 0

    def call(self, tool: str, args: dict) -> Any:
        with self._lock:
            self._ensure_alive()
            self._req_id += 1
            request_id = self._req_id
            payload = json.dumps({"id": request_id, "tool": tool, "args": args or {}})
            body = payload.encode("utf-8")
            header = f"{len(body)}\n".encode("ascii")
            assert self._proc is not None and self._proc.stdin is not None
            try:
                self._proc.stdin.write(header)
                self._proc.stdin.write(body)
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                self._kill()
                raise JxaWorkerError(f"JXA worker pipe broken: {exc}") from exc

            response = self._read_framed(self._timeout)
            if not isinstance(response, dict):
                self._kill()
                raise JxaWorkerError("JXA worker returned a non-object response")
            if response.get("id") != request_id:
                # Out-of-band frame (or stale response from a killed call). Reset.
                self._kill()
                raise JxaWorkerError(
                    f"JXA worker response id {response.get('id')!r} did not match request {request_id}"
                )
            if not response.get("ok"):
                error = response.get("error") or {}
                raise RuntimeError(error.get("message", "Native automation script failed"))
            return response.get("result")

    def shutdown(self) -> None:
        with self._lock:
            self._kill()

    # Internal --------------------------------------------------------------

    def _ensure_alive(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        self._proc = subprocess.Popen(
            ["osascript", "-l", "JavaScript", str(self._script_path), "--server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        try:
            handshake = self._read_framed(self.HANDSHAKE_TIMEOUT)
        except Exception:
            self._kill()
            raise
        if not isinstance(handshake, dict) or not handshake.get("ready"):
            self._kill()
            raise JxaWorkerError(f"JXA worker handshake failed: {handshake!r}")

    def _kill(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.kill()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=2)
        except Exception:
            pass
        self._proc = None

    def _read_framed(self, timeout: float):
        assert self._proc is not None and self._proc.stdout is not None
        fd = self._proc.stdout.fileno()
        deadline = time.monotonic() + timeout
        # Read header line "<n>\n"
        header = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._kill()
                raise TimeoutError(f"JXA worker timed out after {timeout}s while reading header")
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                self._kill()
                raise TimeoutError(f"JXA worker timed out after {timeout}s while reading header")
            chunk = os.read(fd, 1)
            if not chunk:
                self._kill()
                raise JxaWorkerError("JXA worker closed unexpectedly while reading header")
            if chunk == b"\n":
                break
            header.extend(chunk)
            if len(header) > 32:
                self._kill()
                raise JxaWorkerError(f"JXA worker header too long: {bytes(header)!r}")
        try:
            n = int(header.decode("ascii"))
        except ValueError as exc:
            self._kill()
            raise JxaWorkerError(f"JXA worker invalid header: {bytes(header)!r}") from exc
        if n <= 0 or n > 64 * 1024 * 1024:
            self._kill()
            raise JxaWorkerError(f"JXA worker frame size out of range: {n}")
        # Read body of exactly n bytes
        body = bytearray()
        while len(body) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._kill()
                raise TimeoutError(f"JXA worker timed out after {timeout}s while reading body")
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                self._kill()
                raise TimeoutError(f"JXA worker timed out after {timeout}s while reading body")
            chunk = os.read(fd, n - len(body))
            if not chunk:
                self._kill()
                raise JxaWorkerError("JXA worker closed unexpectedly while reading body")
            body.extend(chunk)
        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            self._kill()
            raise JxaWorkerError(f"JXA worker returned invalid JSON: {bytes(body)!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"", "0", "false", "no", "off"}


def _env_timeout() -> int:
    raw = os.environ.get(TIMEOUT_ENV_VAR)
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        parsed = int(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return max(1, parsed)


def _build_default_logger() -> Optional[logging.Logger]:
    log_path = os.environ.get(LOG_PATH_ENV_VAR)
    if not log_path:
        return None
    logger = logging.getLogger("apple_productivity")
    if not logger.handlers:
        try:
            handler = logging.FileHandler(log_path)
        except OSError:
            return None
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


class AppleProductivityService:
    """Reusable core that wraps osascript-driven Apple automation.

    Both the MCP server and the CLI dispatch through this class so the
    transport layer stays thin. Validation reuses shared_validation.
    """

    def __init__(
        self,
        timeout_seconds: Optional[int] = None,
        logger: Optional[logging.Logger] = None,
        script_path: Optional[Path] = None,
        use_persistent_worker: Optional[bool] = None,
        read_only: Optional[bool] = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else _env_timeout()
        self.logger = logger if logger is not None else _build_default_logger()
        self.script_path = (script_path if script_path is not None else SCRIPT_PATH).resolve()
        if not self.script_path.is_file():
            raise RuntimeError(
                "Mail automation script missing at "
                f"{self.script_path}. Reinstall or upgrade the Apple Productivity "
                "plugin, then restart Codex so the MCP server reloads from the "
                "current plugin cache."
            )
        self.scope_cache = MessageScopeCache()
        self.read_only = read_only if read_only is not None else _env_bool(READ_ONLY_ENV_VAR, False)
        if use_persistent_worker is None:
            use_persistent_worker = _env_bool(PERSISTENT_ENV_VAR, True)
        self._worker: Optional[PersistentJxaWorker] = (
            PersistentJxaWorker(self.script_path, self.timeout_seconds)
            if use_persistent_worker
            else None
        )
        self._worker_failures = 0
        self._mail_index = None
        self._mail_index_probed = False
        if not _env_bool(MAIL_INDEX_ENV_VAR, True):
            # User opted out; never probe.
            self._mail_index_probed = True
        self._eventkit = None
        self._eventkit_probed = False
        if not _env_bool(EVENTKIT_ENV_VAR, True):
            self._eventkit_probed = True

    def dispatch(self, tool_name: str, arguments: Optional[dict] = None) -> Any:
        if tool_name not in KNOWN_TOOLS:
            raise RuntimeError(f"Unknown tool: {tool_name}")
        args = dict(arguments or {})
        validate_tool_arguments(tool_name, args)
        if self.read_only and _is_mutating(tool_name, args):
            raise RuntimeError(
                f"Read-only mode is enabled (APPLE_PRODUCTIVITY_READ_ONLY=1); "
                f"refusing {tool_name} action {args.get('action')!r}."
            )
        if args.get("dry_run") and _is_mutating(tool_name, args):
            return dry_run_response(tool_name, args)
        # Calendar/Reminders writes go through PyObjC EventKit when available.
        # Falls through to JXA on any failure or when EventKit isn't installed.
        ek_result = self._maybe_via_eventkit(tool_name, args)
        if ek_result is not None:
            return ek_result
        # Calendar delete fallback (only reached if EventKit unavailable).
        if tool_name == "calendar_events" and args.get("action") == "delete":
            return self._delete_calendar_event_via_applescript(args["event_id"])
        if tool_name == "mail_messages":
            return self._dispatch_mail_messages(args)
        if tool_name == "mail_analyze":
            return self._dispatch_mail_analyze(args)
        if tool_name == "mail_permissions_check":
            result = self._invoke_jxa(tool_name, args)
            if isinstance(result, dict):
                result = dict(result)
                result["envelope_index"] = self._envelope_index_diagnostic()
            return result
        return self._invoke_jxa(tool_name, args)

    def _get_eventkit(self):
        if self._eventkit_probed:
            return self._eventkit
        self._eventkit_probed = True
        if _open_eventkit is None:
            return None
        try:
            self._eventkit = _open_eventkit(logger=self.logger)
        except Exception as exc:
            if self.logger:
                self.logger.info("EventKit probe raised: %s", exc)
            self._eventkit = None
        return self._eventkit

    def _maybe_via_eventkit(self, tool_name: str, args: dict):
        """Try the EventKit fast path for write operations on Calendar/Reminders.

        Returns the result on success, or None to fall through to JXA. Any
        EventKit-side failure (permission, schema mismatch, missing field)
        also returns None so the caller falls back transparently.
        """
        if tool_name not in {"calendar_events", "reminders_tasks"}:
            return None
        action = args.get("action")
        backend = self._get_eventkit()
        if backend is None:
            return None
        try:
            if tool_name == "calendar_events":
                if action == "create" and backend.has_event_access:
                    return backend.create_event(args)
                if action == "update" and backend.has_event_access:
                    return backend.update_event(args)
                if action == "delete" and backend.has_event_access:
                    return backend.delete_event(args["event_id"])
            elif tool_name == "reminders_tasks":
                if action == "create" and backend.has_reminder_access:
                    return backend.create_reminder(args)
                if action == "update" and backend.has_reminder_access:
                    return backend.update_reminder(args)
                if action == "delete" and backend.has_reminder_access:
                    return backend.delete_reminder(args["reminder_id"])
                if action == "complete" and backend.has_reminder_access:
                    return backend.set_reminder_completed(args["reminder_id"], True)
                if action == "incomplete" and backend.has_reminder_access:
                    return backend.set_reminder_completed(args["reminder_id"], False)
        except Exception as exc:
            # Transparent fallback: log and let JXA path try.
            if self.logger:
                self.logger.info(
                    "EventKit path failed for %s/%s, falling back to JXA: %s",
                    tool_name, action, exc,
                )
            return None
        return None

    def _get_mail_index(self):
        """Lazily probe and return the Envelope Index reader, or None.

        Probed once per service instance; failures cache as None so we never
        re-probe a broken install. Returning None means the caller falls
        back to JXA.
        """
        if self._mail_index_probed:
            return self._mail_index
        self._mail_index_probed = True
        if MailIndexReader is None:
            return None
        try:
            self._mail_index = MailIndexReader.open_default(logger=self.logger)
        except Exception as exc:
            if self.logger:
                self.logger.info("Mail Envelope Index probe raised: %s", exc)
            self._mail_index = None
        return self._mail_index

    def _index_account_url_hints(self, reader, account_name: Optional[str]) -> Optional[list[str]]:
        if not account_name:
            return None
        hints: list[str] = []
        match_tokens: list[str] = []
        try:
            accounts = self._invoke_jxa("mail_accounts", {})
            if isinstance(accounts, list):
                for account in accounts:
                    if str(account.get("name", "")).lower() != str(account_name).lower():
                        continue
                    user_name = str(account.get("userName", "")).strip()
                    if user_name:
                        hints.append(user_name)
                        match_tokens.append(user_name)
                        if "@" in user_name:
                            local, domain = user_name.rsplit("@", 1)
                            if domain:
                                hints.append(domain)
                                match_tokens.append(domain)
                            if local:
                                match_tokens.append(local)
                    break
        except Exception as exc:
            if self.logger:
                self.logger.info("Mail account lookup for index hints failed: %s", exc)
        if account_url_hints_from_accounts_map is not None:
            try:
                hints.extend(account_url_hints_from_accounts_map(reader.db_path, match_tokens))
            except Exception as exc:
                if self.logger:
                    self.logger.info("AccountsMap lookup for index hints failed: %s", exc)
        unique: list[str] = []
        for hint in hints:
            candidate = hint.strip()
            if candidate and candidate not in unique:
                unique.append(candidate)
        return unique or None

    def _index_scoped_mail_args(self, args: dict) -> dict:
        action = args.get("action")
        if action == "search":
            return refine_mail_search_arguments(args)
        if action == "list":
            return normalize_mail_mailbox_scope(args)
        return args

    def _try_search_via_index(self, args: dict):
        """Attempt to satisfy a mail_messages.search call via SQLite.

        Returns the result dict on success, or None to fall through to JXA.
        Only handles the filter shape the index can express cheaply; falls
        through if the index is unavailable or the query needs JXA.
        """
        reader = self._get_mail_index()
        if reader is None:
            return None
        account_url_hints = self._index_account_url_hints(reader, args.get("account_name"))
        since_epoch = None
        since = args.get("since")
        if since:
            since_epoch = reader.iso_to_index_epoch(since)
            if since_epoch is None:
                return None
        try:
            rows = reader.search_messages(
                query=args.get("query"),
                mailbox_name=args.get("mailbox_name"),
                account_name=args.get("account_name"),
                account_url_hints=account_url_hints,
                from_address=args.get("from_address"),
                to_address=args.get("to_address"),
                subject_contains=args.get("subject_contains"),
                since_epoch=since_epoch,
                limit=int(args.get("limit") or 25),
                offset=int(args.get("offset") or 0),
                unread_only=bool(args.get("unread_only")),
                flagged_only=bool(args.get("flagged_only")),
            )
        except MailIndexUnavailable as exc:
            if self.logger:
                self.logger.info("Mail index search fell back to JXA: %s", exc)
            return None
        except Exception as exc:
            if self.logger:
                self.logger.info("Mail index search raised, falling back: %s", exc)
            return None
        messages = [_row_to_summary(row) for row in rows]
        if not messages and (args.get("account_name") or args.get("mailbox_name")):
            return None
        limit = int(args.get("limit") or 25)
        offset = int(args.get("offset") or 0)
        return _mail_page_payload(
            limit,
            offset,
            messages,
            query=args.get("query"),
            source="envelope_index",
        )

    def _try_list_via_index(self, args: dict):
        reader = self._get_mail_index()
        if reader is None:
            return None
        account_url_hints = self._index_account_url_hints(reader, args.get("account_name"))
        try:
            payload = reader.list_messages(
                mailbox_name=args["mailbox_name"],
                account_name=args.get("account_name"),
                account_url_hints=account_url_hints,
                limit=int(args.get("limit") or 25),
                offset=int(args.get("offset") or 0),
                unread_only=bool(args.get("unread_only")),
                flagged_only=bool(args.get("flagged_only")),
            )
        except MailIndexUnavailable as exc:
            if self.logger:
                self.logger.info("Mail index list fell back to JXA: %s", exc)
            return None
        except Exception as exc:
            if self.logger:
                self.logger.info("Mail index list raised, falling back: %s", exc)
            return None
        messages = [_row_to_summary(row) for row in payload["messages"]]
        if not messages:
            return None
        limit = int(args.get("limit") or 25)
        offset = int(args.get("offset") or 0)
        return _mail_page_payload(
            limit,
            offset,
            messages,
            mailbox=payload["mailbox"],
            source="envelope_index",
        )

    def classify_received_aggregate(self, arguments: dict) -> dict:
        scoped = self._index_scoped_mail_args({"action": "list", **arguments})
        payload = self._try_classify_received_via_index(scoped)
        if payload is None:
            raise RuntimeError(
                "Envelope Index classify is unavailable for this mailbox scope. "
                "Run mail_permissions_check (doctor) and verify envelope_index.ok."
            )
        return payload

    def _try_classify_received_via_index(self, args: dict) -> Optional[dict]:
        reader = self._get_mail_index()
        if reader is None:
            return None
        account_url_hints = self._index_account_url_hints(reader, args.get("account_name"))
        since_epoch = None
        since = args.get("since")
        if since:
            since_epoch = reader.iso_to_index_epoch(since)
            if since_epoch is None:
                return None
        try:
            payload = reader.classify_received_aggregate(
                mailbox_name=args.get("mailbox_name") or "INBOX",
                account_name=args.get("account_name"),
                account_url_hints=account_url_hints,
                since_epoch=since_epoch,
                unread_only=bool(args.get("unread_only")),
                flagged_only=bool(args.get("flagged_only")),
            )
        except MailIndexUnavailable as exc:
            if self.logger:
                self.logger.info("Mail index classify unavailable: %s", exc)
            return None
        except Exception as exc:
            if self.logger:
                self.logger.info("Mail index classify raised: %s", exc)
            return None
        summary = summarize_automation_classification(payload)
        return {
            "summary": summary,
            "scope": payload.get("scope"),
            "signals": payload.get("signals"),
            "source": "envelope_index",
        }

    def _try_thread_via_index(self, args: dict):
        reader = self._get_mail_index()
        if reader is None:
            return None
        try:
            rows = reader.list_thread_by_rowid(
                int(args["message_id"]),
                limit=int(args.get("limit") or 100),
            )
        except MailIndexUnavailable as exc:
            if self.logger:
                self.logger.info("Mail index thread fell back to JXA: %s", exc)
            return None
        except Exception as exc:
            if self.logger:
                self.logger.info("Mail index thread raised, falling back: %s", exc)
            return None
        messages = [_row_to_summary(row) for row in rows]
        return {"count": len(messages), "messages": messages, "source": "envelope_index"}

    def _dispatch_mail_messages(self, args: dict) -> Any:
        action = args.get("action")
        message_id = args.get("message_id")
        used_cached_hint = False

        if action == "search":
            args = self._index_scoped_mail_args(args)
            indexed = self._try_search_via_index(args)
            if indexed is not None:
                return indexed
        elif action == "list":
            args = self._index_scoped_mail_args(args)
            indexed = self._try_list_via_index(args)
            if indexed is not None:
                return indexed
        elif action == "get-thread" and not args.get("mailbox_name"):
            indexed = self._try_thread_via_index(args)
            if indexed is not None and indexed.get("count", 0) > 0:
                return indexed

        if (
            action in SCOPED_MAIL_ACTIONS
            and message_id is not None
            and not args.get("mailbox_name")
        ):
            cached = self.scope_cache.get(message_id)
            if cached:
                cached_account, cached_mailbox = cached
                args["mailbox_name"] = cached_mailbox
                if cached_account and not args.get("account_name"):
                    args["account_name"] = cached_account
                used_cached_hint = True

        try:
            result = self._invoke_jxa("mail_messages", args)
        except RuntimeError as exc:
            if used_cached_hint and "not found in mailbox" in str(exc).lower():
                # Cached scope is stale (message moved or deleted). Drop it
                # and retry once with no scoping hint.
                self.scope_cache.forget(message_id)
                retry_args = dict(args)
                retry_args.pop("mailbox_name", None)
                retry_args.pop("account_name", None)
                result = self._invoke_jxa("mail_messages", retry_args)
            else:
                raise

        self._update_scope_cache(action, args, result)
        if action in {"list", "search"} and isinstance(result, dict):
            limit = int(args.get("limit") or 25)
            offset = int(args.get("offset") or 0)
            messages = result.get("messages")
            if isinstance(messages, list):
                extra = {
                    key: value
                    for key, value in result.items()
                    if key not in {"messages", "count", "offset", "limit", "hasMore", "nextOffset"}
                }
                result = _mail_page_payload(limit, offset, messages, **extra)
        return _annotate_mail_read_source(action, result, "jxa")

    def _envelope_index_diagnostic(self) -> dict:
        if not _env_bool(MAIL_INDEX_ENV_VAR, True):
            return {"ok": False, "error": "disabled by APPLE_PRODUCTIVITY_MAIL_INDEX=0"}
        if MailIndexReader is None:
            return {"ok": False, "error": "Mail index module unavailable"}
        reader = MailIndexReader.open_default(logger=self.logger)
        if reader is None:
            return {"ok": False, "error": "Envelope Index unavailable"}
        try:
            path = str(reader.db_path)
        finally:
            reader.close()
        return {"ok": True, "error": None, "path": path}

    def _dispatch_mail_analyze(self, args: dict) -> Any:
        action = args.get("action")
        if action == "triage":
            return run_mail_triage_workflow(self, args)
        if action == "newsletters":
            return run_mail_newsletters_workflow(self, args)
        if action == "classify":
            return run_mail_classify_workflow(self, args)
        raise RuntimeError(f"Unsupported mail_analyze action: {action}")

    def _update_scope_cache(self, action: str, args: dict, result: Any) -> None:
        if not isinstance(result, (dict, list)):
            return
        if action == "move":
            self.scope_cache.remember(
                args.get("message_id"),
                args.get("target_account"),
                args.get("target_mailbox"),
            )
            return
        if action == "delete":
            self.scope_cache.forget(args.get("message_id"))
            return
        # For list/get/search/set-read/set-flag/open/get-attachment, harvest
        # any (id, account, mailbox) triples present in the result payload.
        for entry in _walk_messages(result):
            self.scope_cache.remember(
                entry.get("id"),
                entry.get("account"),
                entry.get("mailbox"),
            )

    # Convenience methods, one per tool. Both MCP and CLI can use these.
    def mail_accounts(self, **arguments: Any) -> Any:
        return self.dispatch("mail_accounts", arguments)

    def mail_mailboxes(self, **arguments: Any) -> Any:
        return self.dispatch("mail_mailboxes", arguments)

    def mail_messages(self, **arguments: Any) -> Any:
        return self.dispatch("mail_messages", arguments)

    def mail_compose(self, **arguments: Any) -> Any:
        return self.dispatch("mail_compose", arguments)

    def calendar_calendars(self, **arguments: Any) -> Any:
        return self.dispatch("calendar_calendars", arguments)

    def calendar_events(self, **arguments: Any) -> Any:
        return self.dispatch("calendar_events", arguments)

    def reminders_lists(self, **arguments: Any) -> Any:
        return self.dispatch("reminders_lists", arguments)

    def reminders_tasks(self, **arguments: Any) -> Any:
        return self.dispatch("reminders_tasks", arguments)

    # Internal -----------------------------------------------------------------

    def _invoke_jxa(self, tool_name: str, arguments: dict) -> Any:
        started = time.monotonic()

        # Persistent worker path. If it breaks twice in a row, disable and use
        # one-shot fallback so the user always gets a response.
        if self._worker is not None and self._worker_failures < 2:
            try:
                result = self._worker.call(tool_name, arguments or {})
            except (JxaWorkerError, TimeoutError) as exc:
                self._worker_failures += 1
                self._log(
                    tool_name,
                    arguments,
                    started,
                    ok=False,
                    detail=f"worker failure ({self._worker_failures}): {exc}",
                )
                if self._worker_failures >= 2:
                    self._worker = None
                # fall through to one-shot path
            except RuntimeError as exc:
                # Tool-level error from JXA — surface as platform error.
                self._worker_failures = 0  # tool errors don't indicate worker health
                self._log(tool_name, arguments, started, ok=False, detail=str(exc))
                raise RuntimeError(format_platform_error(str(exc))) from None
            else:
                self._worker_failures = 0
                self._log(tool_name, arguments, started, ok=True)
                return result

        return self._invoke_jxa_one_shot(tool_name, arguments, started)

    def _invoke_jxa_one_shot(self, tool_name: str, arguments: dict, started: float) -> Any:
        command = [
            "osascript",
            "-l",
            "JavaScript",
            str(self.script_path),
            tool_name,
            json.dumps(arguments or {}),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            self._log(tool_name, arguments, started, ok=False, detail=f"timeout after {self.timeout_seconds}s")
            raise RuntimeError(
                f"Automation timed out after {self.timeout_seconds} seconds while running {tool_name}."
            ) from exc

        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "Unknown osascript failure"
            self._log(tool_name, arguments, started, ok=False, detail=detail)
            raise RuntimeError(format_platform_error(detail))

        raw_output = completed.stdout.strip()
        if not raw_output:
            self._log(tool_name, arguments, started, ok=False, detail="empty stdout")
            raise RuntimeError("Native automation script returned no output.")

        try:
            payload = json.loads(raw_output)
        except json.JSONDecodeError as exc:
            self._log(tool_name, arguments, started, ok=False, detail="invalid json")
            raise RuntimeError(f"Native automation script returned invalid JSON: {raw_output}") from exc

        if not payload.get("ok"):
            error = payload.get("error") or {}
            message = error.get("message", "Native automation script failed")
            self._log(tool_name, arguments, started, ok=False, detail=message)
            raise RuntimeError(format_platform_error(message))

        self._log(tool_name, arguments, started, ok=True)
        return payload.get("result")

    def _delete_calendar_event_via_applescript(self, event_id: str) -> dict:
        parsed = parse_calendar_event_id(event_id)
        script = """
on run argv
tell application "Calendar"
  set targetCalendarName to item 1 of argv
  set targetUid to item 2 of argv
  tell calendar named targetCalendarName
    delete (first event whose uid is targetUid)
  end tell
end tell
return "deleted"
end run
"""
        command = [
            "osascript",
            "-e",
            script,
            parsed["calendar_name"],
            parsed["uid"],
        ]
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            self._log("calendar_events.delete", {"event_id": event_id}, started, ok=False, detail="timeout")
            raise RuntimeError(
                f"Automation timed out after {self.timeout_seconds} seconds while deleting the Calendar event."
            ) from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "Unknown Calendar deletion failure"
            self._log("calendar_events.delete", {"event_id": event_id}, started, ok=False, detail=detail)
            raise RuntimeError(format_platform_error(detail))
        self._log("calendar_events.delete", {"event_id": event_id}, started, ok=True)
        return {"deleted": True, "eventId": event_id}

    def _log(
        self,
        tool_name: str,
        arguments: dict,
        started: float,
        *,
        ok: bool,
        detail: Optional[str] = None,
    ) -> None:
        if not self.logger:
            return
        duration_ms = int((time.monotonic() - started) * 1000)
        action = arguments.get("action") if isinstance(arguments, dict) else None
        argument_keys = sorted(arguments.keys()) if isinstance(arguments, dict) else []
        message = (
            f"tool={tool_name} action={action} ok={ok} duration_ms={duration_ms} "
            f"keys={argument_keys}"
        )
        if detail:
            message += f" detail={detail!r}"
        self.logger.info(message)


def _iso_to_epoch_or_none(value: str) -> Optional[float]:
    """Convert our standard date strings to a Mail-comparable epoch.

    Mail's ``date_received`` column stores Apple absolute time (seconds since
    2001-01-01 UTC). Returns None on parse failure so the caller falls back
    to JXA filtering.
    """
    from datetime import datetime, timezone

    try:
        candidate = value.strip()
        if len(candidate) == 10:
            dt = datetime.strptime(candidate, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        else:
            normalized = candidate.replace(" ", "T")
            if normalized.endswith("Z"):
                normalized = normalized[:-1] + "+00:00"
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        # Apple absolute time epoch: 2001-01-01 UTC.
        apple_epoch = datetime(2001, 1, 1, tzinfo=timezone.utc).timestamp()
        return dt.timestamp() - apple_epoch
    except (ValueError, AttributeError):
        return None


def _mail_page_payload(limit: int, offset: int, messages: list, **extra: Any) -> dict:
    payload = {
        "count": len(messages),
        "messages": messages,
        "offset": offset,
        "limit": limit,
        "hasMore": len(messages) >= limit,
        "nextOffset": offset + len(messages),
    }
    payload.update(extra)
    return payload


def _annotate_mail_read_source(action: str, result: Any, source: str) -> Any:
    if action not in {"list", "search", "get-thread"} or not isinstance(result, dict):
        return result
    if result.get("source"):
        return result
    annotated = dict(result)
    annotated["source"] = source
    return annotated


def _format_index_sender(row: dict) -> Any:
    sender = row.get("sender")
    address = row.get("sender_address")
    comment = row.get("sender_comment")
    if address is None and comment is None:
        return sender
    address = address or ""
    comment = comment or ""
    if comment and address:
        return f"{comment} <{address}>"
    return address or comment or sender


def _row_to_summary(row: dict) -> dict:
    """Build a message summary from an Envelope Index row.

    Shape matches what JXA's ``messageSummary`` returns for the fields we can
    derive from the index. Fields we cannot derive (``account``, ``mailbox``
    name, recipient lists, attachments) stay as None — agents that need them
    can call ``mail_messages get`` with the message_id header to upgrade.
    """
    return {
        "id": row.get("rowid"),
        "messageId": row.get("message_id"),
        "subject": row.get("subject"),
        "sender": _format_index_sender(row),
        "read": bool(row.get("read")) if row.get("read") is not None else None,
        "flagged": bool(row.get("flagged")) if row.get("flagged") is not None else None,
        "dateReceived": _index_timestamp_to_iso(row.get("date_received")),
        "dateSent": _index_timestamp_to_iso(row.get("date_sent")),
        "mailbox": None,
        "account": None,
        "to": None,
        "cc": None,
        "bcc": None,
        "attachments": None,
        "preview": row.get("snippet"),
        "conversationId": row.get("conversation_id"),
    }


def _index_timestamp_to_iso(seconds) -> Optional[str]:
    if seconds is None:
        return None
    try:
        from datetime import datetime, timezone

        value = float(seconds)
        if value >= 1_000_000_000:
            return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
        apple_epoch = datetime(2001, 1, 1, tzinfo=timezone.utc).timestamp()
        return datetime.fromtimestamp(apple_epoch + value, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _apple_epoch_to_iso(seconds) -> Optional[str]:
    return _index_timestamp_to_iso(seconds)


_READ_ONLY_ACTIONS = {
    "list",
    "get",
    "search",
    "get-attachment",
    "get-thread",
    "get-unsubscribe-link",
}


def _is_mutating(tool_name: str, args: dict) -> bool:
    """Return True if the call would mutate state (used by read-only mode)."""
    if tool_name in {"mail_accounts", "mail_mailboxes", "calendar_calendars"}:
        return False
    if tool_name == "mail_compose":
        return True  # all compose actions create or send
    if tool_name == "mail_drafts":
        return args.get("action") not in {"list", "get"}
    if tool_name == "mail_permissions_check":
        return False
    if tool_name == "mail_analyze":
        return False
    action = args.get("action")
    if action in _READ_ONLY_ACTIONS:
        return False
    return True


def dry_run_response(tool_name: str, args: dict) -> dict:
    return {
        "dryRun": True,
        "wouldMutate": True,
        "tool": tool_name,
        "action": args.get("action"),
        "arguments": {
            key: value
            for key, value in args.items()
            if key not in {"body", "notes"}
        },
    }


def _walk_messages(value):
    """Yield message-summary-shaped dicts found anywhere in the result.

    A summary is recognised by having an integer-ish `id` plus a `mailbox`
    string. Walks one level into top-level lists/dicts (which is enough for
    every shape the JXA layer produces today).
    """
    if isinstance(value, dict):
        if (
            value.get("id") is not None
            and isinstance(value.get("mailbox"), str)
        ):
            yield value
        for nested in value.values():
            if isinstance(nested, list):
                for item in nested:
                    if isinstance(item, dict):
                        if (
                            item.get("id") is not None
                            and isinstance(item.get("mailbox"), str)
                        ):
                            yield item


def parse_calendar_event_id(event_id: str) -> dict:
    separator = "::"
    if separator not in event_id:
        raise RuntimeError(
            "calendar event ids must include both the calendar and uid. "
            "Use ids returned by calendar_events list/get/create."
        )
    calendar_name, uid = event_id.split(separator, 1)
    return {"calendar_name": unquote(calendar_name), "uid": unquote(uid)}


def format_platform_error(detail: str) -> str:
    lowered = detail.lower()
    if "not authorized to send apple events" in lowered or "(-1743)" in lowered:
        return (
            "macOS automation permission is blocked. Open System Settings > "
            "Privacy & Security > Automation and allow the host app or osascript "
            "to control Mail, Calendar, or Reminders."
        )
    if "calendar" in lowered and "not authorized" in lowered:
        return (
            "Calendar access is blocked. Open System Settings > Privacy & Security > "
            "Calendars and grant full access to the app running this server."
        )
    if "reminders" in lowered and "not authorized" in lowered:
        return (
            "Reminders access is blocked. Open System Settings > Privacy & Security > "
            "Reminders and grant access to the app running this server."
        )
    return detail
