from datetime import datetime

import pytest

from app.catalogue import entry, seed_catalogue
from app.catalogue_open import PROVIDER_DETAILS, SNAPSHOT, activity_record
from app.models import Activity
from app.visit_time import fits_activity_time, visit_timing


def results(client, query):
    response = client.get(f"/api/activities?when=anytime&{query}")
    assert response.status_code == 200
    return {a["id"]: a for a in response.json()["items"]}


def test_two_hour_quiet_study_search_keeps_self_paced_places(client):
    items = results(client, "company=solo&feature=quiet&feature=study&max_minutes=120")
    assert {"fszek-study", "kommunity-desk"} <= items.keys()
    for activity in items.values():
        assert activity["duration_kind"] == "flexible"
        assert activity["duration"] is None
        assert "opening hours" in activity["duration_note"]
        assert any("You choose your visit length" in reason for reason in activity["reasons"])


def test_budget_still_applies_to_self_paced_study_places(client):
    items = results(client, "mood=focus&max_price=3000&max_minutes=120")
    assert "fszek-study" in items
    assert "kommunity-desk" not in items
    assert all(a["price_high"] is not None and a["price_high"] <= 3000 for a in items.values())


@pytest.mark.parametrize("limit", [30, 60, 120])
def test_cafes_and_parks_do_not_need_an_invented_duration(client, limit):
    items = results(client, f"max_minutes={limit}")
    for id in ("kommunity-coffee", "margaret-picnic"):
        assert id in items
        assert items[id]["duration"] is None
        assert items[id]["duration_kind"] == "flexible"


def test_published_session_upper_bound_is_enforced(client):
    assert "she-manicure" not in results(client, "max_minutes=44")
    assert "she-manicure" in results(client, "max_minutes=45")
    assert "artegalata-wheel" not in results(client, "max_minutes=119")
    assert "artegalata-wheel" in results(client, "max_minutes=120")


def test_unconfirmed_treatments_classes_and_guided_programmes_do_not_pass(client):
    unfiltered = results(client, "")
    timed = results(client, "max_minutes=240")
    for id in (
        "kanchana-massage",
        "chefparade-class",
        "overture-6a740479-b01f-49fa-bab7-ab1ba275030c-place",
    ):
        assert id in unfiltered
        assert unfiltered[id]["duration_kind"] == "unknown"
        assert id not in timed
    assert "choose your visit length" not in unfiltered["kanchana-massage"]["duration_note"].lower()


def test_museum_visit_estimate_is_not_a_required_session_length(client):
    museum = results(client, "max_minutes=60")["lam-visit"]
    assert museum["duration_kind"] == "flexible"
    assert museum["duration"] is None
    assert museum["visit_estimate_minutes"] == 90
    assert "60–90" in museum["visit_estimate_note"]
    assert "not a fixed session" in museum["visit_estimate_note"]


def test_ticket_validity_is_a_maximum_stay_and_does_not_reduce_the_price(client):
    spa = results(client, "max_minutes=60")["mandala-bath"]
    assert spa["duration_kind"] == "flexible"
    assert spa["duration"] is None
    assert spa["visit_limit_minutes"] == 180
    assert spa["price_low"] == spa["price_high"] == 13700
    assert "full ticket" in spa["visit_limit_note"]
    assert "mandala-classic" not in results(client, "max_minutes=60")  # Fixed 80-minute package.


def test_a_directory_category_does_not_imply_verified_flexible_access(client):
    unfiltered = results(client, "category=coffee")
    basic = {id: a for id, a in unfiltered.items() if a["source_kind"] == "open_data"}
    assert basic
    assert all(a["duration_kind"] == "unknown" for a in basic.values())
    assert not basic.keys() & results(client, "category=coffee&max_minutes=240").keys()


def test_session_duration_cannot_be_silently_relabelled_as_flexible():
    with pytest.raises(ValueError, match="Store visit estimates and ticket limits separately"):
        visit_timing("culture", "service", {"duration_kind": "flexible", "duration": 180})


@pytest.mark.parametrize("category", ["coffee", "culture", "study", "fitness"])
def test_even_a_provider_page_cannot_supply_a_format_from_its_category(category):
    details = visit_timing(category, "place", {"source_kind": "provider_page"})
    assert details["duration_kind"] == "unknown"
    assert not fits_activity_time(details, 240)


def test_a_new_catalogue_activity_requires_an_explicit_classification_and_explanation():
    with pytest.raises(ValueError, match="explicit duration kind and an explanation"):
        entry("coffee-tasting", "kommunity", "A guided coffee tasting", "coffee")
    with pytest.raises(ValueError, match="explicit duration kind and an explanation"):
        entry("coffee-stop", "kommunity", "Coffee", "coffee", duration_kind="flexible")


def test_an_unclassified_number_does_not_turn_a_legacy_record_into_a_timed_session():
    assert not fits_activity_time({"duration": 30}, 60)
    assert fits_activity_time({"duration": 30, "duration_kind": "fixed"}, 60)


def test_a_directory_record_cannot_assert_a_reviewed_activity_format():
    for mode, extra in (("flexible", {}), ("fixed", {"duration": 60})):
        with pytest.raises(ValueError, match="basic directory listing"):
            visit_timing("culture", "place", {"source_kind": "open_data", "duration_kind": mode, **extra})


def test_provider_enrichment_cannot_inherit_an_unreviewed_directory_classification(monkeypatch):
    place = SNAPSHOT["places"][0]
    monkeypatch.setitem(PROVIDER_DETAILS, place["id"], {"source_kind": "provider_enriched", "title": "Newly added guided visit"})
    with pytest.raises(ValueError, match="explicit timing classification"):
        activity_record(place)


def test_one_venue_can_offer_both_independent_visits_and_scheduled_workshops(client):
    catalogue = results(client, "")
    exhibition = catalogue["mai-mano-photography"]
    workshop = catalogue["mai-mano-zine-20261128"]
    assert exhibition["venue"]["name"] == workshop["venue"]["name"]
    assert exhibition["duration_kind"] == "flexible"
    assert workshop["duration_kind"] == "fixed"
    assert workshop["duration"] == 180
    timed = results(client, "max_minutes=120")
    assert exhibition["id"] in timed
    assert workshop["id"] not in timed


def test_facility_access_and_coached_training_have_separate_time_conditions(client):
    timed = results(client, "category=fitness&max_minutes=60")
    assert "mecca-day" in timed
    club = timed["overture-12ae1f74-47df-4531-97c3-1888f9c56ced-place"]
    assert "have their own session lengths" in club["duration_note"]
    assert "overture-373b0df2-b130-41b2-a7f8-1d99abbe3c5f-place" not in timed


def test_building_visit_does_not_claim_the_duration_or_price_of_ticketed_programmes(client):
    client.app.state.clock = lambda: datetime.fromisoformat("2026-11-24T12:00:00+01:00")
    visit = results(client, "max_minutes=60")["house-music-explore"]
    assert "public building" in visit["description"]
    assert "have their own tickets and schedules" in visit["duration_note"]
    assert visit["price_low"] == visit["price_high"] == 0
    assert "not covered by this listing" in visit["price_note"]
    assert visit["timing_checked_on"] == "2026-11-24"


def test_an_open_studio_is_not_a_guaranteed_short_completion(client):
    studio = results(client, "")["madebyyou-paint"]
    assert studio["duration_kind"] == "unknown"
    assert "few hours" in studio["duration_note"]
    assert "firing and collection happen later" in studio["duration_note"]
    assert studio["id"] not in results(client, "max_minutes=240")


def test_reviewed_classifications_replace_old_metadata_without_losing_reviews_or_ids(client):
    published = client.post("/api/activities/house-music-explore/reviews", json={
        "nickname": "A visitor", "rating": 5, "visited_on": "2026-11-21",
        "text": "I enjoyed exploring the public building.",
    })
    assert published.status_code == 200
    with client.app.state.sessions() as db:
        identities = set(db.query(Activity.id).all())
        db.get(Activity, "house-music-explore").details = {"duration_kind": "flexible", "duration_note": "Old combined programme listing"}
        db.commit()
        seed_catalogue(db)
        db.commit()
        assert set(db.query(Activity.id).all()) == identities
    activity = client.get("/api/activities/house-music-explore").json()
    assert "have their own tickets and schedules" in activity["duration_note"]
    reviews = client.get("/api/activities/house-music-explore/reviews").json()
    assert len(reviews) == 1
    assert reviews[0]["text"] == "I enjoyed exploring the public building."
