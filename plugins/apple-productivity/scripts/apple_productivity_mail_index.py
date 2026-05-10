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
    "encoding",
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

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def search_messages(
        self,
        query: Optional[str] = None,
        mailbox_name: Optional[str] = None,
        account_name: Optional[str] = None,
        from_address: Optional[str] = None,
        to_address: Optional[str] = None,
        subject_contains: Optional[str] = None,
        since_epoch: Optional[float] = None,
        limit: int = 25,
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

        def like(value: str) -> str:
            return f"%{value.lower()}%"

        if query:
            # Search across subject + sender + snippet (if present).
            sub_clauses = ["LOWER(m.subject) LIKE ?", "LOWER(m.sender) LIKE ?"]
            params.extend([like(query), like(query)])
            if self.has_column("snippet"):
                sub_clauses.append("LOWER(m.snippet) LIKE ?")
                params.append(like(query))
            clauses.append("(" + " OR ".join(sub_clauses) + ")")
        if from_address:
            clauses.append("LOWER(m.sender) LIKE ?")
            params.append(like(from_address))
        if subject_contains:
            clauses.append("LOWER(m.subject) LIKE ?")
            params.append(like(subject_contains))
        if since_epoch is not None:
            clauses.append("m.date_received >= ?")
            params.append(since_epoch)
        if unread_only:
            clauses.append("m.read = 0")
        if flagged_only:
            clauses.append("m.flagged = 1")
        mailbox_filter = self._build_mailbox_filter(mailbox_name, account_name)
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

        sql_parts = [
            "SELECT m.ROWID AS rowid, m.message_id AS message_id, m.subject AS subject, "
            "m.sender AS sender, m.date_received AS date_received, m.date_sent AS date_sent, "
            "m.mailbox AS mailbox_rowid, m.read AS read, m.flagged AS flagged"
        ]
        if self.has_column("snippet"):
            sql_parts.append(", m.snippet AS snippet")
        if self.has_column("conversation_id"):
            sql_parts.append(", m.conversation_id AS conversation_id")
        sql_parts.append("FROM messages m")
        if clauses:
            sql_parts.append("WHERE " + " AND ".join(clauses))
        sql_parts.append("ORDER BY m.date_received DESC")
        sql_parts.append("LIMIT ?")
        params.append(int(max(1, min(limit, 200))))

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
        limit: int = 25,
        unread_only: bool = False,
        flagged_only: bool = False,
    ) -> dict:
        rows = self.search_messages(
            mailbox_name=mailbox_name,
            account_name=account_name,
            limit=limit,
            unread_only=unread_only,
            flagged_only=flagged_only,
        )
        return {
            "mailbox": {"name": mailbox_name, "path": mailbox_name, "account": account_name},
            "count": len(rows),
            "messages": rows,
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
        try:
            rows = self._conn.execute(
                "SELECT ROWID AS rowid, message_id, subject, sender, date_received, "
                "date_sent, mailbox AS mailbox_rowid, read, flagged "
                "FROM messages WHERE conversation_id = ? "
                "ORDER BY date_received ASC LIMIT ?",
                (seed["conversation_id"], int(max(1, min(limit, 500)))),
            ).fetchall()
        except sqlite3.Error as exc:
            raise MailIndexUnavailable(f"thread fetch failed: {exc}") from exc
        return [dict(row) for row in rows]

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
            f"WHERE r.{msg_fk} = m.ROWID AND LOWER(a.{addr_value_col}) LIKE ?)"
        )
        return clause, [f"%{to_address.lower()}%"]

    def _build_mailbox_filter(self, mailbox_name: Optional[str], account_name: Optional[str]):
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
            clauses.append("(" + " OR ".join(comparisons) + ")")
        if account_name and account_cols:
            comparisons = []
            lowered = account_name.lower()
            for col in account_cols:
                comparisons.append(f"LOWER(CAST(mb.{col} AS TEXT)) = ?")
                params.append(lowered)
            clauses.append("(" + " OR ".join(comparisons) + ")")
        elif account_name:
            raise MailIndexUnavailable("mailboxes table has no usable account column")
        return (
            "EXISTS (SELECT 1 FROM mailboxes mb WHERE mb.ROWID = m.mailbox AND "
            + " AND ".join(clauses)
            + ")",
            params,
        )

    def _list_conversation(self, conversation_id, limit: int) -> list:
        assert self._conn is not None
        select_parts = [
            "SELECT ROWID AS rowid, message_id, subject, sender, date_received, "
            "date_sent, mailbox AS mailbox_rowid, read, flagged"
        ]
        if self.has_column("snippet"):
            select_parts.append(", snippet")
        select_parts.append(", conversation_id FROM messages WHERE conversation_id = ? ORDER BY date_received ASC LIMIT ?")
        try:
            rows = self._conn.execute(
                "".join(select_parts),
                (conversation_id, int(max(1, min(limit, 500)))),
            ).fetchall()
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
