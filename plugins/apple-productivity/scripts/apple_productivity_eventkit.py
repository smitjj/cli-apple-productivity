#!/usr/bin/env python3
"""PyObjC EventKit backend for Calendar and Reminders.

Direct binding to the EventKit framework that Calendar.app and Reminders.app
both use. Avoids JXA's known quirks (silent ``calendar.delete()`` no-op),
unlocks EventKit-only features (real RRULEs, multiple alarms, timezones,
geofence triggers, source disambiguation), and is faster than osascript.

The whole module is best-effort: if PyObjC is missing or EventKit access is
denied, :func:`open_default` returns None and the service falls back to JXA.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Optional


# Entity-type constants from EventKit (kept as module-level so we don't have
# to reach into objc-bridged enums; the values are stable parts of the
# framework's public API).
_EK_ENTITY_TYPE_EVENT = 0
_EK_ENTITY_TYPE_REMINDER = 1


class EventKitUnavailable(RuntimeError):
    """EventKit backend cannot be used (missing PyObjC, denied access, etc.)."""


def open_default(logger: Optional[logging.Logger] = None) -> Optional["EventKitBackend"]:
    """Probe for PyObjC + EventKit and request access.

    Returns an opened backend on success, or None when EventKit cannot be
    used. Callers should fall back to JXA on None.

    Note: the access request is synchronous via a NSCondition, with a 5-second
    timeout for the user to respond to the OS prompt. If the user has already
    granted/denied access, this returns immediately.
    """
    try:
        import objc  # noqa: F401  -- import smoke test
        import EventKit
        import Foundation  # noqa: F401
    except ImportError as exc:
        if logger:
            logger.info("PyObjC/EventKit not importable: %s", exc)
        return None

    backend = EventKitBackend(logger=logger)
    if not backend.request_access():
        return None
    return backend


class EventKitBackend:
    """Thin facade over EKEventStore providing the operations the service
    layer needs. Read paths can stay on JXA; this class focuses on writes
    plus the new fields (alarms, timezone, source).
    """

    ACCESS_TIMEOUT_SECONDS = 5.0

    def __init__(self, logger: Optional[logging.Logger] = None) -> None:
        self.logger = logger
        self._store = None
        self._event_access = None
        self._reminder_access = None

    # ------------------------------------------------------------------
    # Access / lifecycle
    # ------------------------------------------------------------------

    def request_access(self) -> bool:
        try:
            import EventKit
        except ImportError:
            return False
        store = EventKit.EKEventStore.alloc().init()
        self._event_access = self._request_access_for(store, _EK_ENTITY_TYPE_EVENT)
        self._reminder_access = self._request_access_for(store, _EK_ENTITY_TYPE_REMINDER)
        if not (self._event_access or self._reminder_access):
            if self.logger:
                self.logger.info("EventKit access denied for both events and reminders")
            return False
        self._store = store
        return True

    def _request_access_for(self, store, entity_type: int) -> bool:
        condition = threading.Condition()
        result = {"granted": False, "responded": False, "error": None}

        def callback(granted: bool, error) -> None:
            with condition:
                result["granted"] = bool(granted)
                result["error"] = error
                result["responded"] = True
                condition.notify_all()

        store.requestAccessToEntityType_completion_(entity_type, callback)

        with condition:
            condition.wait(timeout=self.ACCESS_TIMEOUT_SECONDS)
            if not result["responded"]:
                if self.logger:
                    self.logger.info(
                        "EventKit access prompt timed out for entity_type=%s", entity_type
                    )
                return False
            return result["granted"]

    @property
    def has_event_access(self) -> bool:
        return bool(self._event_access)

    @property
    def has_reminder_access(self) -> bool:
        return bool(self._reminder_access)

    # ------------------------------------------------------------------
    # Permissions diagnostic (parallels JXA's mail_permissions_check)
    # ------------------------------------------------------------------

    def permissions_snapshot(self) -> dict:
        return {
            "event_access": self._event_access,
            "reminder_access": self._reminder_access,
            "store_open": self._store is not None,
        }

    # ------------------------------------------------------------------
    # Calendar events
    # ------------------------------------------------------------------

    def create_event(self, args: dict) -> dict:
        self._require_event_access()
        import EventKit
        import Foundation

        event = EventKit.EKEvent.eventWithEventStore_(self._store)
        event.setTitle_(str(args.get("summary") or ""))
        cal = self._resolve_calendar(args.get("calendar_name"), args.get("source"))
        event.setCalendar_(cal)
        event.setStartDate_(_to_nsdate(args["start_date"]))
        event.setEndDate_(_to_nsdate(args["end_date"]))
        if args.get("location") is not None:
            event.setLocation_(str(args["location"]))
        if args.get("notes") is not None:
            event.setNotes_(str(args["notes"]))
        if args.get("all_day") is not None:
            event.setAllDay_(bool(args["all_day"]))
        if args.get("url"):
            event.setURL_(Foundation.NSURL.URLWithString_(str(args["url"])))
        if args.get("timezone"):
            tz = Foundation.NSTimeZone.timeZoneWithName_(str(args["timezone"]))
            if tz is not None:
                event.setTimeZone_(tz)
        if args.get("recurrence_rule"):
            rule = _parse_rrule(args["recurrence_rule"])
            if rule is not None:
                event.setRecurrenceRules_([rule])
        for alarm_offset in args.get("alarms", []) or []:
            alarm = EventKit.EKAlarm.alarmWithRelativeOffset_(float(alarm_offset))
            event.addAlarm_(alarm)

        success, error = self._store.saveEvent_span_error_(
            event, EventKit.EKSpanThisEvent, None
        )
        if not success:
            raise RuntimeError(_describe_error(error, "save event"))
        return self._event_summary(event)

    def update_event(self, args: dict) -> dict:
        self._require_event_access()
        import EventKit

        event = self._fetch_event(args["event_id"])
        if args.get("summary") is not None:
            event.setTitle_(str(args["summary"]))
        if args.get("calendar_name"):
            event.setCalendar_(self._resolve_calendar(args["calendar_name"], args.get("source")))
        if args.get("start_date") is not None:
            event.setStartDate_(_to_nsdate(args["start_date"]))
        if args.get("end_date") is not None:
            event.setEndDate_(_to_nsdate(args["end_date"]))
        if args.get("location") is not None:
            event.setLocation_(str(args["location"]))
        if args.get("notes") is not None:
            event.setNotes_(str(args["notes"]))
        if args.get("all_day") is not None:
            event.setAllDay_(bool(args["all_day"]))
        if args.get("url") is not None:
            import Foundation
            event.setURL_(Foundation.NSURL.URLWithString_(str(args["url"])))
        if args.get("timezone"):
            import Foundation
            tz = Foundation.NSTimeZone.timeZoneWithName_(str(args["timezone"]))
            if tz is not None:
                event.setTimeZone_(tz)
        if args.get("recurrence_rule") is not None:
            rule = _parse_rrule(args["recurrence_rule"])
            event.setRecurrenceRules_([rule] if rule else [])

        success, error = self._store.saveEvent_span_error_(
            event, EventKit.EKSpanThisEvent, None
        )
        if not success:
            raise RuntimeError(_describe_error(error, "update event"))
        return self._event_summary(event)

    def delete_event(self, event_id: str) -> dict:
        self._require_event_access()
        import EventKit

        event = self._fetch_event(event_id)
        success, error = self._store.removeEvent_span_error_(
            event, EventKit.EKSpanThisEvent, None
        )
        if not success:
            raise RuntimeError(_describe_error(error, "delete event"))
        return {"deleted": True, "eventId": event_id}

    # ------------------------------------------------------------------
    # Reminders
    # ------------------------------------------------------------------

    def create_reminder(self, args: dict) -> dict:
        self._require_reminder_access()
        import EventKit

        reminder = EventKit.EKReminder.reminderWithEventStore_(self._store)
        reminder.setTitle_(str(args.get("title") or ""))
        list_obj = self._resolve_reminders_list(args.get("list_name"), args.get("source"))
        reminder.setCalendar_(list_obj)
        if args.get("notes") is not None:
            reminder.setNotes_(str(args["notes"]))
        if args.get("priority") is not None:
            reminder.setPriority_(int(args["priority"]))
        if args.get("flagged") is not None:
            # EKReminder has no `flagged` property; we model it via priority
            # (1=high) when set true, otherwise leave priority as-is. JXA's
            # path still sees the boolean directly.
            if args["flagged"] and args.get("priority") is None:
                reminder.setPriority_(1)
        if args.get("due_date"):
            reminder.setDueDateComponents_(_date_components(args["due_date"]))
        for alarm_offset in args.get("alarms", []) or []:
            alarm = EventKit.EKAlarm.alarmWithRelativeOffset_(float(alarm_offset))
            reminder.addAlarm_(alarm)
        if args.get("geofence"):
            alarm = _build_geofence_alarm(args["geofence"])
            if alarm is not None:
                reminder.addAlarm_(alarm)

        success, error = self._store.saveReminder_commit_error_(reminder, True, None)
        if not success:
            raise RuntimeError(_describe_error(error, "save reminder"))
        return self._reminder_summary(reminder)

    def delete_reminder(self, reminder_id: str) -> dict:
        self._require_reminder_access()

        reminder = self._fetch_reminder(reminder_id)
        success, error = self._store.removeReminder_commit_error_(reminder, True, None)
        if not success:
            raise RuntimeError(_describe_error(error, "delete reminder"))
        return {"deleted": True, "reminderId": reminder_id}

    def update_reminder(self, args: dict) -> dict:
        self._require_reminder_access()

        reminder = self._fetch_reminder(args["reminder_id"])
        if args.get("title") is not None:
            reminder.setTitle_(str(args["title"]))
        if args.get("list_name"):
            reminder.setCalendar_(self._resolve_reminders_list(args.get("list_name"), args.get("source")))
        if args.get("notes") is not None:
            reminder.setNotes_(str(args["notes"]))
        if args.get("priority") is not None:
            reminder.setPriority_(int(args["priority"]))
        if args.get("flagged") is not None and args["flagged"] and args.get("priority") is None:
            reminder.setPriority_(1)
        if args.get("due_date") is not None:
            reminder.setDueDateComponents_(_date_components(args["due_date"]))
        if args.get("completed") is not None:
            reminder.setCompleted_(bool(args["completed"]))
        if "alarms" in args:
            _replace_alarms(reminder, args.get("alarms") or [])
        if args.get("geofence"):
            alarm = _build_geofence_alarm(args["geofence"])
            if alarm is not None:
                reminder.addAlarm_(alarm)

        success, error = self._store.saveReminder_commit_error_(reminder, True, None)
        if not success:
            raise RuntimeError(_describe_error(error, "update reminder"))
        return self._reminder_summary(reminder)

    def set_reminder_completed(self, reminder_id: str, completed: bool) -> dict:
        self._require_reminder_access()

        reminder = self._fetch_reminder(reminder_id)
        reminder.setCompleted_(bool(completed))
        success, error = self._store.saveReminder_commit_error_(reminder, True, None)
        if not success:
            raise RuntimeError(_describe_error(error, "complete reminder"))
        return self._reminder_summary(reminder)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_event_access(self) -> None:
        if not self.has_event_access:
            raise EventKitUnavailable("EventKit event access not granted")

    def _require_reminder_access(self) -> None:
        if not self.has_reminder_access:
            raise EventKitUnavailable("EventKit reminder access not granted")

    def _resolve_calendar(self, name: Optional[str], source_filter: Optional[str]):
        import EventKit

        calendars = self._store.calendarsForEntityType_(_EK_ENTITY_TYPE_EVENT)
        if not name:
            return self._store.defaultCalendarForNewEvents()
        match = None
        for cal in calendars:
            if str(cal.title()) != name:
                continue
            if source_filter and not _source_matches(cal, source_filter):
                continue
            match = cal
            break
        if match is None:
            raise RuntimeError(
                f"Calendar not found: {name}"
                + (f" (source={source_filter})" if source_filter else "")
            )
        return match

    def _resolve_reminders_list(self, name: Optional[str], source_filter: Optional[str]):
        calendars = self._store.calendarsForEntityType_(_EK_ENTITY_TYPE_REMINDER)
        if not name:
            return self._store.defaultCalendarForNewReminders()
        for cal in calendars:
            if str(cal.title()) != name:
                continue
            if source_filter and not _source_matches(cal, source_filter):
                continue
            return cal
        raise RuntimeError(
            f"Reminders list not found: {name}"
            + (f" (source={source_filter})" if source_filter else "")
        )

    def _fetch_event(self, event_id: str):
        # Our event_id format is `<calendar-name-encoded>::<uid-encoded>`.
        # EventKit identifies events by `eventIdentifier` (a different scheme)
        # OR by uid + calendar. Use predicate over a wide date range to find
        # by uid since that's what JXA exposes today.
        import EventKit
        from urllib.parse import unquote

        if "::" not in event_id:
            raise RuntimeError("calendar event ids must include both the calendar and uid.")
        cal_name, uid = event_id.split("::", 1)
        cal_name = unquote(cal_name)
        uid = unquote(uid)
        event = self._store.calendarItemWithIdentifier_(uid)
        if event is not None:
            return event
        # Fallback: scan calendars by uid.
        calendars = self._store.calendarsForEntityType_(_EK_ENTITY_TYPE_EVENT)
        for cal in calendars:
            if str(cal.title()) != cal_name:
                continue
            # 1-year window around today.
            import Foundation
            now = Foundation.NSDate.date()
            past = now.dateByAddingTimeInterval_(-365 * 24 * 3600)
            future = now.dateByAddingTimeInterval_(365 * 24 * 3600)
            predicate = self._store.predicateForEventsWithStartDate_endDate_calendars_(
                past, future, [cal]
            )
            for candidate in self._store.eventsMatchingPredicate_(predicate) or []:
                if str(candidate.calendarItemExternalIdentifier() or "") == uid:
                    return candidate
                if str(candidate.eventIdentifier() or "") == uid:
                    return candidate
        raise RuntimeError(f"Calendar event not found: {event_id}")

    def _fetch_reminder(self, reminder_id: str):
        item = self._store.calendarItemWithIdentifier_(reminder_id)
        if item is None:
            raise RuntimeError(f"Reminder not found: {reminder_id}")
        return item

    def _event_summary(self, event) -> dict:
        cal = event.calendar()
        rrule = (event.recurrenceRules() or [None])[0]
        return {
            "id": _make_event_id(str(cal.title()), str(event.calendarItemExternalIdentifier() or event.eventIdentifier())),
            "uid": str(event.calendarItemExternalIdentifier() or event.eventIdentifier()),
            "summary": str(event.title() or ""),
            "location": str(event.location() or "") or None,
            "notes": str(event.notes() or "") or None,
            "calendar": str(cal.title()),
            "startDate": _from_nsdate(event.startDate()),
            "endDate": _from_nsdate(event.endDate()),
            "allDay": bool(event.isAllDay()),
            "url": str(event.URL().absoluteString()) if event.URL() else None,
            "timezone": str(event.timeZone().name()) if event.timeZone() else None,
            "recurrence": _format_rrule(rrule) if rrule else None,
            "alarms": [float(a.relativeOffset()) for a in (event.alarms() or [])],
            "source": str(cal.source().title()) if cal.source() else None,
        }

    def _reminder_summary(self, reminder) -> dict:
        cal = reminder.calendar()
        return {
            "id": str(reminder.calendarItemIdentifier()),
            "title": str(reminder.title() or ""),
            "notes": str(reminder.notes() or "") or None,
            "completed": bool(reminder.isCompleted()),
            "dueDate": _components_to_iso(reminder.dueDateComponents()),
            "list": str(cal.title()),
            "priority": int(reminder.priority()),
            "alarms": [float(a.relativeOffset()) for a in (reminder.alarms() or [])],
            "source": str(cal.source().title()) if cal.source() else None,
        }


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


def _to_nsdate(value):
    """Convert one of our ISO date strings to NSDate."""
    import Foundation

    s = str(value).strip()
    if len(s) == 10:
        dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        normalized = s.replace(" ", "T")
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    return Foundation.NSDate.dateWithTimeIntervalSince1970_(dt.timestamp())


def _from_nsdate(nsdate) -> Optional[str]:
    if nsdate is None:
        return None
    secs = float(nsdate.timeIntervalSince1970())
    return datetime.fromtimestamp(secs, tz=timezone.utc).isoformat()


def _date_components(value):
    """Build NSDateComponents (year/month/day/hour/minute) from our ISO date."""
    import Foundation

    s = str(value).strip()
    if len(s) == 10:
        dt = datetime.strptime(s, "%Y-%m-%d")
    else:
        normalized = s.replace(" ", "T")
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    components = Foundation.NSDateComponents.alloc().init()
    components.setYear_(dt.year)
    components.setMonth_(dt.month)
    components.setDay_(dt.day)
    if len(s) > 10:
        components.setHour_(dt.hour)
        components.setMinute_(dt.minute)
        components.setSecond_(dt.second)
    return components


def _components_to_iso(components) -> Optional[str]:
    if components is None:
        return None
    year = components.year()
    month = components.month()
    day = components.day()
    if year <= 0 or month <= 0 or day <= 0:
        return None
    hour = components.hour()
    minute = components.minute()
    second = components.second()
    if hour > 0 or minute > 0 or second > 0:
        return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}"
    return f"{year:04d}-{month:02d}-{day:02d}"


def _parse_rrule(rule_text: str):
    """Parse an RFC 5545 RRULE string into an EKRecurrenceRule.

    Supports the common shape ``FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE``. Returns
    None on any unsupported field so the caller can keep going without rrule.
    """
    try:
        import EventKit
    except ImportError:
        return None

    parts = {}
    for chunk in str(rule_text).split(";"):
        if "=" not in chunk:
            return None
        key, value = chunk.split("=", 1)
        parts[key.strip().upper()] = value.strip()

    freq_map = {
        "DAILY": EventKit.EKRecurrenceFrequencyDaily,
        "WEEKLY": EventKit.EKRecurrenceFrequencyWeekly,
        "MONTHLY": EventKit.EKRecurrenceFrequencyMonthly,
        "YEARLY": EventKit.EKRecurrenceFrequencyYearly,
    }
    freq = freq_map.get(parts.get("FREQ", "").upper())
    if freq is None:
        return None
    interval = int(parts.get("INTERVAL", "1") or 1)

    end = None
    if "COUNT" in parts:
        end = EventKit.EKRecurrenceEnd.recurrenceEndWithOccurrenceCount_(int(parts["COUNT"]))
    elif "UNTIL" in parts:
        end = EventKit.EKRecurrenceEnd.recurrenceEndWithEndDate_(_to_nsdate(parts["UNTIL"]))

    days = None
    if "BYDAY" in parts:
        day_codes = {"SU": 1, "MO": 2, "TU": 3, "WE": 4, "TH": 5, "FR": 6, "SA": 7}
        days = []
        for token in parts["BYDAY"].split(","):
            code = token.strip().upper()
            if code in day_codes:
                days.append(EventKit.EKRecurrenceDayOfWeek.dayOfWeek_(day_codes[code]))

    rule = EventKit.EKRecurrenceRule.alloc().initRecurrenceWithFrequency_interval_daysOfTheWeek_daysOfTheMonth_monthsOfTheYear_weeksOfTheYear_daysOfTheYear_setPositions_end_(
        freq,
        interval,
        days,
        None,
        None,
        None,
        None,
        None,
        end,
    )
    return rule


def _format_rrule(rule) -> Optional[str]:
    """Best-effort RRULE → string serialization for symmetry with ``_parse_rrule``."""
    try:
        import EventKit
    except ImportError:
        return None
    if rule is None:
        return None
    freq_map = {
        EventKit.EKRecurrenceFrequencyDaily: "DAILY",
        EventKit.EKRecurrenceFrequencyWeekly: "WEEKLY",
        EventKit.EKRecurrenceFrequencyMonthly: "MONTHLY",
        EventKit.EKRecurrenceFrequencyYearly: "YEARLY",
    }
    parts = [f"FREQ={freq_map.get(rule.frequency(), 'UNKNOWN')}"]
    if rule.interval() and rule.interval() != 1:
        parts.append(f"INTERVAL={rule.interval()}")
    days = rule.daysOfTheWeek() or []
    if days:
        codes = ["SU", "MO", "TU", "WE", "TH", "FR", "SA"]
        parts.append("BYDAY=" + ",".join(codes[d.dayOfTheWeek() - 1] for d in days))
    end = rule.recurrenceEnd()
    if end is not None:
        if end.occurrenceCount():
            parts.append(f"COUNT={end.occurrenceCount()}")
        elif end.endDate():
            parts.append(f"UNTIL={_from_nsdate(end.endDate())}")
    return ";".join(parts)


def _build_geofence_alarm(spec: dict):
    """Build an EKAlarm with a structured location trigger.

    ``spec`` is ``{lat, lon, radius_meters, proximity}`` where proximity is
    "enter" or "leave". Returns None if the spec is malformed.
    """
    try:
        import EventKit
        import CoreLocation
    except ImportError:
        return None
    try:
        lat = float(spec["lat"])
        lon = float(spec["lon"])
        radius = float(spec.get("radius_meters", 100))
    except (KeyError, TypeError, ValueError):
        return None
    coord = CoreLocation.CLLocationCoordinate2D(lat, lon)
    location = CoreLocation.CLLocation.alloc().initWithLatitude_longitude_(lat, lon)
    structured = EventKit.EKStructuredLocation.locationWithTitle_(spec.get("title", ""))
    structured.setGeoLocation_(location)
    structured.setRadius_(radius)
    alarm = EventKit.EKAlarm.alloc().init()
    alarm.setStructuredLocation_(structured)
    proximity = str(spec.get("proximity", "enter")).lower()
    alarm.setProximity_(
        EventKit.EKAlarmProximityEnter if proximity == "enter" else EventKit.EKAlarmProximityLeave
    )
    return alarm


def _replace_alarms(item, offsets: list) -> None:
    try:
        import EventKit
    except ImportError:
        return
    for alarm in item.alarms() or []:
        item.removeAlarm_(alarm)
    for alarm_offset in offsets:
        item.addAlarm_(EventKit.EKAlarm.alarmWithRelativeOffset_(float(alarm_offset)))


def _source_matches(calendar, filter_value: str) -> bool:
    """Match an EKCalendar's source title against a user filter.

    Accepts loose tokens like 'icloud', 'google', 'exchange', 'local'.
    """
    if not filter_value:
        return True
    source = calendar.source()
    if source is None:
        return False
    title = str(source.title() or "").lower()
    needle = filter_value.lower().strip()
    if needle in title:
        return True
    aliases = {
        "icloud": ("icloud",),
        "google": ("google", "gmail"),
        "exchange": ("exchange",),
        "local": ("on my mac", "local"),
    }
    for token in aliases.get(needle, ()):
        if token in title:
            return True
    return False


def _make_event_id(calendar_name: str, uid: str) -> str:
    from urllib.parse import quote

    return f"{quote(calendar_name, safe='')}::{quote(uid, safe='')}"


def _describe_error(error, action: str) -> str:
    if error is None:
        return f"EventKit failed to {action}"
    try:
        return f"EventKit failed to {action}: {error.localizedDescription()}"
    except AttributeError:
        return f"EventKit failed to {action}: {error}"
