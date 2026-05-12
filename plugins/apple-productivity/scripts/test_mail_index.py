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


class MailIndexAddressFkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "Envelope Index"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE addresses (
              ROWID INTEGER PRIMARY KEY,
              address TEXT,
              comment TEXT
            );
            CREATE TABLE messages (
              ROWID INTEGER PRIMARY KEY,
              message_id TEXT,
              subject TEXT,
              sender INTEGER,
              date_sent REAL,
              date_received REAL,
              mailbox INTEGER,
              read INTEGER,
              flagged INTEGER
            );
            """
        )
        conn.executemany(
            "INSERT INTO addresses (ROWID, address, comment) VALUES (?, ?, ?)",
            [(1, "werner@hostafrica.com", "Werner Moller")],
        )
        conn.execute(
            """
            INSERT INTO messages
              (ROWID, message_id, subject, sender, date_sent, date_received, mailbox, read, flagged)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (100, "<a@example.com>", "Re: Churn & Cohort Analysis", 1, 1, 3, 1, 0, 0),
        )
        conn.commit()
        conn.close()
        self.reader = MailIndexReader(self.db_path)
        self.reader._connect()
        self.reader._probe_schema()

    def tearDown(self):
        self.reader.close()
        self.tmp.cleanup()

    def test_sender_name_and_address_search(self):
        rows = self.reader.search_messages(query="Werner", limit=10)
        self.assertEqual([row["rowid"] for row in rows], [100])
        rows = self.reader.search_messages(from_address="werner@hostafrica.com", limit=10)
        self.assertEqual([row["rowid"] for row in rows], [100])


class MailIndexBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "Envelope Index"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE addresses (
              ROWID INTEGER PRIMARY KEY,
              address TEXT,
              comment TEXT
            );
            CREATE TABLE mailboxes (
              ROWID INTEGER PRIMARY KEY,
              url TEXT
            );
            CREATE TABLE messages (
              ROWID INTEGER PRIMARY KEY,
              message_id TEXT,
              subject_prefix TEXT,
              subject TEXT,
              sender INTEGER,
              date_sent REAL,
              date_received REAL,
              mailbox INTEGER,
              read INTEGER,
              flagged INTEGER,
              deleted INTEGER
            );
            """
        )
        conn.executemany(
            "INSERT INTO addresses (ROWID, address, comment) VALUES (?, ?, ?)",
            [
                (1, "werner@hostafrica.com", "Werner Moller"),
                (2, "werner@hostafrica.co.za", "Werner Moller"),
            ],
        )
        conn.execute(
            "INSERT INTO mailboxes (ROWID, url) VALUES (1, 'imap://account/INBOX')"
        )
        conn.executemany(
            """
            INSERT INTO messages
              (ROWID, message_id, subject_prefix, subject, sender, date_sent, date_received, mailbox, read, flagged, deleted)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (100, "<a@example.com>", "Re:", "Churn", 1, 1, 3, 1, 0, 0, 0),
                (101, "<b@example.com>", "", "Deleted", 2, 2, 4, 1, 0, 0, 1),
                (102, "<c@example.com>", "", "Later", 2, 3, 5, 1, 0, 0, 0),
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

    def test_subject_prefix_is_searchable(self):
        rows = self.reader.search_messages(query="churn", limit=10)
        self.assertEqual([row["rowid"] for row in rows], [100])

    def test_deleted_messages_are_excluded(self):
        rows = self.reader.search_messages(query="deleted", limit=10)
        self.assertEqual(rows, [])

    def test_from_address_matches_domain_variants(self):
        rows = self.reader.search_messages(from_address="werner@hostafrica.com", limit=10)
        self.assertEqual({row["rowid"] for row in rows}, {100, 102})

    def test_offset_skips_matching_rows(self):
        rows = self.reader.search_messages(query="werner", limit=1, offset=1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rowid"], 100)


class MailIndexQueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "Envelope Index"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE mailboxes (
              ROWID INTEGER PRIMARY KEY,
              url TEXT
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
              automated_conversation INTEGER
            );
            """
        )
        conn.execute(
            "INSERT INTO mailboxes (ROWID, url) VALUES (1, 'imap://3AE968BD-19BC-44B5-A14D-39791781DE37/INBOX')"
        )
        conn.executemany(
            """
            INSERT INTO messages
              (ROWID, message_id, subject, sender, date_sent, date_received, mailbox, read, flagged, automated_conversation)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (100, "<a@example.com>", "Human", "alice@example.com", 1, 3, 1, 0, 0, 0),
                (101, "<b@example.com>", "Ambiguous", "bob@example.com", 1, 4, 1, 0, 0, 1),
                (102, "<c@example.com>", "Automated", "noreply@example.com", 1, 5, 1, 0, 0, 2),
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

    def test_describe_reports_capabilities(self):
        payload = self.reader.describe_index()
        self.assertIn("automated_conversation", payload["capabilities"]["aggregate"]["groupBy"])
        self.assertIn("count", payload["capabilities"]["aggregate"]["measures"])

    def test_aggregate_groups_by_signal(self):
        payload = self.reader.aggregate_messages(
            group_by=["automated_conversation"],
            measures=["count"],
            mailbox_name="INBOX",
            account_url_hints=["3AE968BD-19BC-44B5-A14D-39791781DE37"],
        )
        counts = {
            int(row["automated_conversation"]): int(row["count"])
            for row in payload["rows"]
        }
        self.assertEqual(counts, {0: 1, 1: 1, 2: 1})

    def test_sample_returns_rows(self):
        payload = self.reader.sample_messages(
            columns=["rowid", "subject", "automated_conversation"],
            mailbox_name="INBOX",
            account_url_hints=["3AE968BD-19BC-44B5-A14D-39791781DE37"],
            limit=2,
        )
        self.assertEqual(payload["count"], 2)
        self.assertEqual(len(payload["rows"]), 2)


class MailIndexAutomationClassifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "Envelope Index"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE mailboxes (
              ROWID INTEGER PRIMARY KEY,
              url TEXT
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
              automated_conversation INTEGER
            );
            """
        )
        conn.execute(
            "INSERT INTO mailboxes (ROWID, url) VALUES (1, 'imap://3AE968BD-19BC-44B5-A14D-39791781DE37/INBOX')"
        )
        conn.executemany(
            """
            INSERT INTO messages
              (ROWID, message_id, subject, sender, date_sent, date_received, mailbox, read, flagged, automated_conversation)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (100, "<a@example.com>", "Human", "alice@example.com", 1, 3, 1, 0, 0, 0),
                (101, "<b@example.com>", "Ambiguous", "bob@example.com", 1, 4, 1, 0, 0, 1),
                (102, "<c@example.com>", "Automated", "noreply@example.com", 1, 5, 1, 0, 0, 2),
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

    def test_classify_received_aggregate_counts_signals(self):
        payload = self.reader.classify_received_aggregate(
            mailbox_name="INBOX",
            account_url_hints=["3AE968BD-19BC-44B5-A14D-39791781DE37"],
        )
        counts = {int(row["signal"]): int(row["count"]) for row in payload["signals"]}
        self.assertEqual(counts, {0: 1, 1: 1, 2: 1})


class MailIndexAccountScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "Envelope Index"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE mailboxes (
              ROWID INTEGER PRIMARY KEY,
              url TEXT
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
              flagged INTEGER
            );
            """
        )
        conn.executemany(
            "INSERT INTO mailboxes (ROWID, url) VALUES (?, ?)",
            [
                (1, "imap://3AE968BD-19BC-44B5-A14D-39791781DE37/INBOX"),
                (2, "imap://030F0A3B-8BC7-4356-ABDC-A5D275BFE4B6/INBOX"),
            ],
        )
        conn.executemany(
            """
            INSERT INTO messages
              (ROWID, message_id, subject, sender, date_sent, date_received, mailbox, read, flagged)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (100, "<a@example.com>", "Host Africa", "alice@example.com", 1, 3, 1, 0, 0),
                (101, "<b@example.com>", "Other account", "bob@example.com", 1, 4, 2, 0, 0),
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

    def test_account_url_hints_scope_mailbox_list(self):
        rows = self.reader.search_messages(
            mailbox_name="INBOX",
            account_name="Host Africa",
            account_url_hints=["3AE968BD-19BC-44B5-A14D-39791781DE37"],
            limit=10,
        )
        self.assertEqual([row["rowid"] for row in rows], [100])


class MailIndexUnixDateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "Envelope Index"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE messages (
              ROWID INTEGER PRIMARY KEY,
              message_id TEXT,
              subject TEXT,
              sender TEXT,
              date_sent REAL,
              date_received REAL,
              mailbox INTEGER,
              read INTEGER,
              flagged INTEGER
            );
            """
        )
        conn.executemany(
            """
            INSERT INTO messages
              (ROWID, message_id, subject, sender, date_sent, date_received, mailbox, read, flagged)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (100, "<a@example.com>", "Newer", "werner@hostafrica.com", 1, 1778577960, 1, 0, 0),
                (101, "<b@example.com>", "Older", "werner@hostafrica.com", 1, 1770000000, 1, 0, 0),
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

    def test_probe_detects_unix_dates(self):
        self.assertTrue(self.reader._date_stored_as_unix)

    def test_since_filter_uses_unix_epoch(self):
        since_epoch = self.reader.iso_to_index_epoch("2026-05-12")
        rows = self.reader.search_messages(
            from_address="werner@hostafrica.com",
            since_epoch=since_epoch,
            limit=10,
        )
        self.assertEqual([row["rowid"] for row in rows], [100])


if __name__ == "__main__":
    unittest.main()
