#!/usr/bin/env python3
"""Read-only access to Mail.app's Envelope Index SQLite database.

Mail.app maintains an FTS5-backed SQLite index at
``~/Library/Mail/V{9,10,11,12}/MailData/Envelope Index``. Reading it directly
is dramatically faster than iterating mailboxes via JXA — competitor
benchmarks report ~50ms vs 8+ minutes on real mailboxes for full-text search.

The schema is undocumented and varies between macOS releases. This module
treats the index as best-effort: if probing finds the expected tables and
columns, it is used; otherwise the caller falls back to JXA.

Requires Full Disk Access for the host process. We open the DB in read-only
mode with ``immutable=1`` so SQLite never tries to acquire write locks (Mail
is normally holding the WAL).
"""

from __future__ import annotations

import glob
import logging
import os
import plistlib
import sqlite3
from pathlib import Path
from typing import Any, Optional


# Columns we rely on. Probed at availability check time. Any missing column
# disables the SQLite path for that capability and the service falls back.
_REQUIRED_MESSAGES_COLUMNS = {
    "ROWID",
    "message_id",
    "subject",
    "sender",
    "date_sent",
    "date_received",
    "mailbox",
    "read",
    "flagged",
}

# Optional columns; their absence degrades features but does not disable the
# index entirely.
_OPTIONAL_MESSAGES_COLUMNS = {
    "conversation_id",
    "size",
    "snippet",
    "summary",
    "encoding",
}

_UNIX_TIMESTAMP_THRESHOLD = 1_000_000_000
_INDEX_FILTER_OPS = frozenset({"eq", "ne", "lt", "lte", "gt", "gte", "like"})
_INDEX_AGGREGATE_MEASURES = {
    "count": ("COUNT(*)", "count"),
    "min_date_received": ("MIN(m.date_received)", "min_date_received"),
    "max_date_received": ("MAX(m.date_received)", "max_date_received"),
}
_INDEX_GROUP_BY_COLUMNS = {
    "automated_conversation": "m.automated_conversation",
    "read": "m.read",
    "flagged": "m.flagged",
    "mailbox": "m.mailbox",
    "conversation_id": "m.conversation_id",
}
_INDEX_FILTER_COLUMNS = {
    "automated_conversation": ("m.automated_conversation", False),
    "read": ("m.read", False),
    "flagged": ("m.flagged", False),
    "mailbox": ("m.mailbox", False),
    "conversation_id": ("m.conversation_id", False),
    "date_received": ("m.date_received", False),
    "date_sent": ("m.date_sent", False),
    "sender_address": ("LOWER(sender_addr.address)", True),
    "sender_comment": ("LOWER(COALESCE(sender_addr.comment, ''))", True),
}
_INDEX_SAMPLE_COLUMNS = {
    "rowid": "m.ROWID AS rowid",
    "message_id": "m.message_id AS message_id",
    "subject": "m.subject AS subject",
    "sender": None,
    "date_received": "m.date_received AS date_received",
    "date_sent": "m.date_sent AS date_sent",
    "mailbox_rowid": "m.mailbox AS mailbox_rowid",
    "read": "m.read AS read",
    "flagged": "m.flagged AS flagged",
    "automated_conversation": "m.automated_conversation AS automated_conversation",
    "conversation_id": "m.conversation_id AS conversation_id",
    "snippet": None,
    "sender_address": "sender_addr.address AS sender_address",
    "sender_comment": "sender_addr.comment AS sender_comment",
}


class MailIndexUnavailable(RuntimeError):
    """Raised when the Envelope Index cannot be opened or its schema does not
    match expectations. Caller should fall back to JXA."""


class MailIndexReader:
    """Read-only connection to Mail.app's Envelope Index.

    Construct via :meth:`open_default` for the auto-detected index path.
    """

    def __init__(
        self,
        db_path: Path,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.db_path = db_path
        self.logger = logger
        self._conn: Optional[sqlite3.Connection] = None
        self._messages_columns: set = set()
        self._table_names: set = set()
        self._sender_address_fk = False
        self._date_stored_as_unix = False

    # ------------------------------------------------------------------
    # Construction / availability
    # ------------------------------------------------------------------

    @classmethod
    def open_default(cls, logger: Optional[logging.Logger] = None) -> Optional["MailIndexReader"]:
        """Locate the newest Envelope Index under ``~/Library/Mail/V*/MailData``
        and return an opened reader. Returns ``None`` if not found or not
        accessible — caller should fall back."""
        candidates = sorted(
            glob.glob(os.path.expanduser("~/Library/Mail/V*/MailData/Envelope Index")),
            key=_version_key,
            reverse=True,
        )
        if not candidates:
            if logger:
                logger.info("Envelope Index not found under ~/Library/Mail/V*/MailData")
            return None
        for candidate in candidates:
            reader = cls(Path(candidate), logger=logger)
            try:
                reader._connect()
                reader._probe_schema()
            except (sqlite3.Error, MailIndexUnavailable, OSError) as exc:
                if logger:
                    logger.info("Envelope Index probe failed for %s: %s", candidate, exc)
                reader.close()
                continue
            return reader
        return None

    def _connect(self) -> None:
        # Read-only, immutable=1 prevents SQLite from creating WAL/SHM files
        # alongside Mail's own. URI form lets us pass these flags.
        uri = f"file:{self.db_path}?mode=ro&immutable=1"
        self._conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        self._conn.row_factory = sqlite3.Row

    def _probe_schema(self) -> None:
        """Sanity-check that the DB has the tables and columns we depend on."""
        assert self._conn is not None
        # Must have a `messages` table at minimum.
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('messages','subjects','addresses','recipients','mailboxes','attachments')"
        ).fetchall()
        self._table_names = {row["name"] for row in rows}
        if "messages" not in self._table_names:
            raise MailIndexUnavailable("messages table not found")

        col_rows = self._conn.execute("PRAGMA table_info('messages')").fetchall()
        self._messages_columns = {row["name"] for row in col_rows}
        missing = _REQUIRED_MESSAGES_COLUMNS - self._messages_columns
        if missing:
            raise MailIndexUnavailable(f"messages table missing columns: {sorted(missing)}")
        col_types = {row["name"]: (row["type"] or "").upper() for row in col_rows}
        if "addresses" in self._table_names:
            address_cols = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info('addresses')").fetchall()
            }
            self._sender_address_fk = "address" in address_cols and col_types.get("sender") == "INTEGER"
        self._probe_date_storage()

    def _probe_date_storage(self) -> None:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT date_received FROM messages WHERE date_received IS NOT NULL "
            "ORDER BY date_received DESC LIMIT 1"
        ).fetchone()
        if row and row[0] is not None and float(row[0]) >= _UNIX_TIMESTAMP_THRESHOLD:
            self._date_stored_as_unix = True

    # ------------------------------------------------------------------
    # Lifecycle / introspection
    # ------------------------------------------------------------------

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

    def has_column(self, name: str) -> bool:
        return name in self._messages_columns

    def has_table(self, name: str) -> bool:
        return name in self._table_names

    def iso_to_index_epoch(self, value: str) -> Optional[float]:
        unix = _iso_to_unix_or_none(value)
        if unix is None:
            return None
        if self._date_stored_as_unix:
            return unix
        from datetime import datetime, timezone

        apple_epoch = datetime(2001, 1, 1, tzinfo=timezone.utc).timestamp()
        return unix - apple_epoch

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def search_messages(
        self,
        query: Optional[str] = None,
        mailbox_name: Optional[str] = None,
        account_name: Optional[str] = None,
        account_url_hints: Optional[list[str]] = None,
        from_address: Optional[str] = None,
        to_address: Optional[str] = None,
        subject_contains: Optional[str] = None,
        since_epoch: Optional[float] = None,
        limit: int = 25,
        offset: int = 0,
        unread_only: bool = False,
        flagged_only: bool = False,
    ) -> list:
        """Return up to ``limit`` candidate matches, newest-first.

        Each row is a dict with: rowid, message_id, subject, sender,
        date_received_epoch, mailbox_rowid, read, flagged, snippet (if
        available). The caller can use ``message_id`` (the RFC 5322
        Message-ID header) to fetch the full record via JXA.
        """
        assert self._conn is not None
        clauses = []
        params: list = []

        if query:
            self._append_text_search(clauses, params, query)
        if from_address:
            self._append_from_address_search(clauses, params, from_address)
        if subject_contains:
            self._append_subject_search(clauses, params, subject_contains)
        if since_epoch is not None:
            clauses.append("m.date_received >= ?")
            params.append(since_epoch)
        if unread_only:
            clauses.append("m.read = 0")
        if flagged_only:
            clauses.append("m.flagged = 1")
        self._append_active_message_filters(clauses)
        mailbox_filter = self._build_mailbox_filter(
            mailbox_name,
            account_name,
            account_url_hints=account_url_hints,
        )
        if mailbox_filter is not None:
            clauses.append(mailbox_filter[0])
            params.extend(mailbox_filter[1])

        # Note: to_address requires joining recipients/addresses; only enable
        # if both tables are present. Falls back to JXA-side filtering otherwise.
        if to_address:
            extra = self._build_recipient_subquery(to_address)
            if extra is not None:
                clauses.append(extra[0])
                params.extend(extra[1])
            else:
                # Caller asked to filter by to_address but the schema does not
                # let us do it efficiently — refuse this index path so JXA
                # handles it.
                raise MailIndexUnavailable("to_address filter requires recipients/addresses tables")

        sql_parts = self._select_message_sql_parts()
        sender_join = self._sender_join_sql()
        if sender_join:
            sql_parts.append(sender_join)
        if clauses:
            sql_parts.append("WHERE " + " AND ".join(clauses))
        sql_parts.append("ORDER BY m.date_received DESC")
        sql_parts.append("LIMIT ? OFFSET ?")
        params.append(int(max(1, min(limit, 200))))
        params.append(int(max(0, offset)))

        sql = "\n".join(sql_parts)
        try:
            rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise MailIndexUnavailable(f"search query failed: {exc}") from exc
        return [dict(row) for row in rows]

    def list_messages(
        self,
        mailbox_name: str,
        account_name: Optional[str] = None,
        account_url_hints: Optional[list[str]] = None,
        limit: int = 25,
        offset: int = 0,
        unread_only: bool = False,
        flagged_only: bool = False,
    ) -> dict:
        rows = self.search_messages(
            mailbox_name=mailbox_name,
            account_name=account_name,
            account_url_hints=account_url_hints,
            limit=limit,
            offset=offset,
            unread_only=unread_only,
            flagged_only=flagged_only,
        )
        return {
            "mailbox": {"name": mailbox_name, "path": mailbox_name, "account": account_name},
            "count": len(rows),
            "messages": rows,
        }

    def describe_index(self) -> dict:
        assert self._conn is not None
        tables = sorted(self._table_names)
        message_columns = []
        for row in self._conn.execute("PRAGMA table_info('messages')").fetchall():
            name = row["name"]
            roles: list[str] = []
            if name in _INDEX_GROUP_BY_COLUMNS:
                roles.append("group")
            if name in _INDEX_FILTER_COLUMNS:
                roles.append("filter")
            if name in _INDEX_SAMPLE_COLUMNS:
                roles.append("sample")
            message_columns.append(
                {
                    "name": name,
                    "type": row["type"],
                    "roles": roles,
                }
            )
        group_by = [
            name
            for name in _INDEX_GROUP_BY_COLUMNS
            if name in self._messages_columns
        ]
        filters = [
            name
            for name in _INDEX_FILTER_COLUMNS
            if name in self._messages_columns
            or (name in {"sender_address", "sender_comment"} and self._sender_address_fk)
        ]
        sample_columns = [
            name
            for name in _INDEX_SAMPLE_COLUMNS
            if name in self._messages_columns
            or name in {"rowid", "sender", "snippet", "sender_address", "sender_comment"}
        ]
        return {
            "indexPath": str(self.db_path),
            "tables": tables,
            "messages": {
                "columns": message_columns,
            },
            "capabilities": {
                "aggregate": {
                    "groupBy": group_by,
                    "measures": list(_INDEX_AGGREGATE_MEASURES),
                    "filters": filters,
                    "filterOps": sorted(_INDEX_FILTER_OPS),
                },
                "sample": {
                    "columns": sample_columns,
                    "filters": filters,
                    "filterOps": sorted(_INDEX_FILTER_OPS),
                    "maxLimit": 50,
                },
            },
            "dateStoredAsUnix": self._date_stored_as_unix,
            "senderAddressFk": self._sender_address_fk,
        }

    def aggregate_messages(
        self,
        group_by: Optional[list[str]] = None,
        measures: Optional[list[str]] = None,
        filters: Optional[list[dict]] = None,
        mailbox_name: Optional[str] = None,
        account_name: Optional[str] = None,
        account_url_hints: Optional[list[str]] = None,
        since_epoch: Optional[float] = None,
        unread_only: bool = False,
        flagged_only: bool = False,
    ) -> dict:
        assert self._conn is not None
        group_by = list(group_by or [])
        if not group_by:
            raise MailIndexUnavailable("aggregate requires at least one group_by column")
        measures = list(measures or ["count"])
        clauses: list[str] = []
        params: list = []
        self._append_scope_filters(
            clauses,
            params,
            mailbox_name=mailbox_name,
            account_name=account_name,
            account_url_hints=account_url_hints,
            since_epoch=since_epoch,
            unread_only=unread_only,
            flagged_only=flagged_only,
        )
        needs_sender_join = self._append_query_filters(clauses, params, filters or [])
        group_exprs = [self._resolve_group_expr(name) for name in group_by]
        select_parts = [f"{expr} AS {name}" for name, expr in zip(group_by, group_exprs)]
        for measure in measures:
            sql_expr, alias = self._resolve_measure(measure)
            select_parts.append(f"{sql_expr} AS {alias}")
        sql_parts = [
            "SELECT " + ", ".join(select_parts),
            "FROM messages m",
        ]
        sender_join = self._sender_join_sql() if needs_sender_join else ""
        if sender_join:
            sql_parts.append(sender_join)
        if clauses:
            sql_parts.append("WHERE " + " AND ".join(clauses))
        sql_parts.append("GROUP BY " + ", ".join(group_exprs))
        sql_parts.append("ORDER BY " + ", ".join(group_exprs))
        try:
            rows = self._conn.execute("\n".join(sql_parts), params).fetchall()
        except sqlite3.Error as exc:
            raise MailIndexUnavailable(f"aggregate query failed: {exc}") from exc
        return {
            "scope": self._index_scope_payload(
                mailbox_name,
                account_name,
                since_epoch,
                unread_only,
                flagged_only,
            ),
            "groupBy": group_by,
            "measures": measures,
            "rows": [dict(row) for row in rows],
        }

    def sample_messages(
        self,
        columns: Optional[list[str]] = None,
        filters: Optional[list[dict]] = None,
        mailbox_name: Optional[str] = None,
        account_name: Optional[str] = None,
        account_url_hints: Optional[list[str]] = None,
        since_epoch: Optional[float] = None,
        unread_only: bool = False,
        flagged_only: bool = False,
        limit: int = 10,
        offset: int = 0,
    ) -> dict:
        assert self._conn is not None
        selected_columns = self._resolve_sample_columns(columns)
        clauses: list[str] = []
        params: list = []
        self._append_scope_filters(
            clauses,
            params,
            mailbox_name=mailbox_name,
            account_name=account_name,
            account_url_hints=account_url_hints,
            since_epoch=since_epoch,
            unread_only=unread_only,
            flagged_only=flagged_only,
        )
        needs_sender_join = self._append_query_filters(clauses, params, filters or [])
        select_parts = self._sample_select_parts(selected_columns)
        sql_parts = ["SELECT " + ", ".join(select_parts), "FROM messages m"]
        sender_join = self._sender_join_sql()
        if sender_join and (needs_sender_join or self._sender_address_fk):
            sql_parts.append(sender_join)
        if clauses:
            sql_parts.append("WHERE " + " AND ".join(clauses))
        sql_parts.append("ORDER BY m.date_received DESC")
        sql_parts.append("LIMIT ? OFFSET ?")
        params.append(int(max(1, min(limit, 50))))
        params.append(int(max(0, offset)))
        try:
            rows = self._conn.execute("\n".join(sql_parts), params).fetchall()
        except sqlite3.Error as exc:
            raise MailIndexUnavailable(f"sample query failed: {exc}") from exc
        return {
            "scope": self._index_scope_payload(
                mailbox_name,
                account_name,
                since_epoch,
                unread_only,
                flagged_only,
            ),
            "columns": selected_columns,
            "count": len(rows),
            "rows": [dict(row) for row in rows],
        }

    def classify_received_aggregate(
        self,
        mailbox_name: Optional[str] = None,
        account_name: Optional[str] = None,
        account_url_hints: Optional[list[str]] = None,
        since_epoch: Optional[float] = None,
        unread_only: bool = False,
        flagged_only: bool = False,
    ) -> dict:
        payload = self.aggregate_messages(
            group_by=["automated_conversation"],
            measures=["count"],
            mailbox_name=mailbox_name,
            account_name=account_name,
            account_url_hints=account_url_hints,
            since_epoch=since_epoch,
            unread_only=unread_only,
            flagged_only=flagged_only,
        )
        signals = []
        for row in payload["rows"]:
            signal = row.get("automated_conversation")
            count = row.get("count")
            if signal is not None and count is not None:
                signals.append({"signal": signal, "count": count})
        return {
            "scope": payload.get("scope"),
            "signals": signals,
        }

    def list_thread(self, message_id_header: str, limit: int = 100) -> list:
        """Return all messages in the same conversation as the given Message-ID
        header. Requires the optional ``conversation_id`` column."""
        assert self._conn is not None
        if not self.has_column("conversation_id"):
            raise MailIndexUnavailable("messages.conversation_id not present in this schema")
        try:
            seed = self._conn.execute(
                "SELECT conversation_id FROM messages WHERE message_id = ? LIMIT 1",
                (message_id_header,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise MailIndexUnavailable(f"thread lookup failed: {exc}") from exc
        if not seed or seed["conversation_id"] is None:
            return []
        return self._list_conversation(seed["conversation_id"], limit)

    def list_thread_by_rowid(self, rowid: int, limit: int = 100) -> list:
        """Return all messages in the same conversation as a Mail message rowid."""
        assert self._conn is not None
        if not self.has_column("conversation_id"):
            raise MailIndexUnavailable("messages.conversation_id not present in this schema")
        try:
            seed = self._conn.execute(
                "SELECT conversation_id FROM messages WHERE ROWID = ? LIMIT 1",
                (rowid,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise MailIndexUnavailable(f"thread rowid lookup failed: {exc}") from exc
        if not seed or seed["conversation_id"] is None:
            return []
        return self._list_conversation(seed["conversation_id"], limit)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _select_message_sql_parts(self) -> list[str]:
        parts = [
            "SELECT m.ROWID AS rowid, m.message_id AS message_id, m.subject AS subject, "
            f"{self._sender_select_expr()} AS sender, "
            "m.date_received AS date_received, m.date_sent AS date_sent, "
            "m.mailbox AS mailbox_rowid, m.read AS read, m.flagged AS flagged"
        ]
        if self._sender_address_fk:
            parts.append(
                ", sender_addr.address AS sender_address, sender_addr.comment AS sender_comment"
            )
        if self.has_column("snippet"):
            parts.append(", m.snippet AS snippet")
        elif self.has_column("summary"):
            parts.append(", m.summary AS snippet")
        if self.has_column("conversation_id"):
            parts.append(", m.conversation_id AS conversation_id")
        parts.append("FROM messages m")
        return parts

    def _append_active_message_filters(self, clauses: list) -> None:
        if self.has_column("deleted"):
            clauses.append("m.deleted = 0")

    def _index_scope_payload(
        self,
        mailbox_name: Optional[str],
        account_name: Optional[str],
        since_epoch: Optional[float],
        unread_only: bool,
        flagged_only: bool,
    ) -> dict:
        return {
            "mailbox": mailbox_name,
            "account": account_name,
            "sinceEpoch": since_epoch,
            "unreadOnly": unread_only,
            "flaggedOnly": flagged_only,
        }

    def _append_scope_filters(
        self,
        clauses: list,
        params: list,
        mailbox_name: Optional[str] = None,
        account_name: Optional[str] = None,
        account_url_hints: Optional[list[str]] = None,
        since_epoch: Optional[float] = None,
        unread_only: bool = False,
        flagged_only: bool = False,
    ) -> None:
        if since_epoch is not None:
            clauses.append("m.date_received >= ?")
            params.append(since_epoch)
        if unread_only:
            clauses.append("m.read = 0")
        if flagged_only:
            clauses.append("m.flagged = 1")
        self._append_active_message_filters(clauses)
        mailbox_filter = self._build_mailbox_filter(
            mailbox_name,
            account_name,
            account_url_hints=account_url_hints,
        )
        if mailbox_filter is not None:
            clauses.append(mailbox_filter[0])
            params.extend(mailbox_filter[1])

    def _append_query_filters(self, clauses: list, params: list, filters: list[dict]) -> bool:
        needs_sender_join = False
        for item in filters:
            if not isinstance(item, dict):
                raise MailIndexUnavailable("filters must be objects")
            column = item.get("column")
            op = str(item.get("op", "eq")).lower()
            if op not in _INDEX_FILTER_OPS:
                raise MailIndexUnavailable(f"unsupported filter op: {op}")
            if column not in _INDEX_FILTER_COLUMNS:
                raise MailIndexUnavailable(f"unsupported filter column: {column}")
            expr, join_sender = _INDEX_FILTER_COLUMNS[column]
            if join_sender and not self._sender_address_fk:
                raise MailIndexUnavailable(f"filter column unavailable: {column}")
            needs_sender_join = needs_sender_join or join_sender
            if column not in self._messages_columns and column not in {"sender_address", "sender_comment"}:
                raise MailIndexUnavailable(f"filter column unavailable: {column}")
            value = self._coerce_filter_value(column, op, item.get("value"))
            if op == "eq":
                clauses.append(f"{expr} = ?")
                params.append(value)
            elif op == "ne":
                clauses.append(f"{expr} != ?")
                params.append(value)
            elif op == "lt":
                clauses.append(f"{expr} < ?")
                params.append(value)
            elif op == "lte":
                clauses.append(f"{expr} <= ?")
                params.append(value)
            elif op == "gt":
                clauses.append(f"{expr} > ?")
                params.append(value)
            elif op == "gte":
                clauses.append(f"{expr} >= ?")
                params.append(value)
            else:
                clauses.append(f"{expr} LIKE ?")
                params.append(value)
        return needs_sender_join

    def _coerce_filter_value(self, column: str, op: str, value):
        if value is None:
            raise MailIndexUnavailable(f"filter value is required for {column}")
        if column in {"date_received", "date_sent"} and op != "like":
            if isinstance(value, str):
                epoch = self.iso_to_index_epoch(value)
                if epoch is None:
                    raise MailIndexUnavailable(f"invalid date filter for {column}")
                return epoch
        if column in {"sender_address", "sender_comment"} and op == "like" and isinstance(value, str):
            return value.lower()
        if column in {"sender_address", "sender_comment"} and isinstance(value, str):
            return value.lower()
        return value

    def _resolve_group_expr(self, name: str) -> str:
        if name not in _INDEX_GROUP_BY_COLUMNS:
            raise MailIndexUnavailable(f"unsupported group_by column: {name}")
        if name not in self._messages_columns:
            raise MailIndexUnavailable(f"group_by column unavailable: {name}")
        return _INDEX_GROUP_BY_COLUMNS[name]

    def _resolve_measure(self, name: str) -> tuple[str, str]:
        if name not in _INDEX_AGGREGATE_MEASURES:
            raise MailIndexUnavailable(f"unsupported measure: {name}")
        return _INDEX_AGGREGATE_MEASURES[name]

    def _resolve_sample_columns(self, columns: Optional[list[str]]) -> list[str]:
        if not columns:
            defaults = ["rowid", "subject", "sender", "date_received", "read", "flagged"]
            return [name for name in defaults if self._sample_column_available(name)]
        selected: list[str] = []
        for name in columns:
            if name not in _INDEX_SAMPLE_COLUMNS:
                raise MailIndexUnavailable(f"unsupported sample column: {name}")
            if not self._sample_column_available(name):
                raise MailIndexUnavailable(f"sample column unavailable: {name}")
            if name not in selected:
                selected.append(name)
        return selected

    def _sample_column_available(self, name: str) -> bool:
        if name in {"rowid", "sender", "snippet"}:
            return True
        if name in {"sender_address", "sender_comment"}:
            return self._sender_address_fk
        if name == "snippet":
            return self.has_column("snippet") or self.has_column("summary")
        return name in self._messages_columns

    def _sample_select_parts(self, columns: list[str]) -> list[str]:
        parts: list[str] = []
        for name in columns:
            if name == "sender":
                parts.append(f"{self._sender_select_expr()} AS sender")
                continue
            if name == "snippet":
                if self.has_column("snippet"):
                    parts.append("m.snippet AS snippet")
                elif self.has_column("summary"):
                    parts.append("m.summary AS snippet")
                continue
            parts.append(_INDEX_SAMPLE_COLUMNS[name])
        return parts

    def _subject_search_expr(self) -> str:
        if self.has_column("subject_prefix"):
            return "LOWER(TRIM(COALESCE(m.subject_prefix, '') || COALESCE(m.subject, '')))"
        return "LOWER(m.subject)"

    def _append_subject_search(self, clauses: list, params: list, value: str) -> None:
        clauses.append(f"{self._subject_search_expr()} LIKE ?")
        params.append(f"%{value.lower()}%")

    def _address_search_patterns(self, value: str) -> list[str]:
        patterns = [f"%{value.lower()}%"]
        if "@" in value:
            local, domain = value.lower().rsplit("@", 1)
            if local and domain and "." in domain:
                stem = domain.rsplit(".", 1)[0]
                if stem:
                    patterns.append(f"%{local}@{stem}.%")
        unique: list[str] = []
        for pattern in patterns:
            if pattern not in unique:
                unique.append(pattern)
        return unique

    def _append_from_address_search(self, clauses: list, params: list, from_address: str) -> None:
        sender_clauses: list[str] = []
        sender_params: list = []
        for needle in self._address_search_patterns(from_address):
            if self._sender_address_fk:
                sender_clauses.append(
                    "(LOWER(sender_addr.address) LIKE ? OR LOWER(COALESCE(sender_addr.comment,'')) LIKE ?)"
                )
                sender_params.extend([needle, needle])
            else:
                sender_clauses.append("LOWER(m.sender) LIKE ?")
                sender_params.append(needle)
            sender_clauses.append(f"{self._subject_search_expr()} LIKE ?")
            sender_params.append(needle)
            body_expr = self._body_search_expr()
            if body_expr:
                sender_clauses.append(f"{body_expr} LIKE ?")
                sender_params.append(needle)
        clauses.append("(" + " OR ".join(sender_clauses) + ")")
        params.extend(sender_params)

    def _sender_join_sql(self) -> str:
        if self._sender_address_fk:
            return "LEFT JOIN addresses sender_addr ON sender_addr.ROWID = m.sender"
        return ""

    def _sender_select_expr(self) -> str:
        if self._sender_address_fk:
            return (
                "CASE WHEN COALESCE(sender_addr.comment, '') != '' "
                "THEN TRIM(sender_addr.comment || ' <' || sender_addr.address || '>') "
                "ELSE sender_addr.address END"
            )
        return "m.sender"

    def _body_search_expr(self) -> Optional[str]:
        if self.has_column("snippet"):
            return "LOWER(m.snippet)"
        if self.has_column("summary"):
            return "LOWER(m.summary)"
        return None

    def _append_text_search(self, clauses: list, params: list, query: str) -> None:
        needle = f"%{query.lower()}%"
        sub_clauses = [f"{self._subject_search_expr()} LIKE ?"]
        sub_params = [needle]
        body_expr = self._body_search_expr()
        if body_expr:
            sub_clauses.append(f"{body_expr} LIKE ?")
            sub_params.append(needle)
        if self._sender_address_fk:
            sub_clauses.append("LOWER(sender_addr.address) LIKE ?")
            sub_clauses.append("LOWER(COALESCE(sender_addr.comment,'')) LIKE ?")
            sub_params.extend([needle, needle])
        else:
            sub_clauses.append("LOWER(m.sender) LIKE ?")
            sub_params.append(needle)
        clauses.append("(" + " OR ".join(sub_clauses) + ")")
        params.extend(sub_params)

    def _build_recipient_subquery(self, to_address: str):
        """Return (clause, params) for filtering by recipient address, or None
        if the recipients/addresses tables are not present."""
        assert self._conn is not None
        if not {"recipients", "addresses"}.issubset(self._table_names):
            return None
        # Schemas vary: `recipients` typically links message_id (FK to
        # messages.ROWID) to address_id (FK to addresses.ROWID). We probe
        # available columns to build a robust query.
        rec_cols = {row["name"] for row in self._conn.execute("PRAGMA table_info('recipients')").fetchall()}
        addr_cols = {row["name"] for row in self._conn.execute("PRAGMA table_info('addresses')").fetchall()}
        msg_fk = "message_id" if "message_id" in rec_cols else ("message" if "message" in rec_cols else None)
        addr_fk = "address_id" if "address_id" in rec_cols else ("address" if "address" in rec_cols else None)
        addr_value_col = "address" if "address" in addr_cols else ("email" if "email" in addr_cols else None)
        if not (msg_fk and addr_fk and addr_value_col):
            return None
        clause = (
            f"EXISTS (SELECT 1 FROM recipients r "
            f"JOIN addresses a ON a.ROWID = r.{addr_fk} "
            f"WHERE r.{msg_fk} = m.ROWID AND (LOWER(a.{addr_value_col}) LIKE ? "
            f"OR LOWER(COALESCE(a.comment,'')) LIKE ?))"
        )
        needle = f"%{to_address.lower()}%"
        return clause, [needle, needle]

    def _build_mailbox_filter(
        self,
        mailbox_name: Optional[str],
        account_name: Optional[str],
        account_url_hints: Optional[list[str]] = None,
    ):
        if not mailbox_name and not account_name:
            return None
        if not self.has_table("mailboxes"):
            raise MailIndexUnavailable("mailbox/account filters require mailboxes table")
        assert self._conn is not None
        mailbox_cols = {row["name"] for row in self._conn.execute("PRAGMA table_info('mailboxes')").fetchall()}
        name_cols = [name for name in ("name", "display_name", "url", "path") if name in mailbox_cols]
        account_cols = [name for name in ("account", "account_name", "source", "store") if name in mailbox_cols]
        if mailbox_name and not name_cols:
            raise MailIndexUnavailable("mailboxes table has no usable name column")

        clauses = []
        params: list = []
        if mailbox_name:
            comparisons = []
            lowered = mailbox_name.lower()
            for col in name_cols:
                comparisons.append(f"LOWER(CAST(mb.{col} AS TEXT)) = ?")
                params.append(lowered)
                comparisons.append(f"LOWER(CAST(mb.{col} AS TEXT)) LIKE ?")
                params.append(f"%/{lowered}")
                comparisons.append(f"LOWER(CAST(mb.{col} AS TEXT)) LIKE ?")
                params.append(f"%/{lowered}/%")
                comparisons.append(f"LOWER(CAST(mb.{col} AS TEXT)) LIKE ?")
                params.append(f"%{lowered}%")
            clauses.append("(" + " OR ".join(comparisons) + ")")
        if account_name:
            comparisons = []
            lowered = account_name.lower()
            for col in account_cols:
                comparisons.append(f"LOWER(CAST(mb.{col} AS TEXT)) = ?")
                params.append(lowered)
                comparisons.append(f"LOWER(CAST(mb.{col} AS TEXT)) LIKE ?")
                params.append(f"%{lowered}%")
            if account_url_hints:
                for hint in account_url_hints:
                    hint_l = hint.lower()
                    if not hint_l:
                        continue
                    comparisons.append("LOWER(CAST(mb.url AS TEXT)) LIKE ?")
                    params.append(f"%{hint_l}%")
            elif name_cols and not account_cols:
                for col in name_cols:
                    comparisons.append(f"LOWER(CAST(mb.{col} AS TEXT)) LIKE ?")
                    params.append(f"%{lowered}%")
            if not comparisons:
                raise MailIndexUnavailable("mailboxes table has no usable account column")
            clauses.append("(" + " OR ".join(comparisons) + ")")
        return (
            "EXISTS (SELECT 1 FROM mailboxes mb WHERE mb.ROWID = m.mailbox AND "
            + " AND ".join(clauses)
            + ")",
            params,
        )

    def _list_conversation(self, conversation_id, limit: int) -> list:
        assert self._conn is not None
        clauses = ["m.conversation_id = ?"]
        params: list = [conversation_id]
        self._append_active_message_filters(clauses)
        sql_parts = self._select_message_sql_parts()
        sender_join = self._sender_join_sql()
        if sender_join:
            sql_parts.append(sender_join)
        sql_parts.append("WHERE " + " AND ".join(clauses))
        sql_parts.append("ORDER BY m.date_received ASC")
        sql_parts.append("LIMIT ?")
        params.append(int(max(1, min(limit, 500))))
        try:
            rows = self._conn.execute("\n".join(sql_parts), params).fetchall()
        except sqlite3.Error as exc:
            raise MailIndexUnavailable(f"thread fetch failed: {exc}") from exc
        return [dict(row) for row in rows]


def _version_key(path: str) -> tuple:
    """Sort key that prefers the newest Mail container (V11 > V10 > V9)."""
    try:
        chunk = path.split("/Library/Mail/V", 1)[1].split("/", 1)[0]
        return (int(chunk),)
    except (IndexError, ValueError):
        return (0,)


def _iso_to_unix_or_none(value: str) -> Optional[float]:
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
        return dt.timestamp()
    except (ValueError, AttributeError):
        return None


def account_url_hints_from_accounts_map(db_path: Path, match_tokens: list[str]) -> list[str]:
    plist_path = db_path.parent / "Signatures" / "AccountsMap.plist"
    if not plist_path.is_file():
        return []
    try:
        with plist_path.open("rb") as handle:
            payload = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return []
    hints: list[str] = []
    tokens = [token.lower() for token in match_tokens if token]
    if not tokens:
        return []
    for account_id, metadata in payload.items():
        if not isinstance(metadata, dict):
            continue
        account_url = str(metadata.get("AccountURL", "")).lower()
        if account_url and any(token in account_url for token in tokens):
            hints.append(str(account_id))
    return hints
