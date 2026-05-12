#!/usr/bin/env python3

from __future__ import annotations

import unittest

from apple_productivity_service import (
    AppleProductivityService,
    MessageScopeCache,
    _walk_messages,
)
from shared_validation import (
    is_valid_email,
    parse_date_string,
    refine_mail_search_arguments,
    validate_tool_arguments,
    validate_value,
)


class EmailValidationTests(unittest.TestCase):
    def test_accepts_simple_addresses(self):
        for address in (
            "alice@example.com",
            "j.smit@hostafrica.com",
            "a+b@example.co",
            "ALL.CAPS@SUB.EXAMPLE.IO",
        ):
            self.assertTrue(is_valid_email(address), address)

    def test_rejects_consecutive_dots(self):
        self.assertFalse(is_valid_email("a..b@example.com"))
        self.assertFalse(is_valid_email("alice@example..com"))

    def test_rejects_missing_tld(self):
        self.assertFalse(is_valid_email("alice@localhost"))
        self.assertFalse(is_valid_email("alice@."))

    def test_rejects_whitespace_and_empty(self):
        self.assertFalse(is_valid_email("alice @example.com"))
        self.assertFalse(is_valid_email(""))
        self.assertFalse(is_valid_email("   "))

    def test_rejects_missing_local_or_domain(self):
        self.assertFalse(is_valid_email("@example.com"))
        self.assertFalse(is_valid_email("alice@"))
        self.assertFalse(is_valid_email("@"))


class DateParsingTests(unittest.TestCase):
    def test_accepts_date_only(self):
        parse_date_string("2026-05-10", "field")

    def test_accepts_datetime(self):
        parse_date_string("2026-05-10T14:00:00", "field")

    def test_accepts_z_suffix(self):
        parse_date_string("2026-05-10T14:00:00Z", "field")

    def test_accepts_offset_suffix(self):
        parse_date_string("2026-05-10T14:00:00+02:00", "field")
        parse_date_string("2026-05-10T14:00:00-05:30", "field")

    def test_rejects_milliseconds(self):
        with self.assertRaises(RuntimeError):
            parse_date_string("2026-05-10T14:00:00.123", "field")
        with self.assertRaises(RuntimeError):
            parse_date_string("2026-05-10T14:00:00.123Z", "field")

    def test_rejects_space_separator(self):
        # JXA accepts a space; we standardise on T to match each other.
        with self.assertRaises(RuntimeError):
            parse_date_string("2026-05-10 14:00:00", "field")

    def test_rejects_calendar_garbage(self):
        with self.assertRaises(RuntimeError):
            parse_date_string("not-a-date", "field")
        with self.assertRaises(RuntimeError):
            parse_date_string("2026-13-01", "field")
        with self.assertRaises(RuntimeError):
            parse_date_string("", "field")


class ControlCharacterTests(unittest.TestCase):
    def test_accepts_normal_text(self):
        validate_value("subject", "Hello, world!\nSecond line\tindented")

    def test_rejects_null_byte(self):
        with self.assertRaises(RuntimeError):
            validate_value("subject", "hello\x00world")

    def test_rejects_bell(self):
        with self.assertRaises(RuntimeError):
            validate_value("subject", "alert\x07")

    def test_rejects_in_nested_list(self):
        with self.assertRaises(RuntimeError):
            validate_value("to", ["ok@example.com", "bad\x00@example.com"])


class MailComposeRecipientTests(unittest.TestCase):
    def test_create_requires_at_least_one_recipient(self):
        with self.assertRaises(RuntimeError) as ctx:
            validate_tool_arguments(
                "mail_compose",
                {"action": "create", "subject": "Hi", "body": "test"},
            )
        self.assertIn("recipient", str(ctx.exception))

    def test_create_accepts_to(self):
        validate_tool_arguments(
            "mail_compose",
            {"action": "create", "to": ["alice@example.com"], "subject": "Hi"},
        )

    def test_create_accepts_only_bcc(self):
        validate_tool_arguments(
            "mail_compose",
            {"action": "create", "bcc": ["alice@example.com"], "subject": "Hi"},
        )

    def test_reply_does_not_require_recipient(self):
        validate_tool_arguments(
            "mail_compose",
            {"action": "reply", "message_id": 42, "body": "ok"},
        )


class ActionEnumTests(unittest.TestCase):
    def test_unknown_tool_raises(self):
        with self.assertRaises(RuntimeError):
            validate_tool_arguments("nope", {"action": "list"})

    def test_unknown_action_raises(self):
        with self.assertRaises(RuntimeError):
            validate_tool_arguments("mail_messages", {"action": "obliterate"})

    def test_get_attachment_requires_choice(self):
        with self.assertRaises(RuntimeError):
            validate_tool_arguments(
                "mail_messages",
                {"action": "get-attachment", "message_id": 1, "attachment_index": 0},
            )

    def test_get_attachment_rejects_both(self):
        with self.assertRaises(RuntimeError):
            validate_tool_arguments(
                "mail_messages",
                {
                    "action": "get-attachment",
                    "message_id": 1,
                    "attachment_index": 0,
                    "save_to": "/tmp/x.pdf",
                    "return_inline": True,
                },
            )

    def test_get_attachment_rejects_relative_path(self):
        with self.assertRaises(RuntimeError):
            validate_tool_arguments(
                "mail_messages",
                {
                    "action": "get-attachment",
                    "message_id": 1,
                    "attachment_index": 0,
                    "save_to": "tmp/x.pdf",
                },
            )

    def test_get_attachment_accepts_save_to(self):
        validate_tool_arguments(
            "mail_messages",
            {
                "action": "get-attachment",
                "message_id": 1,
                "attachment_index": 0,
                "save_to": "/tmp/x.pdf",
            },
        )

    def test_get_attachment_accepts_return_inline(self):
        validate_tool_arguments(
            "mail_messages",
            {
                "action": "get-attachment",
                "message_id": 1,
                "attachment_index": 0,
                "return_inline": True,
            },
        )

    def test_search_accepts_new_filters(self):
        validate_tool_arguments(
            "mail_messages",
            {
                "action": "search",
                "query": "hello",
                "from_address": "alice@example.com",
                "subject_contains": "report",
                "since": "2026-01-01",
            },
        )

    def test_search_accepts_filter_only_query(self):
        validate_tool_arguments(
            "mail_messages",
            {
                "action": "search",
                "mailbox_name": "INBOX",
                "unread_only": True,
            },
        )

    def test_search_rejects_no_query_or_filter(self):
        with self.assertRaises(RuntimeError):
            validate_tool_arguments("mail_messages", {"action": "search"})


class IntegerValidationTests(unittest.TestCase):
    def test_rejects_boolean_for_integer(self):
        with self.assertRaises(RuntimeError):
            validate_tool_arguments(
                "mail_messages",
                {"action": "get", "message_id": True},
            )

    def test_accepts_integer(self):
        validate_tool_arguments("mail_messages", {"action": "get", "message_id": 42})

    def test_registry_bounds_apply_to_limit(self):
        with self.assertRaises(RuntimeError):
            validate_tool_arguments(
                "mail_messages",
                {"action": "list", "mailbox_name": "INBOX", "limit": 101},
            )


class CalendarFieldTests(unittest.TestCase):
    def test_create_accepts_url_and_recurrence(self):
        validate_tool_arguments(
            "calendar_events",
            {
                "action": "create",
                "summary": "Sync",
                "start_date": "2026-05-10T09:00:00",
                "end_date": "2026-05-10T09:30:00",
                "url": "https://example.com/sync",
                "recurrence": "FREQ=WEEKLY;BYDAY=MO,WE,FR",
            },
        )

    def test_update_accepts_url_and_recurrence(self):
        validate_tool_arguments(
            "calendar_events",
            {
                "action": "update",
                "event_id": "Calendar::abcd",
                "url": "https://example.com",
            },
        )


class ReminderFieldTests(unittest.TestCase):
    def test_create_accepts_priority_and_flagged(self):
        validate_tool_arguments(
            "reminders_tasks",
            {"action": "create", "title": "x", "priority": 5, "flagged": True},
        )

    def test_update_accepts_priority_and_flagged(self):
        validate_tool_arguments(
            "reminders_tasks",
            {"action": "update", "reminder_id": "x-apple-reminder://abc", "priority": 1},
        )

    def test_priority_must_be_in_range(self):
        with self.assertRaises(RuntimeError):
            validate_tool_arguments(
                "reminders_tasks",
                {"action": "create", "title": "x", "priority": 10},
            )

    def test_priority_rejects_boolean(self):
        with self.assertRaises(RuntimeError):
            validate_tool_arguments(
                "reminders_tasks",
                {"action": "create", "title": "x", "priority": True},
            )


class MessageScopeCacheTests(unittest.TestCase):
    def test_remember_and_get(self):
        cache = MessageScopeCache()
        cache.remember(42, "iCloud", "INBOX")
        self.assertEqual(cache.get(42), ("iCloud", "INBOX"))

    def test_overwrite_promotes_to_newest(self):
        cache = MessageScopeCache()
        cache.remember(1, "a", "INBOX")
        cache.remember(2, "a", "INBOX")
        cache.remember(1, "a", "Archive")
        # 1 should now be the most-recently-set; iteration order reflects that
        keys = list(cache._entries.keys())
        self.assertEqual(keys[-1], 1)

    def test_evicts_oldest_at_capacity(self):
        cache = MessageScopeCache()
        original = MessageScopeCache.MAX_ENTRIES
        try:
            MessageScopeCache.MAX_ENTRIES = 3
            cache.remember(1, "a", "X")
            cache.remember(2, "a", "X")
            cache.remember(3, "a", "X")
            cache.remember(4, "a", "X")
            self.assertIsNone(cache.get(1))
            self.assertEqual(cache.get(4), ("a", "X"))
        finally:
            MessageScopeCache.MAX_ENTRIES = original

    def test_silently_ignores_none_inputs(self):
        cache = MessageScopeCache()
        cache.remember(None, "a", "X")
        cache.remember(7, "a", None)
        self.assertEqual(len(cache), 0)


class WalkMessagesTests(unittest.TestCase):
    def test_picks_messages_from_list_response(self):
        sample = {
            "mailbox": {"name": "INBOX", "path": "INBOX", "account": "iCloud"},
            "count": 2,
            "messages": [
                {"id": 100, "mailbox": "INBOX", "account": "iCloud"},
                {"id": 101, "mailbox": "INBOX", "account": "iCloud"},
            ],
        }
        ids = sorted(m["id"] for m in _walk_messages(sample))
        self.assertEqual(ids, [100, 101])

    def test_picks_summary_from_get_response(self):
        sample = {"id": 7, "mailbox": "Sent", "account": "iCloud", "subject": "hi"}
        results = list(_walk_messages(sample))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], 7)

    def test_ignores_results_without_id_or_mailbox(self):
        sample = {"deleted": True, "messageId": 9}
        self.assertEqual(list(_walk_messages(sample)), [])


class BulkMailValidationTests(unittest.TestCase):
    def test_bulk_set_read_requires_message_ids(self):
        with self.assertRaises(RuntimeError):
            validate_tool_arguments(
                "mail_messages",
                {"action": "bulk-set-read", "read": True},
            )

    def test_bulk_set_read_requires_read_flag(self):
        with self.assertRaises(RuntimeError):
            validate_tool_arguments(
                "mail_messages",
                {"action": "bulk-set-read", "message_ids": [1, 2]},
            )

    def test_bulk_caps_at_50(self):
        with self.assertRaises(RuntimeError):
            validate_tool_arguments(
                "mail_messages",
                {"action": "bulk-delete", "message_ids": list(range(51))},
            )

    def test_bulk_accepts_within_limit(self):
        validate_tool_arguments(
            "mail_messages",
            {
                "action": "bulk-move",
                "message_ids": [1, 2, 3],
                "target_mailbox": "Archive",
                "dry_run": True,
            },
        )

    def test_bulk_rejects_non_integer_ids(self):
        with self.assertRaises(RuntimeError):
            validate_tool_arguments(
                "mail_messages",
                {"action": "bulk-delete", "message_ids": [1, "2"]},
            )

    def test_bulk_rejects_booleans_as_ids(self):
        with self.assertRaises(RuntimeError):
            validate_tool_arguments(
                "mail_messages",
                {"action": "bulk-delete", "message_ids": [True]},
            )


class MailDraftsValidationTests(unittest.TestCase):
    def test_list_does_not_require_message_id(self):
        validate_tool_arguments("mail_drafts", {"action": "list"})

    def test_get_requires_message_id(self):
        with self.assertRaises(RuntimeError):
            validate_tool_arguments("mail_drafts", {"action": "get"})

    def test_update_accepts_subject_body(self):
        validate_tool_arguments(
            "mail_drafts",
            {"action": "update", "message_id": 1, "subject": "x", "body": "y"},
        )

    def test_unknown_action_rejected(self):
        with self.assertRaises(RuntimeError):
            validate_tool_arguments("mail_drafts", {"action": "send_now"})


class MailAnalyzeValidationTests(unittest.TestCase):
    def test_triage_accepts_filters(self):
        validate_tool_arguments(
            "mail_analyze",
            {
                "action": "triage",
                "mailbox_name": "INBOX",
                "unread_only": True,
                "limit": 10,
            },
        )

    def test_newsletters_accepts_with_links(self):
        validate_tool_arguments(
            "mail_analyze",
            {"action": "newsletters", "query": "unsubscribe", "with_links": True, "limit": 10},
        )

    def test_newsletters_with_links_rejects_high_limit(self):
        with self.assertRaises(RuntimeError):
            validate_tool_arguments(
                "mail_analyze",
                {"action": "newsletters", "with_links": True, "limit": 30},
            )


class PermissionsCheckTests(unittest.TestCase):
    def test_check_action_optional(self):
        validate_tool_arguments("mail_permissions_check", {})

    def test_unknown_action_rejected(self):
        with self.assertRaises(RuntimeError):
            validate_tool_arguments("mail_permissions_check", {"action": "probe"})


class AlarmAndGeofenceTests(unittest.TestCase):
    def test_event_create_accepts_alarms(self):
        validate_tool_arguments(
            "calendar_events",
            {
                "action": "create",
                "summary": "x",
                "start_date": "2026-05-10T09:00:00",
                "end_date": "2026-05-10T09:30:00",
                "alarms": [-300, -60],
                "timezone": "Asia/Tokyo",
                "source": "icloud",
                "recurrence_rule": "FREQ=WEEKLY;BYDAY=MO",
            },
        )

    def test_alarms_rejects_non_numbers(self):
        with self.assertRaises(RuntimeError):
            validate_tool_arguments(
                "calendar_events",
                {
                    "action": "create",
                    "summary": "x",
                    "start_date": "2026-05-10T09:00:00",
                    "end_date": "2026-05-10T09:30:00",
                    "alarms": ["hello"],
                },
            )

    def test_alarms_caps_at_10(self):
        with self.assertRaises(RuntimeError):
            validate_tool_arguments(
                "calendar_events",
                {
                    "action": "create",
                    "summary": "x",
                    "start_date": "2026-05-10T09:00:00",
                    "end_date": "2026-05-10T09:30:00",
                    "alarms": [-i for i in range(11)],
                },
            )

    def test_alarms_clamped_to_seven_days(self):
        with self.assertRaises(RuntimeError):
            validate_tool_arguments(
                "calendar_events",
                {
                    "action": "create",
                    "summary": "x",
                    "start_date": "2026-05-10T09:00:00",
                    "end_date": "2026-05-10T09:30:00",
                    "alarms": [-30 * 24 * 3600],
                },
            )

    def test_geofence_requires_lat_and_lon(self):
        with self.assertRaises(RuntimeError):
            validate_tool_arguments(
                "reminders_tasks",
                {"action": "create", "title": "x", "geofence": {"lat": 35.0}},
            )

    def test_geofence_proximity_must_be_enter_or_leave(self):
        with self.assertRaises(RuntimeError):
            validate_tool_arguments(
                "reminders_tasks",
                {
                    "action": "create",
                    "title": "x",
                    "geofence": {"lat": 35.0, "lon": 139.0, "proximity": "near"},
                },
            )

    def test_geofence_accepts_full_spec(self):
        validate_tool_arguments(
            "reminders_tasks",
            {
                "action": "create",
                "title": "x",
                "geofence": {
                    "lat": 35.0,
                    "lon": 139.0,
                    "radius_meters": 200,
                    "proximity": "leave",
                    "title": "Office",
                },
            },
        )


class MailSearchRefinementTests(unittest.TestCase):
    def test_natural_language_sender_query_promotes_from_address(self):
        refined = refine_mail_search_arguments(
            {
                "action": "search",
                "query": "search for emails from werner@hostafrica.com",
            }
        )
        self.assertEqual(refined["from_address"], "werner@hostafrica.com")
        self.assertNotIn("query", refined)

    def test_subject_query_with_email_is_left_unchanged(self):
        refined = refine_mail_search_arguments(
            {
                "action": "search",
                "query": "invoice werner@hostafrica.com",
            }
        )
        self.assertEqual(refined["from_address"], "werner@hostafrica.com")
        self.assertEqual(refined["query"], "invoice werner@hostafrica.com")


class MailReadSourceTests(unittest.TestCase):
    def test_service_rejects_missing_jxa_script(self):
        from apple_productivity_service import AppleProductivityService
        from pathlib import Path

        with self.assertRaises(RuntimeError) as ctx:
            AppleProductivityService(script_path=Path("/tmp/apple-productivity-missing-jxa.js"))
        self.assertIn("Mail automation script missing", str(ctx.exception))

    def test_annotate_mail_read_source_tags_jxa_reads(self):
        from apple_productivity_service import _annotate_mail_read_source

        annotated = _annotate_mail_read_source(
            "search",
            {"count": 1, "messages": []},
            "jxa",
        )
        self.assertEqual(annotated["source"], "jxa")

    def test_annotate_mail_read_source_preserves_existing_source(self):
        from apple_productivity_service import _annotate_mail_read_source

        annotated = _annotate_mail_read_source(
            "search",
            {"count": 1, "messages": [], "source": "envelope_index"},
            "jxa",
        )
        self.assertEqual(annotated["source"], "envelope_index")


class IsMutatingTests(unittest.TestCase):
    def test_list_actions_are_read_only(self):
        from apple_productivity_service import _is_mutating
        self.assertFalse(_is_mutating("mail_messages", {"action": "list"}))
        self.assertFalse(_is_mutating("mail_messages", {"action": "search"}))
        self.assertFalse(_is_mutating("mail_messages", {"action": "get-thread"}))
        self.assertFalse(_is_mutating("mail_drafts", {"action": "list"}))
        self.assertFalse(_is_mutating("mail_permissions_check", {}))

    def test_mutating_actions_are_flagged(self):
        from apple_productivity_service import _is_mutating
        self.assertTrue(_is_mutating("mail_messages", {"action": "delete"}))
        self.assertTrue(_is_mutating("mail_messages", {"action": "bulk-move"}))
        self.assertTrue(_is_mutating("mail_compose", {"action": "create"}))
        self.assertTrue(_is_mutating("mail_drafts", {"action": "send"}))
        self.assertTrue(_is_mutating("calendar_events", {"action": "create"}))


class ReadOnlyModeTests(unittest.TestCase):
    def test_read_only_blocks_mutations(self):
        from apple_productivity_service import AppleProductivityService
        svc = AppleProductivityService(use_persistent_worker=False, read_only=True)
        with self.assertRaises(RuntimeError) as ctx:
            svc.dispatch("mail_messages", {"action": "delete", "message_id": 1})
        self.assertIn("read-only", str(ctx.exception).lower())

    def test_dry_run_returns_without_mutating(self):
        svc = AppleProductivityService(use_persistent_worker=False)
        result = svc.dispatch(
            "calendar_events",
            {"action": "delete", "event_id": "Work::abc", "dry_run": True},
        )
        self.assertEqual(result["dryRun"], True)
        self.assertEqual(result["tool"], "calendar_events")
        self.assertEqual(result["action"], "delete")

    def test_dry_run_is_available_for_compose(self):
        svc = AppleProductivityService(use_persistent_worker=False)
        result = svc.dispatch(
            "mail_compose",
            {
                "action": "create",
                "to": ["alice@example.com"],
                "subject": "Hello",
                "body": "secret body",
                "dry_run": True,
            },
        )
        self.assertTrue(result["dryRun"])
        self.assertNotIn("body", result["arguments"])

    def test_dry_run_must_be_boolean(self):
        with self.assertRaises(RuntimeError):
            validate_tool_arguments("calendar_events", {"action": "delete", "event_id": "x", "dry_run": "yes"})


class EventKitRoutingTests(unittest.TestCase):
    def test_reminder_update_uses_eventkit_when_available(self):
        class FakeBackend:
            has_event_access = False
            has_reminder_access = True

            def __init__(self):
                self.calls = []

            def update_reminder(self, args):
                self.calls.append(("update", args))
                return {"id": args["reminder_id"], "title": args["title"]}

        backend = FakeBackend()
        svc = AppleProductivityService(use_persistent_worker=False)
        svc._eventkit = backend
        svc._eventkit_probed = True
        result = svc.dispatch(
            "reminders_tasks",
            {"action": "update", "reminder_id": "abc", "title": "Updated"},
        )
        self.assertEqual(result["title"], "Updated")
        self.assertEqual(backend.calls[0][0], "update")

    def test_reminder_complete_uses_eventkit_when_available(self):
        class FakeBackend:
            has_event_access = False
            has_reminder_access = True

            def set_reminder_completed(self, reminder_id, completed):
                return {"id": reminder_id, "completed": completed}

        svc = AppleProductivityService(use_persistent_worker=False)
        svc._eventkit = FakeBackend()
        svc._eventkit_probed = True
        result = svc.dispatch("reminders_tasks", {"action": "complete", "reminder_id": "abc"})
        self.assertEqual(result, {"id": "abc", "completed": True})


class IsoEpochTests(unittest.TestCase):
    def test_iso_to_apple_epoch_round_trip(self):
        from apple_productivity_service import _iso_to_epoch_or_none, _apple_epoch_to_iso
        epoch = _iso_to_epoch_or_none("2026-05-10T12:34:56Z")
        self.assertIsNotNone(epoch)
        iso = _apple_epoch_to_iso(epoch)
        self.assertEqual(iso, "2026-05-10T12:34:56+00:00")

    def test_iso_to_epoch_handles_date_only(self):
        from apple_productivity_service import _iso_to_epoch_or_none
        self.assertIsNotNone(_iso_to_epoch_or_none("2026-05-10"))

    def test_iso_to_epoch_returns_none_on_garbage(self):
        from apple_productivity_service import _iso_to_epoch_or_none
        self.assertIsNone(_iso_to_epoch_or_none("not-a-date"))

    def test_index_timestamp_to_iso_unix(self):
        from apple_productivity_service import _index_timestamp_to_iso

        self.assertEqual(
            _index_timestamp_to_iso(1778577960),
            "2026-05-12T09:26:00+00:00",
        )

    def test_index_timestamp_to_iso_apple(self):
        from apple_productivity_service import _index_timestamp_to_iso, _iso_to_epoch_or_none

        epoch = _iso_to_epoch_or_none("2026-05-10T12:34:56Z")
        self.assertEqual(
            _index_timestamp_to_iso(epoch),
            "2026-05-10T12:34:56+00:00",
        )


class ServiceCacheIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.svc = AppleProductivityService()

    def test_list_response_populates_cache(self):
        sample = {
            "mailbox": {"name": "INBOX", "path": "INBOX", "account": "iCloud"},
            "count": 1,
            "messages": [{"id": 100, "mailbox": "INBOX", "account": "iCloud"}],
        }
        self.svc._update_scope_cache("list", {}, sample)
        self.assertEqual(self.svc.scope_cache.get(100), ("iCloud", "INBOX"))

    def test_move_updates_cache_to_target(self):
        self.svc.scope_cache.remember(100, "iCloud", "INBOX")
        self.svc._update_scope_cache(
            "move",
            {"message_id": 100, "target_account": "Gmail", "target_mailbox": "Archive"},
            {"moved": True},
        )
        self.assertEqual(self.svc.scope_cache.get(100), ("Gmail", "Archive"))

    def test_delete_evicts(self):
        self.svc.scope_cache.remember(100, "iCloud", "INBOX")
        self.svc._update_scope_cache("delete", {"message_id": 100}, {"deleted": True})
        self.assertIsNone(self.svc.scope_cache.get(100))


if __name__ == "__main__":
    unittest.main()
