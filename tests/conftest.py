from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

NOW = datetime(2026, 11, 22, 14, 0, tzinfo=UTC)


@pytest.fixture
def client():
    app = create_app("sqlite://", clock=lambda: NOW)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def friend(client):
    other = TestClient(client.app)
    yield other
    other.close()


@pytest.fixture
def plan(client):
    response = client.post(
        "/api/plans", json={"title": "Our Saturday", "day": "2026-11-28", "nickname": "Alex"}
    )
    assert response.status_code == 201
    return response.json()
