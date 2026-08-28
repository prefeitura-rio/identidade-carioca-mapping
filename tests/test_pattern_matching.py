import re

import pytest

from app.services.pattern_matching import path_pattern_to_regex


def test_path_pattern_to_regex_anchors_a_literal_pattern() -> None:
    regex = path_pattern_to_regex("/api/v1/health")

    assert regex == "^/api/v1/health$"
    assert re.match(regex, "/api/v1/health")
    assert not re.match(regex, "/api/v1/healthz")
    assert not re.match(regex, "/api/v1/health/extra")


def test_path_pattern_to_regex_converts_named_param_to_named_group() -> None:
    regex = path_pattern_to_regex("/api/v1/users/:user_id")

    match = re.match(regex, "/api/v1/users/123")

    assert match is not None
    assert match.group("user_id") == "123"


def test_path_pattern_to_regex_named_param_does_not_cross_a_path_segment() -> None:
    regex = path_pattern_to_regex("/api/v1/users/:user_id")

    assert re.match(regex, "/api/v1/users/123/profile") is None


def test_path_pattern_to_regex_single_star_matches_one_segment_only() -> None:
    regex = path_pattern_to_regex("/api/v1/*/items")

    assert re.match(regex, "/api/v1/groups/items")
    assert re.match(regex, "/api/v1/groups/subgroups/items") is None


def test_path_pattern_to_regex_double_star_matches_multiple_segments() -> None:
    regex = path_pattern_to_regex("/api/v1/**/items")

    assert re.match(regex, "/api/v1/groups/items")
    assert re.match(regex, "/api/v1/groups/subgroups/items")


def test_path_pattern_to_regex_preserves_raw_regex_with_capture_groups() -> None:
    regex = path_pattern_to_regex("/heimdall-admin/api/v1/users/(.*)")

    assert regex == "^/heimdall-admin/api/v1/users/(.*)$"
    assert re.match(regex, "/heimdall-admin/api/v1/users/123")


def test_path_pattern_to_regex_raw_regex_pattern_is_not_double_anchored() -> None:
    regex = path_pattern_to_regex("^/api/v1/users/(\\d+)$")

    assert regex == "^/api/v1/users/(\\d+)$"


def test_path_pattern_to_regex_output_is_always_anchored() -> None:
    for pattern in ("/x", "/x/:id", "/x/*", "/x/**", "/x/(y)"):
        regex = path_pattern_to_regex(pattern)
        assert regex.startswith("^")
        assert regex.endswith("$")


def test_path_pattern_to_regex_special_characters_are_escaped() -> None:
    regex = path_pattern_to_regex("/api/v1/users.export")

    assert re.match(regex, "/api/v1/users.export")
    assert re.match(regex, "/api/v1/usersXexport") is None


def test_path_pattern_to_regex_is_valid_for_python_re_compile() -> None:
    for pattern in ("/x/:id", "/x/*", "/x/**", "/x/(y)", "/plain"):
        re.compile(path_pattern_to_regex(pattern))


@pytest.mark.parametrize(
    ("path_pattern", "path", "should_match"),
    [
        ("/api/v1/users/:user_id", "/api/v1/users/abc-123", True),
        ("/api/v1/users/:user_id", "/api/v1/users/", False),
        ("/api/v1/groups/:group_name/members", "/api/v1/groups/eng/members", True),
        ("/api/v1/groups/:group_name/members", "/api/v1/groups/eng/members/1", False),
    ],
)
def test_path_pattern_to_regex_end_to_end_matching(
    path_pattern: str, path: str, should_match: bool
) -> None:
    regex = path_pattern_to_regex(path_pattern)

    assert bool(re.match(regex, path)) is should_match
