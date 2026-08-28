import pytest
from sqlalchemy.orm import Session

from app.models import Action, Endpoint
from app.services.mapping import MappingService
from app.services.redis_mapping import RedisMappingService
from app.services.redis_mapping_keys import (
    LOOKUP_CACHE_PREFIX,
    MAPPING_ALL_KEY,
    PATTERN_KEY_PREFIX,
)


@pytest.fixture
def mapping_service(redis_mapping_service: RedisMappingService) -> MappingService:
    """A MappingService wired to the fake-Redis-backed RedisMappingService."""
    service = MappingService()
    service.redis_mapping_service = redis_mapping_service
    return service


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


def test_resolve_mapping_returns_the_projected_action_on_a_redis_hit(
    db_session: Session,
    mapping_service: MappingService,
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

    result = mapping_service.resolve_mapping(
        db_session, path="/api/v1/users/42", method="GET"
    )

    assert result is not None
    assert result["action"] == "user:read"
    assert result["path_pattern"] == "/api/v1/users/:user_id"


def test_resolve_mapping_returns_none_on_a_cold_projection_with_a_matching_db_row(
    db_session: Session, mapping_service: MappingService
) -> None:
    """A Redis miss returns None immediately - a matching PostgreSQL row must
    not be used as a fallback."""
    _seed_endpoint(db_session)

    result = mapping_service.resolve_mapping(
        db_session, path="/api/v1/users/42", method="GET"
    )

    assert result is None


def test_resolve_mapping_cold_projection_never_queries_postgresql(
    db_session: Session,
    mapping_service: MappingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_endpoint(db_session)

    def _fail_if_queried(*_args, **_kwargs):
        raise AssertionError("resolve_mapping must not query PostgreSQL")

    monkeypatch.setattr(db_session, "query", _fail_if_queried)

    result = mapping_service.resolve_mapping(
        db_session, path="/api/v1/users/42", method="GET"
    )

    assert result is None


def test_resolve_mapping_cold_projection_creates_no_redis_keys(
    db_session: Session,
    mapping_service: MappingService,
    fake_redis,
) -> None:
    _seed_endpoint(db_session)

    result = mapping_service.resolve_mapping(
        db_session, path="/api/v1/users/42", method="GET"
    )

    assert result is None
    assert fake_redis.exists(MAPPING_ALL_KEY) == 0
    assert fake_redis.keys(f"{LOOKUP_CACHE_PREFIX}*") == []
    assert fake_redis.keys(f"{PATTERN_KEY_PREFIX}*") == []
