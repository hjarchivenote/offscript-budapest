"""Assess a visit starting immediately, without inventing travel or slot availability."""

import math
from datetime import UTC, datetime, timedelta

from app.opening_hours import BUDAPEST
from app.visit_time import positive_minutes


def current_visit(activity, now, opening, max_minutes=None):
    details = activity.details
    mode = details.get("duration_kind", "unknown")
    duration = details.get("duration")
    result = {
        "state": "unknown",
        "label": "Finish time to confirm",
        "reason": "The session length or usable visitor hours have not been confirmed.",
        "available_minutes": None,
        "usable_minutes": None,
        "usable_until": opening.get("usable_until"),
        "finishes_at": None,
        "time_limit_minutes": max_minutes,
    }
    if opening["state"] != "open":
        result.update(state="not_open", label="Current use not confirmed", reason="There is no confirmed open window for starting this activity now.")
        return result
    if opening.get("usage_state") == "closed":
        result.update(state="does_not_fit", label="Visitor use is closed", reason="Admission or orders may be open, but the published usable hours do not cover a visit now.")
        return result
    current = now.astimezone(UTC)
    finish = None
    if mode == "fixed" and positive_minutes(duration):
        finish = current + timedelta(minutes=duration)
        result["finishes_at"] = finish.astimezone(BUDAPEST).isoformat()
        if max_minutes is not None and duration > max_minutes:
            result.update(state="does_not_fit", label="Session exceeds your time limit", reason="The full published session is longer than your selected activity-time limit.")
            return result
    until = opening.get("usable_until")
    if not until:
        return result
    end = datetime.fromisoformat(until).astimezone(UTC)
    remaining = max(0, math.floor((end - current).total_seconds() / 60))
    result["available_minutes"] = remaining
    if mode == "fixed" and positive_minutes(duration):
        if finish > end:
            if opening.get("usable_end_reason") == "unconfirmed_next_day":
                result["reason"] = "The full session extends beyond the hours confirmed for the following date."
            else:
                result.update(state="does_not_fit", label="Not enough time before closing", reason="Starting immediately, the full published session would finish after this continuous usable window ends.")
        else:
            result.update(state="fits", label="Session fits before closing", reason="The full published session fits if started immediately. Arrival, preparation and appointment availability are not included.", usable_minutes=duration)
    elif mode == "flexible":
        limits = [remaining]
        if positive_minutes(details.get("visit_limit_minutes")):
            limits.append(details["visit_limit_minutes"])
        if max_minutes is not None:
            limits.append(max_minutes)
        usable = min(limits)
        result.update(
            state="fits",
            label=f"Up to {usable} min for a visit now" if usable else "Less than 1 min for a visit now",
            usable_minutes=usable,
            reason="Maximum stay if starting immediately, capped by confirmed usable hours and your selected activity-time limit."
            + (" The ticket time limit also applies; a shorter stay still requires the full ticket." if positive_minutes(details.get("visit_limit_minutes")) else ""),
        )
    return result
