"""Rebuild the Redis mapping projection from PostgreSQL.

Entrypoint for the staging-only rebuild Job
(`k8s/staging/mapping-projection-rebuild-job.yaml`), run once Alembic
migrations and DB seeding have completed. PostgreSQL stays the source of
truth; this only refreshes the read-optimized Redis copy that
`resolve_mapping_fast()` and the AuthZ Go reader consume. Safe to re-run:
each run is a full atomic snapshot-and-swap
(see `app.services.redis_mapping_rebuild`).

Usage: `python -m app.rebuild_mapping_projection`
"""

import sys

from dotenv import load_dotenv

load_dotenv()

from app.database import get_db_session  # noqa: E402
from app.logging_config import (  # noqa: E402
    get_structured_logger,
    setup_structured_logging,
)
from app.services.mapping import MappingService  # noqa: E402
from app.settings import validate_environment  # noqa: E402

setup_structured_logging()
logger = get_structured_logger(__name__)


def main() -> int:
    """Validate config, rebuild the projection, and return a process exit code."""
    try:
        validate_environment()
    except Exception as e:
        logger.log_operation(
            level=50,  # ERROR
            message=f"Environment configuration validation failed: {e}",
            operation="rebuild_mapping_projection_config_invalid",
        )
        return 1

    mapping_service = MappingService()
    with get_db_session() as db:
        success = mapping_service.sync_all_mappings_to_redis(db)

    if not success:
        logger.log_operation(
            level=50,  # ERROR
            message="Mapping projection rebuild failed",
            operation="rebuild_mapping_projection_failed",
        )
        return 1

    logger.log_operation(
        level=20,  # INFO
        message="Mapping projection rebuild completed successfully",
        operation="rebuild_mapping_projection_success",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
