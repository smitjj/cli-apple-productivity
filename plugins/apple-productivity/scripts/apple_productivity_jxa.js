function run(argv) {
  if (argv && argv[0] === "--server") {
    return runServer();
  }
  try {
    const toolName = argv[0];
    const input = argv[1] ? JSON.parse(argv[1]) : {};
    const result = dispatch(toolName, input);
    return JSON.stringify({ ok: true, result: sanitize(result) });
  } catch (error) {
    return JSON.stringify({
      ok: false,
      error: {
        message: String(error),
        stack: error && error.stack ? String(error.stack) : null,
      },
    });
  }
}

// Persistent server mode. Reads framed requests from stdin:
//   "<byte-count>\n<JSON request>"
// and writes framed responses to stdout in the same format. Each request is
// {id, tool, args}; each response is {id, ok, result?, error?}. Loops until
// stdin closes or a fatal error fires.
function runServer() {
  ObjC.import("Foundation");
  const stdin = $.NSFileHandle.fileHandleWithStandardInput;
  const stdout = $.NSFileHandle.fileHandleWithStandardOutput;
  const stderr = $.NSFileHandle.fileHandleWithStandardError;

  const writeFramed = function (obj) {
    const body = JSON.stringify(obj);
    const framed = body.length + "\n" + body;
    const data = $.NSString.alloc.initWithUTF8String(framed).dataUsingEncoding($.NSUTF8StringEncoding);
    stdout.writeData(data);
  };

  const readByteCount = function () {
    let buf = "";
    while (true) {
      const chunk = stdin.readDataOfLength(1);
      if (!chunk || chunk.length === 0) return null;
      const ch = ObjC.unwrap($.NSString.alloc.initWithDataEncoding(chunk, $.NSUTF8StringEncoding));
      if (ch === "\n") return buf;
      if (ch === null || ch === undefined) return null;
      buf += ch;
      if (buf.length > 32) return null; // sanity cap
    }
  };

  const readBody = function (n) {
    const data = stdin.readDataOfLength(n);
    if (!data || data.length !== n) return null;
    return ObjC.unwrap($.NSString.alloc.initWithDataEncoding(data, $.NSUTF8StringEncoding));
  };

  // Hello frame so the Python side can confirm liveness.
  writeFramed({ ready: true, version: 1 });

  while (true) {
    const lenStr = readByteCount();
    if (lenStr === null) break;
    const n = parseInt(lenStr, 10);
    if (!isFinite(n) || n <= 0) {
      writeFramed({ id: null, ok: false, error: { message: "invalid frame length: " + lenStr } });
      continue;
    }
    const body = readBody(n);
    if (body === null) break;
    let request;
    try {
      request = JSON.parse(body);
    } catch (parseError) {
      writeFramed({ id: null, ok: false, error: { message: "invalid JSON: " + parseError } });
      continue;
    }
    const requestId = request && request.id !== undefined ? request.id : null;
    try {
      const result = dispatch(request.tool, request.args || {});
      writeFramed({ id: requestId, ok: true, result: sanitize(result) });
    } catch (error) {
      writeFramed({
        id: requestId,
        ok: false,
        error: {
          message: String(error),
          stack: error && error.stack ? String(error.stack) : null,
        },
      });
    }
  }
  return "";
}

ObjC.import("Foundation");
ObjC.import("AppKit");

const fileManager = $.NSFileManager.defaultManager;
const workspace = $.NSWorkspace.sharedWorkspace;

// Some hosts fail to resolve app display names via JXA Application("Mail"),
// even though the apps exist at their standard bundle paths.
function resolveApplication(displayName, bundleId, fallbackPaths) {
  const candidates = [];
  const seen = {};
  let lastError = null;

  function pushCandidate(candidate) {
    if (!candidate) return;
    const normalized = String(candidate);
    if (seen[normalized]) return;
    seen[normalized] = true;
    candidates.push(normalized);
  }

  function unwrapString(value) {
    if (!value) return null;
    try {
      return ObjC.unwrap(value);
    } catch (error) {
      return null;
    }
  }

  try {
    const url = workspace.URLForApplicationWithBundleIdentifier($(bundleId));
    if (url && !url.isNil()) pushCandidate(unwrapString(url.path()));
  } catch (error) {
    lastError = error;
  }

  try {
    const path = workspace.fullPathForApplication($(displayName));
    if (path && !path.isNil()) pushCandidate(unwrapString(path));
  } catch (error) {
    lastError = error;
  }

  toArray(fallbackPaths).forEach(pushCandidate);
  pushCandidate(displayName);

  for (let i = 0; i < candidates.length; i += 1) {
    const candidate = candidates[i];
    if (candidate.charAt(0) === "/" && !fileManager.fileExistsAtPath($(candidate))) {
      continue;
    }
    try {
      const app = Application(candidate);
      app.includeStandardAdditions = true;
      return app;
    } catch (error) {
      lastError = error;
    }
  }

  throw new Error(
    "Application could not be resolved for " + displayName + ". "
      + (lastError ? String(lastError) : "No usable application target found.")
  );
}

const mail = resolveApplication("Mail", "com.apple.mail", [
  "/System/Applications/Mail.app",
  "/Applications/Mail.app",
]);
const calendar = resolveApplication("Calendar", "com.apple.iCal", [
  "/System/Applications/Calendar.app",
  "/Applications/Calendar.app",
]);
const reminders = resolveApplication("Reminders", "com.apple.reminders", [
  "/System/Applications/Reminders.app",
  "/Applications/Reminders.app",
]);

function dispatch(toolName, input) {
  switch (toolName) {
    case "mail_accounts":
      return mailAccounts(input);
    case "mail_mailboxes":
      return mailMailboxes(input);
    case "mail_messages":
      return mailMessages(input);
    case "mail_compose":
      return mailCompose(input);
    case "mail_drafts":
      return mailDrafts(input);
    case "mail_permissions_check":
      return mailPermissionsCheck(input);
    case "calendar_calendars":
      return calendarCalendars(input);
    case "calendar_events":
      return calendarEvents(input);
    case "reminders_lists":
      return remindersLists(input);
    case "reminders_tasks":
      return remindersTasks(input);
    default:
      throw new Error("Unsupported tool: " + toolName);
  }
}

function mailAccounts() {
  return toArray(mail.accounts()).map(accountSummary);
}

function mailMailboxes(input) {
  const includeCounts = Boolean(input.include_counts);
  return getMailAccounts(input.account_name).map(function (account) {
    return {
      account: accountSummary(account),
      mailboxes: flattenMailboxes(toArray(account.mailboxes()), account.name(), "").map(function (entry) {
        const mailbox = { name: entry.name, path: entry.path, account: entry.accountName };
        if (includeCounts) {
          mailbox.messageCount = safeCall(function () { return entry.mailbox.messages().length; }, null);
        }
        return mailbox;
      }),
    };
  });
}

function mailMessages(input) {
  switch (input.action) {
    case "list":
      return listMailMessages(input);
    case "get":
      return messageSummary(findMailMessageById(input.message_id, input.account_name, input.mailbox_name), true, Boolean(input.include_source));
    case "search":
      return searchMailMessages(input);
    case "move":
      return moveMailMessage(input);
    case "delete":
      return deleteMailMessage(input);
    case "set-read":
      return setMailRead(input);
    case "set-flag":
      return setMailFlag(input);
    case "open":
      return openMailMessage(input);
    case "get-attachment":
      return getMailAttachment(input);
    case "get-thread":
      return getMailThread(input);
    case "get-unsubscribe-link":
      return getMailUnsubscribeLink(input);
    case "bulk-set-read":
      return bulkSetMailRead(input);
    case "bulk-set-flag":
      return bulkSetMailFlag(input);
    case "bulk-move":
      return bulkMoveMail(input);
    case "bulk-delete":
      return bulkDeleteMail(input);
    default:
      throw new Error("Unsupported mail_messages action: " + input.action);
  }
}

function mailCompose(input) {
  switch (input.action) {
    case "create":
      return createMailMessage(input);
    case "reply":
      return replyMailMessage(input);
    case "forward":
      return forwardMailMessage(input);
    default:
      throw new Error("Unsupported mail_compose action: " + input.action);
  }
}

function calendarCalendars(input) {
  const includeCounts = Boolean(input.include_counts);
  return toArray(calendar.calendars()).map(function (item) {
    const summary = calendarSummary(item);
    if (includeCounts) {
      summary.eventCount = safeCall(function () { return item.events().length; }, null);
    }
    return summary;
  });
}

function calendarEvents(input) {
  switch (input.action) {
    case "list":
      return listCalendarEvents(input);
    case "get":
      return calendarGetEvent(input);
    case "create":
      return createCalendarEvent(input);
    case "update":
      return updateCalendarEvent(input);
    case "delete":
      return deleteCalendarEvent(input);
    case "open":
      return openCalendarEvent(input);
    default:
      throw new Error("Unsupported calendar_events action: " + input.action);
  }
}

function remindersLists(input) {
  switch (input.action) {
    case "list":
      return listReminderLists(input);
    case "create":
      return createReminderList(input);
    case "update":
      return updateReminderList(input);
    case "delete":
      return deleteReminderList(input);
    default:
      throw new Error("Unsupported reminders_lists action: " + input.action);
  }
}

function remindersTasks(input) {
  switch (input.action) {
    case "list":
      return listReminderTasks(input);
    case "get":
      return reminderSummary(findReminderById(input.reminder_id));
    case "create":
      return createReminderTask(input);
    case "update":
      return updateReminderTask(input);
    case "delete":
      return deleteReminderTask(input);
    case "complete":
      return setReminderCompleted(input.reminder_id, true);
    case "incomplete":
      return setReminderCompleted(input.reminder_id, false);
    default:
      throw new Error("Unsupported reminders_tasks action: " + input.action);
  }
}

function listMailMessages(input) {
  const mailbox = resolveMailMailbox(input.mailbox_name, input.account_name);
  const limit = clampLimit(input.limit, 25);
  const offset = clampOffset(input.offset);
  const unreadOnly = Boolean(input.unread_only);
  const flaggedOnly = Boolean(input.flagged_only);
  const messages = toArray(mailbox.mailbox.messages())
    .filter(function (message) {
      if (unreadOnly && safeCall(function () { return message.readStatus(); }, true)) return false;
      if (flaggedOnly && !safeCall(function () { return message.flaggedStatus(); }, false)) return false;
      return true;
    })
    .slice(offset, offset + limit)
    .map(function (message) { return messageSummary(message, false, false); });
  return { mailbox: { name: mailbox.name, path: mailbox.path, account: mailbox.accountName }, count: messages.length, messages: messages };
}

function searchMailMessages(input) {
  const query = normalizeLower(input.query);
  const fromFilter = normalizeLower(input.from_address);
  const toFilter = normalizeLower(input.to_address);
  const subjectFilter = normalizeLower(input.subject_contains);
  const sinceBound = input.since ? parseDateInput(input.since) : null;
  const limit = clampLimit(input.limit, 25);
  const offset = clampOffset(input.offset);
  const unreadOnly = Boolean(input.unread_only);
  const flaggedOnly = Boolean(input.flagged_only);
  const mailboxes = input.mailbox_name
    ? [resolveMailMailbox(input.mailbox_name, input.account_name)]
    : getSearchMailboxes(input.account_name);
  const results = [];
  const seen = {};
  let skipped = 0;
  for (let i = 0; i < mailboxes.length; i += 1) {
    const messages = toArray(mailboxes[i].mailbox.messages());
    for (let j = 0; j < messages.length; j += 1) {
      const message = messages[j];
      const id = safeCall(function () { return message.id(); }, null);
      if (id === null || seen[id]) continue;
      if (query && !matchesMailQuery(message, query)) continue;
      if (unreadOnly && safeCall(function () { return message.readStatus(); }, true)) continue;
      if (flaggedOnly && !safeCall(function () { return message.flaggedStatus(); }, false)) continue;
      if (fromFilter) {
        const sender = String(safeCall(function () { return message.sender(); }, "") || "").toLowerCase();
        if (!senderMatchesFilter(sender, fromFilter)) continue;
      }
      if (toFilter) {
        const recipientText = String(toArray(safeCall(function () { return message.toRecipients(); }, [])).map(function (r) {
          return safeCall(function () { return r.address(); }, "");
        }).join(" ")).toLowerCase();
        if (recipientText.indexOf(toFilter) === -1) continue;
      }
      if (subjectFilter) {
        const subject = String(safeCall(function () { return message.subject(); }, "") || "").toLowerCase();
        if (subject.indexOf(subjectFilter) === -1) continue;
      }
      if (sinceBound) {
        const received = safeCall(function () { return message.dateReceived(); }, null);
        if (!received || new Date(received) < sinceBound) continue;
      }
      seen[id] = true;
      if (skipped < offset) {
        skipped += 1;
        continue;
      }
      results.push(messageSummary(message, false, false));
      if (results.length >= limit) {
        return { query: input.query, count: results.length, messages: results };
      }
    }
  }
  return { query: input.query, count: results.length, messages: results };
}

function moveMailMessage(input) {
  const message = findMailMessageById(input.message_id, input.account_name, input.mailbox_name);
  const target = resolveMailMailbox(input.target_mailbox, input.target_account);
  message.mailbox = target.mailbox;
  return { moved: true, messageId: input.message_id, targetMailbox: target.path, targetAccount: target.accountName };
}

function deleteMailMessage(input) {
  mail.delete(findMailMessageById(input.message_id, input.account_name, input.mailbox_name));
  return { deleted: true, messageId: input.message_id };
}

function setMailRead(input) {
  const message = findMailMessageById(input.message_id, input.account_name, input.mailbox_name);
  message.readStatus = Boolean(input.read);
  return { updated: true, messageId: input.message_id, read: Boolean(input.read) };
}

function setMailFlag(input) {
  const message = findMailMessageById(input.message_id, input.account_name, input.mailbox_name);
  message.flaggedStatus = Boolean(input.flagged);
  return { updated: true, messageId: input.message_id, flagged: Boolean(input.flagged) };
}

function openMailMessage(input) {
  mail.open(findMailMessageById(input.message_id, input.account_name, input.mailbox_name));
  return { opened: true, messageId: input.message_id };
}

function getMailAttachment(input) {
  const message = findMailMessageById(input.message_id, input.account_name, input.mailbox_name);
  const attachments = toArray(safeCall(function () { return message.mailAttachments(); }, []));
  const index = Number(input.attachment_index);
  if (!isFinite(index) || index < 0 || index >= attachments.length) {
    throw new Error(
      "attachment_index " + input.attachment_index + " is out of range; message has " +
      attachments.length + " attachment(s)."
    );
  }
  const attachment = attachments[index];
  const isDownloaded = safeCall(function () { return attachment.downloaded(); }, false);
  if (!isDownloaded) {
    safeCall(function () { return attachment.download(); }, null);
  }
  const name = safeCall(function () { return attachment.name(); }, "attachment-" + index);
  const mimeType = safeCall(function () { return attachment.mimeType(); }, null);

  if (input.save_to) {
    saveAttachmentToPath(attachment, input.save_to);
    return {
      saved: true,
      path: input.save_to,
      attachment: { name: name, mimeType: mimeType, downloaded: true },
    };
  }
  if (input.return_inline) {
    const tempPath = makeTempPath(name);
    saveAttachmentToPath(attachment, tempPath);
    const sizeBytes = fileSize(tempPath);
    if (sizeBytes !== null && sizeBytes > 5 * 1024 * 1024) {
      removeFile(tempPath);
      throw new Error(
        "Attachment is " + sizeBytes + " bytes; the 5 MB inline limit was exceeded. Use save_to instead."
      );
    }
    const base64 = readFileAsBase64(tempPath);
    removeFile(tempPath);
    return {
      attachment: {
        name: name,
        mimeType: mimeType,
        downloaded: true,
        sizeBytes: sizeBytes,
      },
      contentBase64: base64,
    };
  }
  throw new Error("get-attachment requires either save_to or return_inline.");
}

function getMailThread(input) {
  // JXA-only thread retrieval. Walks the message's mailbox looking for
  // entries that share an `Subject:` (after stripping Re:/Fwd:) AND any
  // overlap in `In-Reply-To`/`References` headers from the source. This is
  // best-effort — for fast results back the call with the SQLite path.
  const message = findMailMessageById(input.message_id, input.account_name, input.mailbox_name);
  const seedSubject = normalizeThreadSubject(safeCall(function () { return message.subject(); }, "") || "");
  const seedReferences = extractReferences(safeCall(function () { return message.source(); }, "") || "");
  const seedMessageIdHeader = safeCall(function () { return message.messageId(); }, null);
  if (seedMessageIdHeader) seedReferences[seedMessageIdHeader] = true;

  const limit = clampLimit(input.limit, 100);
  const mailbox = safeCall(function () { return message.mailbox(); }, null);
  const candidates = mailbox ? toArray(mailbox.messages()) : [];
  const seen = {};
  const results = [];
  for (let i = 0; i < candidates.length && results.length < limit; i += 1) {
    const candidate = candidates[i];
    const id = safeCall(function () { return candidate.id(); }, null);
    if (id === null || seen[id]) continue;
    const subject = normalizeThreadSubject(safeCall(function () { return candidate.subject(); }, "") || "");
    const refs = extractReferences(safeCall(function () { return candidate.source(); }, "") || "");
    const candHeader = safeCall(function () { return candidate.messageId(); }, null);
    let matches = false;
    if (subject && subject === seedSubject) matches = true;
    if (!matches && candHeader && seedReferences[candHeader]) matches = true;
    if (!matches) {
      for (const ref in refs) {
        if (seedReferences[ref]) { matches = true; break; }
      }
    }
    if (matches) {
      seen[id] = true;
      results.push(messageSummary(candidate, false, false));
    }
  }
  results.sort(function (a, b) {
    const left = a.dateReceived || "";
    const right = b.dateReceived || "";
    return left < right ? -1 : left > right ? 1 : 0;
  });
  return { count: results.length, messages: results };
}

function normalizeThreadSubject(subject) {
  let s = String(subject || "").trim().toLowerCase();
  // Strip leading Re:/Fwd:/Fw: chains commonly added by clients.
  while (true) {
    const next = s.replace(/^(re|fwd?|aw|sv|antw|tr)\s*(\[\d+\])?\s*:\s*/i, "");
    if (next === s) break;
    s = next;
  }
  return s;
}

function extractReferences(source) {
  // Pull values from In-Reply-To and References headers. Returns a set
  // (object with truthy values) of normalized message-id headers.
  const result = {};
  if (!source) return result;
  const text = String(source);
  const headers = text.split(/\r?\n\r?\n/, 1)[0];
  const lines = headers.split(/\r?\n/);
  let current = "";
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (/^\s/.test(line)) {
      current += " " + line.trim();
    } else {
      processReferenceHeader(current, result);
      current = line;
    }
  }
  processReferenceHeader(current, result);
  return result;
}

function processReferenceHeader(line, result) {
  if (!line) return;
  const lower = line.toLowerCase();
  if (lower.indexOf("in-reply-to:") !== 0 && lower.indexOf("references:") !== 0) return;
  const value = line.split(":").slice(1).join(":").trim();
  const matches = value.match(/<[^>]+>/g);
  if (!matches) return;
  for (let i = 0; i < matches.length; i += 1) {
    result[matches[i]] = true;
  }
}

function getMailUnsubscribeLink(input) {
  const message = findMailMessageById(input.message_id, input.account_name, input.mailbox_name);
  const source = String(safeCall(function () { return message.source(); }, "") || "");
  if (!source) {
    return { messageId: input.message_id, found: false, urls: [], oneClickPost: null };
  }
  const headersBlock = source.split(/\r?\n\r?\n/, 1)[0];
  const lines = headersBlock.split(/\r?\n/);
  let unsubscribe = "";
  let unsubscribePost = "";
  let current = "";
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (/^\s/.test(line)) {
      current += " " + line.trim();
    } else {
      assignUnsubHeader(current, function (val) { unsubscribe = val; }, function (val) { unsubscribePost = val; });
      current = line;
    }
  }
  assignUnsubHeader(current, function (val) { unsubscribe = val; }, function (val) { unsubscribePost = val; });

  const urls = [];
  const mailtos = [];
  if (unsubscribe) {
    const tokens = unsubscribe.match(/<[^>]+>/g) || [];
    for (let i = 0; i < tokens.length; i += 1) {
      const inner = tokens[i].slice(1, -1).trim();
      if (/^mailto:/i.test(inner)) mailtos.push(inner);
      else if (/^https?:\/\//i.test(inner)) urls.push(inner);
    }
  }
  return {
    messageId: input.message_id,
    found: urls.length > 0 || mailtos.length > 0,
    urls: urls,
    mailtos: mailtos,
    oneClickPost: unsubscribePost || null,
  };
}

function assignUnsubHeader(line, setUnsub, setUnsubPost) {
  if (!line) return;
  const lower = line.toLowerCase();
  if (lower.indexOf("list-unsubscribe-post:") === 0) {
    setUnsubPost(line.split(":").slice(1).join(":").trim());
  } else if (lower.indexOf("list-unsubscribe:") === 0) {
    setUnsub(line.split(":").slice(1).join(":").trim());
  }
}

function bulkSetMailRead(input) {
  return runBulk(input, function (message) {
    message.readStatus = Boolean(input.read);
    return { read: Boolean(input.read) };
  });
}

function bulkSetMailFlag(input) {
  return runBulk(input, function (message) {
    message.flaggedStatus = Boolean(input.flagged);
    return { flagged: Boolean(input.flagged) };
  });
}

function bulkMoveMail(input) {
  const target = resolveMailMailbox(input.target_mailbox, input.target_account);
  return runBulk(input, function (message) {
    message.mailbox = target.mailbox;
    return { targetMailbox: target.path, targetAccount: target.accountName };
  });
}

function bulkDeleteMail(input) {
  return runBulk(input, function (message) {
    mail.delete(message);
    return { deleted: true };
  });
}

function runBulk(input, action) {
  const ids = toArray(input.message_ids);
  const dryRun = Boolean(input.dry_run);
  const accountName = input.account_name;
  const mailboxName = input.mailbox_name;
  const results = [];
  let succeeded = 0;
  let failed = 0;
  for (let i = 0; i < ids.length; i += 1) {
    const id = ids[i];
    try {
      const message = findMailMessageById(id, accountName, mailboxName);
      if (dryRun) {
        results.push({ messageId: id, ok: true, dryRun: true });
        succeeded += 1;
      } else {
        const detail = action(message) || {};
        const entry = { messageId: id, ok: true };
        for (const key in detail) entry[key] = detail[key];
        results.push(entry);
        succeeded += 1;
      }
    } catch (error) {
      results.push({ messageId: id, ok: false, error: String(error) });
      failed += 1;
    }
  }
  return { succeeded: succeeded, failed: failed, dryRun: dryRun, results: results };
}

function mailDrafts(input) {
  switch (input.action) {
    case "list":
      return listMailDrafts(input);
    case "get":
      return getMailDraft(input);
    case "update":
      return updateMailDraft(input);
    case "send":
      return sendMailDraft(input);
    case "delete":
      return deleteMailDraft(input);
    default:
      throw new Error("Unsupported mail_drafts action: " + input.action);
  }
}

function listMailDrafts(input) {
  const accounts = getMailAccounts(input.account_name);
  const limit = clampLimit(input.limit, 50);
  const drafts = [];
  for (let i = 0; i < accounts.length && drafts.length < limit; i += 1) {
    const draftMailbox = safeCall(function () { return accounts[i].drafts(); }, null)
      || findMailboxByName(toArray(accounts[i].mailboxes()), accounts[i].name(), "Drafts");
    if (!draftMailbox) continue;
    const messages = toArray(safeCall(function () { return draftMailbox.mailbox.messages(); }, []));
    for (let j = 0; j < messages.length && drafts.length < limit; j += 1) {
      drafts.push(messageSummary(messages[j], false, false));
    }
  }
  return { count: drafts.length, drafts: drafts };
}

function findMailboxByName(mailboxes, accountName, name) {
  const flat = flattenMailboxes(toArray(mailboxes), accountName, "");
  for (let i = 0; i < flat.length; i += 1) {
    if (flat[i].name === name) return flat[i];
  }
  return null;
}

function getMailDraft(input) {
  const message = findMailMessageById(input.message_id, input.account_name, input.mailbox_name);
  return messageSummary(message, true, false);
}

function updateMailDraft(input) {
  const message = findMailMessageById(input.message_id, input.account_name, input.mailbox_name);
  if (input.subject !== undefined) message.subject = String(input.subject || "");
  if (input.body !== undefined) message.content = String(input.body || "");
  return messageSummary(message, true, false);
}

function sendMailDraft(input) {
  const message = findMailMessageById(input.message_id, input.account_name, input.mailbox_name);
  mail.send(message);
  return { sent: true, messageId: input.message_id };
}

function deleteMailDraft(input) {
  const message = findMailMessageById(input.message_id, input.account_name, input.mailbox_name);
  mail.delete(message);
  return { deleted: true, messageId: input.message_id };
}

function mailPermissionsCheck() {
  // Each probe is wrapped so a single failure does not poison the whole
  // diagnostic. The result is structured so callers can act on it.
  const automation = probePermission(function () {
    mail.accounts();
    return true;
  });
  const fullDisk = probeFullDiskAccess();
  const mailRunning = probePermission(function () {
    return Boolean(mail.running());
  });
  const calendarAccess = probePermission(function () {
    calendar.calendars();
    return true;
  });
  const remindersAccess = probePermission(function () {
    reminders.lists();
    return true;
  });
  return {
    automation: automation,
    full_disk_access: fullDisk,
    mail_running: mailRunning,
    calendar: calendarAccess,
    reminders: remindersAccess,
  };
}

function probePermission(fn) {
  try {
    return { ok: Boolean(fn()), error: null };
  } catch (error) {
    return { ok: false, error: String(error) };
  }
}

function probeFullDiskAccess() {
  try {
    ObjC.import("Foundation");
    const home = ObjC.unwrap($.NSHomeDirectory());
    const fm = $.NSFileManager.defaultManager;
    const mailRoot = home + "/Library/Mail";
    if (!fm.fileExistsAtPath(mailRoot)) {
      return { ok: false, error: "Mail directory not present at " + mailRoot };
    }
    const entries = ObjC.unwrap(fm.contentsOfDirectoryAtPathError(mailRoot, null));
    if (!entries) {
      return { ok: false, error: "Could not list ~/Library/Mail (Full Disk Access required)" };
    }
    return { ok: true, error: null };
  } catch (error) {
    return { ok: false, error: String(error) };
  }
}

function saveAttachmentToPath(attachment, path) {
  // Mail.app exposes `save` as a verb on the application that accepts an
  // attachment plus an `in` parameter. Path() builds a NSURL-style file ref.
  mail.save(attachment, { in: Path(path) });
}

function makeTempPath(name) {
  const sanitized = String(name || "attachment").replace(/[\/\0]/g, "_");
  const stamp = Date.now() + "-" + Math.floor(Math.random() * 1e6);
  return "/tmp/apple-productivity-" + stamp + "-" + sanitized;
}

function fileSize(path) {
  try {
    ObjC.import("Foundation");
    const fm = $.NSFileManager.defaultManager;
    const attrs = fm.attributesOfItemAtPathError($(path), null);
    if (!attrs || attrs.isNil()) return null;
    const size = attrs.objectForKey($("NSFileSize"));
    if (!size || size.isNil()) return null;
    return Number(size.unsignedLongLongValue);
  } catch (error) {
    return null;
  }
}

function readFileAsBase64(path) {
  ObjC.import("Foundation");
  const data = $.NSData.dataWithContentsOfFile($(path));
  if (!data || data.isNil()) {
    throw new Error("Could not read attachment from temp file: " + path);
  }
  return ObjC.unwrap(data.base64EncodedStringWithOptions(0));
}

function removeFile(path) {
  try {
    ObjC.import("Foundation");
    $.NSFileManager.defaultManager.removeItemAtPathError($(path), null);
  } catch (error) {
    // best effort
  }
}

function createMailMessage(input) {
  const message = mail.OutgoingMessage({
    subject: String(input.subject || ""),
    content: String(input.body || ""),
    visible: Boolean(input.open_in_mail),
  });
  mail.outgoingMessages.push(message);
  addRecipients(message, "toRecipients", input.to);
  addRecipients(message, "ccRecipients", input.cc);
  addRecipients(message, "bccRecipients", input.bcc);
  if (input.send_now) mail.send(message);
  return { sent: Boolean(input.send_now), action: "create", message: outgoingMessageSummary(message) };
}

function replyMailMessage(input) {
  const reply = mail.reply(findMailMessageById(input.message_id), {
    openingWindow: Boolean(input.open_in_mail),
    replyAll: Boolean(input.reply_all),
  });
  if (input.send_now) mail.send(reply);
  return { sent: Boolean(input.send_now), action: "reply", message: outgoingMessageSummary(reply) };
}

function forwardMailMessage(input) {
  const forwarded = mail.forward(findMailMessageById(input.message_id), {
    openingWindow: Boolean(input.open_in_mail),
  });
  addRecipients(forwarded, "toRecipients", input.to);
  addRecipients(forwarded, "ccRecipients", input.cc);
  addRecipients(forwarded, "bccRecipients", input.bcc);
  if (input.send_now) mail.send(forwarded);
  return { sent: Boolean(input.send_now), action: "forward", message: outgoingMessageSummary(forwarded) };
}

function listCalendarEvents(input) {
  const limit = clampLimit(input.limit, 25);
  const query = normalizeLower(input.search);
  const startBound = input.date_from ? parseDateInput(input.date_from) : null;
  const endBound = input.date_to ? parseDateInput(input.date_to) : null;
  const calendars = input.calendar_name ? [resolveCalendarByName(input.calendar_name)] : toArray(calendar.calendars());
  const items = [];
  for (let i = 0; i < calendars.length; i += 1) {
    const currentCalendar = calendars[i];
    const calendarName = safeCall(function () { return currentCalendar.name(); }, null);
    const events = toArray(currentCalendar.events());
    for (let j = 0; j < events.length; j += 1) {
      const event = events[j];
      if (query && !matchesCalendarQuery(event, query)) continue;
      const startDate = safeCall(function () { return event.startDate(); }, null);
      const endDate = safeCall(function () { return event.endDate(); }, null);
      if (startBound && startDate && new Date(startDate) < startBound) continue;
      if (endBound && endDate && new Date(endDate) > endBound) continue;
      items.push(calendarEventSummary(event, calendarName));
    }
  }
  items.sort(function (left, right) {
    const leftValue = left.startDate || "";
    const rightValue = right.startDate || "";
    return leftValue < rightValue ? -1 : leftValue > rightValue ? 1 : 0;
  });
  return { count: Math.min(items.length, limit), events: items.slice(0, limit) };
}

function calendarGetEvent(input) {
  const eventRef = findCalendarEventById(input.event_id);
  return calendarEventSummary(eventRef.event, eventRef.calendarName);
}

function createCalendarEvent(input) {
  const targetCalendar = input.calendar_name ? resolveCalendarByName(input.calendar_name) : toArray(calendar.calendars())[0];
  const targetCalendarName = safeCall(function () { return targetCalendar.name(); }, null);
  const event = calendar.Event(compactObject({
    summary: String(input.summary || ""),
    startDate: parseDateInput(input.start_date),
    endDate: parseDateInput(input.end_date),
  }));
  targetCalendar.events.push(event);
  if (input.location !== undefined) event.location = String(input.location || "");
  if (input.notes !== undefined) event.description = String(input.notes || "");
  if (input.all_day !== undefined) event.alldayEvent = Boolean(input.all_day);
  if (input.url !== undefined) event.url = String(input.url || "");
  if (input.recurrence !== undefined) event.recurrence = String(input.recurrence || "");
  return calendarEventSummary(event, targetCalendarName);
}

function updateCalendarEvent(input) {
  const eventRef = findCalendarEventById(input.event_id);
  const event = eventRef.event;
  if (input.calendar_name) {
    event.calendar = resolveCalendarByName(input.calendar_name);
  }
  if (input.summary !== undefined) event.summary = String(input.summary || "");
  if (input.start_date !== undefined) event.startDate = parseDateInput(input.start_date);
  if (input.end_date !== undefined) event.endDate = parseDateInput(input.end_date);
  if (input.location !== undefined) event.location = String(input.location || "");
  if (input.notes !== undefined) event.description = String(input.notes || "");
  if (input.all_day !== undefined) event.alldayEvent = Boolean(input.all_day);
  if (input.url !== undefined) event.url = String(input.url || "");
  if (input.recurrence !== undefined) event.recurrence = String(input.recurrence || "");
  const ownerCalendarName = input.calendar_name || eventRef.calendarName;
  return calendarEventSummary(event, ownerCalendarName);
}

function deleteCalendarEvent(input) {
  calendar.delete(findCalendarEventById(input.event_id));
  return { deleted: true, eventId: input.event_id };
}

function openCalendarEvent(input) {
  const eventRef = findCalendarEventById(input.event_id);
  calendar.open(eventRef.event);
  return { opened: true, eventId: input.event_id };
}

function listReminderLists(input) {
  const includeCounts = Boolean(input.include_counts);
  return toArray(reminders.lists()).map(function (list) {
    const summary = reminderListSummary(list);
    if (includeCounts) summary.reminderCount = safeCall(function () { return list.reminders().length; }, null);
    return summary;
  });
}

function createReminderList(input) {
  const list = reminders.List({ name: String(input.name) });
  reminders.lists.push(list);
  return reminderListSummary(list);
}

function updateReminderList(input) {
  const list = findReminderListById(input.list_id);
  list.name = String(input.name);
  return reminderListSummary(list);
}

function deleteReminderList(input) {
  reminders.delete(findReminderListById(input.list_id));
  return { deleted: true, listId: input.list_id };
}

function listReminderTasks(input) {
  const limit = clampLimit(input.limit, 25);
  const query = normalizeLower(input.search);
  const showCompleted = Boolean(input.show_completed);
  const sourceLists = input.list_name ? [resolveReminderListByName(input.list_name)] : toArray(reminders.lists());
  const items = [];
  for (let i = 0; i < sourceLists.length; i += 1) {
    const list = sourceLists[i];
    const tasks = toArray(list.reminders());
    for (let j = 0; j < tasks.length; j += 1) {
      const task = tasks[j];
      if (!showCompleted && safeCall(function () { return task.completed(); }, false)) continue;
      if (query && !matchesReminderQuery(task, query)) continue;
      items.push(reminderSummary(task));
      if (items.length >= limit) {
        return { count: items.length, reminders: items };
      }
    }
  }
  return { count: items.length, reminders: items };
}

function createReminderTask(input) {
  const list = input.list_name ? resolveReminderListByName(input.list_name) : toArray(reminders.lists())[0];
  const reminder = reminders.Reminder(compactObject({
    name: String(input.title || ""),
  }));
  list.reminders.push(reminder);
  if (input.notes !== undefined) reminder.body = String(input.notes || "");
  if (input.due_date !== undefined) reminder.dueDate = input.due_date ? parseDateInput(input.due_date) : null;
  if (input.priority !== undefined) reminder.priority = Number(input.priority);
  if (input.flagged !== undefined) reminder.flagged = Boolean(input.flagged);
  return reminderSummary(reminder);
}

function updateReminderTask(input) {
  const reminder = findReminderById(input.reminder_id);
  if (input.list_name) {
    reminder.container = resolveReminderListByName(input.list_name);
  }
  if (input.title !== undefined) reminder.name = String(input.title || "");
  if (input.notes !== undefined) reminder.body = String(input.notes || "");
  if (input.due_date !== undefined) reminder.dueDate = input.due_date ? parseDateInput(input.due_date) : null;
  if (input.completed !== undefined) reminder.completed = Boolean(input.completed);
  if (input.priority !== undefined) reminder.priority = Number(input.priority);
  if (input.flagged !== undefined) reminder.flagged = Boolean(input.flagged);
  return reminderSummary(reminder);
}

function deleteReminderTask(input) {
  reminders.delete(findReminderById(input.reminder_id));
  return { deleted: true, reminderId: input.reminder_id };
}

function setReminderCompleted(reminderId, completed) {
  const reminder = findReminderById(reminderId);
  reminder.completed = Boolean(completed);
  return reminderSummary(reminder);
}

function getMailAccounts(accountName) {
  const accounts = toArray(mail.accounts());
  if (!accountName) return accounts;
  const match = accounts.find(function (account) { return safeCall(function () { return account.name(); }, "") === accountName; });
  if (!match) throw new Error("Mail account not found: " + accountName);
  return [match];
}

function getSearchMailboxes(accountName) {
  return getMailAccounts(accountName).reduce(function (all, account) {
    return all.concat(flattenMailboxes(toArray(account.mailboxes()), account.name(), ""));
  }, []);
}

function resolveMailMailbox(mailboxName, accountName) {
  const candidates = getSearchMailboxes(accountName);
  const match = candidates.find(function (entry) { return entry.path === mailboxName || entry.name === mailboxName; });
  if (!match) throw new Error("Mail mailbox not found: " + mailboxName);
  return match;
}

function findMailMessageById(messageId, accountName, mailboxName) {
  const target = Number(messageId);
  let mailboxes;
  if (mailboxName) {
    // Caller pinned the mailbox: look there only. Avoids the full O(n) scan
    // and fails fast if the hint is wrong.
    mailboxes = [resolveMailMailbox(mailboxName, accountName)];
  } else if (accountName) {
    mailboxes = getSearchMailboxes(accountName);
  } else {
    mailboxes = getSearchMailboxes();
  }
  for (let i = 0; i < mailboxes.length; i += 1) {
    const messages = toArray(mailboxes[i].mailbox.messages());
    for (let j = 0; j < messages.length; j += 1) {
      const message = messages[j];
      if (safeCall(function () { return message.id(); }, null) === target) return message;
    }
  }
  const scope = mailboxName
    ? "mailbox " + mailboxName
    : accountName
    ? "account " + accountName
    : "any mailbox";
  throw new Error("Mail message " + messageId + " not found in " + scope + ".");
}

function resolveCalendarByName(name) {
  const match = toArray(calendar.calendars()).find(function (item) { return safeCall(function () { return item.name(); }, "") === name; });
  if (!match) throw new Error("Calendar not found: " + name);
  return match;
}

function findCalendarEventById(eventId) {
  const parsedId = parseCalendarEventId(eventId);
  const calendars = parsedId.calendarName
    ? [resolveCalendarByName(parsedId.calendarName)]
    : toArray(calendar.calendars());
  for (let i = 0; i < calendars.length; i += 1) {
    const ownerCalendar = calendars[i];
    const ownerCalendarName = safeCall(function () { return ownerCalendar.name(); }, null);
    const events = toArray(ownerCalendar.events());
    for (let j = 0; j < events.length; j += 1) {
      const event = events[j];
      if (safeCall(function () { return event.uid(); }, null) === parsedId.uid) {
        return {
          event: event,
          calendar: ownerCalendar,
          calendarName: ownerCalendarName,
          uid: parsedId.uid,
        };
      }
    }
  }
  throw new Error("Calendar event not found: " + eventId);
}

function resolveReminderListByName(name) {
  const match = toArray(reminders.lists()).find(function (list) { return safeCall(function () { return list.name(); }, "") === name; });
  if (!match) throw new Error("Reminders list not found: " + name);
  return match;
}

function findReminderListById(listId) {
  const match = toArray(reminders.lists()).find(function (list) { return safeCall(function () { return list.id(); }, null) === listId; });
  if (!match) throw new Error("Reminders list not found: " + listId);
  return match;
}

function findReminderById(reminderId) {
  const lists = toArray(reminders.lists());
  for (let i = 0; i < lists.length; i += 1) {
    const tasks = toArray(lists[i].reminders());
    for (let j = 0; j < tasks.length; j += 1) {
      const reminder = tasks[j];
      if (safeCall(function () { return reminder.id(); }, null) === reminderId) return reminder;
    }
  }
  throw new Error("Reminder not found: " + reminderId);
}

function accountSummary(account) {
  return {
    name: safeCall(function () { return account.name(); }, null),
    userName: safeCall(function () { return account.userName(); }, null),
    fullName: safeCall(function () { return account.fullName(); }, null),
    enabled: safeCall(function () { return account.enabled(); }, null),
  };
}

function flattenMailboxes(mailboxes, accountName, prefix) {
  return toArray(mailboxes).reduce(function (all, mailbox) {
    const name = safeCall(function () { return mailbox.name(); }, "Unknown");
    const path = prefix ? prefix + "/" + name : name;
    const entry = { mailbox: mailbox, name: name, path: path, accountName: accountName };
    return all.concat([entry]).concat(flattenMailboxes(toArray(safeCall(function () { return mailbox.mailboxes(); }, [])), accountName, path));
  }, []);
}

function matchesMailQuery(message, query) {
  if (!query) return true;
  const haystacks = [
    safeCall(function () { return message.subject(); }, ""),
    safeCall(function () { return message.sender(); }, ""),
    safeCall(function () { return message.content(); }, ""),
    safeCall(function () { return message.messageId(); }, ""),
  ];
  return haystacks.some(function (value) { return String(value || "").toLowerCase().indexOf(query) !== -1; });
}

function senderMatchesFilter(sender, filter) {
  if (!filter) return true;
  if (sender.indexOf(filter) !== -1) return true;
  return extractEmailAddresses(sender).some(function (address) { return address.indexOf(filter) !== -1; });
}

function extractEmailAddresses(value) {
  const matches = String(value || "").toLowerCase().match(/[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}/g);
  return matches || [];
}

function matchesCalendarQuery(event, query) {
  const haystacks = [
    safeCall(function () { return event.summary(); }, ""),
    safeCall(function () { return event.location(); }, ""),
    safeCall(function () { return event.description(); }, ""),
  ];
  return haystacks.some(function (value) { return String(value || "").toLowerCase().indexOf(query) !== -1; });
}

function matchesReminderQuery(reminder, query) {
  const haystacks = [
    safeCall(function () { return reminder.name(); }, ""),
    safeCall(function () { return reminder.body(); }, ""),
  ];
  return haystacks.some(function (value) { return String(value || "").toLowerCase().indexOf(query) !== -1; });
}

function messageSummary(message, includeBody, includeSource) {
  const summary = {
    id: safeCall(function () { return message.id(); }, null),
    messageId: safeCall(function () { return message.messageId(); }, null),
    subject: safeCall(function () { return message.subject(); }, null),
    sender: safeCall(function () { return message.sender(); }, null),
    read: safeCall(function () { return message.readStatus(); }, null),
    flagged: safeCall(function () { return message.flaggedStatus(); }, null),
    dateReceived: toIsoString(safeCall(function () { return message.dateReceived(); }, null)),
    dateSent: toIsoString(safeCall(function () { return message.dateSent(); }, null)),
    mailbox: safeCall(function () { return message.mailbox().name(); }, null),
    account: safeCall(function () { return message.mailbox().account().name(); }, null),
    to: recipients(message, "toRecipients"),
    cc: recipients(message, "ccRecipients"),
    bcc: recipients(message, "bccRecipients"),
    attachments: toArray(safeCall(function () { return message.mailAttachments(); }, [])).map(function (attachment) {
      return {
        name: safeCall(function () { return attachment.name(); }, null),
        mimeType: safeCall(function () { return attachment.mimeType(); }, null),
        downloaded: safeCall(function () { return attachment.downloaded(); }, null),
      };
    }),
  };
  if (includeBody) summary.content = safeCall(function () { return message.content(); }, null);
  else summary.preview = String(safeCall(function () { return message.content(); }, "") || "").slice(0, 280);
  if (includeSource) summary.source = safeCall(function () { return message.source(); }, null);
  return summary;
}

function outgoingMessageSummary(message) {
  return {
    subject: safeCall(function () { return message.subject(); }, null),
    sender: safeCall(function () { return message.sender(); }, null),
    visible: safeCall(function () { return message.visible(); }, null),
    to: recipients(message, "toRecipients"),
    cc: recipients(message, "ccRecipients"),
    bcc: recipients(message, "bccRecipients"),
  };
}

function recipients(message, propertyName) {
  return toArray(safeCall(function () { return message[propertyName](); }, [])).map(function (recipient) {
    return { address: safeCall(function () { return recipient.address(); }, null), name: safeCall(function () { return recipient.name(); }, null) };
  });
}

function addRecipients(message, propertyName, addresses) {
  toArray(addresses).forEach(function (address) {
    message[propertyName].push(mail.ToRecipient({ address: String(address) }));
  });
}

function calendarSummary(item) {
  const name = safeCall(function () { return item.name(); }, null);
  return {
    id: name,
    name: name,
  };
}

function calendarEventSummary(event, ownerCalendarName) {
  const uid = safeCall(function () { return event.uid(); }, null);
  const calendarName = ownerCalendarName || safeCall(function () { return event.calendar().name(); }, null);
  return {
    id: makeCalendarEventId(calendarName, uid),
    uid: uid,
    summary: safeCall(function () { return event.summary(); }, null),
    location: safeCall(function () { return event.location(); }, null),
    notes: safeCall(function () { return event.description(); }, null),
    calendar: calendarName,
    startDate: toIsoString(safeCall(function () { return event.startDate(); }, null)),
    endDate: toIsoString(safeCall(function () { return event.endDate(); }, null)),
    allDay: safeCall(function () { return event.alldayEvent(); }, null),
    url: safeCall(function () { return event.url(); }, null),
    recurrence: safeCall(function () { return event.recurrence(); }, null),
  };
}

function reminderListSummary(list) {
  return {
    id: safeCall(function () { return list.id(); }, null),
    name: safeCall(function () { return list.name(); }, null),
  };
}

function reminderSummary(reminder) {
  return {
    id: safeCall(function () { return reminder.id(); }, null),
    title: safeCall(function () { return reminder.name(); }, null),
    notes: safeCall(function () { return reminder.body(); }, null),
    completed: safeCall(function () { return reminder.completed(); }, null),
    dueDate: toIsoString(safeCall(function () { return reminder.dueDate(); }, null)),
    list: safeCall(function () { return reminder.container().name(); }, null),
    priority: safeCall(function () { return reminder.priority(); }, null),
    flagged: safeCall(function () { return reminder.flagged(); }, null),
  };
}

function parseDateInput(value) {
  const raw = String(value || "").trim();
  const normalized = raw.indexOf("T") === -1 && raw.indexOf(" ") !== -1 ? raw.replace(" ", "T") : raw;
  const parsed = new Date(normalized);
  if (Number.isNaN(parsed.getTime())) throw new Error("Invalid date: " + value);
  return parsed;
}

function normalizeLower(value) {
  return value ? String(value).trim().toLowerCase() : "";
}

function clampLimit(value, defaultValue) {
  const parsed = Number(value || defaultValue);
  if (!isFinite(parsed)) return defaultValue;
  return Math.max(1, Math.min(100, Math.floor(parsed)));
}

function clampOffset(value) {
  const parsed = Number(value || 0);
  if (!isFinite(parsed) || parsed < 0) return 0;
  return Math.min(10000, Math.floor(parsed));
}

function toIsoString(value) {
  if (!value) return null;
  try {
    return new Date(value).toISOString();
  } catch (error) {
    return String(value);
  }
}

function toArray(value) {
  if (!value) return [];
  if (Array.isArray(value)) return value;
  return Array.from(value);
}

function safeCall(fn, fallback) {
  try {
    return fn();
  } catch (error) {
    return fallback;
  }
}

function sanitize(value) {
  return JSON.parse(JSON.stringify(value, function (key, innerValue) {
    if (typeof innerValue === "undefined") return null;
    return innerValue;
  }));
}

function compactObject(value) {
  const result = {};
  Object.keys(value).forEach(function (key) {
    if (typeof value[key] !== "undefined") result[key] = value[key];
  });
  return result;
}

function makeCalendarEventId(calendarName, uid) {
  if (!uid) return null;
  if (!calendarName) return uid;
  return encodeURIComponent(calendarName) + "::" + encodeURIComponent(uid);
}

function parseCalendarEventId(eventId) {
  const raw = String(eventId || "");
  const separatorIndex = raw.indexOf("::");
  if (separatorIndex === -1) {
    return { calendarName: null, uid: raw };
  }
  return {
    calendarName: decodeURIComponent(raw.slice(0, separatorIndex)),
    uid: decodeURIComponent(raw.slice(separatorIndex + 2)),
  };
}
