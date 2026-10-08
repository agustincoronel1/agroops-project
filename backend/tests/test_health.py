from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import main
from app.infrastructure.redis import get_redis, ping_redis
from app.main import app
from app.schemas.health import HealthResponse


@pytest.fixture
def redis_mock() -> Iterator[AsyncMock]:
    mock = AsyncMock()
    app.dependency_overrides[get_redis] = lambda: mock
    yield mock
    app.dependency_overrides.clear()


def test_health_response_model() -> None:
    r = HealthResponse(status="ok", service="s", version="1", redis="ok")
    assert r.model_dump() == {
        "status": "ok",
        "service": "s",
        "version": "1",
        "redis": "ok",
    }
    with pytest.raises(ValidationError):
        HealthResponse(status="bad", service="s", version="1", redis="ok")


def test_health_ok(redis_mock: AsyncMock) -> None:
    redis_mock.ping.return_value = True
    resp = TestClient(app).get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["redis"] == "ok"


def test_health_redis_down(redis_mock: AsyncMock) -> None:
    redis_mock.ping.side_effect = ConnectionError("boom")
    resp = TestClient(app).get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["redis"] == "unavailable"
    assert "boom" not in resp.text


def test_ping_redis_handles_errors() -> None:
    import asyncio

    client = AsyncMock()
    client.ping.return_value = True
    assert asyncio.run(ping_redis(client)) is True
    client.ping.side_effect = ConnectionError()
    assert asyncio.run(ping_redis(client)) is False


def test_lifespan_fails_clearly_when_redis_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = AsyncMock()
    fake.ping.side_effect = ConnectionError()
    monkeypatch.setattr(main, "create_redis_client", lambda url: fake)
    with pytest.raises(RuntimeError, match="Cannot connect to Redis"):
        with TestClient(app):
            pass
    fake.aclose.assert_awaited_once()


def test_lifespan_closes_redis_on_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = AsyncMock()
    fake.ping.return_value = True
    monkeypatch.setattr(main, "create_redis_client", lambda url: fake)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
    fake.aclose.assert_awaited_once()
