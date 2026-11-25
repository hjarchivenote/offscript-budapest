"""Keep expired commercial information out of current prices and offers."""

from datetime import date, datetime, time, timedelta

from app.opening_hours import BUDAPEST

MAX_COMMERCIAL_AGE_DAYS = 90
PROVIDER_KINDS = {"provider_page", "provider_enriched"}


def _field_status(details, field, now, present):
    checked = details.get(f"{field}_checked_on")
    if checked is None:
        source_field = "prices" if field == "price" else "student_offer"
        sources = [s.get("checked_on") for s in details.get("provider_sources", []) if source_field in s.get("fields", [])]
        # A field's own evidence takes precedence over the general record date.
        checked = max((value for value in sources if isinstance(value, str)), default=None) if sources else details.get("source_checked_on")
    result = {"state": "missing", "checked_on": checked, "valid_through": None, "expires_at": None, "message": ""}
    if not present:
        return result
    result.update(state="unconfirmed", message="Price needs rechecking." if field == "price" else "Student offer needs rechecking.")
    if details.get("catalogue_active") is False or details.get("source_kind") not in PROVIDER_KINDS:
        return result
    try:
        check_day = date.fromisoformat(checked)
        today = now.astimezone(BUDAPEST).date()
        expires = check_day + timedelta(days=MAX_COMMERCIAL_AGE_DAYS)
        if details.get(f"{field}_valid_until"):
            expires = min(expires, date.fromisoformat(details[f"{field}_valid_until"]))
        result["valid_through"] = expires.isoformat()
        result["expires_at"] = datetime.combine(expires + timedelta(days=1), time.min, BUDAPEST).isoformat()
        if check_day > today:
            return result
        result["state"] = "current" if today <= expires else "expired"
        if result["state"] == "current":
            result["message"] = ""
        else:
            result["message"] += f" Last checked {check_day.isoformat()}; excluded from current price and offer filters."
    except (TypeError, ValueError):
        pass
    return result


def commercial_status(details, now):
    return {
        "price": _field_status(details, "price", now, details.get("price_high") is not None or bool(details.get("published_price_label"))),
        "student_offer": _field_status(details, "student_offer", now, bool(details.get("student_discount"))),
    }


def commercial_view(details, now):
    """One effective representation for cards, detail pages and shared plans."""
    status = commercial_status(details, now)
    result = {"provider_freshness": status}
    if status["price"]["state"] in {"expired", "unconfirmed"}:
        result.update(
            price_low=None, price_high=None, published_price_label=None,
            last_published_price={"low": details.get("price_low"), "high": details.get("price_high"), "label": details.get("published_price_label")},
            price_note=status["price"]["message"] + " Previously published information: " + details.get("price_note", ""),
        )
    if status["student_offer"]["state"] in {"expired", "unconfirmed"}:
        result.update(
            student_discount=False,
            student_note=status["student_offer"]["message"] + " Previously published information: " + details.get("student_note", ""),
        )
    return result
