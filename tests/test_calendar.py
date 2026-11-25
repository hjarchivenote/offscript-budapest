from datetime import datetime

import pytest


@pytest.mark.parametrize(
    ("instant", "day"),
    [
        ("2026-11-24T22:59:59+00:00", "2026-11-24"),
        ("2026-11-24T23:00:00+00:00", "2026-11-25"),
        ("2026-12-31T23:00:00+00:00", "2027-01-01"),
        ("2027-03-28T00:30:00+00:00", "2027-03-28"),
        ("2027-03-28T01:30:00+00:00", "2027-03-28"),
        ("2026-10-25T00:30:00+00:00", "2026-10-25"),
        ("2026-10-25T01:30:00+00:00", "2026-10-25"),
    ],
)
def test_calendar_endpoints_use_the_same_budapest_date(client, instant, day):
    client.app.state.clock = lambda: datetime.fromisoformat(instant)
    response = client.get("/api/clock")
    assert response.status_code == 200
    clock = response.json()
    config = client.get("/api/config").json()
    assert clock["today"] == config["today"] == day
    assert clock["as_of"] == config["as_of"]
    assert clock["timezone"] == config["timezone"] == "Europe/Budapest"
    assert response.headers["cache-control"] == "no-store"


def test_plan_defaults_and_review_limits_can_use_the_new_day_without_reloading(client):
    client.app.state.clock = lambda: datetime.fromisoformat("2026-11-24T23:59:00+01:00")
    assert client.get("/api/config").json()["today"] == "2026-11-24"
    client.app.state.clock = lambda: datetime.fromisoformat("2026-11-25T00:01:00+01:00")
    day = client.get("/api/clock").json()["today"]
    assert day == "2026-11-25"
    plan = client.post("/api/plans", json={"title": "After midnight", "day": day, "nickname": "Alex"})
    assert plan.status_code == 201
    assert plan.json()["day"] == day
    review = client.post("/api/activities/mandala-bath/reviews", json={
        "nickname": "Alex", "rating": 5, "visited_on": day,
        "text": "A fresh review for today's visit.",
    })
    assert review.status_code == 200


def test_automatic_today_is_decided_when_the_plan_is_saved(client):
    client.app.state.clock = lambda: datetime.fromisoformat("2026-11-25T00:00:00+01:00")
    response = client.post("/api/plans", json={
        "title": "A plan across midnight", "day": "2026-11-24",
        "nickname": "Alex", "use_today": True,
    })
    assert response.status_code == 201
    assert response.json()["day"] == "2026-11-25"


def test_a_manually_chosen_day_is_preserved_and_past_days_still_fail(client):
    client.app.state.clock = lambda: datetime.fromisoformat("2026-11-25T00:00:00+01:00")
    for day, status in (("2026-11-24", 422), ("2026-11-28", 201)):
        response = client.post("/api/plans", json={
            "title": "My chosen date", "day": day, "nickname": "Alex", "use_today": False,
        })
        assert response.status_code == status
        if status == 201:
            assert response.json()["day"] == day
