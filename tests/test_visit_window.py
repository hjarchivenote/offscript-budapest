from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.discovery import discover
from app.models import Activity
from app.opening_hours import opening_status
from app.visit_window import current_visit

CASTLE = "overture-490799b3-d5a3-4a24-88f2-74395b273241-place"
KIOSK = "overture-49624605-082e-4187-a918-330a49b67bc1-place"


def scheduled(*, duration=60, slots=None, checked="2026-11-01", exceptions=None, mode="fixed", **schedule_fields):
    return Activity(
        id="finish-test", kind="service", category="wellness",
        details={
            "duration_kind": mode, "duration": duration,
            "opening_hours": {
                "weekly": {str(d): slots or [["09:00", "18:00"]] for d in range(7)},
                "exceptions": exceptions or {}, "checked_on": checked,
                "source_url": "https://example.com/verified-hours", **schedule_fields,
            },
        },
    )


def assess(activity, stamp, maximum=None):
    now = datetime.fromisoformat(stamp)
    opening = opening_status(activity, now)
    return opening, current_visit(activity, now, opening, maximum)


def at(client, stamp):
    instant = datetime.fromisoformat(stamp)
    client.app.state.clock = lambda: instant


def test_real_late_nail_session_is_excluded_even_though_the_salon_is_open(client):
    at(client, "2026-11-24T17:45:00+01:00")
    session = client.get("/api/activities/she-gel-polish?max_minutes=120").json()
    assert session["opening"]["state"] == "open"
    assert session["visit_now"]["state"] == "does_not_fit"
    assert session["visit_now"]["finishes_at"] == "2026-11-24T19:25:00+01:00"
    for query in ("open_now=true", "open_now=true&max_minutes=120"):
        items = client.get(f"/api/activities?{query}").json()["items"]
        assert "she-gel-polish" not in {a["id"] for a in items}


@pytest.mark.parametrize(
    ("stamp", "state"),
    [("2026-11-24T17:15:00+01:00", "fits"), ("2026-11-24T17:15:01+01:00", "does_not_fit")],
)
def test_finishing_exactly_at_closing_fits_but_finishing_one_second_late_does_not(stamp, state):
    _, visit = assess(scheduled(duration=45), stamp, 45)
    assert visit["state"] == state


def test_fixed_session_cannot_cross_a_real_break_but_adjacent_periods_are_continuous():
    split = scheduled(slots=[["09:00", "12:00"], ["13:00", "18:00"]])
    assert assess(split, "2026-11-24T11:30:00+01:00")[1]["state"] == "does_not_fit"
    assert assess(split, "2026-11-24T11:00:00+01:00")[1]["state"] == "fits"
    assert assess(split, "2026-11-24T13:00:00+01:00")[1]["state"] == "fits"
    adjacent = scheduled(slots=[["09:00", "12:00"], ["12:00", "18:00"]])
    assert assess(adjacent, "2026-11-24T11:30:00+01:00")[1]["state"] == "fits"


def test_overnight_session_checks_its_finish_on_the_following_date():
    venue = scheduled(duration=90, slots=[["22:00", "06:00"]])
    assert assess(venue, "2026-11-24T23:30:00+01:00")[1]["state"] == "fits"
    assert assess(venue, "2026-11-25T04:30:00+01:00")[1]["state"] == "fits"
    assert assess(venue, "2026-11-25T05:00:00+01:00")[1]["state"] == "does_not_fit"


def test_tomorrow_closure_already_limits_a_session_starting_before_midnight():
    venue = scheduled(duration=90, slots=[["18:00", "06:00"]], exceptions={"2026-11-25": []})
    opening, visit = assess(venue, "2026-11-24T23:30:00+01:00")
    assert opening["state"] == "open"
    assert opening["until"] == "2026-11-25T00:00:00+01:00"
    assert visit["state"] == "does_not_fit"


def test_explicit_overnight_override_can_extend_the_continuous_usable_window():
    venue = scheduled(duration=90, slots=[["18:00", "06:00"]], exceptions={"2026-11-25": [["00:00", "01:00"], ["10:00", "18:00"]]})
    opening, visit = assess(venue, "2026-11-24T23:30:00+01:00")
    assert opening["until"] == "2026-11-25T01:00:00+01:00"
    assert visit["state"] == "fits"
    venue.details["duration"] = 91
    assert assess(venue, "2026-11-24T23:30:00+01:00")[1]["state"] == "does_not_fit"


def test_unconfirmed_next_date_does_not_get_an_invented_session_finish_window():
    venue = scheduled(duration=90, slots=[["18:00", "06:00"]], exceptions={"2026-11-25": None})
    opening, visit = assess(venue, "2026-11-24T23:30:00+01:00")
    assert opening["state"] == "open"
    assert opening["usable_end_reason"] == "unconfirmed_next_day"
    assert visit["state"] == "unknown"
    now = datetime.fromisoformat("2026-11-24T23:30:00+01:00")
    assert not discover([venue], now=now, open_now=True)


def test_usable_window_stops_at_the_end_of_a_verified_season():
    venue = scheduled(duration=90, slots=[["18:00", "06:00"]], valid_until="2026-11-24")
    opening, visit = assess(venue, "2026-11-24T23:30:00+01:00")
    assert opening["usable_until"] == "2026-11-25T00:00:00+01:00"
    assert visit["state"] == "unknown"


def test_museum_admission_cutoff_and_usable_exhibition_hours_are_separate(client):
    at(client, "2026-11-24T17:20:00+01:00")
    museum = client.get(f"/api/activities/{CASTLE}?max_minutes=120").json()
    assert museum["opening"]["entry_until"] == "2026-11-24T17:30:00+01:00"
    assert museum["opening"]["usable_until"] == "2026-11-24T17:45:00+01:00"
    assert museum["visit_now"]["usable_minutes"] == 25
    at(client, "2026-11-24T17:30:00+01:00")
    assert client.get(f"/api/activities/{CASTLE}").json()["visit_now"]["state"] == "not_open"


def test_kitchen_order_cutoff_is_not_mistaken_for_the_end_of_a_dining_stay(client):
    at(client, "2026-11-24T16:20:00+01:00")
    meal = client.get(f"/api/activities/{KIOSK}?max_minutes=120").json()
    assert meal["opening"]["orders_until"] == "2026-11-24T16:30:00+01:00"
    assert meal["opening"]["usable_until"] == "2026-11-24T23:00:00+01:00"
    assert meal["visit_now"]["usable_minutes"] == 120
    at(client, "2026-11-24T16:30:00+01:00")
    assert client.get(f"/api/activities/{KIOSK}").json()["visit_now"]["state"] == "not_open"


def test_admission_hours_alone_do_not_prove_a_fixed_session_can_finish():
    venue = scheduled(scope="admission")
    opening, visit = assess(venue, "2026-11-24T16:00:00+01:00")
    assert opening["state"] == "open"
    assert opening["usable_until"] is None
    assert visit["state"] == "unknown"
    now = datetime.fromisoformat("2026-11-24T16:00:00+01:00")
    assert not discover([venue], now=now, open_now=True)


def test_a_special_admission_cutoff_cannot_silently_reuse_ordinary_finish_hours():
    venue = scheduled(scope="admission", exceptions={"2026-11-24": [["09:00", "16:00"]]}, usage_hours={"weekly": {str(d): [["09:00", "20:00"]] for d in range(7)}})
    opening, visit = assess(venue, "2026-11-24T15:30:00+01:00")
    assert opening["state"] == "open"
    assert visit["state"] == "unknown"


def test_flexible_visit_is_capped_by_remaining_hours_not_required_to_fill_the_users_limit():
    venue = scheduled(duration=None, mode="flexible")
    opening, visit = assess(venue, "2026-11-24T17:40:00+01:00", 120)
    assert visit["state"] == "fits"
    assert visit["usable_minutes"] == 20
    assert visit["available_minutes"] == 20
    assert venue.details["duration"] is None


def test_ticket_limit_and_user_limit_do_not_change_the_full_admission_price(client):
    at(client, "2026-11-24T14:00:00+01:00")
    spa = client.get("/api/activities/mandala-bath").json()
    assert spa["visit_now"]["available_minutes"] == 360
    assert spa["visit_now"]["usable_minutes"] == 180
    limited = client.get("/api/activities/mandala-bath?max_minutes=60").json()
    assert limited["visit_now"]["usable_minutes"] == 60
    assert limited["price_high"] == spa["price_high"] == 13700


def test_unconfirmed_session_length_remains_unknown_when_the_business_is_open():
    venue = scheduled(duration=None, mode="unknown")
    opening, visit = assess(venue, "2026-11-24T17:45:00+01:00")
    assert opening["state"] == "open"
    assert visit["state"] == "unknown"
    assert visit["finishes_at"] is None


def test_elapsed_session_duration_is_correct_across_both_daylight_saving_changes():
    autumn = scheduled(duration=60, slots=[["00:00", "04:00"]], checked="2026-10-01")
    now = datetime(2026, 10, 25, 2, 30, tzinfo=ZoneInfo("Europe/Budapest"), fold=0)
    visit = current_visit(autumn, now, opening_status(autumn, now))
    assert visit["state"] == "fits"
    assert visit["finishes_at"] == "2026-10-25T02:30:00+01:00"
    assert (datetime.fromisoformat(visit["finishes_at"]).astimezone(UTC) - now.astimezone(UTC)).total_seconds() == 3600
    spring = scheduled(duration=90, checked="2027-03-01", exceptions={"2027-03-28": [["00:00", "04:00"]]})
    _, visit = assess(spring, "2027-03-28T01:30:00+01:00")
    assert visit["state"] == "fits"
    assert visit["finishes_at"] == "2027-03-28T04:00:00+02:00"
