from datetime import datetime

import pytest

from app.catalogue import ACTIVITIES
from app.discovery import discover
from app.models import Activity
from app.opening_hours import opening_status


def visit(*, weekdays=(1, 2, 3, 4, 5, 6), exceptions=None, checked="2026-11-24", schedule_fields=None, **details):
    return Activity(
        id="schedule-review", venue_id="kommunity", title="Schedule review visit",
        kind="place", category="culture",
        details={
            "duration_kind": "flexible", "duration": None, "weekdays": list(weekdays),
            "opening_hours": {
                "weekly": {str(day): [["10:00", "18:00"]] if day in weekdays else [] for day in range(7)},
                "exceptions": exceptions or {}, "checked_on": checked,
                "source_url": "https://example.com/checked-hours", **(schedule_fields or {}),
            },
            **details,
        },
    )


def results(client, activity, stamp, when="today", *, open_now=False):
    client.app.state.clock = lambda: datetime.fromisoformat(stamp)
    with client.app.state.sessions() as db:
        db.merge(activity)
        db.commit()
    body = client.get("/api/activities", params={"when": when, "open_now": open_now}).json()
    return {item["id"]: item for item in body["items"]}


def test_dated_opening_beats_a_closed_weekday_and_general_closure_in_both_searches(client):
    activity = visit(
        exceptions={"2026-11-30": [["10:00", "18:00"]]},
        closed_month_days=["11-30"], closed_ranges=[["2026-11-30", "2026-12-01"]],
    )
    for open_now in (False, True):
        items = results(client, activity, "2026-11-30T12:00:00+01:00", open_now=open_now)
        assert activity.id in items
        assert items[activity.id]["opening"]["state"] == "open"


def test_dated_closure_removes_an_ordinary_open_day_but_not_the_any_day_listing(client):
    activity = visit(exceptions={"2026-11-24": []})
    for open_now in (False, True):
        assert activity.id not in results(client, activity, "2026-11-24T12:00:00+01:00", open_now=open_now)
    assert activity.id in results(client, activity, "2026-11-24T12:00:00+01:00", "anytime")


def test_special_weekend_opening_is_not_lost_to_weekday_only_metadata(client):
    activity = visit(weekdays=(0, 1, 2, 3, 4), exceptions={"2026-11-28": [["10:00", "14:00"]]})
    assert activity.id in results(client, activity, "2026-11-27T12:00:00+01:00", "weekend")


@pytest.mark.parametrize("sunday", [[], [["10:00", "14:00"]]])
def test_weekend_search_requires_at_least_one_day_without_a_confirmed_closure(client, sunday):
    activity = visit(exceptions={"2026-11-28": [], "2026-11-29": sunday})
    items = results(client, activity, "2026-11-27T12:00:00+01:00", "weekend")
    assert (activity.id in items) == bool(sunday)


def test_holiday_hours_beat_the_ordinary_weekday_but_not_a_known_annual_closure(client):
    activity = visit(weekdays=(1,), schedule_fields={"holiday_hours": [["10:00", "14:00"]]})
    for open_now in (False, True):
        assert activity.id in results(client, activity, "2026-12-25T12:00:00+01:00", open_now=open_now)
    activity.details = {**activity.details, "closed_month_days": ["12-25"]}
    for open_now in (False, True):
        assert activity.id not in results(client, activity, "2026-12-25T12:00:00+01:00", open_now=open_now)


def test_shortened_hours_change_open_now_without_erasing_the_whole_day(client):
    activity = visit(exceptions={"2026-11-24": [["10:00", "14:00"]]})
    assert activity.id in results(client, activity, "2026-11-24T15:00:00+01:00")
    assert activity.id not in results(client, activity, "2026-11-24T15:00:00+01:00", open_now=True)


@pytest.mark.parametrize("override", [None, [["25:00", "27:00"]]])
def test_unconfirmed_or_invalid_exceptions_stay_browsable_without_claiming_open_now(client, override):
    activity = visit(exceptions={"2026-11-30": override})
    ordinary = results(client, activity, "2026-11-30T12:00:00+01:00")
    assert activity.id in ordinary
    assert ordinary[activity.id]["opening"]["state"] == "unknown"
    assert activity.id not in results(client, activity, "2026-11-30T12:00:00+01:00", open_now=True)


@pytest.mark.parametrize("checked", ["2026-01-01", "2026-12-01"])
def test_expired_or_future_checked_exceptions_cannot_reopen_a_closed_weekday(client, checked):
    activity = visit(checked=checked, exceptions={"2026-11-30": [["10:00", "18:00"]]})
    for open_now in (False, True):
        assert activity.id not in results(client, activity, "2026-11-30T12:00:00+01:00", open_now=open_now)


def test_specific_opening_does_not_remove_the_advance_booking_requirement(client):
    activity = visit(exceptions={"2026-11-30": [["10:00", "18:00"]]}, booking_lead_days=1)
    for open_now in (False, True):
        assert activity.id not in results(client, activity, "2026-11-30T12:00:00+01:00", open_now=open_now)


@pytest.mark.parametrize("override", ["ordinary", [], None])
def test_overnight_tail_is_shared_by_both_searches_unless_today_replaces_it(client, override):
    activity = visit(weekdays=(0,), exceptions={} if override == "ordinary" else {"2026-11-24": override})
    activity.details["opening_hours"]["weekly"]["0"] = [["22:00", "06:00"]]
    ordinary = results(client, activity, "2026-11-24T02:00:00+01:00")
    current = results(client, activity, "2026-11-24T02:00:00+01:00", open_now=True)
    assert (activity.id in ordinary) == (override != [])
    assert (activity.id in current) == (override == "ordinary")


def test_confirmed_open_now_matches_are_also_candidates_for_today():
    catalogue = [Activity(**record) for record in ACTIVITIES]
    for stamp in (
        "2026-11-24T02:00:00+01:00", "2026-11-24T17:45:00+01:00",
        "2026-12-08T15:59:00+01:00", "2026-12-08T16:00:00+01:00",
        "2026-12-25T12:00:00+01:00", "2026-12-26T12:00:00+01:00",
    ):
        now = datetime.fromisoformat(stamp)
        for limit in (None, 60, 120):
            ordinary = {activity.id for activity, _ in discover(catalogue, now=now, max_minutes=limit)}
            current = {activity.id for activity, _ in discover(catalogue, now=now, open_now=True, max_minutes=limit)}
            assert current <= ordinary


def test_uncertain_holiday_hours_do_not_become_confirmed_regular_hours():
    activity = visit(weekdays=(0, 1, 2, 3, 4, 5, 6))
    now = datetime.fromisoformat("2026-12-25T12:00:00+01:00")
    assert opening_status(activity, now)["state"] == "unknown"
    assert discover([activity], now=now)
    assert not discover([activity], now=now, open_now=True)
