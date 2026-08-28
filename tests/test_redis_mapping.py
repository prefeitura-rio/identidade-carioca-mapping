import json

from app.services.redis_mapping import RedisMappingService
from app.services.redis_mapping_keys import MAPPING_ALL_KEY, pattern_key


def _mapping(id: int, method: str, path_pattern: str, action_name: str = "user:read"):
    return {
        "id": id,
        "method": method,
        "path_pattern": path_pattern,
        "action_id": 1,
        "action_name": action_name,
        "description": "test mapping",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": None,
    }


def test_store_mapping_adds_match_regex_without_dropping_existing_fields(
    redis_mapping_service: RedisMappingService, fake_redis
) -> None:
    mapping_data = _mapping(1, "GET", "/api/v1/users/:user_id")

    assert redis_mapping_service.store_mapping(mapping_data) is True

    stored = json.loads(fake_redis.hget(MAPPING_ALL_KEY, "mapping_1"))
    assert stored["match_regex"] == "^/api/v1/users/(?P<user_id>[^/]+)$"
    for key, value in mapping_data.items():
        assert stored[key] == value
    assert fake_redis.lrange(pattern_key("GET"), 0, -1) == [b"mapping_1"]


def test_resolve_mapping_fast_matches_named_param_via_match_regex(
    redis_mapping_service: RedisMappingService,
) -> None:
    redis_mapping_service.store_mapping(_mapping(1, "GET", "/api/v1/users/:user_id"))

    result = redis_mapping_service.resolve_mapping_fast("GET", "/api/v1/users/42")

    assert result is not None
    assert result["action_name"] == "user:read"


def test_resolve_mapping_fast_rejects_non_matching_path(
    redis_mapping_service: RedisMappingService,
) -> None:
    redis_mapping_service.store_mapping(_mapping(1, "GET", "/api/v1/users/:user_id"))

    assert redis_mapping_service.resolve_mapping_fast("GET", "/api/v1/other") is None


def test_resolve_mapping_fast_tolerates_a_cold_legacy_payload_without_match_regex(
    redis_mapping_service: RedisMappingService, fake_redis
) -> None:
    """A payload written before `match_regex` existed still resolves correctly."""
    legacy_payload = _mapping(1, "GET", "/api/v1/users/:user_id")
    fake_redis.hset(MAPPING_ALL_KEY, "mapping_1", json.dumps(legacy_payload))
    fake_redis.rpush(pattern_key("GET"), "mapping_1")

    result = redis_mapping_service.resolve_mapping_fast("GET", "/api/v1/users/42")

    assert result is not None
    assert result["action_name"] == "user:read"


def test_resolve_mapping_fast_never_writes_match_regex_back_to_redis(
    redis_mapping_service: RedisMappingService, fake_redis
) -> None:
    legacy_payload = _mapping(1, "GET", "/api/v1/users/:user_id")
    fake_redis.hset(MAPPING_ALL_KEY, "mapping_1", json.dumps(legacy_payload))
    fake_redis.rpush(pattern_key("GET"), "mapping_1")

    redis_mapping_service.resolve_mapping_fast("GET", "/api/v1/users/42")

    unchanged = json.loads(fake_redis.hget(MAPPING_ALL_KEY, "mapping_1"))
    assert "match_regex" not in unchanged
    assert unchanged == legacy_payload


def test_remove_mapping_deletes_from_hash_and_pattern_list(
    redis_mapping_service: RedisMappingService, fake_redis
) -> None:
    redis_mapping_service.store_mapping(_mapping(1, "GET", "/api/v1/users/:user_id"))

    assert redis_mapping_service.remove_mapping(1, "GET") is True
    assert fake_redis.hget(MAPPING_ALL_KEY, "mapping_1") is None
    assert fake_redis.lrange(pattern_key("GET"), 0, -1) == []
