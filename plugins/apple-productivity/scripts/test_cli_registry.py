#!/usr/bin/env python3

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

import apple_productivity_cli as cli
import apple_productivity_mcp_server as mcp_server
from apple_productivity_registry import TOOL_SPECS, mcp_tools
from shared_validation import validate_action, validate_tool_arguments


class RegistryParityTests(unittest.TestCase):
    def test_every_registry_tool_has_mcp_schema(self):
        schemas = {tool["name"]: tool for tool in mcp_tools()}
        self.assertEqual({spec.name for spec in TOOL_SPECS}, set(schemas))

    def test_registry_actions_match_mcp_enums_and_validation(self):
        schemas = {tool["name"]: tool for tool in mcp_tools()}
        for spec in TOOL_SPECS:
            action_schema = schemas[spec.name]["inputSchema"]["properties"]["action"]
            self.assertEqual(set(spec.actions), set(action_schema["enum"]))
            self.assertEqual(validate_action({"action": spec.actions[0]}, set(spec.actions)), spec.actions[0])

    def test_validation_rejects_arguments_missing_from_registry(self):
        for spec in TOOL_SPECS:
            with self.assertRaises(RuntimeError, msg=spec.name):
                validate_tool_arguments(spec.name, {"action": spec.actions[0], "__extra": True})

    def test_cli_parser_exposes_registry_tools(self):
        parser = cli.build_parser()
        for spec in TOOL_SPECS:
            namespace = parser.parse_args([spec.cli_name, spec.actions[0]])
            self.assertEqual(namespace.tool_spec.name, spec.name)

    def test_cli_maps_hyphen_flags_to_service_arguments(self):
        parser = cli.build_parser()
        namespace = parser.parse_args(
            ["mail-messages", "bulk-set-read", "--message-ids", "1", "--message-ids", "2", "--read", "true"]
        )
        tool, args = cli.namespace_to_tool_call(namespace)
        self.assertEqual(tool, "mail_messages")
        self.assertEqual(args["message_ids"], [1, 2])
        self.assertIs(args["read"], True)

    def test_cli_preserves_singular_alarm_alias(self):
        parser = cli.build_parser()
        namespace = parser.parse_args(
            ["calendar-events", "create", "--summary", "x", "--start-date", "2026-05-10T09:00:00",
             "--end-date", "2026-05-10T10:00:00", "--alarm", "-300"]
        )
        tool, args = cli.namespace_to_tool_call(namespace)
        self.assertEqual(tool, "calendar_events")
        self.assertEqual(args["alarms"], [-300.0])


class BatchAndOutputTests(unittest.TestCase):
    def test_parse_json_array_batch(self):
        calls = cli.parse_calls('[{"tool":"mail_accounts","arguments":{"action":"list"}}]')
        self.assertEqual(calls[0]["tool"], "mail_accounts")

    def test_parse_jsonl_batch(self):
        calls = cli.parse_calls(
            '{"tool":"mail_accounts","arguments":{"action":"list"}}\n'
            '{"tool":"mail_mailboxes","args":{"action":"list"}}\n'
        )
        self.assertEqual(len(calls), 2)

    def test_batch_uses_one_service_instance(self):
        class FakeService:
            def __init__(self):
                self.calls = []

            def dispatch(self, tool, args):
                self.calls.append((tool, args))
                return {"tool": tool, "calls": len(self.calls)}

        namespace = type("Namespace", (), {"path": "-", "jsonl": False})()
        with mock.patch.object(sys, "stdin", io.StringIO(
            '{"tool":"mail_accounts","arguments":{"action":"list"}}\n'
            '{"tool":"mail_mailboxes","arguments":{"action":"list"}}\n'
        )):
            service = FakeService()
            result = cli.run_batch(namespace, service)
        self.assertEqual([item["ok"] for item in result], [True, True])
        self.assertEqual(result[1]["result"]["calls"], 2)
        self.assertEqual(len(service.calls), 2)

    def test_batch_preserves_call_ids_and_fail_fast(self):
        class FakeService:
            def dispatch(self, tool, args):
                if tool == "bad":
                    raise RuntimeError("boom")
                return {"tool": tool}

        namespace = type("Namespace", (), {"path": "-", "jsonl": False, "fail_fast": True})()
        with mock.patch.object(sys, "stdin", io.StringIO(
            '{"id":"first","tool":"mail_accounts","arguments":{"action":"list"}}\n'
            '{"id":"bad-call","tool":"bad","arguments":{}}\n'
            '{"id":"never","tool":"mail_mailboxes","arguments":{"action":"list"}}\n'
        )):
            result = cli.run_batch(namespace, FakeService())
        self.assertEqual([item["id"] for item in result], ["first", "bad-call"])
        self.assertFalse(result[1]["ok"])

    def test_repl_jsonl_no_prompt_envelopes_results(self):
        class FakeService:
            def dispatch(self, tool, args):
                return {"tool": tool, "args": args}

        namespace = type("Namespace", (), {"no_prompt": True, "jsonl": True})()
        with mock.patch.object(sys, "stdin", io.StringIO(
            '{"tool":"mail_accounts","arguments":{"action":"list"}}\n'
            "exit\n"
        )), mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(cli.run_repl(namespace, FakeService()), cli.EXIT_OK)
        lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
        self.assertEqual(lines[0]["ok"], True)
        self.assertEqual(lines[0]["result"]["tool"], "mail_accounts")

    def test_compact_json_has_no_pretty_whitespace(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            cli.emit_json({"a": 1, "b": [2]}, compact=True)
        self.assertEqual(stdout.getvalue().strip(), '{"a":1,"b":[2]}')

    def test_project_metadata_includes_public_release_fields(self):
        metadata = cli.project_metadata()
        self.assertEqual(metadata["owner"], "smitjj")
        self.assertEqual(metadata["license"], "Apache-2.0")
        self.assertEqual(metadata["repository"], "https://github.com/smitjj/cli-apple-productivity")
        self.assertIn("risk", metadata["riskNotice"].lower())


class CompoundSummaryTests(unittest.TestCase):
    def test_mail_summary_counts_triage_signals(self):
        summary = cli.summarize_mail_messages(
            {
                "messages": [
                    {"read": False, "flagged": True, "attachments": [{"name": "x"}], "dateReceived": "2026-01-02"},
                    {"read": True, "flagged": False, "dateReceived": "2026-01-01"},
                ]
            }
        )
        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["unread"], 1)
        self.assertEqual(summary["flagged"], 1)
        self.assertEqual(summary["withAttachments"], 1)
        self.assertEqual(summary["oldestDateReceived"], "2026-01-01")

    def test_agenda_summary_detects_conflicts(self):
        summary = cli.summarize_agenda(
            {
                "events": [
                    {
                        "id": "a",
                        "summary": "A",
                        "startDate": "2026-01-01T09:00:00Z",
                        "endDate": "2026-01-01T10:00:00Z",
                        "allDay": False,
                    },
                    {
                        "id": "b",
                        "summary": "B",
                        "startDate": "2026-01-01T09:30:00Z",
                        "endDate": "2026-01-01T11:00:00Z",
                        "allDay": False,
                    },
                ]
            }
        )
        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["conflictCount"], 1)
        self.assertEqual(summary["conflicts"][0]["leftId"], "a")

    def test_day_plan_summary_counts_due_reminders(self):
        summary = cli.summarize_day_plan(
            "2026-01-02",
            {"events": []},
            {
                "reminders": [
                    {"dueDate": "2026-01-01T09:00:00Z", "completed": False},
                    {"dueDate": "2026-01-02T09:00:00Z", "completed": False, "flagged": True},
                ]
            },
        )
        self.assertEqual(summary["reminders"]["overdue"], 1)
        self.assertEqual(summary["reminders"]["dueToday"], 1)
        self.assertEqual(summary["reminders"]["flagged"], 1)

    def test_mail_archive_compound_maps_to_move_action(self):
        class FakeService:
            def __init__(self):
                self.calls = []

            def dispatch(self, tool, args):
                self.calls.append((tool, args))
                return {"moved": True}

        parser = cli.build_parser()
        namespace = parser.parse_args(["mail", "archive", "42", "--target-mailbox", "Processed"])
        service = FakeService()
        result = cli.run_compound(namespace, service)
        self.assertEqual(result["workflow"], "mail.archive")
        self.assertEqual(
            service.calls,
            [("mail_messages", {"action": "move", "message_id": 42, "target_mailbox": "Processed"})],
        )

        namespace = parser.parse_args(["mail", "archive", "42", "--dry-run"])
        service = FakeService()
        cli.run_compound(namespace, service)
        self.assertTrue(service.calls[0][1]["dry_run"])

    def test_mail_open_compound_maps_to_open_action(self):
        class FakeService:
            def dispatch(self, tool, args):
                return {"tool": tool, "args": args}

        parser = cli.build_parser()
        namespace = parser.parse_args(["mail", "open", "42", "--mailbox-name", "INBOX"])
        result = cli.run_compound(namespace, FakeService())
        self.assertEqual(result["workflow"], "mail.open")
        self.assertEqual(result["result"]["args"]["action"], "open")
        self.assertEqual(result["result"]["args"]["mailbox_name"], "INBOX")


class CliSubprocessTests(unittest.TestCase):
    def test_help_exits_zero(self):
        completed = subprocess.run(
            [sys.executable, cli.__file__, "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("batch", completed.stdout)
        self.assertIn("Apache-2.0", completed.stdout)
        self.assertIn("github.com/smitjj/cli-apple-productivity", completed.stdout.replace("\n", ""))

    def test_about_exposes_repo_and_license(self):
        completed = subprocess.run(
            [sys.executable, cli.__file__, "about"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["license"], "Apache-2.0")
        self.assertEqual(payload["repository"], "https://github.com/smitjj/cli-apple-productivity")

    def test_completions_do_not_touch_native_service(self):
        completed = subprocess.run(
            [sys.executable, cli.__file__, "completions", "zsh"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("#compdef apple-productivity", completed.stdout)
        self.assertIn("archive", completed.stdout)

    def test_invalid_args_use_stable_usage_exit(self):
        completed = subprocess.run(
            [sys.executable, cli.__file__, "mail-messages", "get"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, cli.EXIT_USAGE)
        self.assertIn("message_id is required", completed.stderr)

    def test_read_only_mutation_uses_usage_class_before_platform(self):
        env = dict(os.environ)
        env["APPLE_PRODUCTIVITY_READ_ONLY"] = "1"
        completed = subprocess.run(
            [sys.executable, cli.__file__, "mail-messages", "delete", "--message-id", "1"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        self.assertEqual(completed.returncode, cli.EXIT_USAGE)
        self.assertIn("Read-only mode", completed.stderr)

    def test_validation_accepts_only_uses_usage_exit(self):
        completed = subprocess.run(
            [
                sys.executable,
                cli.__file__,
                "mail-messages",
                "get-attachment",
                "--message-id",
                "1",
                "--attachment-index",
                "0",
                "--save-to",
                "/tmp/x",
                "--return-inline",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, cli.EXIT_USAGE)
        self.assertIn("accepts only", completed.stderr)

    def test_validation_requires_uses_usage_exit(self):
        completed = subprocess.run(
            [sys.executable, cli.__file__, "mail-compose", "create", "--subject", "no recipients"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, cli.EXIT_USAGE)
        self.assertIn("requires", completed.stderr)


class McpMetadataTests(unittest.TestCase):
    def test_initialize_exposes_public_release_metadata(self):
        with mock.patch.object(mcp_server, "write_message") as write_message:
            mcp_server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        response = write_message.call_args.args[0]
        server_info = response["result"]["serverInfo"]
        self.assertEqual(server_info["owner"], "smitjj")
        self.assertEqual(server_info["license"], "Apache-2.0")
        self.assertEqual(server_info["repository"], "https://github.com/smitjj/cli-apple-productivity")
        self.assertIn("risk", server_info["riskNotice"].lower())


if __name__ == "__main__":
    unittest.main()
