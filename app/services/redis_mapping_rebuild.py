"""Atomic, from-scratch rebuild of the Redis mapping projection.

Builds a fresh projection under a disjoint staging namespace and publishes
it onto the live keys (shared with `RedisMappingService.resolve_mapping_fast`
and the AuthZ Go reader) with one Redis transaction, so a rebuild in
progress is never observable from outside and live keys are never deleted
ahead of their replacement.

Intended caller: the staging rebuild Job, run once after Alembic migrations
and DB seeding complete (see `app/rebuild_mapping_projection.py`). This is a
one-shot snapshot-and-swap, not a continuously-running reconciler: a CRUD
write (create/update/delete_mapping) landing on the live keys mid-rebuild
would be overwritten by the swap.
"""

import json
import uuid
from collections.abc import Callable
from typing import Any

import redis

from app.services.redis_mapping_keys import (
    LOOKUP_CACHE_PREFIX,
    MAPPING_ALL_KEY,
    PATTERN_KEY_PREFIX,
    pattern_key,
)

_REBUILD_KEY_PREFIX = "heimdall:mappings:rebuild:"
# Safety net only: staging keys self-expire if a rebuild crashes before the
# atomic swap. On success every staging key is PERSISTed (TTL cleared) in the
# same transaction that renames it onto its live counterpart, so this TTL
# never applies to data that reaches the live keys.
_REBUILD_STAGING_TTL_SECONDS = 900


class MappingProjectionRebuilder:
    """Rebuilds the entire Redis mapping projection from a PostgreSQL snapshot."""

    def __init__(
        self,
        redis_client: redis.Redis,
        serialize_mapping: Callable[[dict[str, Any]], str],
    ):
        self._redis = redis_client
        self._serialize_mapping = serialize_mapping

    def rebuild(self, mappings: list[dict[str, Any]]) -> set[str]:
        """Stage `mappings`, then atomically publish them onto the live keys.

        Returns the set of HTTP methods whose live pattern list existed
        before this call but had no replacement in `mappings` (and was
        therefore cleared rather than renamed).
        """
        build_token = uuid.uuid4().hex
        staging_all_key, staging_pattern_keys = self._stage(mappings, build_token)
        return self._swap(staging_all_key, staging_pattern_keys)

    def _stage(
        self, mappings: list[dict[str, Any]], build_token: str
    ) -> tuple[str, dict[str, str]]:
        """Write `mappings` into fresh, TTL-guarded staging keys.

        Returns the staging "all" hash key and a `{method: staging_list_key}`
        map for every method that has at least one mapping.
        """
        staging_all_key = f"{_REBUILD_KEY_PREFIX}{build_token}:all"
        if not mappings:
            # A marker field guarantees this hash key exists when `mappings`
            # is empty (a HSET with zero fields would leave nothing to
            # rename). It is never written when there is at least one real
            # mapping, so it never leaks into `RedisMappingService.get_all_mappings()`.
            self._redis.hset(
                staging_all_key, "__build__", json.dumps({"build_token": build_token})
            )
        self._redis.expire(staging_all_key, _REBUILD_STAGING_TTL_SECONDS)

        staging_pattern_keys: dict[str, str] = {}
        for mapping_data in mappings:
            mapping_key = f"mapping_{mapping_data['id']}"
            self._redis.hset(
                staging_all_key, mapping_key, self._serialize_mapping(mapping_data)
            )

            method = mapping_data["method"]
            staging_pattern_key = staging_pattern_keys.get(method)
            if staging_pattern_key is None:
                staging_pattern_key = (
                    f"{_REBUILD_KEY_PREFIX}{build_token}:patterns:{method}"
                )
                staging_pattern_keys[method] = staging_pattern_key
                self._redis.expire(staging_pattern_key, _REBUILD_STAGING_TTL_SECONDS)
            self._redis.rpush(staging_pattern_key, mapping_key)

        return staging_all_key, staging_pattern_keys

    def _swap(
        self, staging_all_key: str, staging_pattern_keys: dict[str, str]
    ) -> set[str]:
        """Publish a staged build onto the live keys in one atomic transaction.

        Also clears any live per-method pattern list that has no replacement
        in this build, and drops the lookup cache, so nothing observable from
        outside this transaction ever mixes old and new projection data.
        """
        live_pattern_keys = self._redis.keys(f"{PATTERN_KEY_PREFIX}*")
        live_methods = {
            key.decode().removeprefix(PATTERN_KEY_PREFIX) for key in live_pattern_keys
        }
        stale_methods = live_methods - staging_pattern_keys.keys()
        lookup_cache_keys = self._redis.keys(f"{LOOKUP_CACHE_PREFIX}*")

        pipe = self._redis.pipeline(transaction=True)
        # PERSIST before RENAME: Redis' RENAME carries the source key's TTL
        # to the destination, so skipping this would make the live key
        # inherit the staging safety-net expiry and vanish later.
        pipe.persist(staging_all_key)
        pipe.rename(staging_all_key, MAPPING_ALL_KEY)
        for method, staging_pattern_key in staging_pattern_keys.items():
            pipe.persist(staging_pattern_key)
            pipe.rename(staging_pattern_key, pattern_key(method))
        for method in stale_methods:
            pipe.delete(pattern_key(method))
        if lookup_cache_keys:
            pipe.delete(*lookup_cache_keys)
        pipe.execute()

        return stale_methods
