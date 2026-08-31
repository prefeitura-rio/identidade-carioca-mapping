from app import import_openapi_catalog
from app.catalog.openapi import CatalogImportResult, OpenApiCatalog


class _SessionContext:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None


class _MappingService:
    def sync_all_mappings_to_redis(self, _db: object) -> bool:
        return True


def test_main_returns_success_when_openapi_import_and_projection_succeed(monkeypatch) -> None:
    monkeypatch.setenv("OPENAPI_SOURCE_URL", "https://example.test/openapi.json")
    monkeypatch.setenv("OPENAPI_SOURCE_REVISION", "revision")
    monkeypatch.setenv("OPENAPI_CATALOG_NAME", "example")
    monkeypatch.setenv("OPENAPI_API_VERSION", "v1")
    monkeypatch.setenv("OPENAPI_ENVIRONMENT", "staging")
    monkeypatch.setenv("OPENAPI_SERVER_URL", "https://example.test/v1")
    monkeypatch.setattr(
        import_openapi_catalog,
        "fetch_openapi_catalog",
        lambda *_args, **_kwargs: OpenApiCatalog(
            catalog_name="example",
            api_version="v1",
            environment="staging",
            base_url="https://example.test/v1",
            source_revision="revision",
            entries=(),
        ),
    )
    monkeypatch.setattr(
        import_openapi_catalog, "get_db_session", lambda: _SessionContext()
    )
    monkeypatch.setattr(
        import_openapi_catalog,
        "import_openapi_catalog",
        lambda _db, _catalog: CatalogImportResult(0, 0),
    )
    monkeypatch.setattr(import_openapi_catalog, "MappingService", _MappingService)

    assert import_openapi_catalog.main() == 0
