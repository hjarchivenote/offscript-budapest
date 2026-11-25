"""Current opening estimates from explicitly checked, activity-specific schedules."""

import json
import re
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

BUDAPEST = ZoneInfo("Europe/Budapest")
SNAPSHOT = json.loads((Path(__file__).parent / "data" / "opening_hours.json").read_text())
SCHEDULES = SNAPSHOT["schedules"]
SPECIAL_DATES = set(SNAPSHOT["special_dates"])
MAX_SCHEDULE_AGE_DAYS = 90


@lru_cache(maxsize=8)
def special_days(year):
    # Gregorian Easter; these dates flag possible special hours, not closures.
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    weekday_adjustment = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * weekday_adjustment) // 451
    month = (h + weekday_adjustment - 7 * m + 114) // 31
    day = (h + weekday_adjustment - 7 * m + 114) % 31 + 1
    easter = date(year, month, day)
    fixed = {(1, 1), (3, 15), (5, 1), (8, 20), (10, 23), (11, 1), (12, 24), (12, 25), (12, 26)}
    return {date(year, month, day) for month, day in fixed} | {
        easter + timedelta(days=offset) for offset in (-2, 0, 1, 49, 50)
    }


def _special(day):
    return day.isoformat() in SPECIAL_DATES or day in special_days(day.year)


def _closed_day(details, day):
    return day.strftime("%m-%d") in details.get("closed_month_days", []) or any(
        date.fromisoformat(start) <= day <= date.fromisoformat(end)
        for start, end in details.get("closed_ranges", [])
    )


def _schedule_applies(schedule, day):
    age = (day - date.fromisoformat(schedule["checked_on"])).days
    return (
        # The check date is not the schedule's start date. Yesterday's published
        # overnight period can still apply on the day the source was checked.
        age <= MAX_SCHEDULE_AGE_DAYS
        and (not schedule.get("valid_from") or day >= date.fromisoformat(schedule["valid_from"]))
        and (not schedule.get("valid_until") or day <= date.fromisoformat(schedule["valid_until"]))
    )


def _slots(schedule, details, day):
    if not _schedule_applies(schedule, day):
        return None
    exceptions = schedule.get("exceptions", {})
    if day.isoformat() in exceptions:
        return exceptions[day.isoformat()]
    if _closed_day(details, day):
        return []
    if day in special_days(day.year):
        return schedule.get("holiday_hours")
    if day.isoformat() in SPECIAL_DATES:
        return None
    weekly = schedule.get("weekly")
    return weekly.get(str(day.weekday())) if isinstance(weekly, dict) else None


def _boundary(day, value):
    if value == "24:00":
        day, value = day + timedelta(days=1), "00:00"
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("Opening boundaries must be HH:MM local times")
    naive = datetime.combine(day, time.fromisoformat(value))
    first = naive.replace(tzinfo=BUDAPEST, fold=0)
    second = naive.replace(tzinfo=BUDAPEST, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise ValueError("Ambiguous or nonexistent opening boundary")
    if first.astimezone(UTC).astimezone(BUDAPEST).replace(tzinfo=None) != naive:
        raise ValueError("Nonexistent opening boundary")
    return first


def _interval(day, slot):
    start_text, end_text = slot
    if start_text == "24:00":
        raise ValueError("Only a closing boundary may use 24:00")
    start = _boundary(day, start_text)
    end = _boundary(day, end_text)
    if end == start:
        raise ValueError("Zero-length opening interval")
    if end < start:
        end = _boundary(day + timedelta(days=1), end_text)
    return start.astimezone(UTC), end.astimezone(UTC)


def _replaces_day(schedule, details, day):
    return (
        day.isoformat() in schedule.get("exceptions", {})
        or _closed_day(details, day)
        or _special(day)
        or not _schedule_applies(schedule, day)
    )


def day_availability(activity, day):
    """Date eligibility using the same exceptions and slots as Open now.

    Unknown hours remain browsable in ordinary date searches. An empty slot
    list is a confirmed closure; None does not establish either opening or closure.
    """
    details = activity.details
    schedule = details.get("opening_hours")
    try:
        if isinstance(schedule, dict) and schedule:
            age = (day - date.fromisoformat(schedule["checked_on"])).days
            if 0 <= age <= MAX_SCHEDULE_AGE_DAYS and _schedule_applies(schedule, day):
                slots = _slots(schedule, details, day)
                if slots is None:
                    return "unknown"
                # Validate published times before asserting that this day is open.
                for slot in slots:
                    _interval(day, slot)
                if slots:
                    return "open"
                # An ordinary closed weekday can still contain yesterday's tail.
                # A dated override or published closure cancels that carry-over.
                if not _replaces_day(schedule, details, day):
                    previous_day = day - timedelta(days=1)
                    midnight = _boundary(day, "00:00").astimezone(UTC)
                    for slot in _slots(schedule, details, previous_day) or []:
                        _, end = _interval(previous_day, slot)
                        if end > midnight:
                            return "open"
                return "closed"
        # Without a current clock-based schedule, retain the existing day-only
        # constraints. Do not revive an expired or future-checked exception.
        if _closed_day(details, day):
            return "closed"
        weekdays = details.get("weekdays")
        if weekdays is not None and day.weekday() not in weekdays:
            return "closed"
    except (KeyError, TypeError, ValueError):
        pass
    return "unknown"


def _current_window(schedule, details, now):
    """Return the continuous, confirmed interval containing this instant."""
    day = now.astimezone(BUDAPEST).date()
    today = _slots(schedule, details, day)
    if today is None and _replaces_day(schedule, details, day):
        return {"state": "unknown", "until": None, "end_reason": None}
    intervals = []
    for offset in (-1, 0, 1):
        origin = day + timedelta(days=offset)
        for slot in _slots(schedule, details, origin) or []:
            start, end = _interval(origin, slot)
            end_reason = "published_hours"
            following = origin + timedelta(days=1)
            midnight = _boundary(following, "00:00").astimezone(UTC)
            if end > midnight and _replaces_day(schedule, details, following):
                end = midnight
                if _slots(schedule, details, following) is None:
                    end_reason = "unconfirmed_next_day"
            intervals.append((start, end, end_reason))
    # Adjacent periods are continuous; a real break must never be crossed.
    merged = []
    for start, end, end_reason in sorted(intervals):
        if merged and start <= merged[-1][1]:
            previous_start, previous_end, _ = merged[-1]
            if end > previous_end:
                merged[-1] = (previous_start, end, end_reason)
            elif end == previous_end and end_reason == "unconfirmed_next_day":
                merged[-1] = (previous_start, previous_end, end_reason)
        else:
            merged.append((start, end, end_reason))
    current = now.astimezone(UTC)
    for start, end, end_reason in merged:
        if start <= current < end:
            return {"state": "open", "until": end, "end_reason": end_reason}
    previous = _slots(schedule, details, day - timedelta(days=1))
    return {
        "state": "unknown" if today is None or previous is None else "closed",
        "until": None,
        "end_reason": None,
    }


def _usage_window(schedule, details, now, opening_window):
    usage = schedule.get("usage_hours")
    if usage is None:
        if schedule.get("scope", "venue") in {"venue", "general_visits"}:
            return opening_window
        return {"state": "unknown", "until": None, "end_reason": None}
    if not isinstance(usage, dict):
        return {"state": "unknown", "until": None, "end_reason": None}
    day = now.astimezone(BUDAPEST).date()
    # Last-admission/order exceptions cannot establish visitor finish times.
    if day.isoformat() in schedule.get("exceptions", {}) and day.isoformat() not in usage.get("exceptions", {}):
        return {"state": "unknown", "until": None, "end_reason": None}
    usage_schedule = {
        **schedule,
        **usage,
        "exceptions": {
            **{day: None for day in schedule.get("exceptions", {})},
            **usage.get("exceptions", {}),
        },
        "holiday_hours": usage.get("holiday_hours"),
    }
    return _current_window(usage_schedule, details, now)


def opening_status(activity, now):
    local = now.astimezone(BUDAPEST)
    day = local.date()
    details = activity.details
    schedule = details.get("opening_hours") or {}
    if not isinstance(schedule, dict):
        schedule = {}
    scope = schedule.get("scope", "venue")
    result = {
        "state": "unknown",
        "label": "Hours to confirm",
        "reason": "No checked clock-based schedule is available for this activity.",
        "as_of": local.isoformat(),
        "timezone": "Europe/Budapest",
        "until": None,
        "window_end_reason": None,
        "entry_until": None,
        "orders_until": None,
        "usage_state": "unknown",
        "usable_until": None,
        "usable_end_reason": None,
        "usage_note": (schedule.get("usage_hours") or {}).get("note", "")
        if isinstance(schedule.get("usage_hours"), dict) else "",
        "source_url": schedule.get("source_url"),
        "hours_checked_on": schedule.get("checked_on"),
        "appointment_required": schedule.get("appointment_required"),
        "scope": scope,
        "note": schedule.get("note", ""),
    }
    if details.get("catalogue_active") is False:
        result["reason"] = "This saved listing is historical. Confirm current hours with the venue."
        return result
    if activity.kind in {"event", "programme"} or details.get("date_status") == "new_dates_needed":
        result["reason"] = "Use the dated programme; current admission has not been confirmed."
        return result
    if not schedule:
        return result
    try:
        age = (day - date.fromisoformat(schedule["checked_on"])).days
        if not 0 <= age <= MAX_SCHEDULE_AGE_DAYS:
            result["reason"] = "The opening schedule needs rechecking."
            return result
        if (
            schedule.get("valid_from") and day < date.fromisoformat(schedule["valid_from"])
        ) or (
            schedule.get("valid_until") and day > date.fromisoformat(schedule["valid_until"])
        ):
            result["reason"] = "The published seasonal schedule does not cover this date."
            return result
        today_override = day.isoformat() in schedule.get("exceptions", {}) or (
            day in special_days(day.year) and "holiday_hours" in schedule
        )
        if not today_override and _closed_day(details, day):
            result.update(state="closed", label="Closed now", reason="A published closure applies today.")
            return result
        window = _current_window(schedule, details, now)
        if window["state"] == "open":
            labels = {"food_service": "Food service open", "admission": "Admission open", "general_visits": "Open for visits"}
            until = window["until"].astimezone(BUDAPEST)
            result.update(state="open", label=labels.get(scope, "Open now"), reason="Within published operating hours; booking availability is not checked.", until=until.isoformat())
            result["window_end_reason"] = window["end_reason"]
            if scope == "admission":
                result["entry_until"] = until.isoformat()
            if scope == "food_service":
                result["orders_until"] = until.isoformat()
            try:
                usage = _usage_window(schedule, details, now, window)
            except (KeyError, TypeError, ValueError):
                usage = {"state": "unknown", "until": None, "end_reason": None}
            result["usage_state"] = usage["state"]
            result["usable_until"] = usage["until"].astimezone(BUDAPEST).isoformat() if usage["until"] else None
            result["usable_end_reason"] = usage["end_reason"]
        elif window["state"] == "unknown":
            result["reason"] = "Special-day opening hours have not been confirmed." if _special(day) else "Part of the current opening schedule is unconfirmed."
        else:
            labels = {"food_service": "Food service closed", "admission": "Admission closed", "general_visits": "General visits closed"}
            result.update(state="closed", label=labels.get(scope, "Closed now"), reason="Outside the published operating hours for this activity.")
    except (KeyError, TypeError, ValueError):
        result["reason"] = "The opening schedule needs checking before it can be used."
    return result
