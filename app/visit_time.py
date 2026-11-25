"""Distinguish session lengths from visits whose length the visitor chooses."""

def positive_minutes(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def visit_timing(category, kind, details):
    duration = details.get("duration")
    # A venue's category, listing kind and source label never establish its
    # activity format. Missing review metadata always remains unconfirmed.
    mode = details.get("duration_kind", "unknown")
    if mode not in {"fixed", "flexible", "unknown"}:
        raise ValueError(f"Unsupported duration kind: {mode}")
    if details.get("source_kind") == "open_data" and mode != "unknown":
        raise ValueError("A basic directory listing cannot establish a session or flexible visit.")
    if mode == "fixed" and not positive_minutes(duration):
        raise ValueError("A fixed session needs a positive published duration.")
    if mode != "fixed" and duration is not None:
        raise ValueError("Store visit estimates and ticket limits separately from session duration.")
    for field in ("visit_estimate_minutes", "visit_limit_minutes"):
        value = details.get(field)
        if value is not None and (mode != "flexible" or not positive_minutes(value)):
            raise ValueError(f"{field} needs a flexible visit and a positive number of minutes.")
    note = details.get("duration_note") or {
        "fixed": "Published session length; allow extra time for arrival and changing.",
        "flexible": "You choose how long to stay. Check opening hours, last admission and any ticket or table limits.",
        "unknown": "Confirm the session or visit duration with the venue before making a timed plan.",
    }[mode]
    return {
        "duration_kind": mode,
        "duration": duration,
        "duration_note": note,
        "visit_estimate_minutes": details.get("visit_estimate_minutes"),
        "visit_estimate_note": details.get("visit_estimate_note", ""),
        "visit_limit_minutes": details.get("visit_limit_minutes"),
        "visit_limit_note": details.get("visit_limit_note", ""),
    }


def fits_activity_time(details, maximum):
    if maximum is None:
        return True
    # Directly inserted activities without timing metadata remain conservative.
    duration = details.get("duration")
    mode = details.get("duration_kind", "unknown")
    if mode == "flexible":
        return True
    return mode == "fixed" and positive_minutes(duration) and duration <= maximum
