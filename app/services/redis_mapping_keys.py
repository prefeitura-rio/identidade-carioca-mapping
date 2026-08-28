"""Canonical Redis key names for the mapping projection.

Shared between `RedisMappingService` (per-mapping read/write) and
`MappingProjectionRebuilder` (atomic full-projection rebuild) so both always
agree on the live key names/shapes the AuthZ Go reader also depends on - see
REDIS_MAPPING_QUERY_GUIDE.md.
"""

MAPPING_ALL_KEY = "heimdall:mappings:all"
PATTERN_KEY_PREFIX = "heimdall:mappings:patterns:"
LOOKUP_CACHE_PREFIX = "heimdall:mappings:lookup:"


def pattern_key(method: str) -> str:
    return f"{PATTERN_KEY_PREFIX}{method}"
