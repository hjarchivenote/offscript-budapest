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
