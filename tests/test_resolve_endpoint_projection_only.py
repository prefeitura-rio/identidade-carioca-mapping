import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_api_user
from app.models import Action, Endpoint
from app.routers.mappings import resolve_router
from app.services.redis_mapping import RedisMappingService
from app.services.redis_mapping_keys import (
    LOOKUP_CACHE_PREFIX,
    MAPPING_ALL_KEY,
    PATTERN_KEY_PREFIX,
)


@pytest.fixture
def client(
    db_session: Session,
    redis_mapping_service: RedisMappingService,  # noqa: ARG001 - wires RedisMappingService.redis_client to fake_redis
) -> TestClient:
    """A minimal app exposing only the real `/resolve` route.

    Auth and the DB session are overridden; `MappingService` is exercised
    for real (including the real `RedisMappingService`), which is wired to
    a per-test fake Redis via the `redis_mapping_service` fixture's
    class-level `redis_client` patch.
    """
    app = FastAPI()
    app.include_router(resolve_router, prefix="/api/v1")
    app.dependency_overrides[get_api_user] = lambda: {"subject": "test-client"}
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def _seed_endpoint(db_session: Session) -> Action:
    action = Action(name="user:read", description="Read a user")
    db_session.add(action)
    db_session.commit()

    db_session.add(
        Endpoint(
            path_pattern="/api/v1/users/:user_id", method="GET", action_id=action.id
        )
    )
    db_session.commit()
    return action


def test_resolve_endpoint_returns_404_on_a_cold_projection(
    client: TestClient, db_session: Session
) -> None:
    """A matching PostgreSQL row must not surface through /resolve while the
    Redis projection is cold - the endpoint must 404, not fall back to the DB."""
    _seed_endpoint(db_session)

    response = client.post(
        "/api/v1/resolve", json={"path": "/api/v1/users/42", "method": "GET"}
    )

    assert response.status_code == 404


def test_resolve_endpoint_cold_projection_creates_no_redis_keys(
    client: TestClient, db_session: Session, fake_redis
) -> None:
    _seed_endpoint(db_session)

    response = client.post(
        "/api/v1/resolve", json={"path": "/api/v1/users/42", "method": "GET"}
    )

    assert response.status_code == 404
    assert fake_redis.exists(MAPPING_ALL_KEY) == 0
    assert fake_redis.keys(f"{LOOKUP_CACHE_PREFIX}*") == []
    assert fake_redis.keys(f"{PATTERN_KEY_PREFIX}*") == []


def test_resolve_endpoint_returns_the_mapping_on_a_warm_projection(
    client: TestClient,
    redis_mapping_service: RedisMappingService,
) -> None:
    redis_mapping_service.store_mapping(
        {
            "id": 1,
            "method": "GET",
            "path_pattern": "/api/v1/users/:user_id",
            "action_id": 1,
            "action_name": "user:read",
            "description": None,
        }
    )

    response = client.post(
        "/api/v1/resolve", json={"path": "/api/v1/users/42", "method": "GET"}
    )

    assert response.status_code == 200
    assert response.json()["action"] == "user:read"
