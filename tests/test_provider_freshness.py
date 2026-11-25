from datetime import datetime

import pytest

from app.models import Activity
from app.provider_freshness import commercial_status, commercial_view


def details(**changes):
    return {
        "source_kind": "provider_page", "source_checked_on": "2026-11-24",
        "price_low": 2500, "price_high": 3000, "price_note": "Adult entry.",
        "student_discount": True, "student_note": "Bring student ID.", **changes,
    }


@pytest.mark.parametrize(("stamp", "state"), [
    ("2026-11-23T12:00:00+01:00", "unconfirmed"),
    ("2026-11-24T12:00:00+01:00", "current"),
    ("2027-02-22T22:59:59+00:00", "current"),
    ("2027-02-22T23:00:00+00:00", "expired"),
])
def test_commercial_expiry_uses_the_budapest_day_and_includes_day_ninety(stamp, state):
    statuses = commercial_status(details(), datetime.fromisoformat(stamp))
    for status in statuses.values():
        assert status["state"] == state
        assert status["valid_through"] == "2027-02-22"
        assert status["expires_at"] == "2027-02-23T00:00:00+01:00"


@pytest.mark.parametrize("checked", [None, "invalid", "2027-11-24"])
def test_missing_invalid_and_future_checks_do_not_establish_a_current_price(checked):
    assert commercial_status(details(source_checked_on=checked), datetime.fromisoformat("2026-11-25T12:00:00+01:00"))["price"]["state"] == "unconfirmed"


def test_field_evidence_is_independent_of_the_overall_record_check_date():
    value = details(source_checked_on="2026-11-25", provider_sources=[
        {"checked_on": "2025-11-24", "fields": ["prices"]},
        {"checked_on": "2026-11-24", "fields": ["student_offer"]},
    ])
    status = commercial_status(value, datetime.fromisoformat("2026-11-25T12:00:00+01:00"))
    assert status["price"]["state"] == "expired"
    assert status["student_offer"]["state"] == "current"
    value["price_checked_on"] = "2026-11-25"
    assert commercial_status(value, datetime.fromisoformat("2026-11-25T12:00:00+01:00"))["price"]["state"] == "current"


def test_explicit_offer_end_dates_shorten_the_review_window():
    status = commercial_status(details(price_valid_until="2026-11-25", student_offer_valid_until="2026-11-26"), datetime.fromisoformat("2026-11-26T12:00:00+01:00"))
    assert status["price"]["state"] == "expired"
    assert status["student_offer"]["state"] == "current"
    assert commercial_status(details(price_valid_until="invalid"), datetime.fromisoformat("2026-11-25T12:00:00+01:00"))["price"]["state"] == "unconfirmed"


def test_basic_directory_records_cannot_supply_confirmed_commercial_facts():
    status = commercial_status(details(source_kind="open_data", price_low=0, price_high=0), datetime.fromisoformat("2026-11-25T12:00:00+01:00"))
    assert {s["state"] for s in status.values()} == {"unconfirmed"}


def test_foreign_currency_labels_expire_without_inventing_a_huf_total():
    value = details(price_low=None, price_high=None, published_price_label="€15 adult")
    current = commercial_view(value, datetime.fromisoformat("2026-11-25T12:00:00+01:00"))
    assert current["provider_freshness"]["price"]["state"] == "current"
    expired = commercial_view(value, datetime.fromisoformat("2027-11-25T12:00:00+01:00"))
    assert expired["published_price_label"] is expired["price_high"] is None
    assert expired["last_published_price"]["label"] == "€15 adult"
    assert value["published_price_label"] == "€15 adult"


def test_expired_prices_and_discounts_agree_in_search_detail_and_friend_plans(client, plan):
    id = "overture-490799b3-d5a3-4a24-88f2-74395b273241-place"
    base = f"/api/plans/{plan['id']}"
    assert client.post(base + "/candidates", json={"activity_id": id}).status_code == 201
    client.app.state.clock = lambda: datetime.fromisoformat("2027-11-25T12:00:00+01:00")
    all_items = client.get("/api/activities?when=anytime").json()["items"]
    item = next(a for a in all_items if a["id"] == id)
    detail = client.get(f"/api/activities/{id}").json()
    proposed = client.get(base).json()["candidates"][0]["activity"]
    for view in (item, detail, proposed):
        assert view["price_low"] is view["price_high"] is None
        assert not view["student_discount"]
        assert "Previously published" in view["price_note"]
        assert "needs rechecking" in view["student_note"]
        assert view["last_published_price"]["high"] == 3800
    assert not any("price" in reason["text"] for reason in item["recommendation"]["basis"])
    for query in ("max_price=1000000", "student=true"):
        assert id not in {a["id"] for a in client.get("/api/activities?when=anytime&" + query).json()["items"]}
    with client.app.state.sessions() as db:
        saved = db.get(Activity, id)
        assert saved.details["price_high"] == 3800
        assert saved.details["student_discount"]
        assert saved.details["source_checked_on"] == "2026-11-22"
