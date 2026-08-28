"""Canonical endpoint path-pattern to regex conversion.

This module is the single source of truth for turning an `Endpoint.path_pattern`
(as authored via the mappings API) into an anchored regular expression. The
resulting text is stored in Redis as `match_regex` and is meant to be
evaluated identically by:

- this service's own fast path (`RedisMappingService.resolve_mapping_fast`)
- this service's PostgreSQL fallback path (`MappingService.resolve_mapping`)
- the AuthZ (Cerbos ext_authz) Go service, which evaluates the same string
  with Go's `regexp` package (RE2)

Only RE2-compatible constructs are emitted (anchors, named groups via
`(?P<name>...)`, `.*`, `[^/]*`) - no backreferences or lookaround - so the
same pattern text matches identically in Python's `re` and Go's `regexp`.

Supported syntaxes:
- `:param` -> named capture group matching a single path segment
- `*`      -> any characters within a single path segment
- `**`     -> any characters across multiple path segments
- a pattern containing parentheses is treated as an already-authored raw
  regex and only gets anchors added
"""

import re

_PARAM_PATTERN = re.compile(r":([a-zA-Z_][a-zA-Z0-9_]*)")


def path_pattern_to_regex(path_pattern: str) -> str:
    """Convert a path pattern into one canonical anchored regex string.

    Note: `re.escape` does not escape `:` (it is not a regex metacharacter
    in Python or RE2), so `_PARAM_PATTERN` intentionally matches the literal,
    unescaped colon left behind by `re.escape` below.
    """
    if "(" in path_pattern and ")" in path_pattern:
        return _anchor(path_pattern)

    escaped = re.escape(path_pattern)
    escaped = _PARAM_PATTERN.sub(r"(?P<\1>[^/]+)", escaped)
    escaped = escaped.replace(r"\*\*", ".*")
    escaped = escaped.replace(r"\*", "[^/]*")
    return _anchor(escaped)


def _anchor(pattern: str) -> str:
    if not pattern.startswith("^"):
        pattern = "^" + pattern
    if not pattern.endswith("$"):
        pattern = pattern + "$"
    return pattern
