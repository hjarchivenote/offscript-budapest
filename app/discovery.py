from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.opening_hours import day_availability, opening_status
from app.provider_freshness import commercial_status
from app.visit_time import fits_activity_time
from app.visit_window import current_visit

BUDAPEST = ZoneInfo("Europe/Budapest")


def planning_window(when: str, now: datetime):
    today = now.astimezone(BUDAPEST).date()
    if when == "anytime":
        return None
    if when == "today":
        return today, today
    if today.weekday() in (5, 6):
        return today, today + timedelta(days=6 - today.weekday())
    saturday = today + timedelta(days=(5 - today.weekday()) % 7)
    return saturday, saturday + timedelta(days=1)


def fits_window(activity, when, now):
    details = activity.details
    if details.get("catalogue_active") is False:
        return False
    window = planning_window(when, now)
    if activity.kind == "programme" and when != "anytime":
        return False
    if details.get("date_status") == "new_dates_needed" and when != "anytime":
        return False
    if activity.kind == "event":
        # An event must have an explicit, timezone-aware occurrence, never a made-up date.
        try:
            start = datetime.fromisoformat(details["starts_at"])
            end = datetime.fromisoformat(details["ends_at"]) if details.get("ends_at") else None
        except (KeyError, TypeError, ValueError):
            return False
        if start.tzinfo is None:
            return False
        if end is not None and (end.tzinfo is None or end <= start):
            return False
        # Without a published end, stop recommending at the published start;
        # do not invent a duration or promise that late entry remains possible.
        if (end or start) <= now:
            return False
        if window:
            lower = datetime.combine(window[0], time.min, BUDAPEST)
            upper = datetime.combine(window[1] + timedelta(days=1), time.min, BUDAPEST)
            return start < upper and (end > lower if end else start >= lower)
        return True
    if window:
        earliest_visit = now.astimezone(BUDAPEST).date() + timedelta(
            days=details.get("booking_lead_days", 0)
        )
        for i in range((window[1] - window[0]).days + 1):
            day = window[0] + timedelta(days=i)
            if day < earliest_visit:
                continue
            if day_availability(activity, day) == "closed":
                continue
            return True
        return False
    return True


def plan_day_problem(activity, day, now):
    """Reject known incompatibilities; unknown access still needs provider confirmation."""
    details = activity.details
    if details.get("catalogue_active") is False:
        return "This activity is no longer in our current collection. Choose another idea."
    if activity.kind == "event":
        try:
            start = datetime.fromisoformat(details["starts_at"])
            end = datetime.fromisoformat(details["ends_at"]) if details.get("ends_at") else None
        except (KeyError, TypeError, ValueError):
            return "This event's date needs confirmation. Choose another idea."
        if start.tzinfo is None or (end is not None and (end.tzinfo is None or end <= start)):
            return "This event's date needs confirmation. Choose another idea."
        lower = datetime.combine(day, time.min, BUDAPEST)
        upper = datetime.combine(day + timedelta(days=1), time.min, BUDAPEST)
        if (end or start) <= now or start >= upper or (end <= lower if end else start < lower):
            return "This event does not match your plan date or has already ended. Choose another date or idea."
    elif day_availability(activity, day) == "closed":
        return "Published hours show this activity is closed on your plan date. Choose another date or idea."
    lead = details.get("booking_lead_days", 0)
    if lead > 0 and day < now.astimezone(BUDAPEST).date() + timedelta(days=lead):
        return "This activity requires more advance booking. Choose a later date or another idea."
    return None


def discover(
    activities,
    *,
    now,
    when="today",
    category=None,
    mood=None,
    company=None,
    max_price=None,
    max_minutes=None,
    feature=None,
    q=None,
    student=False,
    energy=None,
    open_now=False,
):
    q = (q or "").strip().casefold()
    matches = []
    for activity in activities:
        d = activity.details
        if d.get("catalogue_active") is False:
            continue
        if open_now:
            opening = opening_status(activity, now)
            if d.get("booking_lead_days", 0) > 0 or opening["state"] != "open":
                continue
            visit = current_visit(activity, now, opening, max_minutes)
            if visit["state"] == "does_not_fit":
                continue
            if (d.get("duration_kind") == "fixed" or max_minutes is not None) and visit["state"] != "fits":
                continue
        elif not fits_window(activity, when, now):
            continue
        if category and category not in d.get("categories", [activity.category]):
            continue
        if mood and mood not in d.get("moods", []):
            continue
        if company and company not in d.get("company", []):
            continue
        if feature and not all(f in d.get("features", []) for f in feature):
            continue
        if energy:
            effort = d.get("effort")
            if effort not in {"low", "medium", "high"}:
                continue
            if {"low": 0, "medium": 1, "high": 2}[effort] > {"low": 0, "medium": 1, "high": 2}[energy]:
                continue
        commercial = commercial_status(d, now) if student or max_price is not None else None
        if student and (not d.get("student_discount") or commercial["student_offer"]["state"] != "current"):
            continue
        if max_price is not None and (
            d.get("price_high") is None or d["price_high"] > max_price
            or commercial["price"]["state"] != "current"
        ):
            continue
        if not fits_activity_time(d, max_minutes):
            continue
        if (
            q
            and q
            not in " ".join(
                [
                    activity.title,
                    d.get("description", ""),
                    d.get("highlight", ""),
                    d.get("search_terms", ""),
                ]
            ).casefold()
        ):
            continue
        reasons = []
        if open_now:
            reasons.append("Within published operating hours now; confirm any required booking")
        if mood:
            reasons.append(
                {
                    "reset": "An idea for a slower day",
                    "focus": "A place to focus",
                    "curious": "Something to explore",
                    "active": "A reason to move",
                    "social": "An idea to share",
                    "treat": "A little time for yourself",
                }[mood]
            )
        if max_price is not None:
            reasons.append("Published upper price fits your budget; check any extras")
        if student:
            reasons.append("Student offer listed; check its eligibility conditions")
        if max_minutes is not None:
            reasons.append(
                "You choose your visit length; check opening hours and entry conditions"
                if d.get("duration_kind") == "flexible"
                else "Published session length is within your activity-time limit"
            )
        matches.append((activity, reasons))
    return matches
