---
name: apple-productivity
description: >-
  Use the Apple Productivity MCP server and CLI for Mail, Calendar, and Reminders
  on macOS. Apply for inbox triage, newsletter analysis, automated-vs-human mail
  classification, search, drafting, calendar and reminder tasks, and when macOS
  Apple app automation is requested.
---

# Apple Productivity

## Agent guidance

- If the user asks how many emails are system-generated vs human-generated, automated vs human, or machine vs person, call `mail_analyze` with `action: "classify"` (alias `system_vs_human`) or `mail classify`. Do not run `sqlite3`, SQL, or shell against `~/Library/Mail/.../Envelope Index`.
- Use Apple Productivity only for Mail, Calendar, and Reminders work on macOS.
- Prefer summary-first tools before raw message dumps:
  - `mail_analyze` with `action: "triage"` for inbox triage (CLI: `mail triage`).
  - `mail_analyze` with `action: "newsletters"` for newsletter/unsubscribe candidates (CLI: `mail newsletters`).
  - `mail_analyze` with `action: "classify"` for aggregate likely-automated vs likely-human counts from Mail's Envelope Index (CLI: `mail classify`). Read `summary` first; do not page the whole mailbox or query the Envelope Index directly for this.
- For schema discovery, grouped counts, and small index samples, use `mail_index` (`describe`, `aggregate`, `sample`; CLI: `mail-index`). Do not run `sqlite3` or other shell SQL against Mail's Envelope Index.
- For system-like vs human-like mail totals, use `mail_analyze` → `classify` before any `mail_messages` paging. Do not open Mail's Envelope Index with `sqlite3`, SQL, or shell for aggregate counts when `classify` is available.
- Use `mail_permissions_check` (CLI: `doctor`) to confirm `envelope_index.path` when you need the index location; use `mail_analyze` → `classify` for the counts themselves.
- Prefer `mail_messages` **search** with scoped filters over unbounded **list** calls. For sender lookups, pass `from_address` (or an email-only `query`); do not rely on natural-language `query` text alone.
- Do not invoke raw `osascript`, JXA, or other direct Mail automation outside this plugin.
- If mail reads are slow, empty, or failing, run `mail_permissions_check` (CLI: `doctor`) before escalating or falling back to other approaches.
- Respect per-call limits (`limit` max 100; `mail_analyze` with `with_links` max 25). Use `offset` and `nextOffset` on `mail_messages` list/search to page through large result sets.
- Read `summary` first on `mail_analyze` responses; use `result` or `candidates` only when you need specific message ids or details.
- On mail list/search/triage/newsletters/classify payloads, top-level `"source": "envelope_index"` means Mail's Envelope Index handled the read; `"source": "jxa"` means the plugin fell back to JXA. If `source` is missing on a read payload, treat it as JXA.
- Run `mail_permissions_check` (CLI: `doctor`) when reads are slow or empty. Check `full_disk_access.ok` and `envelope_index.ok` before assuming the fast index path is available.
- Set `APPLE_PRODUCTIVITY_LOG=/path/to/log` on the MCP host to see index-vs-JXA fallback lines when diagnosing silent slow reads.

## Tool map

| Goal | MCP tool | CLI |
| --- | --- | --- |
| Inbox triage / unread counts | `mail_analyze` → `triage` | `mail triage` |
| Newsletter / unsubscribe scan | `mail_analyze` → `newsletters` | `mail newsletters` |
| System-generated vs human-generated counts | `mail_analyze` → `classify` or `system_vs_human` | `mail classify` |
| Index schema / grouped counts / samples | `mail_index` → `describe` / `aggregate` / `sample` | `mail-index` |
| Targeted lookup | `mail_messages` → `search` | `mail-messages search` |
| Create a Mail folder | `mail_mailboxes` → `create` | `mail-mailboxes create` |
| Rename a Mail folder | `mail_mailboxes` → `rename` | `mail-mailboxes rename` |
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

### `classify` / `system_vs_human`

Use for aggregate likely-automated vs likely-human counts over a mailbox scope. `system_vs_human` is an alias for `classify`. Defaults to mailbox `INBOX` when omitted. This action is Envelope Index-backed; it does not page individual messages.

Common arguments: `mailbox_name`, `account_name`, `since`, `unread_only`, `flagged_only`.

Read `summary.collapsed` first (`likelyHuman`, `likelyAutomated`, `ambiguous`). Use `summary.automatedConversation` when you need raw Mail `automated_conversation` signal counts.

## `mail_index` actions

### `describe`

Use before custom breakdowns to see probed tables, message columns, allowed `groupBy` / `measures` / `filters`, and sample columns.

### `aggregate`

Use for grouped counts over a mailbox scope. Pass `group_by` (repeatable) and optional `measures` (`count`, `min_date_received`, `max_date_received`). Optional `filters` use `{column, op, value}` with ops `eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `like`.

### `sample`

Use for a small capped row sample from the index. Optional `columns` and `filters`; `limit` max 50. Prefer `messages` in the response for follow-up `mail_messages` actions.
