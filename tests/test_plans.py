from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import NOW


def add_candidate(client, plan, activity="mecca-day"):
    response = client.post(f"/api/plans/{plan['id']}/candidates", json={"activity_id": activity})
    assert response.status_code == 201
    return response.json()["candidates"][-1]["id"]


def test_invitation_and_guest_join_need_no_account(client, friend, plan):
    assert plan["me"] == {"nickname": "Alex", "is_host": True}
    assert client.get(plan["invite_path"]).status_code == 200
    view = friend.get(f"/api/plans/{plan['id']}").json()
    assert view["me"] is None
    assert "visitor_hash" not in str(view)
    joined = friend.post(f"/api/plans/{plan['id']}/join", json={"nickname": "Sam"}).json()
    assert joined["me"] == {"nickname": "Sam", "is_host": False}
    assert joined["participants"] == ["Alex", "Sam"]


def test_joining_twice_updates_name_without_duplicate_participant(client, friend, plan):
    url = f"/api/plans/{plan['id']}/join"
    friend.post(url, json={"nickname": "Sam"})
    response = friend.post(url, json={"nickname": "Samantha"})
    assert response.json()["participants"] == ["Alex", "Samantha"]


def test_uninvited_participant_cannot_suggest_or_vote(client, friend, plan):
    candidate_id = add_candidate(client, plan)
    base = f"/api/plans/{plan['id']}/candidates"
    assert friend.post(base, json={"activity_id": "fszek-study"}).status_code == 403
    assert friend.put(f"{base}/{candidate_id}/vote", json={"choice": "yes"}).status_code == 403


def test_friends_can_suggest_and_change_vote_without_double_count(client, friend, plan):
    friend.post(f"/api/plans/{plan['id']}/join", json={"nickname": "Sam"})
    candidate_id = add_candidate(friend, plan)
    url = f"/api/plans/{plan['id']}/candidates/{candidate_id}/vote"
    client.put(url, json={"choice": "yes"})
    friend.put(url, json={"choice": "yes"})
    friend.put(url, json={"choice": "yes"})
    result = friend.put(url, json={"choice": "maybe"}).json()
    assert result["candidates"][0]["votes"] == {"yes": 1, "maybe": 1, "no": 0}
    assert result["candidates"][0]["my_vote"] == "maybe"


def test_duplicate_suggestion_is_rejected(client, plan):
    add_candidate(client, plan)
    response = client.post(f"/api/plans/{plan['id']}/candidates", json={"activity_id": "mecca-day"})
    assert response.status_code == 409


def test_candidate_from_another_plan_cannot_be_voted_or_picked(client, plan):
    candidate_id = add_candidate(client, plan)
    other = client.post(
        "/api/plans", json={"title": "Other day", "day": "2026-11-29", "nickname": "Alex"}
    ).json()
    base = f"/api/plans/{other['id']}"
    assert (
        client.put(f"{base}/candidates/{candidate_id}/vote", json={"choice": "yes"}).status_code
        == 404
    )
    assert client.post(f"{base}/pick", json={"candidate_id": candidate_id}).status_code == 404


def test_only_creator_can_finalize_and_finalized_plan_is_read_only(client, friend, plan):
    candidate_id = add_candidate(client, plan)
    base = f"/api/plans/{plan['id']}"
    friend.post(f"{base}/join", json={"nickname": "Sam"})
    assert friend.post(f"{base}/pick", json={"candidate_id": candidate_id}).status_code == 403
    response = client.post(f"{base}/pick", json={"candidate_id": candidate_id})
    assert response.json()["selected_candidate"] == candidate_id
    assert (
        friend.put(f"{base}/candidates/{candidate_id}/vote", json={"choice": "yes"}).status_code
        == 409
    )
    assert client.post(f"{base}/candidates", json={"activity_id": "fszek-study"}).status_code == 409


def test_plan_dates_and_nicknames_are_validated(client):
    assert (
        client.post(
            "/api/plans", json={"title": "Weekend", "day": "2026-11-21", "nickname": "Alex"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/plans", json={"title": "Weekend", "day": "2026-11-28", "nickname": "  "}
        ).status_code
        == 422
    )


def test_cross_origin_mutations_are_rejected(client):
    response = client.post(
        "/api/plans",
        headers={"Origin": "https://unrelated.example"},
        json={"title": "Weekend", "day": "2026-11-28", "nickname": "Alex"},
    )
    assert response.status_code == 403


def test_unknown_plan_is_not_found(client):
    assert client.get("/api/plans/not-a-real-link").status_code == 404


def test_plan_and_votes_survive_application_restart(tmp_path):
    url = f"sqlite:///{tmp_path / 'persistent.db'}"
    with TestClient(create_app(url, clock=lambda: NOW)) as first:
        plan = first.post(
            "/api/plans", json={"title": "Keep this plan", "day": "2026-11-28", "nickname": "Alex"}
        ).json()
        candidate_id = add_candidate(first, plan)
        first.put(f"/api/plans/{plan['id']}/candidates/{candidate_id}/vote", json={"choice": "yes"})
    with TestClient(create_app(url, clock=lambda: NOW)) as second:
        result = second.get(f"/api/plans/{plan['id']}").json()
        assert result["title"] == "Keep this plan"
        assert result["candidates"][0]["votes"]["yes"] == 1


def test_production_requires_shared_database(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    try:
        create_app("sqlite://")
    except RuntimeError as error:
        assert "shared PostgreSQL" in str(error)
    else:
        raise AssertionError("Production must not use a per-replica SQLite database")
