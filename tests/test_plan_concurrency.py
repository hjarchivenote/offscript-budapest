from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier, Event, Lock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from app.discovery import plan_day_problem
from app.main import create_app
from app.models import Activity


@pytest.fixture
def shared(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'parallel.db'}", clock=lambda: datetime.fromisoformat("2026-11-25T12:00:00+01:00"))
    with TestClient(app) as host:
        plan = host.post("/api/plans", json={"title": "Concurrent requests", "nickname": "Host", "day": "2026-11-28"}).json()
        yield host, f"/api/plans/{plan['id']}"


def request(host, method, path, body, *, cookies=None, barrier=None):
    with TestClient(host.app) as other:
        other.cookies.update(cookies if cookies is not None else dict(host.cookies))
        if barrier:
            barrier.wait(timeout=5)
        return other.request(method, path, json=body)


def parallel(host, operations):
    barrier = Barrier(len(operations))
    with ThreadPoolExecutor(max_workers=len(operations)) as pool:
        pending = [pool.submit(request, host, *operation, barrier=barrier) for operation in operations]
        return [future.result(timeout=15) for future in pending]


def candidates(host, base):
    for id in ("mecca-day", "fszek-study"):
        assert host.post(base + "/candidates", json={"activity_id": id}).status_code == 201
    return [c["id"] for c in host.get(base).json()["candidates"]]


def test_two_final_choices_have_one_winner_and_cannot_overwrite_it(shared):
    host, base = shared
    ids = candidates(host, base)
    responses = parallel(host, [("POST", base + "/pick", {"candidate_id": id}) for id in ids])
    assert sorted(r.status_code for r in responses) == [200, 409]
    winner = next(r.json()["selected_candidate"] for r in responses if r.status_code == 200)
    assert host.get(base).json()["selected_candidate"] == winner


def test_simultaneous_votes_from_one_guest_update_one_row(shared):
    host, base = shared
    id = candidates(host, base)[0]
    responses = parallel(host, [("PUT", f"{base}/candidates/{id}/vote", {"choice": choice}) for choice in ("yes", "maybe")])
    assert [r.status_code for r in responses] == [200, 200]
    final = host.get(base).json()["candidates"][0]
    assert sum(final["votes"].values()) == 1
    assert final["my_vote"] in {"yes", "maybe"}


def test_duplicate_join_and_suggestion_are_consistent_under_concurrency(shared):
    host, base = shared
    joins = parallel(host, [("POST", base + "/join", {"nickname": name}) for name in ("Host A", "Host B")])
    assert [r.status_code for r in joins] == [200, 200]
    assert len(host.get(base).json()["participants"]) == 1
    suggestions = parallel(host, [("POST", base + "/candidates", {"activity_id": "mecca-day"})] * 2)
    assert sorted(r.status_code for r in suggestions) == [201, 409]
    assert len(host.get(base).json()["candidates"]) == 1


def test_concurrent_suggestions_cannot_exceed_the_thirty_candidate_limit(shared):
    host, base = shared
    from datetime import date

    with host.app.state.sessions() as db:
        ids = [a.id for a in db.scalars(select(Activity)) if plan_day_problem(a, date(2026, 11, 28), host.app.state.clock()) is None]
    for id in ids[:29]:
        assert host.post(base + "/candidates", json={"activity_id": id}).status_code == 201
    responses = parallel(host, [("POST", base + "/candidates", {"activity_id": id}) for id in ids[29:31]])
    assert sorted(r.status_code for r in responses) == [201, 422]
    assert len(host.get(base).json()["candidates"]) == 30


@pytest.mark.parametrize("operation", ["vote", "join", "suggest"])
def test_waiting_mutations_recheck_the_committed_final_choice(shared, operation):
    host, base = shared
    id = candidates(host, base)[0]
    held, release, requested = Event(), Event(), Event()
    guard = Lock()
    used = [False]

    def hold_first_writer(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE plans SET selected_candidate=plans.selected_candidate"):
            with guard:
                first = not used[0]
                used[0] = True
            if first:
                held.set()
                assert release.wait(timeout=10)

    event.listen(host.app.state.engine, "after_cursor_execute", hold_first_writer)
    operations = {
        "vote": ("PUT", f"{base}/candidates/{id}/vote", {"choice": "yes"}),
        "join": ("POST", base + "/join", {"nickname": "Changed after final choice"}),
        "suggest": ("POST", base + "/candidates", {"activity_id": "embassy-coffee"}),
    }

    def waiting():
        requested.set()
        return request(host, *operations[operation])

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            confirm = pool.submit(request, host, "POST", base + "/pick", {"candidate_id": id})
            assert held.wait(timeout=5)
            mutation = pool.submit(waiting)
            assert requested.wait(timeout=5)
            release.set()
            assert confirm.result(timeout=10).status_code == 200
            assert mutation.result(timeout=10).status_code == 409
    finally:
        release.set()
        event.remove(host.app.state.engine, "after_cursor_execute", hold_first_writer)
    result = host.get(base).json()
    assert result["selected_candidate"] == id
    assert result["participants"] == ["Host"]
    assert len(result["candidates"]) == 2
    assert sum(result["candidates"][0]["votes"].values()) == 0
