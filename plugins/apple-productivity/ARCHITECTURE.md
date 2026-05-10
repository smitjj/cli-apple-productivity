# Architecture Notes

## Current shape

This plugin is CLI-first, backed by a reusable Python core and an optional stdio MCP adapter:

- `apple_productivity_service.py` — `AppleProductivityService`, the core dispatch layer. Holds the persistent JXA worker, the message-id scope cache, the Envelope Index reader, and the EventKit backend probe; routes each call to the fastest available path with transparent JXA fallback.
- `apple_productivity_registry.py` — shared action/argument registry; CLI help/flags and MCP JSON schemas derive from this so transport contracts cannot drift.
- `apple_productivity_cli.py` — canonical CLI transport (argparse → service), including compact JSON, batch, REPL, and compound workflows.
- `apple_productivity_mcp_server.py` — thin stdio MCP transport (JSON-RPC frames in/out) generated from the same registry.
- `apple_productivity_mail_index.py` — read-only SQLite reader for Mail's Envelope Index, used as the fast path for `mail_messages.search`
- `apple_productivity_eventkit.py` — PyObjC EventKit binding for Calendar and Reminders writes; retires the AppleScript-delete fallback and unlocks recurrence/alarms/geofence/source/timezone
- `shared_validation.py` — schema and value validation reused by all transports
- `apple_productivity_jxa.js` — Mail, Calendar, and Reminders automation via JXA. Supports both one-shot invocation (legacy) and `--server` mode (a length-prefixed JSON loop on stdin/stdout) used by the persistent worker.
- `test_validation.py` and `test_cli_registry.py` — pure-Python unit tests
- `cli_smoke_test.py` — end-to-end smoke test of the CLI, including batch, REPL, Mail reads, and Calendar/Reminders CRUD on a real macOS host
- `smoke_test.py` — end-to-end smoke test of MCP + JXA on a real macOS host

The action-oriented tool model is unchanged at the surface:

- `mail_accounts`, `mail_mailboxes`, `mail_messages`, `mail_compose`
- `calendar_calendars`, `calendar_events`
- `reminders_lists`, `reminders_tasks`

## Design principles

- CLI-first product surface; MCP remains a compatibility adapter over the same service core
- Shared registry and validation rules; identical low-level contract regardless of transport
- Grouped action-oriented tools instead of one tool per verb
- Compound CLI commands for common agent workflows that should not require many round trips
- Native macOS automation as the platform boundary
- Clear permission failures with actionable recovery guidance
- Operability via env vars (`APPLE_PRODUCTIVITY_TIMEOUT_SECONDS`, `APPLE_PRODUCTIVITY_LOG`) rather than code changes

## Known limitations

- **Calendar event delete** falls through to AppleScript via `osascript -e` instead of the JXA dispatcher. In the macOS releases tested, JXA `calendar.delete(event)` would silently no-op against an event fetched by uid; the AppleScript path (`tell calendar named X to delete (first event whose uid is Y)`) reliably removes the event. The fallback lives inside `AppleProductivityService._delete_calendar_event_via_applescript` so the surface stays consistent — re-test whenever upgrading macOS and remove the special case if JXA delete starts working.
- **Inline attachment cap is 5 MB.** Larger attachments must use `save_to`. The cap exists because the JSON-RPC envelope and process pipes do not handle multi-megabyte payloads gracefully.
- **`safeCall` in JXA swallows property-access errors and substitutes a fallback value.** This is intentional — JXA throws on missing properties for individual messages or accounts and we do not want one broken record to fail an entire list operation. The trade-off is that broken accounts surface as `null` fields rather than a loud error. Set `APPLE_PRODUCTIVITY_LOG=/path` to see one line per JXA call when diagnosing silent failures.
- **`osascript` argument passing** is via `subprocess.run(list_args)` (not a shell string), so there is no shell-injection surface. Inside JXA, the second argv slot is parsed with `JSON.parse`; only the calendar-delete fallback uses positional AppleScript args.

## Message-id scope cache

`AppleProductivityService` keeps a bounded `MessageScopeCache` (256 entries, LRU-ish) keyed on Mail message id, valued with `(account_name, mailbox_name)`. Successful list/search/get responses populate it from the message summaries that JXA already returns. Subsequent targeted actions (`get`, `set-read`, `set-flag`, `delete`, `open`, `get-attachment`) inject the cached scope as `mailbox_name`/`account_name` hints when the caller didn't supply them, so JXA's `findMailMessageById` skips its global mailbox scan. `move` updates the cached scope to the new target; `delete` evicts. If a cached entry is stale (message moved out-of-band), the service evicts it and retries once with no hint — at most one extra `osascript` invocation per stale id.

The cache lives for the lifetime of one service instance: one MCP server process, one `batch`/`repl` session, or one single CLI invocation. There is no on-disk persistence and no cross-process sharing.

## Roadmap status

Phases 1–4 below are **implemented** and shipped behind capability probes; each new path is default-on with transparent JXA fallback so worst case is "behaves like before." Cross-cutting polish items (`.mcpb` packaging) remain open.

### Phase 1 — Persistent JXA helper subprocess (no surface change)

Each `osascript -l JavaScript` invocation costs ~150–250 ms of cold start. We pay it on every tool call, including trivial ones like `mail_accounts list`. A long-lived JXA worker fed line-delimited JSON over stdin/stdout collapses that to single-digit ms per call.

- New `_JxaWorker` class inside `apple_productivity_service.py`: spawns `osascript -l JavaScript -i` once, sends framed `{tool, args}` requests, reads framed `{ok, result|error}` responses, holds a per-call mutex and a per-call timeout that respects `APPLE_PRODUCTIVITY_TIMEOUT_SECONDS`.
- Crash recovery: if the worker dies mid-call, surface the original error AND respawn for the next call. Never auto-retry transparently — the caller may be mid-side-effect.
- Keep `_invoke_jxa` as the public seam; the worker is a private optimization.
- Risk: low–medium. Bench against the existing one-shot path before flipping default.

### Phase 2 — SQLite Envelope Index read path for Mail

Mail.app maintains `~/Library/Mail/V{9,10,11}/MailData/Envelope Index`, a SQLite database with FTS5-backed `subjects` and join tables for `addresses`/`recipients`/`attachments`. Two competitors (`p-l-ta/mail-mcp` Node, `imdinu/apple-mail-mcp` Python) and the `openclaw/apple-mail-search` skill benchmark **~50 ms vs 8+ minutes** for full-text search on real mailboxes. Our `since`/`from_address`/`to_address`/`subject_contains` filters are still JXA post-fetch scans; they hit a wall at scale.

- New `apple_productivity_mail_index.py`: probes for the Envelope Index, opens it `mode=ro&immutable=1`, exposes parameterised queries for list/search/get-thread.
- Capability probe at service start: if the DB is unreachable (sandboxed host, missing Full Disk Access, unexpected schema version), fall back to the existing JXA path silently and log the reason via `APPLE_PRODUCTIVITY_LOG`.
- Map SQLite `ROWID` ↔ JXA `message.id()` once per process; the existing `MessageScopeCache` slots in unchanged.
- Writes (move/delete/set-read/set-flag/compose) keep going through JXA — the SQLite DB is read-only territory. The combined effect: cold reads are 10⁴× faster, writes are unchanged but benefit from Phase 1.
- Schema risk: undocumented and changes between macOS releases. Mitigate with a `SCHEMA_PROBE_QUERIES` table and per-version branches; refuse to use the index if any probe fails.
- Adds the `Full Disk Access` requirement, but only for the SQLite path. Permissions troubleshooting in [README.md](README.md) needs a fourth bullet.

### Phase 3 — Mail surface additions

Individually small; ride on Phases 1 and 2 where noted:

- `mail_messages.bulk_set_read` / `bulk_set_flag` / `bulk_move` / `bulk_delete` — accept an array of `message_id`s, hard cap at 50, return a per-id success/error map. Cuts agent token usage and exploits the persistent JXA worker.
- `mail_messages.get_thread` — return the full conversation for a message_id. Uses the SQLite `conversation_id` column (free after Phase 2); JXA fallback iterates Mail's threading API at higher cost.
- `mail_messages.get_unsubscribe_link` — pull `List-Unsubscribe` and `List-Unsubscribe-Post` headers from the message source. Available via Mail's `source` already; we just need to surface it as a structured field.
- **Drafts as first-class** (`mail_drafts` tool, pattern from `s-morgan-jeffries/apple-mail-mcp`). Today `mail_compose.create` with `send_now: false` produces a draft that is then unreachable via our surface; the agent can't iterate on it. New tool with actions `list` (enumerate drafts in each account's Drafts mailbox via JXA), `get`, `update` (subject/body/recipients), `send`, `delete`. Enables the realistic workflow of "draft → review → revise → send" instead of forcing the whole message to be regenerated.
- **`mail_permissions_check` diagnostic tool** (pattern from `snarris/apple-eventkit-mcp`). Probes Automation permission for Mail, Full Disk Access (needed for the Phase 2 SQLite path), and the Mail.app process state; returns a structured `{automation: ok, full_disk: denied, mail_running: true}` payload with the same actionable hints `format_platform_error` already produces. Turns "why is nothing working" into a single tool call. A sibling `eventkit_permissions_check` lands with Phase 4.

### Phase 4 — PyObjC EventKit backend for Calendar and Reminders

EventKit is the system framework Mail/Calendar/Reminders themselves use. JXA is a brittle wrapper over a wrapper; PyObjC binds EventKit directly with no Swift build, no Xcode project, no notarization. This both retires our AppleScript-delete fallback and unlocks features JXA cannot reach.

- New `apple_productivity_eventkit.py` using `EventKit` and `Foundation` via PyObjC. Permissions: request via `EKEventStore.requestAccessToEntityType_completion_` once per process; surface the result via a new `permissions_check` tool (cribbed from `snarris/apple-eventkit-mcp`).
- Migrate Calendar writes (`create`/`update`/`delete`/`move-between-calendars`) and Reminders writes to EventKit. Reads can stay on JXA initially; migrate per use case.
- Drop `_delete_calendar_event_via_applescript`. The whole class of "JXA `.delete()` no-ops" disappears.
- New fields surfaced on EventKit: structured `recurrence_rule` (RFC 5545 generated, not user-typed), per-event `timezone` (IANA name; fixes silent host-TZ binding), `alarms` array (relative + absolute), `attendees` (read-only), `source` qualifier (iCloud / Google / Exchange / Local) for same-name calendar disambiguation, geofence triggers on Reminders.
- Backwards compat: existing string `recurrence` / `url` / `priority` / `flagged` keep working unchanged.
- Risk: medium. Permission UX is different (EventKit prompts via `tccd`, not Automation). Ship the `permissions_check` tool first.

### Cross-cutting polish

- **Dry-run flag** — implemented for `mail_messages.bulk-*` actions (`dry_run: true` returns the would-affect set without doing the work). Calendar/Reminders single-item `delete` is left as-is for now; agents can probe via `get` first.
- **Read-only mode** — implemented via `APPLE_PRODUCTIVITY_READ_ONLY=1`. Rejects every mutating action with a clear error message; `mail_permissions_check` and all read paths still work.
- **`.mcpb` bundle** for one-click install in Claude Desktop / Codex marketplaces — still open. ~30-line manifest plus the existing wheel; no notarization needed since we stay pure-Python+PyObjC.
- **Re-test JXA `calendar.delete()`** — academic now that EventKit owns the Calendar write path when available. The AppleScript fallback only fires when EventKit access is denied or PyObjC is missing.

### Explicitly out of scope

These appear in competitor servers but are deliberately skipped:

- **Native Swift Mail helper**. Was item #1 in the previous plan. PyObjC EventKit covers the only concrete need we'd have for Swift, and Mail itself doesn't have an EventKit-equivalent system framework worth wrapping. Revisit only if a Mail feature surfaces that JXA *and* the SQLite read path both can't reach.
- **In-memory mail templates / mail-merge** (sweetrb, patrickfreyer, s-morgan-jeffries). State the agent should hold itself.
- **"Smart inbox" heuristics** (`get_awaiting_reply`, `get_needs_response`). Brittle; better as client-side prompts over our list/search primitives.
- **Native SMTP fallback** that bypasses Mail.app (Apple-PIM-Agent). Wrong scope: we own Mail.app integration, not deliverability.
- **DKIM/SPF sender verification + trusted-senders allowlist** (Apple-PIM-Agent). Trust decisions don't belong at the MCP layer.
- **Undo/redo stack** (che-ical-mcp). Stateful and racy across multiple agents hitting the same store. Dry-run covers the same risk surface.
- **Mail rules CRUD** (sweetrb, s-morgan-jeffries). AppleScript surface is rough; agents configure rules once and never touch them.
- **Custom `.emlx`-derived FTS5 index** (imdinu). Mail already maintains FTS5 for body search inside the Envelope Index. Reuse, don't duplicate.
