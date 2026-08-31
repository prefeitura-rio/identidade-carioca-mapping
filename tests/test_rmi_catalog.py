import pytest

from app.catalog.rmi import CatalogCollisionError, import_rmi_catalog, parse_rmi_catalog
from app.models import Action, Endpoint


def _document() -> dict[str, object]:
    return {
        "info": {"version": "1.0.0"},
        "servers": [
            {"url": "https://services.pref.rio/rmi/v1"},
            {"url": "https://services.staging.app.dados.rio/rmi/v1"},
        ],
        "paths": {
            "/citizen/{cpf}": {
                "get": {
                    "summary": "Read citizen",
                    "tags": ["citizen"],
                    "security": [{"bearerAuth": []}],
                }
            },
            "/admin/cache/read": {
                "post": {"tags": ["admin"], "security": [{"bearerAuth": []}]}
            },
            "/health": {
                "get": {"tags": ["health"], "security": [{"bearerAuth": []}]}
            },
        },
    }


def test_parse_rmi_catalog_derives_stable_actions_and_staging_server() -> None:
    catalog = parse_rmi_catalog(_document(), environment="staging", source_revision="rev")

    assert catalog.base_url == "https://services.staging.app.dados.rio/rmi/v1"
    assert catalog.source_revision == "rev"
    citizen = next(entry for entry in catalog.entries if entry.path == "/citizen/{cpf}")
    assert citizen.action_name == "rmi.v1.get.citizen.by-cpf"
    assert citizen.path_pattern == "/citizen/:cpf"
    assert citizen.classification == "business"

    admin = next(entry for entry in catalog.entries if entry.path == "/admin/cache/read")
    assert admin.classification == "admin"
    health = next(entry for entry in catalog.entries if entry.path == "/health")
    assert health.classification == "health"


def test_parse_rmi_catalog_selects_production_server_separately() -> None:
    catalog = parse_rmi_catalog(_document(), environment="production", source_revision="rev")

    assert catalog.base_url == "https://services.pref.rio/rmi/v1"


def test_import_rmi_catalog_is_idempotent_and_preserves_synthetic_mapping(db_session) -> None:
    synthetic_action = Action(name="identity:health", description="synthetic")
    db_session.add(synthetic_action)
    db_session.flush()
    db_session.add(
        Endpoint(
            path_pattern="/api/v1/healthz",
            method="GET",
            action_id=synthetic_action.id,
            description="synthetic",
        )
    )
    db_session.commit()
    catalog = parse_rmi_catalog(_document(), environment="staging", source_revision="rev")

    first = import_rmi_catalog(db_session, catalog)
    second = import_rmi_catalog(db_session, catalog)

    assert first.created_actions == 3
    assert first.created_bindings == 3
    assert second.created_actions == 0
    assert second.created_bindings == 0
    assert db_session.query(Action).count() == 4
    assert db_session.query(Endpoint).count() == 4
    assert db_session.query(Endpoint).filter_by(path_pattern="/api/v1/healthz").count() == 1


def test_import_rmi_catalog_rejects_binding_collision(db_session) -> None:
    existing_action = Action(name="different.action")
    db_session.add(existing_action)
    db_session.flush()
    db_session.add(
        Endpoint(
            path_pattern="/citizen/:cpf",
            method="GET",
            action_id=existing_action.id,
            description="unrelated binding",
        )
    )
    db_session.commit()
    catalog = parse_rmi_catalog(_document(), environment="staging", source_revision="rev")

    with pytest.raises(CatalogCollisionError):
        import_rmi_catalog(db_session, catalog)
