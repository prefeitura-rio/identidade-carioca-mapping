from __future__ import annotations

import os
import sys
from pathlib import Path

from app.catalog.declarative import (
    import_declarative_catalog,
    parse_declarative_catalog,
)
from app.database import get_db_session
from app.logging_config import get_structured_logger, setup_structured_logging
from app.services.mapping import MappingService

setup_structured_logging()
logger = get_structured_logger(__name__)


def _load_catalog_text() -> str:
    content = os.getenv("DECLARATIVE_CATALOG_CONTENT")
    if content and content.strip():
        return content

    file_path = os.getenv("DECLARATIVE_CATALOG_FILE_PATH")
    if file_path and file_path.strip():
        return Path(file_path).read_text(encoding="utf-8")

    raise ValueError("Missing DECLARATIVE_CATALOG_CONTENT or DECLARATIVE_CATALOG_FILE_PATH environment variable")


def main() -> int:
    try:
        raw_text = _load_catalog_text()
        catalog = parse_declarative_catalog(raw_text)

        with get_db_session() as db:
            result = import_declarative_catalog(db, catalog)
            projection_rebuilt = MappingService().sync_all_mappings_to_redis(db)

        if not projection_rebuilt:
            raise RuntimeError("Declarative catalog imported but Redis projection rebuild failed")

        logger.log_operation(
            level=20,
            message="Declarative catalog import completed successfully",
            operation="declarative_catalog_import_success",
            extra_fields={
                "catalog": catalog.catalog_name,
                "version": catalog.version,
                "environment": catalog.environment,
                "source_revision": catalog.source_revision,
                "entry_count": len(catalog.actions),
                "created_actions": result.created_actions,
                "created_bindings": result.created_bindings,
            },
        )
        return 0
    except Exception as error:
        logger.log_operation(
            level=50,
            message=f"Declarative catalog import failed: {error}",
            operation="declarative_catalog_import_failed",
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
