from datetime import datetime

import pytest

from app.discovery import discover
from app.models import Activity
from app.ranking import rank_matches

NOW = datetime.fromisoformat("2026-11-24T12:00:00+01:00")


def idea(identity, title, *, venue="kommunity", kind="place", complete=False, **details):
    values = {
        "description": "Ranking coffee fixture", "highlight": "", "search_terms": "",
        "source_kind": "provider_page", "source_checked_on": "2026-11-20",
        "duration_kind": "unknown", "duration": None, "price_low": None, "price_high": None,
        "moods": ["focus"], "company": ["solo"], "features": ["quiet", "study"],
        "effort": "low", "student_discount": True,
    }
    if complete:
        values.update(price_low=2000, price_high=2000, duration_kind="fixed", duration=45, booking_url="https://example.com/book")
    values.update(details)
    return Activity(id=identity, venue_id=venue, title=title, category="coffee", kind=kind, details=values)


def hours(slots=None, days=range(7)):
    return {
        "weekly": {str(day): (slots or [["09:00", "18:00"]]) if day in days else [] for day in range(7)},
        "checked_on": "2026-11-20", "source_url": "https://example.com/hours",
    }


def ordered(activities, *, when="anytime", query=None, now=NOW, maximum=None, open_now=False):
    matches = discover(activities, now=now, when=when, q=query, max_minutes=maximum, open_now=open_now)
    return rank_matches(matches, now=now, when=when, query=query, max_minutes=maximum, open_now=open_now)


def identities(rows):
    return [row.activity.id for row in rows]


def test_search_relevance_precedes_amount_of_provider_information():
    exact = idea("exact", "Coffee", source_kind="open_data", source_checked_on=None)
    title = idea("title", "Coffee after class")
    description = idea("description", "A break", complete=True)
    location = idea("location", "A walk", description="A quiet path", search_terms="Coffee Street 10", complete=True)
    result = ordered([location, description, title, exact], query="coffee")
    assert identities(result) == ["exact", "title", "description", "location"]
    assert "exactly" in result[0].recommendation["basis"][0]["text"]
    assert "Venue or location" in result[-1].recommendation["basis"][0]["text"]


def test_today_events_use_start_times_before_planning_detail_counts():
    early = idea("early", "Z early concert", kind="event", starts_at="2026-11-24T13:00:00+01:00")
    later = idea("later", "A later concert", kind="event", complete=True, starts_at="2026-11-24T18:00:00+01:00")
    visit = idea("visit", "An independent visit", complete=True, opening_hours=hours())
    ongoing = idea("ongoing", "Already started", kind="event", starts_at="2026-11-24T11:00:00+01:00", ends_at="2026-11-24T15:00:00+01:00")
    result = ordered([ongoing, visit, later, early], when="today")
    assert identities(result) == ["early", "later", "visit", "ongoing"]
    assert "already started" in result[-1].recommendation["basis"][0]["text"]


def test_weekend_hours_are_assessed_for_the_weekend_instead_of_right_now():
    known = idea("known", "Z Saturday visit", opening_hours=hours(days=[5]))
    unknown = idea("unknown", "A visit with no confirmed days", complete=True)
    event = idea("event", "An event", kind="event", starts_at="2026-11-29T18:00:00+01:00")
    result = ordered([unknown, known, event], when="weekend")
    assert identities(result) == ["event", "known", "unknown"]
    assert "selected weekend" in result[1].recommendation["basis"][0]["text"]


def test_any_day_order_is_independent_of_the_current_open_or_closed_state():
    a = idea("a", "A visit", complete=True, opening_hours=hours())
    b = idea("b", "B visit", complete=True, opening_hours=hours([["15:00", "23:00"]]))
    morning = ordered([b, a], now=datetime.fromisoformat("2026-11-24T09:30:00+01:00"))
    evening = ordered([b, a], now=datetime.fromisoformat("2026-11-24T20:00:00+01:00"))
    assert identities(morning) == identities(evening) == ["a", "b"]
    assert not any(reason["time_sensitive"] for row in evening for reason in row.recommendation["basis"])


def test_recently_reviewed_planning_facts_precede_missing_or_stale_details():
    basic = idea("basic", "A basic place", complete=True, source_kind="open_data", source_checked_on=None)
    stale = idea("stale", "B old details", complete=True, source_checked_on="2025-01-01")
    future = idea("future", "C future check", complete=True, source_checked_on="2026-11-25")
    reviewed = idea("reviewed", "Z usable details", complete=True)
    result = ordered([basic, stale, future, reviewed])
    assert result[0].activity.id == "reviewed"
    assert result[0].recommendation["planning_details"] == ["price", "visit timing", "booking/contact"]
    assert all(row.recommendation["planning_details"] == [] for row in result[1:])


def test_equal_matches_rotate_venues_and_do_not_drop_suggestions():
    activities = [
        idea("a1", "A first", venue="same"), idea("a2", "A second", venue="same"),
        idea("a3", "A third", venue="same"), idea("b", "B visit", venue="other"),
        idea("c", "C visit", venue="third"),
    ]
    expected = ["a1", "b", "c", "a2", "a3"]
    assert identities(ordered(activities)) == expected
    assert identities(ordered(list(reversed(activities)))) == expected


def test_venue_rotation_does_not_promote_weaker_search_matches():
    first = idea("first", "Coffee A", venue="same")
    second = idea("second", "Coffee B", venue="same")
    description = idea("description", "A different venue", venue="other")
    assert identities(ordered([description, second, first], query="coffee")) == ["first", "second", "description"]


def test_equal_titles_use_stable_ids_rather_than_database_insertion_order():
    rows = [idea("b", "Same name"), idea("a", "Same name")]
    assert identities(ordered(rows)) == identities(ordered(list(reversed(rows)))) == ["a", "b"]


def test_lower_prices_and_shorter_sessions_are_not_assumed_to_be_better():
    cheaper = idea("cheap", "Z short visit", complete=True, price_low=100, price_high=100, duration=10)
    standard = idea("standard", "A standard visit", complete=True)
    assert identities(ordered([cheaper, standard], maximum=60)) == ["standard", "cheap"]


def test_current_unknown_session_length_does_not_receive_a_finish_time_claim():
    unknown = idea("unknown", "A treatment", opening_hours=hours())
    known = idea("known", "Z timed treatment", complete=True, opening_hours=hours())
    result = ordered([unknown, known], when="today", open_now=True)
    assert identities(result) == ["known", "unknown"]
    assert "duration still needs checking" in result[1].recommendation["basis"][0]["text"]
    for row in result:
        assert all(reason["time_sensitive"] for reason in row.recommendation["filter_reasons"])


def test_ranking_does_not_use_unrelated_tags_or_invented_ratings():
    a = idea("a", "A visit", complete=True)
    b = idea("b", "B visit", complete=True, moods=["focus", "social", "active"], rating=5, review_count=10000)
    assert identities(ordered([b, a])) == ["a", "b"]


@pytest.mark.parametrize(
    "change",
    [
        {"price_low": 9000, "price_high": 9000}, {"duration": 180},
        {"company": ["friends"]}, {"features": ["study"]},
        {"moods": ["active"]}, {"effort": "high"}, {"student_discount": False},
    ],
)
def test_high_relevance_cannot_override_a_required_filter(client, change):
    eligible = idea("ranking-eligible", "Ranking coffee break", complete=True)
    rejected = idea("ranking-rejected", "Ranking coffee", complete=True, **change)
    with client.app.state.sessions() as db:
        db.add_all([eligible, rejected])
        db.commit()
    response = client.get("/api/activities", params={
        "when": "anytime", "q": "Ranking coffee", "max_price": 6000, "max_minutes": 60,
        "company": "solo", "feature": "quiet", "mood": "focus", "energy": "low", "student": True,
    })
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == ["ranking-eligible"]


def test_whitespace_and_case_do_not_change_search_eligibility(client):
    def ids(query):
        return [item["id"] for item in client.get("/api/activities", params={"when": "anytime", "q": query}).json()["items"]]

    assert ids("  COFFEE  ") == ids("coffee")
    assert ids("   ") == ids("")


def test_api_explains_its_order_and_keeps_legacy_filter_reasons(client):
    response = client.get("/api/activities?when=anytime&mood=focus&max_minutes=120")
    assert response.status_code == 200
    body = response.json()
    assert body["ordering"]["id"] == "suggested"
    assert "Every selected filter is required" in body["ordering"]["description"]
    assert body["items"]
    for item in body["items"]:
        assert item["reasons"]
        assert item["recommendation"]["basis"]
        assert all(isinstance(reason["time_sensitive"], bool) for reason in item["recommendation"]["basis"])
