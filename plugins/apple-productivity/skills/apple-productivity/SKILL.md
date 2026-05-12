---
name: apple-productivity
description: >-
  Use the Apple Productivity MCP server and CLI for Mail, Calendar, and Reminders
  on macOS. Apply for inbox triage, newsletter analysis, search, drafting,
  calendar and reminder tasks, and when macOS Apple app automation is requested.
---

# Apple Productivity

## Agent guidance

- Use Apple Productivity only for Mail, Calendar, and Reminders work on macOS.
- Prefer summary-first tools before raw message dumps:
  - `mail_analyze` with `action: "triage"` for inbox triage (CLI: `mail triage`).
  - `mail_analyze` with `action: "newsletters"` for newsletter/unsubscribe candidates (CLI: `mail newsletters`).
- Prefer `mail_messages` **search** with scoped filters over unbounded **list** calls. For sender lookups, pass `from_address` (or an email-only `query`); do not rely on natural-language `query` text alone.
- Do not invoke raw `osascript`, JXA, or other direct Mail automation outside this plugin.
- If mail reads are slow, empty, or failing, run `mail_permissions_check` (CLI: `doctor`) before escalating or falling back to other approaches.
- Respect per-call limits (`limit` max 100; `mail_analyze` with `with_links` max 25); narrow the query instead of pulling a full mailbox in one step.
- There is no `offset` or cursor on `mail_messages` list/search yet. To walk a large mailbox, narrow with `since`, `query`, sender/subject filters, or mailbox scope; stop when a page returns fewer than `limit` rows or when `summary` counts stop changing.
- Read `summary` first on `mail_analyze` responses; use `result` or `candidates` only when you need specific message ids or details.
- On mail list/search/triage/newsletters payloads, top-level `"source": "envelope_index"` means Mail's Envelope Index handled the read; `"source": "jxa"` means the plugin fell back to JXA. If `source` is missing on a read payload, treat it as JXA.
- Run `mail_permissions_check` (CLI: `doctor`) when reads are slow or empty. Check `full_disk_access.ok` and `envelope_index.ok` before assuming the fast index path is available.
- Set `APPLE_PRODUCTIVITY_LOG=/path/to/log` on the MCP host to see index-vs-JXA fallback lines when diagnosing silent slow reads.

## Tool map

| Goal | MCP tool | CLI |
| --- | --- | --- |
| Inbox triage / unread counts | `mail_analyze` → `triage` | `mail triage` |
| Newsletter / unsubscribe scan | `mail_analyze` → `newsletters` | `mail newsletters` |
| Targeted lookup | `mail_messages` → `search` | `mail-messages search` |
| Permissions / slow reads | `mail_permissions_check` | `doctor` |
| Calendar day view | `calendar_events` → `list` | `calendar agenda` |
| Day plan | `calendar_events` + `reminders_tasks` | `day plan` |

## `mail_analyze` actions

### `triage`

Use for unread/flagged review, recent inbox slices, or filtered triage. Defaults to mailbox `INBOX` and `limit` 10 when omitted.

Common arguments: `mailbox_name`, `account_name`, `query`, `since`, `limit`, `unread_only`, `flagged_only`.

### `newsletters`

Use for bulk/marketing mail and unsubscribe discovery. Defaults `query` to `unsubscribe` and `limit` to 10.

Set `with_links: true` only for a small candidate set (limit at most 25); it fetches `List-Unsubscribe` metadata per message.
