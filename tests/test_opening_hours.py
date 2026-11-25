from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.discovery import discover
from app.models import Activity
from app.opening_hours import opening_status

KIOSK = "overture-49624605-082e-4187-a918-330a49b67bc1-place"
PASKAL = "overture-e9d4cea1-0966-435f-86db-000c2009203f-place"
CASTLE = "overture-490799b3-d5a3-4a24-88f2-74395b273241-place"


def clock(client, stamp="2026-11-24T15:00:00+01:00"):
    instant = datetime.fromisoformat(stamp)
    client.app.state.clock = lambda: instant
    return instant


def activity(weekly=None, *, exceptions=None, checked="2026-11-01", **details):
    schedule = {
        "weekly": weekly or {str(d): [["10:00", "17:00"]] for d in range(7)},
        "checked_on": checked,
        "exceptions": exceptions or {},
        "source_url": "https://example.com/hours",
    }
    return Activity(id="hours-test", kind="place", category="coffee", details={"opening_hours": schedule, **details})


@pytest.mark.parametrize(
    ("stamp", "state"),
    [
        ("2026-11-24T09:59:59+01:00", "closed"),
        ("2026-11-24T10:00:00+01:00", "open"),
        ("2026-11-24T16:59:59+01:00", "open"),
        ("2026-11-24T17:00:00+01:00", "closed"),
        ("2026-11-24T09:00:00+00:00", "open"),
    ],
)
def test_opening_boundaries_use_budapest_time_and_exclude_closing_instant(stamp, state):
    assert opening_status(activity(), datetime.fromisoformat(stamp))["state"] == state


def test_overnight_hours_survive_a_change_of_weekday_and_calendar_filter():
    weekly = {str(d): [] for d in range(7)}
    weekly["0"] = [["22:00", "06:00"]]
    venue = activity(weekly, weekdays=[0])
    now = datetime.fromisoformat("2026-11-24T02:00:00+01:00")
    status = opening_status(venue, now)
    assert status["state"] == "open"
    assert status["until"] == "2026-11-24T06:00:00+01:00"
    assert discover([venue], now=now, when="today", open_now=True)
    assert opening_status(venue, datetime.fromisoformat("2026-11-24T06:00:00+01:00"))["state"] == "closed"


def test_dated_closure_cancels_the_previous_nights_hours():
    venue = activity(
        {str(d): [["18:00", "06:00"]] for d in range(7)},
        exceptions={"2026-11-24": []},
    )
    assert opening_status(venue, datetime.fromisoformat("2026-11-24T02:00:00+01:00"))["state"] == "closed"


def test_midnight_closing_is_not_treated_as_twenty_four_hour_opening():
    venue = activity({str(d): [["16:00", "24:00"]] for d in range(7)})
    assert opening_status(venue, datetime.fromisoformat("2026-11-24T23:59:00+01:00"))["state"] == "open"
    assert opening_status(venue, datetime.fromisoformat("2026-11-25T00:00:00+01:00"))["state"] == "closed"


def test_repeated_autumn_hour_is_compared_as_an_actual_instant():
    venue = activity({str(d): [["18:00", "06:00"]] for d in range(7)}, checked="2026-10-01")
    for fold in (0, 1):
        now = datetime(2026, 10, 25, 2, 30, tzinfo=ZoneInfo("Europe/Budapest"), fold=fold)
        assert opening_status(venue, now.astimezone(UTC))["state"] == "open"
    ambiguous = activity({str(d): [["01:00", "02:30"]] for d in range(7)}, checked="2026-10-01")
    assert opening_status(ambiguous, now)["state"] == "unknown"


def test_unstructured_or_stale_hours_are_not_guessed():
    unknown = Activity(kind="place", details={"hours": "Open every day 24/7"})
    now = datetime.fromisoformat("2026-11-24T15:00:00+01:00")
    assert opening_status(unknown, now)["state"] == "unknown"
    assert opening_status(activity(checked="2026-01-01"), now)["state"] == "unknown"
    assert opening_status(activity({"1": None}), now)["state"] == "unknown"


def test_holiday_hours_need_explicit_confirmation_and_dated_overrides_win():
    now = datetime.fromisoformat("2026-12-25T13:00:00+01:00")
    assert opening_status(activity(), now)["state"] == "unknown"
    venue = activity(exceptions={"2026-12-25": [["12:00", "14:00"]]}, closed_month_days=["12-25"])
    assert opening_status(venue, now)["state"] == "open"
    assert opening_status(activity(closed_ranges=[["2026-12-20", "2027-01-03"]]), now)["state"] == "closed"


def test_open_now_results_only_contain_confirmed_schedules(client):
    instant = clock(client)
    response = client.get("/api/activities?open_now=true")
    assert response.status_code == 200
    body = response.json()
    assert body["items"]
    assert body["open_now"]
    assert body["as_of"] == instant.isoformat()
    assert all(a["opening"]["state"] == "open" for a in body["items"])
    assert all(a["opening"]["as_of"] == body["as_of"] for a in body["items"])
    assert all(a["opening"]["source_url"].startswith("https://") for a in body["items"])
    assert all(a["source_kind"] != "open_data" for a in body["items"])
    assert "fszek-study" not in {a["id"] for a in body["items"]}
    assert "fszek-study" in {a["id"] for a in client.get("/api/activities").json()["items"]}


def test_appointment_only_does_not_mean_a_treatment_slot_is_available(client):
    clock(client)
    salon = client.get("/api/activities/she-manicure").json()
    assert salon["opening"]["state"] == "open"
    assert salon["opening"]["appointment_required"] is True
    assert "booking availability is not checked" in salon["opening"]["reason"]


@pytest.mark.parametrize("stamp", ["2026-11-24T16:30:00+01:00", "2026-11-24T22:00:00+01:00"])
def test_meal_listing_uses_kitchen_breaks_and_kitchen_closing(client, stamp):
    clock(client, stamp)
    meal = client.get(f"/api/activities/{KIOSK}").json()
    assert meal["opening"]["state"] == "closed"
    assert meal["opening"]["label"] == "Food service closed"
    assert KIOSK not in {a["id"] for a in client.get("/api/activities?open_now=true").json()["items"]}


def test_last_admission_cutoff_excludes_a_still_open_museum(client):
    clock(client, "2026-11-24T17:45:00+01:00")
    museum = client.get(f"/api/activities/{CASTLE}").json()
    assert museum["opening"]["label"] == "Admission closed"
    assert CASTLE not in {a["id"] for a in client.get("/api/activities?open_now=true").json()["items"]}


def test_published_early_closing_overrides_the_normal_tuesday_schedule(client):
    clock(client, "2026-12-08T15:59:00+01:00")
    assert client.get(f"/api/activities/{PASKAL}").json()["opening"]["state"] == "open"
    clock(client, "2026-12-08T16:00:00+01:00")
    assert client.get(f"/api/activities/{PASKAL}").json()["opening"]["state"] == "closed"


def test_published_holiday_hours_and_annual_closures_are_distinct(client):
    clock(client, "2026-12-26T10:00:00+01:00")
    assert client.get("/api/activities/monkey-day").json()["opening"]["state"] == "open"
    clock(client, "2026-12-25T10:00:00+01:00")
    assert client.get("/api/activities/monkey-day").json()["opening"]["state"] == "closed"


def test_future_windows_cannot_be_combined_with_open_now(client):
    assert client.get("/api/activities?when=weekend&open_now=true").status_code == 422
    assert client.get("/api/activities?when=anytime&open_now=true").status_code == 422
