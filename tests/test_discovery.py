from datetime import UTC, datetime

import pytest

from app.discovery import fits_window, planning_window
from app.models import Activity


def ids(response):
    assert response.status_code == 200
    return {a["id"] for a in response.json()["items"]}


def test_health_version_and_readiness(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json() == {"status": "ready"}
    assert client.get("/version").json()["version"] == "0.1.0"


def test_frontend_and_static_files_are_served(client):
    home = client.get("/")
    assert home.status_code == 200
    assert '<html lang="en">' in home.text
    assert "Plan with friends" in home.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200
    assert "frame-ancestors 'none'" in home.headers["content-security-policy"]


def test_budget_uses_upper_price_and_excludes_unknowns(client):
    response = client.get("/api/activities?when=anytime&max_price=5000")
    results = response.json()["items"]
    assert results
    assert all(a["price_high"] is not None and a["price_high"] <= 5000 for a in results)
    assert "kommunity-desk" not in ids(response)
    assert "kommunity-coffee" not in ids(response)
    assert "mecca-day" not in ids(response)


def test_known_closed_days_are_excluded_but_any_day_can_find_them(client):
    assert "she-manicure" not in ids(client.get("/api/activities?when=today"))
    assert "she-manicure" in ids(client.get("/api/activities?when=anytime"))


def test_mood_company_and_multiple_features_combine(client):
    response = client.get(
        "/api/activities?when=anytime&mood=focus&company=solo&feature=quiet&feature=study"
    )
    assert {"kommunity-desk", "fszek-study"} <= ids(response)
    assert all({"quiet", "study"} <= set(a["features"]) for a in response.json()["items"])
    assert all(a["reasons"] for a in response.json()["items"])


def test_low_energy_does_not_recommend_high_effort_workout(client):
    assert "mecca-day" not in ids(client.get("/api/activities?energy=low"))
    assert "mecca-day" in ids(client.get("/api/activities?energy=high"))


def test_time_filter_keeps_flexible_visits_but_not_unconfirmed_sessions(client):
    response = client.get("/api/activities?when=anytime&max_minutes=60")
    assert "she-manicure" in ids(response)
    assert {"kommunity-coffee", "fszek-study"} <= ids(response)
    assert "kanchana-massage" not in ids(response)
    assert all(
        a["duration_kind"] == "flexible"
        or (a["duration_kind"] == "fixed" and a["duration"] <= 60)
        for a in response.json()["items"]
    )


def test_student_offer_keeps_group_eligibility_visible(client):
    items = client.get("/api/activities?when=anytime&student=true").json()["items"]
    ludwig = next(a for a in items if a["id"] == "ludwig-art")
    assert "more than 10" in ludwig["student_note"]
    assert ludwig["price_low"] == 3300
    assert all(a["student_note"] for a in items)


@pytest.mark.parametrize(
    "query",
    [
        "when=yesterday",
        "max_price=-1",
        "max_minutes=0",
        "category=bogus",
        "mood=bogus",
        "energy=bogus",
        "feature=bogus",
    ],
)
def test_invalid_filters_are_rejected(client, query):
    assert client.get(f"/api/activities?{query}").status_code == 422


def test_missing_activity_returns_not_found(client):
    assert client.get("/api/activities/missing").status_code == 404


def test_catalogue_preserves_sources_and_unknown_values(client):
    checked_on_24 = {
        "house-music-explore",
        *{
            f"overture-{id}-place" for id in (
                "c9ee2c41-fca8-42f3-8d69-0ef6250a4a82", "b5cd4a1d-46e4-43b8-a4d1-6aae3107e5c2",
                "1f5599de-71e4-4ffc-9a2e-920983f7ea3b", "98f6bdcf-856b-474d-8808-61f33c621de5",
                "d8456b56-86a9-4bcc-a445-404b4a286894", "d2a4818e-3811-4a96-85f1-3279ee3c4e26",
                "64ec40bf-c520-4656-9152-5f78d059c904", "c6fcfde7-a945-4187-8824-2d2756d8acd8",
            )
        },
    }
    items = client.get("/api/activities?when=anytime").json()["items"]
    assert len(items) >= 70
    assert len({a["id"] for a in items}) == len(items)
    for activity in items:
        assert activity["source_url"].startswith("https://")
        if activity["source_kind"] == "open_data":
            assert activity["source_checked_on"] is None
            assert activity["data_imported_on"] == "2026-11-22"
        else:
            expected = "2026-11-24" if activity["id"] in checked_on_24 else "2026-11-22"
            assert activity["source_checked_on"] == expected
        assert (activity["price_low"] is None) == (activity["price_high"] is None)
        if activity["price_low"] is not None:
            assert 0 <= activity["price_low"] <= activity["price_high"]


def test_every_category_has_real_sourced_entries(client):
    config = client.get("/api/config").json()
    assert config["catalogue_stats"]["places"] >= 50
    for category in config["categories"]:
        assert client.get(f"/api/activities?when=anytime&category={category}").json()["total"] > 0


def test_search_can_find_provider_address(client):
    results = client.get("/api/activities?when=anytime&q=Ipoly").json()["items"]
    assert results
    assert all(a["venue"]["name"] == "Mandala Day Spa" for a in results)


def test_new_places_keep_provenance_licence_and_unknown_fields(client):
    from urllib.parse import urlsplit

    items = client.get("/api/activities?when=anytime").json()["items"]
    imported = [a for a in items if a.get("overture_id")]
    basic = [a for a in items if a["source_kind"] == "open_data"]
    assert len(imported) >= 100
    assert len({a["overture_id"] for a in imported}) == len(imported)
    hosts = {urlsplit(a["venue"]["website"]).hostname.removeprefix("www.") for a in imported}
    assert len(hosts) == len(imported)
    provider_hosts = {
        urlsplit(a["venue"]["website"]).hostname.removeprefix("www.")
        for a in items
        if not a.get("overture_id")
    }
    assert not hosts & provider_hosts
    for activity in basic:
        assert activity["price_low"] is activity["price_high"] is None
        assert activity["duration"] is activity["effort"] is None
        assert activity["moods"] == activity["company"] == activity["features"] == []
        assert not activity["student_discount"]
        assert activity["source_checked_on"] is None
    for activity in imported:
        assert {r["license"] for r in activity["source_records"]} == {"CDLA-Permissive-2.0"}
        assert activity["data_release"] == "2026-11-18.0"
        assert 47.45 <= activity["coordinates"]["latitude"] <= 47.57
        assert 18.99 <= activity["coordinates"]["longitude"] <= 19.15
    licence = client.get(basic[0]["data_license_url"])
    assert licence.status_code == 200
    assert "2.1. A Data Recipient may share Data" in licence.text
    assert "5.4." in licence.text
    assert client.get("/api/config").json()["catalogue_stats"]["basic_places"] == len(basic)


@pytest.mark.parametrize(
    "query",
    [
        "energy=high",
        "company=friends",
        "mood=reset",
        "feature=quiet",
        "max_price=1000000",
        "max_minutes=1440",
        "student=true",
    ],
)
def test_unassessed_places_are_not_assumed_to_match_specific_filters(client, query):
    response = client.get(f"/api/activities?when=anytime&{query}")
    assert response.status_code == 200
    assert all(a["source_kind"] != "open_data" for a in response.json()["items"])


def test_programme_links_and_sold_out_sessions_are_not_today_events(client):
    today = ids(client.get("/api/activities?when=today"))
    assert "turbina-programme" not in today
    assert "artegalata-clay-coffee" not in today
    assert "turbina-programme" in ids(client.get("/api/activities?when=anytime"))


def test_start_only_event_does_not_get_an_invented_end_time():
    event = Activity(
        id="start-only",
        venue_id="a38",
        title="A concert",
        kind="event",
        category="events",
        details={"starts_at": "2026-11-28T19:15:00+01:00"},
    )
    assert fits_window(event, "today", datetime(2026, 11, 28, 16, tzinfo=UTC))
    assert not fits_window(event, "today", datetime(2026, 11, 28, 20, tzinfo=UTC))
    assert "ends_at" not in event.details


def test_closures_override_normal_weekly_opening():
    activity = Activity(
        id="garden",
        venue_id="fuveszkert",
        title="Garden",
        kind="place",
        category="outdoors",
        details={"weekdays": list(range(7)), "closed_ranges": [["2026-12-20", "2027-01-03"]]},
    )
    assert not fits_window(activity, "today", datetime(2026, 12, 22, 12, tzinfo=UTC))
    assert fits_window(activity, "today", datetime(2027, 1, 4, 12, tzinfo=UTC))
    activity.details = {"weekdays": list(range(7)), "closed_month_days": ["12-24"]}
    assert not fits_window(activity, "today", datetime(2026, 12, 24, 12, tzinfo=UTC))


def test_weekend_on_sunday_does_not_include_past_saturday():
    now = datetime(2026, 11, 22, 14, tzinfo=UTC)
    start, end = planning_window("weekend", now)
    assert start.isoformat() == end.isoformat() == "2026-11-22"


def test_window_uses_budapest_calendar_date():
    now = datetime(2026, 11, 22, 23, 30, tzinfo=UTC)
    start, end = planning_window("today", now)
    assert start.isoformat() == end.isoformat() == "2026-11-23"


def test_event_window_excludes_expired_and_unscheduled_events(client):
    with client.app.state.sessions() as db:
        for id, details in [
            (
                "expired",
                {"starts_at": "2026-11-21T19:00:00+01:00", "ends_at": "2026-11-21T23:00:00+01:00"},
            ),
            (
                "tonight",
                {"starts_at": "2026-11-22T19:00:00+01:00", "ends_at": "2026-11-22T23:00:00+01:00"},
            ),
            ("undated", {}),
        ]:
            db.add(
                Activity(
                    id=id,
                    venue_id="kommunity",
                    title=id,
                    kind="event",
                    category="events",
                    details=details,
                )
            )
        db.commit()
    assert ids(client.get("/api/activities?when=today&category=events")) == {"tonight"}


def test_html_search_input_is_treated_as_plain_text(client):
    response = client.get("/api/activities", params={"q": "<script>alert(1)</script>"})
    assert response.status_code == 200
    assert response.json()["total"] == 0
