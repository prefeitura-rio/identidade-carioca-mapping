from __future__ import annotations

import os
import sys

from app.catalog.openapi import fetch_openapi_catalog, import_openapi_catalog
from app.database import get_db_session
from app.logging_config import get_structured_logger, setup_structured_logging
from app.services.mapping import MappingService

setup_structured_logging()
logger = get_structured_logger(__name__)


def _required_environment(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ValueError(f"Missing required catalog environment variable: {name}")
    return value


def main() -> int:
    try:
        source_url = _required_environment("OPENAPI_SOURCE_URL")
        source_revision = _required_environment("OPENAPI_SOURCE_REVISION")
        catalog_name = _required_environment("OPENAPI_CATALOG_NAME")
        api_version = _required_environment("OPENAPI_API_VERSION")
        environment = _required_environment("OPENAPI_ENVIRONMENT")
        server_url = _required_environment("OPENAPI_SERVER_URL")
        catalog = fetch_openapi_catalog(
            source_url,
            catalog_name=catalog_name,
            api_version=api_version,
            environment=environment,
            server_url=server_url,
            source_revision=source_revision,
        )
        with get_db_session() as db:
            result = import_openapi_catalog(db, catalog)
            projection_rebuilt = MappingService().sync_all_mappings_to_redis(db)
        if not projection_rebuilt:
            raise RuntimeError("OpenAPI catalog imported but Redis projection rebuild failed")
        logger.log_operation(
            level=20,
            message="OpenAPI catalog import completed successfully",
            operation="openapi_catalog_import_success",
            extra_fields={
                "environment": environment,
                "source_revision": source_revision,
                "base_url": catalog.base_url,
                "entry_count": len(catalog.entries),
                "created_actions": result.created_actions,
                "created_bindings": result.created_bindings,
            },
        )
        return 0
    except Exception as error:
        logger.log_operation(
            level=50,
            message=f"OpenAPI catalog import failed: {error}",
            operation="openapi_catalog_import_failed",
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
