from pathlib import Path

_REPO_ROOT = Path(__file__).parents[1]
_SENTINEL_ENV_LINES = (
    "name: REDIS_SENTINEL_HOSTS",
    "value: redis.cerbos-redis.svc.cluster.local:26379",
    "name: REDIS_SENTINEL_SERVICE_NAME",
    "value: mymaster",
)


def _assert_sentinel_environment(manifest: str) -> None:
    for line in _SENTINEL_ENV_LINES:
        assert line in manifest


def test_staging_mapping_api_uses_sentinel_for_redis_failover() -> None:
    manifest = (_REPO_ROOT / "k8s/staging/helmrelease.yaml").read_text()

    _assert_sentinel_environment(manifest)


def test_staging_projection_rebuild_uses_sentinel_for_redis_failover() -> None:
    manifest = (_REPO_ROOT / "k8s/staging/mapping-projection-rebuild-job.yaml").read_text()

    _assert_sentinel_environment(manifest)
