from datetime import UTC, datetime

from app.discovery import fits_window
from app.models import Activity

GOOSEBUMPS = "overture-c552b3b1-1970-47f6-af6f-bf8823d0a58c-place"
HOUDINI = "overture-6a740479-b01f-49fa-bab7-ab1ba275030c-place"
LIAN = "overture-d88db040-18e5-4cf7-b91f-41235f1ba951-place"
EXOTIC = "overture-31a44010-825a-4814-a60e-ff3aa89144e1-place"
GOACTIVE = "overture-12ae1f74-47df-4531-97c3-1888f9c56ced-place"
OIK = "overture-3fab9a77-fe82-4129-973e-095e7564cde9-place"
JOKAI = "overture-7f570460-db29-4a92-a268-3821df393edd-place"
CASTLE = "overture-490799b3-d5a3-4a24-88f2-74395b273241-place"


def detail(client, id):
    response = client.get(f"/api/activities/{id}")
    assert response.status_code == 200
    return response.json()


def result_ids(client, query):
    response = client.get(f"/api/activities?{query}")
    assert response.status_code == 200
    return {a["id"] for a in response.json()["items"]}


def test_enrichment_keeps_identity_provenance_and_field_sources(client):
    items = client.get("/api/activities?when=anytime").json()["items"]
    enriched = [a for a in items if a["source_kind"] == "provider_enriched"]
    assert len(enriched) == 28
    for activity in enriched:
        assert activity["overture_id"]
        assert activity["source_records"]
        assert activity["data_license"] == "CDLA-Permissive-2.0"
        fields = set()
        for source in activity["provider_sources"]:
            assert source["url"].startswith("https://")
            assert source["checked_on"] == activity["source_checked_on"]
            fields.update(source["fields"])
        assert fields == set(activity["verified_fields"])
        if activity["price_high"] is not None:
            assert "prices" in fields
        if activity["duration"] is not None:
            assert "duration" in fields
        if activity["student_discount"]:
            assert "student_offer" in fields
    stats = client.get("/api/config").json()["catalogue_stats"]
    assert stats == {"places": 152, "activities": 172, "basic_places": 72, "enriched_places": 28}


def test_published_closed_days_remove_places_from_sunday_recommendations(client):
    today = result_ids(client, "when=today")
    anytime = result_ids(client, "when=anytime")
    assert {GOOSEBUMPS, OIK, JOKAI} <= anytime
    assert not {GOOSEBUMPS, OIK, JOKAI} & today


def test_a_priced_treatment_can_match_budget_and_duration_together(client):
    matches = result_ids(client, "when=anytime&max_price=13000&max_minutes=45")
    assert LIAN in matches
    treatment = detail(client, LIAN)
    assert treatment["price_low"] == treatment["price_high"] == 13000
    assert treatment["duration"] == 45
    assert treatment["booking_url"] == "https://lianmassage.onlinefoglalas.hu/"


def test_weekend_surcharge_is_included_in_budget_upper_bound(client):
    manicure = detail(client, EXOTIC)
    assert (manicure["price_low"], manicure["price_high"]) == (6500, 8000)
    assert "cash" in " ".join(manicure["requirements"]).lower()
    assert EXOTIC not in result_ids(client, "when=anytime&max_price=7000")
    assert EXOTIC in result_ids(client, "when=anytime&max_price=8000")


def test_euro_price_and_start_frequency_are_not_invented_huf_or_duration(client):
    houdini = detail(client, HOUDINI)
    assert houdini["published_price_label"] == "€15 · adult"
    assert houdini["price_low"] is houdini["price_high"] is None
    assert houdini["duration"] is None
    assert HOUDINI not in result_ids(client, "when=anytime&max_price=1000000")
    assert HOUDINI not in result_ids(client, "when=anytime&max_minutes=60")
    assert HOUDINI not in result_ids(client, "when=today")
    assert HOUDINI in result_ids(client, "when=anytime")


def test_advance_booking_rule_uses_the_budapest_date_and_weekend_window():
    activity = Activity(
        id="advance-booking",
        venue_id="houdini",
        kind="place",
        details={"booking_lead_days": 1},
    )
    friday = datetime(2026, 11, 27, 15, tzinfo=UTC)
    saturday = datetime(2026, 11, 28, 15, tzinfo=UTC)
    sunday = datetime(2026, 11, 29, 15, tzinfo=UTC)
    assert fits_window(activity, "weekend", friday)
    assert fits_window(activity, "weekend", saturday)  # Sunday is still a future booking day.
    assert not fits_window(activity, "weekend", sunday)
    assert not fits_window(activity, "today", friday)
    assert fits_window(activity, "anytime", sunday)


def test_ambiguous_registration_charge_and_old_fee_table_keep_total_unknown(client):
    gym = detail(client, GOACTIVE)
    library = detail(client, OIK)
    assert gym["price_high"] is library["price_high"] is None
    assert "5,000" in gym["price_note"]
    assert "three entries" in gym["price_note"]
    assert "2017" in library["price_note"]
    assert not library["student_discount"]
    assert GOACTIVE not in result_ids(client, "when=anytime&max_price=8000")
    assert OIK not in result_ids(client, "when=anytime&max_price=1000")


def test_free_garden_does_not_inherit_paid_tour_cost_or_duration(client):
    garden = detail(client, JOKAI)
    assert garden["price_low"] == garden["price_high"] == 0
    assert garden["duration"] is None
    assert JOKAI in result_ids(client, "when=anytime&max_price=0")
    assert garden["duration_kind"] == "flexible"
    assert JOKAI in result_ids(client, "when=anytime&max_minutes=120")


def test_student_discount_retains_citizenship_restriction_and_standard_price(client):
    castle = detail(client, CASTLE)
    assert castle["student_discount"]
    assert "EEA citizens" in castle["student_note"]
    assert castle["price_low"] == castle["price_high"] == 3800
    assert CASTLE in result_ids(client, "when=anytime&student=true")
