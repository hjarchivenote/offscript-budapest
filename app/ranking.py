"""Explainable ordering of activities which have already passed every filter."""

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from itertools import groupby
from math import isfinite
from urllib.parse import urlsplit

from app.discovery import BUDAPEST, planning_window
from app.models import Activity
from app.opening_hours import MAX_SCHEDULE_AGE_DAYS, day_availability, opening_status
from app.provider_freshness import commercial_status
from app.visit_time import positive_minutes
from app.visit_window import current_visit


def order_description(when, query=None):
    parts = ["Every selected filter is required."]
    if query and query.strip():
        parts.append("Activity-name matches come first, followed by description and venue/location matches.")
    if when == "today":
        parts.append("Then we prioritise dated events yet to start today, in start-time order, and visits that fit published hours now.")
    elif when == "weekend":
        parts.append("Then we prioritise dated weekend events in start-time order and venues with confirmed opening days in your weekend.")
    parts.append("Next come recently reviewed price, visit-time and booking details. Equal matches are spread across venues.")
    return " ".join(parts)


def _recent(checked_on, today):
    try:
        return 0 <= (today - date.fromisoformat(checked_on)).days <= MAX_SCHEDULE_AGE_DAYS
    except (TypeError, ValueError):
        return False


def _link_available(value):
    try:
        parsed = urlsplit(value or "")
        return parsed.scheme == "https" and bool(parsed.netloc)
    except (TypeError, ValueError):
        return False


def _reason(text, *, time_sensitive=False, requires_opening_window=False):
    return {"text": text, "time_sensitive": time_sensitive, "requires_opening_window": requires_opening_window}


def _search_priority(activity, query):
    if not query:
        return 0, None
    title = activity.title.casefold()
    if query == title:
        return 0, "Activity name matches your search exactly."
    if query in title:
        return 1, "Activity name contains your search."
    details = activity.details
    if any(query in details.get(field, "").casefold() for field in ("description", "highlight")):
        return 2, "Activity description matches your search."
    return 3, "Venue or location information matches your search."


def _start(activity):
    if activity.kind != "event":
        return None
    try:
        value = datetime.fromisoformat(activity.details["starts_at"])
        return value if value.tzinfo else None
    except (KeyError, TypeError, ValueError):
        return None


def _context(activity, now, when, maximum, open_now):
    details = activity.details
    if when == "anytime":
        return 0, None, False
    if when == "weekend":
        start = _start(activity)
        if start is not None:
            lower = datetime.combine(planning_window(when, now)[0], time.min, BUDAPEST)
            if start < lower:
                return 3, "Starts before your selected weekend and overlaps it; confirm late entry with the venue.", False
            if start >= now:
                return 0, "Dated event in your selected weekend.", False
            return 3, "This event has already started; confirm late entry with the venue.", False
        window = planning_window(when, now)
        schedule = details.get("opening_hours") or {}
        earliest = now.astimezone(BUDAPEST).date() + timedelta(days=details.get("booking_lead_days", 0))
        if isinstance(schedule, dict) and _recent(schedule.get("checked_on"), now.astimezone(BUDAPEST).date()):
            for offset in range((window[1] - window[0]).days + 1):
                day = window[0] + timedelta(days=offset)
                if day >= earliest and day_availability(activity, day) == "open":
                    return 1, "Published opening hours cover a day in your selected weekend.", True
        return 2, None, False
    opening = opening_status(activity, now)
    visit = current_visit(activity, now, opening, maximum)
    fits_now = opening["state"] == "open" and visit["state"] == "fits"
    if open_now:
        if fits_now:
            return 0, "Fits published usable hours if started now; confirm any required booking.", True
        return 1, "Business hours are confirmed now; the full activity duration still needs checking.", True
    start = _start(activity)
    if start is not None:
        if start >= now:
            return 0, "Dated event for today.", False
        return 3, "This event has already started; confirm late entry with the venue.", False
    if fits_now:
        return 1, "Fits published usable hours if started now; confirm any required booking.", True
    return 2, None, opening["state"] != "unknown"


def _planning_details(activity, now, known_hours):
    details = activity.details
    reviewed = details.get("source_kind") in {"provider_page", "provider_enriched"} and _recent(
        details.get("source_checked_on"), now.astimezone(BUDAPEST).date()
    )
    facts = []
    low, high = details.get("price_low"), details.get("price_high")
    if commercial_status(details, now)["price"]["state"] == "current" and type(low) in (int, float) and type(high) in (int, float) and isfinite(low) and isfinite(high) and 0 <= low <= high:
        facts.append("price")
    if reviewed:
        if details.get("duration_kind") == "flexible" or (
            details.get("duration_kind") == "fixed" and positive_minutes(details.get("duration"))
        ):
            facts.append("visit timing")
        if _link_available(details.get("booking_url")) or _link_available(details.get("whatsapp_url")):
            facts.append("booking/contact")
        if known_hours:
            facts.append("opening hours for your dates")
    return int(reviewed or bool(facts)) + len(facts), facts


@dataclass
class RankedMatch:
    activity: Activity
    reasons: list[str]
    recommendation: dict
    priority: tuple[int, int, float, int]
    tie: tuple[str, str]


def rank_matches(matches, *, now, when="today", query=None, max_minutes=None, open_now=False):
    """Sort lexicographically; do not relax eligibility or invent match percentages."""
    query = (query or "").strip().casefold()
    profiles = []
    for activity, reasons in matches:
        search, search_note = _search_priority(activity, query)
        context, context_note, known_hours = _context(activity, now, when, max_minutes, open_now)
        detail_count, facts = _planning_details(activity, now, known_hours)
        basis = []
        if search_note:
            basis.append(_reason(search_note))
        if context_note:
            # Date/window explanations also expire when the results need updating.
            basis.append(_reason(context_note, time_sensitive=True, requires_opening_window=when == "today" and activity.kind != "event"))
        elif known_hours and "opening hours for your dates" in facts:
            basis.append(_reason("Published opening hours are available for your selected dates.", time_sensitive=True))
        if facts:
            static_facts = [fact for fact in facts if fact != "opening hours for your dates"]
            if static_facts:
                reason = _reason("Published planning details: " + ", ".join(static_facts) + ".")
                if "price" in static_facts:
                    reason["commercial_field"] = "price"
                basis.append(reason)
        if not any(not item["time_sensitive"] for item in basis):
            basis.append(_reason("Practical details need confirmation with the venue."))
        start = _start(activity)
        event_time = start.timestamp() if start is not None and when != "anytime" else 0
        profiles.append(RankedMatch(
            activity=activity, reasons=reasons,
            recommendation={
                "basis": basis,
                "filter_reasons": [
                    {**_reason(reason, time_sensitive=open_now, requires_opening_window=open_now),
                     **({"commercial_field": "price"} if reason.startswith("Published upper price") else {"commercial_field": "student_offer"} if reason.startswith("Student offer") else {})}
                    for reason in reasons
                ],
                "planning_details": facts,
            },
            priority=(search, context, event_time, -detail_count),
            tie=(activity.title.casefold(), activity.id),
        ))
    profiles.sort(key=lambda profile: (profile.priority, profile.tie))
    ordered = []
    for _, group in groupby(profiles, key=lambda profile: profile.priority):
        venues = defaultdict(deque)
        for profile in group:
            venues[profile.activity.venue_id or profile.activity.id].append(profile)
        rotation = deque(venues.values())
        while rotation:
            venue = rotation.popleft()
            ordered.append(venue.popleft())
            if venue:
                rotation.append(venue)
    return ordered
