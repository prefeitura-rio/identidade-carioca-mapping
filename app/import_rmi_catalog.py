from __future__ import annotations

import os
import sys

from app.catalog.rmi import CatalogEnvironment, fetch_rmi_catalog, import_rmi_catalog
from app.database import get_db_session
from app.logging_config import get_structured_logger, setup_structured_logging
from app.services.mapping import MappingService

_DEFAULT_SOURCE_URL = (
    "https://raw.githubusercontent.com/prefeitura-rio/app-rmi/"
    "ce77e9f9d8bf68d8aa416b0c56be8620a7e7e07d/docs/openapi-v3.json"
)
_DEFAULT_SOURCE_REVISION = "ce77e9f9d8bf68d8aa416b0c56be8620a7e7e07d"

setup_structured_logging()
logger = get_structured_logger(__name__)


def _catalog_environment() -> CatalogEnvironment:
    environment = os.getenv("RMI_OPENAPI_ENVIRONMENT", "staging")
    if environment == "staging":
        return "staging"
    if environment == "production":
        return "production"
    raise ValueError(f"Unsupported RMI catalog environment: {environment}")


def main() -> int:
    try:
        environment = _catalog_environment()
        source_url = os.getenv("RMI_OPENAPI_URL", _DEFAULT_SOURCE_URL)
        source_revision = os.getenv(
            "RMI_OPENAPI_SOURCE_REVISION", _DEFAULT_SOURCE_REVISION
        )
        catalog = fetch_rmi_catalog(
            source_url,
            environment=environment,
            source_revision=source_revision,
        )
        with get_db_session() as db:
            result = import_rmi_catalog(db, catalog)
            projection_rebuilt = MappingService().sync_all_mappings_to_redis(db)
        if not projection_rebuilt:
            raise RuntimeError("RMI catalog imported but Redis projection rebuild failed")
        logger.log_operation(
            level=20,
            message="RMI catalog import completed successfully",
            operation="rmi_catalog_import_success",
            environment=environment,
            source_revision=source_revision,
            base_url=catalog.base_url,
            entry_count=len(catalog.entries),
            created_actions=result.created_actions,
            created_bindings=result.created_bindings,
        )
        return 0
    except Exception as error:
        logger.log_operation(
            level=50,
            message=f"RMI catalog import failed: {error}",
            operation="rmi_catalog_import_failed",
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
