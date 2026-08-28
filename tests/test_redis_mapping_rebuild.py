import json

import pytest
import redis

from app.services.redis_mapping_keys import (
    LOOKUP_CACHE_PREFIX,
    MAPPING_ALL_KEY,
    pattern_key,
)
from app.services.redis_mapping_rebuild import MappingProjectionRebuilder


def _serialize(mapping_data: dict) -> str:
    return json.dumps(mapping_data)


def _rebuilder(fake_redis) -> MappingProjectionRebuilder:
    return MappingProjectionRebuilder(fake_redis, _serialize)


def test_rebuild_publishes_all_mappings_with_no_ttl_on_live_keys(fake_redis) -> None:
    mappings = [
        {"id": 1, "method": "GET", "path_pattern": "/api/v1/users/:id"},
        {"id": 2, "method": "POST", "path_pattern": "/api/v1/users"},
    ]

    cleared = _rebuilder(fake_redis).rebuild(mappings)

    assert cleared == set()
    assert json.loads(fake_redis.hget(MAPPING_ALL_KEY, "mapping_1"))["id"] == 1
    assert json.loads(fake_redis.hget(MAPPING_ALL_KEY, "mapping_2"))["id"] == 2
    assert fake_redis.lrange(pattern_key("GET"), 0, -1) == [b"mapping_1"]
    assert fake_redis.lrange(pattern_key("POST"), 0, -1) == [b"mapping_2"]
    assert fake_redis.ttl(MAPPING_ALL_KEY) == -1
    assert fake_redis.ttl(pattern_key("GET")) == -1


def test_rebuild_on_empty_projection_creates_only_the_meta_marker(fake_redis) -> None:
    cleared = _rebuilder(fake_redis).rebuild([])

    assert cleared == set()
    all_fields = fake_redis.hgetall(MAPPING_ALL_KEY)
    assert list(all_fields.keys()) == [b"__build__"]


def test_rebuild_clears_a_method_pattern_list_with_no_replacement(fake_redis) -> None:
    _rebuilder(fake_redis).rebuild(
        [{"id": 1, "method": "DELETE", "path_pattern": "/api/v1/users/:id"}]
    )

    cleared = _rebuilder(fake_redis).rebuild(
        [{"id": 2, "method": "GET", "path_pattern": "/api/v1/users"}]
    )

    assert cleared == {"DELETE"}
    assert fake_redis.lrange(pattern_key("DELETE"), 0, -1) == []
    assert fake_redis.lrange(pattern_key("GET"), 0, -1) == [b"mapping_2"]


def test_rebuild_drops_the_lookup_cache(fake_redis) -> None:
    fake_redis.setex(f"{LOOKUP_CACHE_PREFIX}GET:/api/v1/users/1", 60, "mapping_1")

    _rebuilder(fake_redis).rebuild(
        [{"id": 1, "method": "GET", "path_pattern": "/api/v1/users/:id"}]
    )

    assert fake_redis.keys(f"{LOOKUP_CACHE_PREFIX}*") == []


def test_rebuild_is_idempotent_across_repeated_runs(fake_redis) -> None:
    mappings = [{"id": 1, "method": "GET", "path_pattern": "/api/v1/users/:id"}]

    _rebuilder(fake_redis).rebuild(mappings)
    _rebuilder(fake_redis).rebuild(mappings)

    assert fake_redis.lrange(pattern_key("GET"), 0, -1) == [b"mapping_1"]
    assert fake_redis.hlen(MAPPING_ALL_KEY) == 1


def test_stage_never_writes_to_the_live_keys(fake_redis) -> None:
    """The staging phase is namespace-isolated: nothing lands on live keys."""
    fake_redis.hset(MAPPING_ALL_KEY, "mapping_1", _serialize({"id": 1}))
    fake_redis.rpush(pattern_key("GET"), "mapping_1")

    rebuilder = _rebuilder(fake_redis)
    rebuilder._stage(
        [{"id": 2, "method": "GET", "path_pattern": "/api/v1/other"}], "token-a"
    )

    assert json.loads(fake_redis.hget(MAPPING_ALL_KEY, "mapping_1")) == {"id": 1}
    assert fake_redis.lrange(pattern_key("GET"), 0, -1) == [b"mapping_1"]
    assert fake_redis.hget(MAPPING_ALL_KEY, "mapping_2") is None


def test_swap_failure_leaves_the_previous_live_projection_fully_intact(
    fake_redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the atomic swap transaction errors, the prior projection must survive
    unmodified - there is no window where live keys are deleted before the
    replacement is confirmed to have landed."""
    fake_redis.hset(MAPPING_ALL_KEY, "mapping_1", _serialize({"id": 1}))
    fake_redis.rpush(pattern_key("GET"), "mapping_1")

    rebuilder = _rebuilder(fake_redis)
    staging_all_key, staging_pattern_keys = rebuilder._stage(
        [{"id": 2, "method": "GET", "path_pattern": "/api/v1/other"}], "token-b"
    )

    real_pipeline = fake_redis.pipeline

    class _FailingPipeline:
        def __init__(self, wrapped):
            self._wrapped = wrapped

        def __getattr__(self, name):
            return getattr(self._wrapped, name)

        def execute(self):
            raise redis.RedisError("simulated transport failure")

    monkeypatch.setattr(
        fake_redis,
        "pipeline",
        lambda transaction=True: _FailingPipeline(
            real_pipeline(transaction=transaction)
        ),
    )

    with pytest.raises(redis.RedisError):
        rebuilder._swap(staging_all_key, staging_pattern_keys)

    assert json.loads(fake_redis.hget(MAPPING_ALL_KEY, "mapping_1")) == {"id": 1}
    assert fake_redis.lrange(pattern_key("GET"), 0, -1) == [b"mapping_1"]
