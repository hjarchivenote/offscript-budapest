"""Planning consequences of the second batch of official provider checks."""

from datetime import datetime

import pytest

from app.catalogue_open import SNAPSHOT

KUNST = "overture-c9ee2c41-fca8-42f3-8d69-0ef6250a4a82-place"
ROTH = "overture-b5cd4a1d-46e4-43b8-a4d1-6aae3107e5c2-place"
MKVM = "overture-1f5599de-71e4-4ffc-9a2e-920983f7ea3b-place"
R70 = "overture-98f6bdcf-856b-474d-8808-61f33c621de5-place"
BRILL = "overture-d8456b56-86a9-4bcc-a445-404b4a286894-place"
ARBOR = "overture-d2a4818e-3811-4a96-85f1-3279ee3c4e26-place"
LANNA = "overture-64ec40bf-c520-4656-9152-5f78d059c904-place"
RADAY = "overture-c6fcfde7-a945-4187-8824-2d2756d8acd8-place"
BATCH = {KUNST, ROTH, MKVM, R70, BRILL, ARBOR, LANNA, RADAY}


def clock(client, stamp="2026-11-24T12:00:00+01:00"):
    client.app.state.clock = lambda: datetime.fromisoformat(stamp)


def detail(client, id):
    response = client.get(f"/api/activities/{id}")
    assert response.status_code == 200
    return response.json()


def matches(client, **filters):
    response = client.get("/api/activities", params={"when": "anytime", **filters})
    assert response.status_code == 200
    return {a["id"]: a for a in response.json()["items"]}


def test_batch_upgrades_existing_places_and_preserves_original_provenance(client):
    clock(client)
    imported = {p["id"]: p for p in SNAPSHOT["places"]}
    for id in BATCH:
        a = detail(client, id)
        original = imported[a["overture_id"]]
        assert id == f"overture-{original['id']}-place"
        assert a["source_kind"] == "provider_enriched"
        assert a["source_checked_on"] == "2026-11-24"
        assert a["coordinates"] == original["coordinates"]
        assert a["source_records"] == original["sources"]
        assert a["opening"]["hours_checked_on"] == "2026-11-24"


def test_single_entry_budget_uses_standard_adult_ticket_not_other_products(client):
    clock(client)
    assert KUNST not in matches(client, max_price=1800)  # Separate anniversary exhibition.
    assert {KUNST, ROTH, MKVM} <= matches(client, max_price=4500, student="true").keys()
    assert not {KUNST, ROTH, MKVM} & matches(client, max_price=2250, student="true").keys()
    assert RADAY not in matches(client, max_price=999)
    assert RADAY in matches(client, max_price=1000)
    assert not {RADAY, BRILL} & matches(client, student="true").keys()  # Annual/monthly offers.


def test_conflicting_gym_tariffs_and_unclear_card_deposit_do_not_pass_budget(client):
    clock(client)
    assert {R70, BRILL} <= matches(client).keys()
    assert not {R70, BRILL} & matches(client, max_price=100000).keys()
    assert "disagree" in detail(client, R70)["highlight"]
    assert "3,000" in detail(client, BRILL)["price_note"]
    # Independent workouts are flexible even while the price needs checking.
    assert {R70, BRILL} <= matches(client, max_minutes=30).keys()


@pytest.mark.parametrize(("id", "stamp", "last_entry", "usable_end", "remaining"), [
    (KUNST, "2026-11-24T17:20:00+01:00", "17:30", "17:45", 25),
    (ROTH, "2026-11-24T13:20:00+01:00", "13:30", "14:00", 40),
    (MKVM, "2026-11-24T17:20:00+01:00", "17:30", "18:00", 40),
    (KUNST, "2026-11-26T19:20:00+01:00", "19:30", "19:45", 25),
])
def test_museum_visit_length_uses_room_closing_while_entry_cutoff_stays_separate(
    client, id, stamp, last_entry, usable_end, remaining
):
    clock(client, stamp)
    a = matches(client, when="today", open_now="true", max_minutes=60)[id]
    assert a["opening"]["entry_until"][11:16] == last_entry
    assert a["opening"]["usable_until"][11:16] == usable_end
    assert a["visit_now"]["usable_minutes"] == remaining
    cutoff = stamp[:11] + last_entry + ":00+01:00"
    clock(client, cutoff)
    assert id not in matches(client, when="today", open_now="true")


@pytest.mark.parametrize("stamp", ["2026-12-03T15:00:00+01:00", "2026-12-18T12:00:00+01:00", "2026-12-28T12:00:00+01:00"])
def test_special_exhibition_days_never_invent_a_last_entry_time(client, stamp):
    clock(client, stamp)
    assert KUNST in matches(client, when="today")
    assert detail(client, KUNST)["opening"]["state"] == "unknown"
    assert KUNST not in matches(client, when="today", open_now="true")


def test_roth_special_monday_and_longer_tuesday_override_normal_weekdays(client):
    for stamp in ("2026-12-28T16:00:00+01:00", "2026-12-29T16:00:00+01:00"):
        clock(client, stamp)
        assert ROTH in matches(client, when="today", open_now="true", max_minutes=60)
        assert detail(client, ROTH)["opening"]["usable_until"][11:16] == "18:00"
    for stamp in ("2026-12-22T12:00:00+01:00", "2027-01-03T12:00:00+01:00"):
        clock(client, stamp)
        assert ROTH not in matches(client, when="today")
        assert ROTH not in matches(client, when="weekend")


def test_free_lower_garden_remains_available_on_winter_weekends_and_holidays(client):
    for stamp in ("2026-11-28T15:45:00+01:00", "2026-12-25T15:45:00+01:00", "2026-12-12T15:45:00+01:00"):
        clock(client, stamp)
        a = matches(client, when="today", open_now="true", max_price=0, max_minutes=30)[ARBOR]
        assert a["visit_now"]["usable_minutes"] == 15
        assert a["duration"] is None
    clock(client, "2026-11-28T16:00:00+01:00")
    assert ARBOR not in matches(client, when="today", open_now="true")
    clock(client, "2027-03-01T12:00:00+01:00")
    assert detail(client, ARBOR)["opening"]["state"] == "unknown"


def test_library_does_not_assert_conflicting_wednesday_hours_or_weekend_access(client):
    clock(client, "2026-11-25T14:00:00+01:00")
    assert RADAY in matches(client, when="today", max_minutes=60)
    assert RADAY not in matches(client, when="today", open_now="true")
    assert detail(client, RADAY)["opening"]["state"] == "unknown"
    assert RADAY not in matches(client, when="weekend")
    clock(client, "2026-11-24T16:45:00+01:00")
    assert matches(client, when="today", open_now="true", max_minutes=60)[RADAY]["visit_now"]["usable_minutes"] == 15


def test_treatment_uses_exact_menu_row_and_full_session_without_enforced_booking_recommendation(client):
    clock(client, "2026-11-27T19:59:00+01:00")
    assert LANNA not in matches(client, max_minutes=45)
    assert LANNA not in matches(client, max_price=12899, max_minutes=60)
    assert LANNA in matches(client, max_price=12900, max_minutes=60)
    a = matches(client, when="today", open_now="true", max_minutes=60)[LANNA]
    assert a["duration_kind"] == "fixed" and a["duration"] == 60
    assert a["opening"]["appointment_required"]
    assert a["booking_url"] == "https://de-lanna-thai-kft.salonic.hu/"
    assert a.get("booking_lead_days", 0) == 0
    assert "Klauzál" in a["venue"]["location"]
    clock(client, "2026-11-27T20:00:00+01:00")
    assert LANNA in matches(client, when="today", open_now="true", max_minutes=60)
    clock(client, "2026-11-27T20:00:01+01:00")
    assert LANNA not in matches(client, when="today", open_now="true", max_minutes=60)
