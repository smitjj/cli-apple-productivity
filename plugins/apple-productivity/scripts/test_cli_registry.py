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


if __name__ == "__main__":
    unittest.main()
