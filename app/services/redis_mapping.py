"""
Redis mapping persistence service with OpenTelemetry tracing.
Implements permanent mapping storage with configurable lookup cache as specified in
REDIS_MAPPING_QUERY_GUIDE.md.
"""

import json
import re
from typing import Any

import redis

from app.services.base import BaseService
from app.services.pattern_matching import path_pattern_to_regex
from app.services.redis_mapping_keys import LOOKUP_CACHE_PREFIX, MAPPING_ALL_KEY
from app.services.redis_mapping_keys import pattern_key as _pattern_key
from app.services.redis_mapping_rebuild import MappingProjectionRebuilder
from app.settings import settings


class RedisMappingService(BaseService):
    """Service for Redis mapping persistence operations with permanent storage and configurable cache."""

    def __init__(self):
        super().__init__("redis_mapping")

        # Import here to avoid circular dependency
        from app.services.cache import CacheService

        self._cache_service = CacheService()

        # Configuration
        self.cache_ttl = settings.REDIS_MAPPING_CACHE_TTL

    @property
    def redis_client(self) -> redis.Redis:
        """Get Redis client from cache service to reuse connection."""
        return self._cache_service._get_redis_connection()

    def _serialize_mapping(self, mapping_data: dict[str, Any]) -> str:
        """Build the JSON payload stored per mapping, including `match_regex`.

        `match_regex` is derived fresh from `path_pattern` every time so it
        can never drift from the canonical conversion in
        `app.services.pattern_matching`. Existing consumers that only read
        `path_pattern`/`action_name`/etc. are unaffected - this only adds a
        field to the JSON object.
        """
        payload = {
            **mapping_data,
            "match_regex": path_pattern_to_regex(mapping_data["path_pattern"]),
        }
        return json.dumps(payload)

    def store_mapping(self, mapping_data: dict[str, Any]) -> bool:
        """Store `mapping_data` in the permanent hash and update its pattern list."""
        with self.trace_operation(
            "store_mapping",
            {
                "redis_mapping.operation": "store",
                "redis_mapping.id": mapping_data.get("id"),
                "redis_mapping.method": mapping_data.get("method"),
                "redis_mapping.path_pattern": mapping_data.get("path_pattern"),
            },
        ) as span:
            try:
                redis_conn = self.redis_client
                mapping_id = mapping_data["id"]
                method = mapping_data["method"]

                # Store mapping details in permanent hash (no TTL)
                mapping_key = f"mapping_{mapping_id}"
                redis_conn.hset(
                    MAPPING_ALL_KEY, mapping_key, self._serialize_mapping(mapping_data)
                )

                # Update pattern list for the method (maintain order by specificity)
                pattern_key = _pattern_key(method)

                # Remove from list if already exists (in case of update)
                redis_conn.lrem(pattern_key, 0, mapping_key)

                # Add to front of list (most specific patterns should be first)
                # For now, we'll add new patterns at the beginning
                # TODO: Implement proper specificity ordering if needed
                redis_conn.lpush(pattern_key, mapping_key)

                span.set_attribute("redis_mapping.stored", True)
                span.set_attribute("redis_mapping.pattern_list_updated", True)
                return True

            except (redis.RedisError, KeyError, TypeError) as e:
                span.record_exception(e)
                span.set_attribute("redis_mapping.error", str(e))
                span.set_attribute("redis_mapping.stored", False)
                return False

    def remove_mapping(self, mapping_id: int, method: str) -> bool:
        """Remove a mapping from the permanent hash and its pattern list."""
        with self.trace_operation(
            "remove_mapping",
            {
                "redis_mapping.operation": "remove",
                "redis_mapping.id": mapping_id,
                "redis_mapping.method": method,
            },
        ) as span:
            try:
                redis_conn = self.redis_client
                mapping_key = f"mapping_{mapping_id}"

                # Remove from main storage
                removed_count = redis_conn.hdel(MAPPING_ALL_KEY, mapping_key)

                # Remove from pattern list
                pattern_key = _pattern_key(method)
                redis_conn.lrem(pattern_key, 0, mapping_key)

                # Invalidate any cached lookups that might reference this mapping
                self.invalidate_lookup_cache()

                span.set_attribute("redis_mapping.removed", removed_count > 0)
                span.set_attribute("redis_mapping.pattern_list_updated", True)
                span.set_attribute("redis_mapping.cache_invalidated", True)
                return removed_count > 0

            except redis.RedisError as e:
                span.record_exception(e)
                span.set_attribute("redis_mapping.error", str(e))
                span.set_attribute("redis_mapping.removed", False)
                return False

    def resolve_mapping_fast(self, method: str, path: str) -> dict[str, Any] | None:
        """Resolve via lookup cache, falling back to pattern matching (REDIS_MAPPING_QUERY_GUIDE.md)."""
        with self.trace_operation(
            "resolve_mapping_fast",
            {
                "redis_mapping.operation": "resolve",
                "redis_mapping.method": method,
                "redis_mapping.path": path,
            },
        ) as span:
            try:
                redis_conn = self.redis_client

                # Step 1: Check cache for exact path match
                if self.cache_ttl > 0:  # Only use cache if TTL > 0
                    cache_key = f"{LOOKUP_CACHE_PREFIX}{method}:{path}"
                    cached_mapping_id = redis_conn.get(cache_key)

                    if cached_mapping_id:
                        # Get mapping details from permanent storage
                        mapping_data = redis_conn.hget(
                            MAPPING_ALL_KEY, cached_mapping_id.decode()
                        )
                        if mapping_data:
                            span.set_attribute("redis_mapping.cache_hit", True)
                            span.set_attribute(
                                "redis_mapping.mapping_id", cached_mapping_id.decode()
                            )
                            return json.loads(mapping_data)

                span.set_attribute("redis_mapping.cache_hit", False)

                # Step 2: Pattern matching fallback
                pattern_key = _pattern_key(method)
                mapping_ids = redis_conn.lrange(pattern_key, 0, -1)

                span.set_attribute("redis_mapping.patterns_to_check", len(mapping_ids))

                for mapping_id_bytes in mapping_ids:
                    mapping_id = mapping_id_bytes.decode()

                    # Get mapping details
                    mapping_data_str = redis_conn.hget(MAPPING_ALL_KEY, mapping_id)
                    if not mapping_data_str:
                        continue

                    mapping_data = json.loads(mapping_data_str)
                    path_pattern = mapping_data.get("path_pattern")

                    if not path_pattern:
                        continue

                    # Cold-projection tolerance: entries written before
                    # `match_regex` existed (or a legacy payload) fall back to
                    # deriving it on the fly. This is read-only - it is never
                    # written back to Redis, so PostgreSQL stays the only
                    # writer of mapping data outside the dedicated rebuild.
                    match_regex = mapping_data.get(
                        "match_regex"
                    ) or path_pattern_to_regex(path_pattern)

                    try:
                        if re.match(match_regex, path):
                            span.set_attribute(
                                "redis_mapping.matched_pattern", path_pattern
                            )
                            span.set_attribute("redis_mapping.mapping_id", mapping_id)

                            # Cache the result for future lookups (if caching enabled)
                            if self.cache_ttl > 0:
                                cache_key = f"{LOOKUP_CACHE_PREFIX}{method}:{path}"
                                redis_conn.setex(cache_key, self.cache_ttl, mapping_id)
                                span.set_attribute("redis_mapping.result_cached", True)

                            return mapping_data

                    except re.error as regex_error:
                        span.record_exception(regex_error)
                        span.set_attribute(
                            "redis_mapping.regex_error", str(regex_error)
                        )
                        continue

                span.set_attribute("redis_mapping.no_match", True)
                return None

            except (redis.RedisError, json.JSONDecodeError) as e:
                span.record_exception(e)
                span.set_attribute("redis_mapping.error", str(e))
                return None

    def get_all_mappings(self) -> list[dict[str, Any]]:
        """Get all mappings from permanent storage."""
        with self.trace_operation(
            "get_all_mappings",
            {"redis_mapping.operation": "get_all"},
        ) as span:
            try:
                redis_conn = self.redis_client
                all_mappings_data = redis_conn.hgetall(MAPPING_ALL_KEY)

                mappings = []
                for mapping_data_str in all_mappings_data.values():
                    try:
                        mapping_data = json.loads(mapping_data_str)
                    except json.JSONDecodeError:
                        continue
                    # Skip non-mapping bookkeeping entries (e.g. the rebuild's
                    # empty-projection marker), which never carry an "id".
                    if "id" in mapping_data:
                        mappings.append(mapping_data)

                span.set_attribute("redis_mapping.mappings_count", len(mappings))
                return mappings

            except redis.RedisError as e:
                span.record_exception(e)
                span.set_attribute("redis_mapping.error", str(e))
                return []

    def invalidate_lookup_cache(self) -> bool:
        """Clear all TTL'd lookup cache entries; permanent mapping data is untouched."""
        with self.trace_operation(
            "invalidate_lookup_cache",
            {"redis_mapping.operation": "invalidate_cache"},
        ) as span:
            try:
                redis_conn = self.redis_client

                # Find and delete all lookup cache keys
                cache_keys = redis_conn.keys(f"{LOOKUP_CACHE_PREFIX}*")
                if cache_keys:
                    deleted_count = redis_conn.delete(*cache_keys)
                    span.set_attribute(
                        "redis_mapping.cache_keys_deleted", deleted_count
                    )
                else:
                    span.set_attribute("redis_mapping.cache_keys_deleted", 0)

                span.set_attribute("redis_mapping.cache_invalidated", True)
                return True

            except redis.RedisError as e:
                span.record_exception(e)
                span.set_attribute("redis_mapping.error", str(e))
                span.set_attribute("redis_mapping.cache_invalidated", False)
                return False

    def sync_all_mappings_from_db(self, mappings: list[dict[str, Any]]) -> bool:
        """Atomically rebuild the Redis projection from `mappings` (see `redis_mapping_rebuild`)."""
        with self.trace_operation(
            "sync_all_mappings_from_db",
            {
                "redis_mapping.operation": "sync_all",
                "redis_mapping.mappings_count": len(mappings),
            },
        ) as span:
            try:
                rebuilder = MappingProjectionRebuilder(
                    self.redis_client, self._serialize_mapping
                )
                cleared_methods = rebuilder.rebuild(mappings)

                span.set_attribute("redis_mapping.sync_successful", True)
                span.set_attribute("redis_mapping.synced_count", len(mappings))
                span.set_attribute(
                    "redis_mapping.methods_cleared", len(cleared_methods)
                )
                return True

            except redis.RedisError as e:
                span.record_exception(e)
                span.set_attribute("redis_mapping.error", str(e))
                span.set_attribute("redis_mapping.sync_failed", True)
                return False

    def health_check(self) -> bool:
        """Check Redis mapping storage health."""
        with self.trace_operation(
            "health_check",
            {"redis_mapping.operation": "health_check"},
        ) as span:
            try:
                redis_conn = self.redis_client

                # Test basic operations
                test_key = "heimdall:mappings:health_check"
                redis_conn.set(test_key, "ok", ex=10)  # 10 second TTL
                result = redis_conn.get(test_key)
                redis_conn.delete(test_key)

                is_healthy = result is not None
                span.set_attribute("redis_mapping.healthy", is_healthy)
                return is_healthy

            except redis.RedisError as e:
                span.record_exception(e)
                span.set_attribute("redis_mapping.error", str(e))
                span.set_attribute("redis_mapping.healthy", False)
                return False
