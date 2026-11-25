import hashlib
import json
from datetime import date, datetime

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError

from app import bootstrap, catalogue
from app.discovery import discover, fits_window, plan_day_problem
from app.models import Activity, DatabaseState, Plan
from tests.test_ranking import hours, idea, identities, ordered

NOW = datetime.fromisoformat("2026-11-27T12:00:00+01:00")


@pytest.mark.parametrize(("end", "matches"), [("00:00:00", False), ("00:00:01", True)])
def test_weekend_midnight_is_an_exclusive_event_end(end, matches):
    activity = idea("overnight", "Friday night", kind="event", starts_at="2026-11-27T22:00:00+01:00", ends_at=f"2026-11-28T{end}+01:00")
    assert fits_window(activity, "weekend", NOW) is matches
    assert (plan_day_problem(activity, date(2026, 11, 28), NOW) is None) is matches


def test_overnight_event_is_not_presented_as_starting_in_the_weekend():
    friday = idea("friday", "Friday night", kind="event", starts_at="2026-11-27T22:00:00+01:00", ends_at="2026-11-28T02:00:00+01:00")
    saturday = idea("saturday", "Saturday night", kind="event", starts_at="2026-11-28T20:00:00+01:00")
    result = ordered([friday, saturday], now=NOW, when="weekend")
    assert identities(result) == ["saturday", "friday"]
    assert "Starts before" in result[-1].recommendation["basis"][0]["text"]


@pytest.mark.parametrize("details", [
    {"starts_at": "2026-11-25T18:00:00+01:00"},
    {"starts_at": "2026-11-28T20:00:00"},
    {"starts_at": "2026-11-28T20:00:00+01:00", "ends_at": "2026-11-28T19:00:00+01:00"},
])
def test_event_date_mismatch_never_creates_an_orphan_plan(client, details):
    with client.app.state.sessions() as db:
        db.add(idea("date-fixture", "A dated event", kind="event", **details))
        db.commit()
        before = db.scalar(select(func.count()).select_from(Plan))
    response = client.post("/api/plans", json={"title": "Our weekend", "day": "2026-11-28", "nickname": "Host", "activity_id": "date-fixture"})
    assert response.status_code == 422
    with client.app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Plan)) == before


def test_creation_can_atomically_include_the_initial_suggestion(client):
    response = client.post("/api/plans", json={"title": "Our weekend", "day": "2026-11-28", "nickname": "Host", "activity_id": "mecca-day"})
    assert response.status_code == 201
    assert response.json()["candidates"][0]["activity"]["id"] == "mecca-day"


def test_plan_rejects_known_closed_day_but_allows_unconfirmed_access(client, plan):
    with client.app.state.sessions() as db:
        db.add(idea("closed-fixture", "Closed Saturday", opening_hours=hours(days=[0, 1, 2, 3, 4])))
        db.add(idea("unknown-fixture", "Confirm with provider"))
        db.commit()
    base = f"/api/plans/{plan['id']}/candidates"
    assert client.post(base, json={"activity_id": "closed-fixture"}).status_code == 422
    assert client.post(base, json={"activity_id": "unknown-fixture"}).status_code == 201


def test_pick_rechecks_the_event_date_after_catalogue_changes(client, plan):
    with client.app.state.sessions() as db:
        db.add(idea("changed-event", "An event", kind="event", starts_at="2026-11-28T18:00:00+01:00"))
        db.commit()
    base = f"/api/plans/{plan['id']}"
    response = client.post(base + "/candidates", json={"activity_id": "changed-event"})
    assert response.status_code == 201
    candidate = response.json()["candidates"][0]["id"]
    with client.app.state.sessions() as db:
        activity = db.get(Activity, "changed-event")
        activity.details = {**activity.details, "starts_at": "2026-11-29T18:00:00+01:00"}
        db.commit()
    assert client.post(base + "/pick", json={"candidate_id": candidate}).status_code == 422
    assert client.get(base).json()["selected_candidate"] is None
    assert client.get(base).json()["candidates"][0]["day_problem"]


def test_retirement_preserves_history_without_recommending_removed_records(client, plan, monkeypatch):
    base = f"/api/plans/{plan['id']}"
    added = client.post(base + "/candidates", json={"activity_id": "mecca-day"}).json()
    candidate = added["candidates"][0]["id"]
    client.post("/api/activities/mecca-day/reviews", json={"nickname": "Host", "rating": 5, "text": "My own visit was enjoyable.", "visited_on": "2026-11-20"})
    reduced = [a for a in catalogue.ACTIVITIES if a["id"] != "mecca-day"]
    with monkeypatch.context() as context:
        context.setattr(catalogue, "ACTIVITIES", reduced)
        context.setattr(bootstrap, "ACTIVITIES", reduced)
        assert bootstrap.initialize_database(client.app.state.engine)
        assert client.get("/api/config").json()["catalogue_stats"]["activities"] == len(reduced)
        assert "mecca-day" not in {a["id"] for a in client.get("/api/activities?when=anytime").json()["items"]}
        saved = client.get(base).json()["candidates"][0]
        assert saved["activity"]["catalogue_active"] is False
        assert saved["activity"]["opening"]["state"] == "unknown"
        assert saved["day_problem"]
        assert client.get("/api/activities/mecca-day/reviews").json()[0]["text"] == "My own visit was enjoyable."
        assert client.post(base + "/pick", json={"candidate_id": candidate}).status_code == 422
        assert client.post("/api/plans", json={"title": "Another plan", "day": "2026-11-28", "nickname": "Host", "activity_id": "mecca-day"}).status_code == 422
    assert bootstrap.initialize_database(client.app.state.engine)
    assert "mecca-day" in {a["id"] for a in client.get("/api/activities?when=anytime").json()["items"]}


def test_retired_listings_cannot_bypass_filters_using_open_now():
    activity = idea("retired", "Saved place", opening_hours=hours(), catalogue_active=False)
    assert discover([activity], now=NOW, open_now=True) == []


def test_upgrade_reconciles_old_catalogue_rows_even_without_a_data_edit(client):
    with client.app.state.sessions() as db:
        db.add(idea("legacy-listing", "Old removed record"))
        marker = db.get(DatabaseState, bootstrap.STATE_ID)
        old_payload = json.dumps([catalogue.VENUES, catalogue.ACTIVITIES], sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        marker.catalogue_digest = hashlib.sha256(old_payload.encode()).hexdigest()
        db.commit()
    assert bootstrap.initialize_database(client.app.state.engine)
    assert client.get("/api/activities/legacy-listing").json()["catalogue_active"] is False
    assert not bootstrap.initialize_database(client.app.state.engine)


def test_database_outage_is_sanitized_and_recoverable(client):
    def unavailable(connection, cursor, statement, parameters, context, many):
        raise OperationalError("secret SQL", {}, Exception("private connection string"))

    engine = client.app.state.engine
    event.listen(engine, "before_cursor_execute", unavailable)
    try:
        response = client.get("/api/activities")
        assert response.status_code == 503
        assert "temporarily unavailable" in response.json()["detail"]
        assert "private" not in response.text and "SQL" not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["retry-after"] == "5"
    finally:
        event.remove(engine, "before_cursor_execute", unavailable)
    assert client.get("/api/activities").status_code == 200


def test_denied_origin_still_receives_security_headers(client):
    response = client.post("/api/plans", headers={"Origin": "https://example.invalid"}, json={})
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_directory_and_shared_plan_fetch_in_bounded_queries(client, plan):
    with client.app.state.sessions() as db:
        db.add_all(idea(f"batch-{i}", f"Batch fixture {i}") for i in range(10))
        db.commit()
    base = f"/api/plans/{plan['id']}"
    for i in range(10):
        assert client.post(base + "/candidates", json={"activity_id": f"batch-{i}"}).status_code == 201
    queries = []

    def count(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(statement)

    event.listen(client.app.state.engine, "before_cursor_execute", count)
    try:
        assert len(client.get("/api/activities?when=anytime").json()["items"]) > 150
        assert len(queries) <= 3
        queries.clear()
        assert len(client.get(base).json()["candidates"]) == 10
        assert len(queries) <= 7
    finally:
        event.remove(client.app.state.engine, "before_cursor_execute", count)
