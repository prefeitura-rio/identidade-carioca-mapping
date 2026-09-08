from app import import_declarative_catalog
from app.catalog.declarative import CatalogImportResult, DeclarativeCatalog


class _SessionContext:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None


class _MappingService:
    def sync_all_mappings_to_redis(self, _db: object) -> bool:
        return True


def test_main_returns_success_when_declarative_import_succeeds(monkeypatch) -> None:
    sample_yaml = """
catalog: sgrc
version: "1"
environment: staging
source_revision: rev123
actions:
  - id: sgrc.v1.tickets.read
    kind: http
    owner: sgrc-team
    bindings:
      - method: GET
        path_pattern: /api/v1/tickets/:id
"""
    monkeypatch.setenv("DECLARATIVE_CATALOG_CONTENT", sample_yaml)
    monkeypatch.setattr(
        import_declarative_catalog,
        "parse_declarative_catalog",
        lambda *_args, **_kwargs: DeclarativeCatalog(
            catalog_name="sgrc",
            version="1",
            environment="staging",
            source_revision="rev123",
            actions=(),
        ),
    )
    monkeypatch.setattr(
        import_declarative_catalog, "get_db_session", lambda: _SessionContext()
    )
    monkeypatch.setattr(
        import_declarative_catalog,
        "import_declarative_catalog",
        lambda _db, _catalog: CatalogImportResult(0, 0),
    )
    monkeypatch.setattr(import_declarative_catalog, "MappingService", _MappingService)

    assert import_declarative_catalog.main() == 0
