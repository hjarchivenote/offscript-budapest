from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

from fastapi.testclient import TestClient
from sqlalchemy import event

from app.main import create_app
from app.models import Review


def test_no_imported_reviews_and_own_review_can_be_updated(client):
    url = "/api/activities/mecca-day/reviews"
    assert client.get(url).json() == []
    body = {
        "nickname": "Alex",
        "rating": 4,
        "text": "My own local test experience.",
        "visited_on": "2026-11-21",
    }
    assert client.post(url, json=body).status_code == 200
    body["rating"] = 3
    assert client.post(url, json=body).status_code == 200
    reviews = client.get(url).json()
    assert len(reviews) == 1
    assert reviews[0]["rating"] == 3
    assert "visitor_hash" not in reviews[0]


def test_future_review_and_invalid_rating_are_rejected(client):
    url = "/api/activities/mecca-day/reviews"
    body = {
        "nickname": "Alex",
        "rating": 5,
        "text": "A genuine visit description.",
        "visited_on": "2026-11-23",
    }
    assert client.post(url, json=body).status_code == 422
    body.update(visited_on="2026-11-21", rating=6)
    assert client.post(url, json=body).status_code == 422


def test_review_response_identifies_the_saved_record_and_returns_normalized_values(client):
    url = "/api/activities/mecca-day/reviews"
    body = {"nickname": "  Alex  ", "rating": 4, "text": "  An original visit description.  ", "visited_on": "2026-11-21"}
    saved = client.post(url, json=body).json()["review"]
    assert saved == {"id": saved["id"], "nickname": "Alex", "rating": 4, "text": "An original visit description.", "visited_on": "2026-11-21"}
    assert client.get(url).json() == [saved]
    updated = client.post(url, json={**body, "rating": 3}).json()["review"]
    assert updated["id"] == saved["id"] and updated["rating"] == 3
    assert client.get(url).json() == [updated]
    assert "visitor_hash" not in updated


def test_concurrent_review_edits_store_one_complete_submission_without_mixing_fields(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'reviews.db'}", clock=lambda: datetime(2026, 11, 22, tzinfo=UTC))
    url = "/api/activities/mecca-day/reviews"
    original = {"nickname": "Alex", "rating": 4, "text": "Original complete visit description.", "visited_on": "2026-11-21"}
    submissions = [{**original, "rating": 5}, {**original, "text": "A revised complete visit description."}]
    with TestClient(app) as host:
        assert host.post(url, json=original).status_code == 200
        barrier = Barrier(2)

        def same_initial_snapshot(review, context):
            if context.session.bind is app.state.engine:
                barrier.wait(timeout=10)

        def write(body):
            other = TestClient(app)
            try:
                other.cookies.update(dict(host.cookies))
                return other.post(url, json=body)
            finally:
                other.close()

        event.listen(Review, "load", same_initial_snapshot)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(write, submissions))
        finally:
            event.remove(Review, "load", same_initial_snapshot)
        assert [response.status_code for response in responses] == [200, 200]
        records = host.get(url).json()
        assert len(records) == 1
        record = {key: records[0][key] for key in original}
        assert record in submissions
