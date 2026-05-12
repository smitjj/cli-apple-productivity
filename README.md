# Apple Productivity CLI

A CLI-first local interface for Apple Mail, Apple Calendar, and Apple Reminders on macOS. The same shared service core also ships a thin stdio MCP adapter for clients that prefer MCP, but the CLI is the canonical human, shell, and agent surface.

## What it supports

- Apple Mail: account, mailbox, message, draft, reply, forward, move, delete, flag, read/unread, open, attachment download, bulk operations, thread retrieval, unsubscribe-link extraction, mailbox folder create/rename
- Apple Calendar: calendar and event read/write/delete/open with EventKit-only fields (recurrence rule, alarms, timezone, source disambiguation)
- Apple Reminders: list and task read/write/delete/complete with priority, flagged, alarms, geofence triggers
- One registry-driven CLI surface across all three apps, plus compound workflows for triage, agenda, day planning, and diagnostics
- Summary-first MCP tools such as `mail_analyze` for inbox triage and newsletter/unsubscribe scans
- A Codex plugin with bundled agent skill, starter prompts, and marketplace install metadata
- Batch and REPL modes that keep the service, JXA worker, EventKit probe, Mail index, and message scope cache warm
- A thin stdio MCP adapter generated from the same command/action registry
- A diagnostic `mail_permissions_check` tool that probes Automation, Full Disk Access, and EventKit state

## Safety notice

This project controls local Apple apps on your Mac. It can read personal Mail, Calendar, and Reminders data, and write operations can send mail, move or delete messages, create or delete calendar events, and create or complete reminders.

Use it at your own risk. Review commands before running them, prefer `--dry-run` for destructive operations, and set `APPLE_PRODUCTIVITY_READ_ONLY=1` when you want read-only behavior. The software is provided as-is under the Apache-2.0 license, without warranty.

## Installation

### Fast install (Codex)

The fastest path is the Codex plugin marketplace. It installs the bundled MCP server, agent skill, and starter prompts from this repo.

```sh
codex plugin marketplace add smitjj/cli-apple-productivity
codex plugin marketplace upgrade smitjj-apple-productivity
```

Then in Codex:

1. Open the plugin directory and select the **smitjj-apple-productivity** marketplace.
2. Install **Apple Productivity** and approve the install prompts.
3. Restart Codex so the MCP server reloads from the plugin cache.
4. Confirm the server is registered:

```sh
codex mcp get apple-productivity
```

You should see `command: /usr/bin/python3`, `args: -u ./scripts/apple_productivity_mcp_server.py`, and a `cwd` under `~/.codex/plugins/cache/smitjj-apple-productivity/apple-productivity/<version>/`.

If you already have the repo open in Codex, the workspace marketplace at `.agents/plugins/marketplace.json` can also expose the same plugin without adding the GitHub marketplace manually. After plugin changes, run `codex plugin marketplace upgrade smitjj-apple-productivity` and restart Codex.

Grant **Automation** and **Full Disk Access** to the Codex host process before large Mail searches. Run `mail_permissions_check` (or CLI `doctor`) and confirm `envelope_index.ok` when you want the fast Envelope Index path.

### CLI install

Prerequisites:

- macOS (Catalina 10.15+ recommended; tested against modern Mail container layouts `V9`–`V12`)
- Python 3.9+ (system Python at `/usr/bin/python3` works)
- Optional: `pyobjc-framework-EventKit` and `pyobjc-framework-CoreLocation` for the EventKit fast path on Calendar/Reminders writes. Without them the service transparently falls back to JXA.

  ```sh
  python3 -m pip install --user pyobjc-framework-EventKit pyobjc-framework-CoreLocation
  ```

Clone the repo:

```sh
git clone https://github.com/smitjj/cli-apple-productivity.git
cd cli-apple-productivity
```

From the repo root, use the short executable:

```sh
./apple-productivity --help
./apple-productivity about --pretty
```

Optional: put the CLI on your `PATH`:

```sh
./install.sh
apple-productivity --help
```

Make sure `~/.local/bin` is on your `PATH`.

Use `./apple-productivity` from the repo root for normal CLI work.

Optional shell completions:

```sh
mkdir -p ~/.zfunc ~/.local/share/bash-completion/completions
apple-productivity completions zsh > ~/.zfunc/_apple-productivity
apple-productivity completions bash > ~/.local/share/bash-completion/completions/apple-productivity
```

The optional MCP server entry point is:

```
<repo>/plugins/apple-productivity/scripts/apple_productivity_mcp_server.py
```

### Grant macOS permissions

The first call to any Mail/Calendar/Reminders action will trigger OS prompts. If anything is silently blocked, open `System Settings → Privacy & Security`:

- **Automation** — allow the host process (Terminal, your MCP client, or `osascript`) to control Mail, Calendar, and Reminders.
- **Calendars** / **Reminders** — grant full access to the host process (required for EventKit).
- **Full Disk Access** — required for the SQLite Envelope Index fast path on `mail_messages` list/search. Falls back to JXA scan if denied.

Run the built-in probe to see which permissions are currently granted:

```sh
./apple-productivity mail-permissions-check
```

### Use the CLI first

```sh
./apple-productivity mail-accounts list
./apple-productivity mail-messages list --mailbox-name INBOX --limit 5 --pretty
./apple-productivity mail triage --unread-only --limit 10
./apple-productivity mail open 12345
./apple-productivity mail archive 12345 --dry-run
./apple-productivity calendar agenda --days 7
./apple-productivity day plan
./apple-productivity doctor
```

CLI output is compact JSON by default for agent token efficiency. Use `--pretty` for human-readable JSON; `--raw` remains an alias for compact output.

`./apple-productivity about` returns machine-readable project metadata including the repository, owner, Apache-2.0 license, and safety notice.

For multiple calls in one warm process:

```sh
printf '%s\n' \
  '{"tool":"mail_accounts","arguments":{"action":"list"}}' \
  '{"tool":"calendar_events","arguments":{"action":"list","date_from":"2026-05-10","date_to":"2026-05-11"}}' \
  | ./apple-productivity batch --jsonl
```

For an interactive warm session:

```sh
./apple-productivity repl
```

For agent-readable REPL output without prompts:

```sh
./apple-productivity repl --jsonl --no-prompt
```

### Optional: connect from your MCP client

Pick the client you use. Prefer project-scoped config with relative paths when your client supports it. Use absolute paths only for global config files, where the client's working directory may not be this repo.

#### Claude Code (CLI)

This repo already includes a project-scoped `.mcp.json`, so Claude Code picks it up automatically when you `cd` into `plugins/apple-productivity`.

```json
{
  "mcpServers": {
    "apple-productivity": {
      "command": "python3",
      "args": ["./scripts/apple_productivity_mcp_server.py"],
      "cwd": "."
    }
  }
}
```

To register globally so it works from any directory:

```sh
REPO="/absolute/path/to/cli-apple-productivity"
claude mcp add apple-productivity \
  python3 "$REPO/plugins/apple-productivity/scripts/apple_productivity_mcp_server.py"
```

#### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "apple-productivity": {
      "command": "python3",
      "args": ["/absolute/path/to/cli-apple-productivity/plugins/apple-productivity/scripts/apple_productivity_mcp_server.py"]
    }
  }
}
```

Restart Claude Desktop after saving.

#### Codex app / Codex CLI (OpenAI)

Prefer the [fast install](#fast-install-codex) steps above. The Codex plugin bundles `plugins/apple-productivity/.codex-plugin/plugin.json`, `plugins/apple-productivity/skills/`, and `plugins/apple-productivity/.mcp.json`.

Manual `~/.codex/config.toml` registration is only needed outside the marketplace flow:

```toml
[mcp_servers.apple-productivity]
command = "/usr/bin/python3"
args = ["-u", "/absolute/path/to/cli-apple-productivity/plugins/apple-productivity/scripts/apple_productivity_mcp_server.py"]
env = { PYTHONUNBUFFERED = "1" }
```

For project-local config, use `plugins/apple-productivity/.mcp.json` with `cwd` set to the plugin root.

#### Gemini CLI (Google)

Edit `~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "apple-productivity": {
      "command": "python3",
      "args": ["/absolute/path/to/cli-apple-productivity/plugins/apple-productivity/scripts/apple_productivity_mcp_server.py"]
    }
  }
}
```

#### Anything else MCP-compliant

The server speaks MCP JSON-RPC 2.0 over stdio. It accepts newline-delimited JSON and `Content-Length`-framed messages and replies in the same format (protocol version `2024-11-05`). Any client that follows the spec works — point its `mcpServers` block at `/usr/bin/python3 -u /absolute/path/to/cli-apple-productivity/plugins/apple-productivity/scripts/apple_productivity_mcp_server.py` with `PYTHONUNBUFFERED=1`.

The MCP `initialize` response includes `serverInfo.repository`, `serverInfo.license`, `serverInfo.owner`, and a short risk notice.

### Verify

Run the CLI doctor first:

```sh
./apple-productivity doctor
```

The structured output identifies which subsystem (Automation, EventKit, Full Disk Access, Envelope Index, Mail.app process) is blocked. If using MCP, then ask the agent something like *"list my Apple Mail accounts"* to exercise the simplest read path.

## Tool surface

Each tool has a single `action` field. Below, **bold** args are required for that action.

### `mail_accounts`

| action | args | returns |
| --- | --- | --- |
| `list` | – | array of `{name, userName, fullName, enabled}` |

### `mail_mailboxes`

| action | args | returns |
| --- | --- | --- |
| `list` | `account_name?`, `include_counts?` | per-account `{account, mailboxes[]}` |
| `create` | **`account_name`**, **`mailbox_name`**, `parent_mailbox?`, `dry_run?` | `{created, mailbox{name, path, account}}` |
| `rename` | **`account_name`**, **`mailbox_name`**, **`name`**, `dry_run?` | `{renamed, mailbox{name, path, account}}` |

### `mail_messages`

| action | args | returns |
| --- | --- | --- |
| `list` | **`mailbox_name`**, `account_name?`, `limit?`, `offset?`, `unread_only?`, `flagged_only?` | `{mailbox?, count, messages[], offset, limit, hasMore, nextOffset, source?}` |
| `get` | **`message_id`**, `account_name?`, `mailbox_name?`, `include_source?` | full message summary |
| `search` | one or more of `query`, `from_address`, `to_address`, `subject_contains`, `since`, `mailbox_name`, `account_name`, `unread_only`, `flagged_only`, plus `limit?`, `offset?` | `{query?, count, messages[], offset, limit, hasMore, nextOffset, source?}` |
| `move` | **`message_id`**, **`target_mailbox`**, `target_account?`, `account_name?`, `mailbox_name?`, `dry_run?` | `{moved, …}` |
| `delete` | **`message_id`**, `account_name?`, `mailbox_name?`, `dry_run?` | `{deleted, …}` |
| `set-read` | **`message_id`**, **`read`**, `account_name?`, `mailbox_name?` | `{updated, read}` |
| `set-flag` | **`message_id`**, **`flagged`**, `account_name?`, `mailbox_name?` | `{updated, flagged}` |
| `open` | **`message_id`**, `account_name?`, `mailbox_name?` | `{opened, …}` |
| `get-attachment` | **`message_id`**, **`attachment_index`**, one of (**`save_to`** absolute path or **`return_inline: true`**), `account_name?`, `mailbox_name?` | `{saved, path, …}` or `{contentBase64, attachment{…}}` |
| `get-thread` | **`message_id`**, `account_name?`, `mailbox_name?`, `limit?` | `{count, messages[]}` — sibling messages by subject + In-Reply-To/References |
| `get-unsubscribe-link` | **`message_id`**, `account_name?`, `mailbox_name?` | `{found, urls[], mailtos[], oneClickPost}` parsed from `List-Unsubscribe` headers |
| `bulk-set-read` / `bulk-set-flag` | **`message_ids`** (max 50), **`read`** or **`flagged`**, `dry_run?`, `account_name?`, `mailbox_name?` | `{succeeded, failed, dryRun, results[]}` |
| `bulk-move` | **`message_ids`**, **`target_mailbox`**, `target_account?`, `dry_run?` | per-id success map |
| `bulk-delete` | **`message_ids`**, `dry_run?` | per-id success map |

`account_name` and `mailbox_name` are optional scoping hints on every action. Pass them when you know where the message lives — they skip the global mailbox scan and surface a clear "not found in mailbox X" error instead.

`search` accepts filter-only calls, such as `--mailbox-name INBOX --unread-only`, but requires at least one query or filter. For sender lookups, prefer `from_address` or an email-only `query`. `limit` defaults to 25 and caps at 100; use `offset` and `nextOffset` to page. When the Envelope Index path is used, payloads include `"source": "envelope_index"`; JXA fallbacks use `"source": "jxa"`.

### `mail_analyze`

| action | args | returns |
| --- | --- | --- |
| `triage` | `mailbox_name?`, `account_name?`, `query?`, `since?`, `limit?`, `unread_only?`, `flagged_only?` | `{workflow, summary, result, source?}` |
| `newsletters` | `query?`, `limit?`, `with_links?` | `{workflow, summary, candidates, source?}` — `with_links` is capped at 25 per call |

These mirror the CLI compound commands `mail triage` and `mail newsletters` and return summary-first payloads for agents.

`dry_run` is accepted on mutating tools and returns a `{dryRun, wouldMutate, tool, action, arguments}` preview without calling Mail, Calendar, or Reminders.

### `mail_compose`

| action | args | returns |
| --- | --- | --- |
| `create` | one or more of **`to`/`cc`/`bcc`**, `subject?`, `body?`, `open_in_mail?`, `send_now?`, `dry_run?` | `{sent, action, message{…}}` |
| `reply` | **`message_id`**, `body?`, `reply_all?`, `open_in_mail?`, `send_now?`, `dry_run?` | `{sent, action, message{…}}` |
| `forward` | **`message_id`**, **`to`**`/cc?/bcc?`, `body?`, `open_in_mail?`, `send_now?`, `dry_run?` | `{sent, action, message{…}}` |

### `calendar_calendars`

| action | args | returns |
| --- | --- | --- |
| `list` | `include_counts?` | array of `{id, name}` |

### `calendar_events`

| action | args | returns |
| --- | --- | --- |
| `list` | `calendar_name?`, `search?`, `date_from?`, `date_to?`, `limit?` | `{count, events[]}` |
| `get` | **`event_id`** | event summary |
| `create` | **`summary`**, **`start_date`**, **`end_date`**, `calendar_name?`, `location?`, `notes?`, `all_day?`, `dry_run?` | event summary |
| `update` | **`event_id`**, plus any field above | event summary |
| `delete` | **`event_id`**, `dry_run?` | `{deleted, eventId}` |
| `open` | **`event_id`** | `{opened, eventId}` |

`create` and `update` also accept `url` (homepage/meeting link) and `recurrence` (RFC 5545 RRULE string, e.g. `FREQ=WEEKLY;BYDAY=MO,WE,FR`). Both fields are surfaced in the event summary returned by `list`/`get`.

When the EventKit backend is active (PyObjC available, permission granted) `create` and `update` additionally accept `recurrence_rule` (parsed RRULE applied as a structured rule), `timezone` (IANA name, e.g. `Asia/Tokyo`), `alarms` (array of seconds offsets, negative = before start), and `source` (filter calendars by source title — `icloud`, `google`, `exchange`, `local`, or any substring).

### `reminders_lists`

| action | args | returns |
| --- | --- | --- |
| `list` | `include_counts?` | array of `{id, name}` |
| `create` | **`name`**, `dry_run?` | list summary |
| `update` | **`list_id`**, **`name`** | list summary |
| `delete` | **`list_id`**, `dry_run?` | `{deleted, listId}` |

### `reminders_tasks`

| action | args | returns |
| --- | --- | --- |
| `list` | `list_name?`, `search?`, `show_completed?`, `limit?` | `{count, reminders[]}` |
| `get` | **`reminder_id`** | reminder summary |
| `create` | **`title`**, `list_name?`, `notes?`, `due_date?`, `dry_run?` | reminder summary |
| `update` | **`reminder_id`**, plus any field above, `completed?` | reminder summary |
| `delete` | **`reminder_id`**, `dry_run?` | `{deleted, reminderId}` |
| `complete` / `incomplete` | **`reminder_id`** | reminder summary |

`create` and `update` also accept `priority` (integer 0–9; 0=none, 1=high, 5=medium, 9=low) and `flagged` (boolean). Both are surfaced in the reminder summary.

When the EventKit backend is active, `create` and `update` additionally accept `alarms` (array of seconds offsets), `geofence` (`{lat, lon, radius_meters?, proximity? "enter"|"leave", title?}`), and `source` for list disambiguation.

### `mail_drafts`

| action | args | returns |
| --- | --- | --- |
| `list` | `account_name?`, `limit?` | `{count, drafts[]}` |
| `get` | **`message_id`**, `account_name?`, `mailbox_name?` | full draft summary |
| `update` | **`message_id`**, `subject?`, `body?` | updated draft summary |
| `send` | **`message_id`** | `{sent, messageId}` |
| `delete` | **`message_id`** | `{deleted, messageId}` |

### `mail_permissions_check`

| action | args | returns |
| --- | --- | --- |
| `check` (default) | – | `{automation, full_disk_access, envelope_index, mail_running, calendar, reminders}` — each subkey is `{ok, error}`; `envelope_index` also includes `path` when available |

## Date format

All date arguments accept one of:

- `YYYY-MM-DD` — e.g. `2026-05-10`
- `YYYY-MM-DDTHH:MM:SS` — e.g. `2026-05-10T14:00:00`
- `YYYY-MM-DDTHH:MM:SS(Z|±HH:MM)` — e.g. `2026-05-10T14:00:00Z`

Milliseconds and other ISO variants are rejected so Python and JXA agree on what is valid.

## CLI

The CLI wraps the same service that backs the MCP server. No JSON-RPC, no subprocess hop beyond `osascript` itself.

```sh
./apple-productivity mail-accounts list
./apple-productivity about --pretty
./apple-productivity mail-mailboxes list --account-name iCloud --include-counts
./apple-productivity mail-mailboxes create --account-name iCloud --mailbox-name Reports
./apple-productivity mail-mailboxes rename --account-name iCloud --mailbox-name Reports --name "Reports 2026"
./apple-productivity mail-messages list --mailbox-name INBOX --limit 5
./apple-productivity mail-messages search --query invoice --since 2026-01-01
./apple-productivity mail-compose create --to alice@example.com --subject Hi --body hello
./apple-productivity calendar-events create --calendar-name Work \
    --summary Standup --start-date 2026-05-11T09:00:00 --end-date 2026-05-11T09:30:00
./apple-productivity reminders-tasks list --list-name Personal
```

Output is compact JSON by default; `--pretty` switches to indented JSON. Errors print to stderr and use stable non-zero exit classes: `2` usage/validation, `3` permission, `4` not found, and `5` platform/automation failure.

Compound commands reduce agent round trips:

```sh
./apple-productivity mail triage --unread-only --limit 10
./apple-productivity mail newsletters --with-links
./apple-productivity mail thread 12345
./apple-productivity calendar agenda --days 7
./apple-productivity day plan
./apple-productivity doctor
```

## Permissions troubleshooting

The first time the plugin runs, macOS should prompt you to allow automation of `Mail`, `Calendar`, and `Reminders` from the host process running Codex or `osascript`.

Calendar and Reminders may also prompt for EventKit permissions. On newer macOS versions, make sure the host app has full access where needed.

If something is blocked:

- `System Settings` → `Privacy & Security` → `Automation` — allow the host to control Mail, Calendar, and Reminders.
- `System Settings` → `Privacy & Security` → `Calendars` and `Reminders` — grant full access to the host.

Errors mentioning `(-1743)` or "not authorized to send Apple events" always come from this Automation pane.

## Limits and tunables

- osascript timeout: 30 seconds by default. Override with `APPLE_PRODUCTIVITY_TIMEOUT_SECONDS=60` (any positive integer).
- Inline attachment size cap: 5 MB. Larger attachments must use `save_to`.
- Mail body cap: 50 000 characters.
- Diagnostic log: set `APPLE_PRODUCTIVITY_LOG=/tmp/ap.log` to record one line per JXA call (tool, action, duration, ok/error).
- Message-id scope cache: 256 most recent message ids stay in-memory per service instance (one MCP session, one `batch`/`repl` session, or one single CLI invocation). Repeat targeted actions on the same message — `get`, `set-read`, `set-flag`, `delete`, `open`, `get-attachment` — skip the global mailbox scan in JXA. The cache evicts on `delete` and updates on `move`; if a cached scope is stale (message moved out-of-band), the service automatically retries once with no hint.
- Persistent JXA worker: a single long-lived `osascript` subprocess is reused across tool calls, eliminating the ~150–250 ms cold start. Disable with `APPLE_PRODUCTIVITY_PERSISTENT_JXA=0`; on two consecutive worker failures the service automatically falls back to one-shot for the remainder of the session.
- Mail Envelope Index fast path: `mail_messages` list/search opens `~/Library/Mail/V*/MailData/Envelope Index` read-only and answers from SQLite (~50 ms vs minutes for JXA scan on large mailboxes). Probed once per session; falls back silently to JXA if Full Disk Access is missing or the schema does not match. Disable with `APPLE_PRODUCTIVITY_MAIL_INDEX=0`. Result payloads include `"source": "envelope_index"` when this path is used and `"source": "jxa"` on fallback.
- EventKit backend: when PyObjC is installed and permission is granted, Calendar `create`/`update`/`delete` and Reminders `create`/`delete` route through EventKit instead of JXA — retiring the AppleScript-delete fallback and unlocking the new `recurrence_rule`, `timezone`, `alarms`, `geofence`, and `source` fields. Failures fall back to JXA transparently. Disable with `APPLE_PRODUCTIVITY_EVENTKIT=0`.
- Read-only mode: `APPLE_PRODUCTIVITY_READ_ONLY=1` rejects every action that mutates state (compose, delete, set-*, move, bulk-*, draft updates, calendar/reminder writes). `mail_permissions_check` and all read paths still work.

## Verification

- `python3 -m unittest discover plugins/apple-productivity/scripts -p 'test_*.py' -v` — pure-Python unit tests, no macOS apps required.
- `python3 plugins/apple-productivity/scripts/cli_smoke_test.py` — destructive end-to-end CLI test. Covers compact/pretty output, validation exits, `batch`, `repl`, Mail reads, and Calendar/Reminders CRUD. Skips cleanly when Automation permission is denied.
- `python3 plugins/apple-productivity/scripts/smoke_test.py` — destructive end-to-end MCP test of read paths plus Calendar and Reminders CRUD. Skips cleanly when Automation permission is denied.

## Notes

- Apple Mail automation is ultimately limited by what Mail exposes to AppleScript/JXA.
- Licensed under the [Apache License 2.0](LICENSE).
- Maintained by [smitjj](https://github.com/smitjj).
- The architectural direction is documented in [plugins/apple-productivity/ARCHITECTURE.md](plugins/apple-productivity/ARCHITECTURE.md).
