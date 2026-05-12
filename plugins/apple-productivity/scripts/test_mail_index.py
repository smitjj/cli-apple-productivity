#!/usr/bin/env python3

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from apple_productivity_mail_index import MailIndexReader


class MailIndexReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "Envelope Index"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE mailboxes (
              ROWID INTEGER PRIMARY KEY,
              name TEXT,
              path TEXT,
              account TEXT
            );
            CREATE TABLE messages (
              ROWID INTEGER PRIMARY KEY,
              message_id TEXT,
              subject TEXT,
              sender TEXT,
              date_sent REAL,
              date_received REAL,
              mailbox INTEGER,
              read INTEGER,
              flagged INTEGER,
              conversation_id INTEGER,
              snippet TEXT
            );
            """
        )
        conn.executemany(
            "INSERT INTO mailboxes (ROWID, name, path, account) VALUES (?, ?, ?, ?)",
            [(1, "INBOX", "INBOX", "iCloud"), (2, "Archive", "Archive", "iCloud")],
        )
        conn.executemany(
            """
            INSERT INTO messages
              (ROWID, message_id, subject, sender, date_sent, date_received, mailbox, read, flagged, conversation_id, snippet)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (100, "<a@example.com>", "Launch plan", "alice@example.com", 1, 3, 1, 0, 1, 77, "alpha"),
                (101, "<b@example.com>", "Re: Launch plan", "bob@example.com", 2, 4, 1, 1, 0, 77, "bravo"),
                (102, "<c@example.com>", "Other", "c@example.com", 3, 5, 2, 0, 0, 88, "charlie"),
            ],
        )
        conn.commit()
        conn.close()
        self.reader = MailIndexReader(self.db_path)
        self.reader._connect()
        self.reader._probe_schema()

    def tearDown(self):
        self.reader.close()
        self.tmp.cleanup()

    def test_scoped_list_uses_mailbox_and_state_filters(self):
        payload = self.reader.list_messages("INBOX", account_name="iCloud", unread_only=True, flagged_only=True)
        self.assertEqual([row["rowid"] for row in payload["messages"]], [100])

    def test_search_can_filter_without_query(self):
        rows = self.reader.search_messages(mailbox_name="Archive", unread_only=True)
        self.assertEqual([row["rowid"] for row in rows], [102])

    def test_from_address_matches_sender_subject_or_snippet(self):
        rows = self.reader.search_messages(from_address="alice@example.com", limit=10)
        self.assertEqual([row["rowid"] for row in rows], [100])

    def test_thread_lookup_by_rowid(self):
        rows = self.reader.list_thread_by_rowid(100)
        self.assertEqual([row["rowid"] for row in rows], [100, 101])


if __name__ == "__main__":
    unittest.main()
