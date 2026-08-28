import fakeredis
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Action, Endpoint, User
from app.services.redis_mapping import RedisMappingService

# Only the tables these tests actually exercise: `AdminAudit`/`FailedOperation`
# use Postgres-only `JSONB` columns that SQLite's compiler cannot render.
_TEST_TABLES = [Action.__table__, User.__table__, Endpoint.__table__]


@pytest.fixture
def fake_redis() -> fakeredis.FakeRedis:
    """A fresh, isolated in-memory Redis server for a single test."""
    return fakeredis.FakeRedis()


@pytest.fixture
def redis_mapping_service(
    monkeypatch: pytest.MonkeyPatch, fake_redis: fakeredis.FakeRedis
) -> RedisMappingService:
    """A `RedisMappingService` backed by `fake_redis` instead of a real server."""
    service = RedisMappingService()
    monkeypatch.setattr(
        RedisMappingService, "redis_client", property(lambda _self: fake_redis)
    )
    return service


@pytest.fixture
def db_session() -> Session:
    """An isolated in-memory SQLite session with the mapping-relevant tables."""
    engine = create_engine("sqlite:///:memory:")
    Action.metadata.create_all(engine, tables=_TEST_TABLES)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
