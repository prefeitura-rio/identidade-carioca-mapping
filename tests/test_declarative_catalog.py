import pytest
from pydantic import ValidationError

from app.catalog.declarative import (
    CatalogCollisionError,
    DeclarativeCatalog,
    import_declarative_catalog,
    parse_declarative_catalog,
)
from app.models import Action, Endpoint


def _document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "catalog": "reports",
        "version": "1",
        "environment": "staging",
        "source_revision": "rev-abc123",
        "actions": [
            {
                "id": "reports.generate_batch",
                "kind": "batch",
                "owner": "reports-team",
                "description": "Nightly batch report generation",
                "bindings": [],
            },
            {
                "id": "reports.notify_completion",
                "kind": "event",
                "owner": "reports-team",
                "description": "Emitted when a report finishes",
            },
            {
                "id": "reports.run_export",
                "kind": "command",
                "owner": "reports-team",
                "description": "CLI-triggered export command",
            },
            {
                "id": "reports.export",
                "kind": "http",
                "owner": "reports-team",
                "description": "Trigger a report export",
                "bindings": [
                    {
                        "method": "post",
                        "path_pattern": "/reports/export",
                        "description": "Trigger export via HTTP",
                    }
                ],
            },
            {
                "id": "reports.dashboard",
                "kind": "ui",
                "owner": "reports-team",
                "description": "Reports dashboard screen",
            },
        ],
    }
    document.update(overrides)
    return document


def _catalog(**overrides: object) -> DeclarativeCatalog:
    return parse_declarative_catalog(_document(**overrides))


def test_parse_declarative_catalog_supports_all_kinds_and_optional_bindings() -> None:
    catalog = _catalog()

    assert catalog.catalog_name == "reports"
    assert catalog.version == "1"
    assert catalog.environment == "staging"
    assert catalog.source_revision == "rev-abc123"
    assert {action.kind for action in catalog.actions} == {
        "http",
        "command",
        "batch",
        "event",
        "ui",
    }

    batch_action = next(
        a for a in catalog.actions if a.action_id == "reports.generate_batch"
    )
    assert batch_action.bindings == ()

    event_action = next(
        a for a in catalog.actions if a.action_id == "reports.notify_completion"
    )
    assert event_action.bindings == ()

    http_action = next(a for a in catalog.actions if a.action_id == "reports.export")
    assert len(http_action.bindings) == 1
    assert http_action.bindings[0].method == "POST"
    assert http_action.bindings[0].path_pattern == "/reports/export"


def test_parse_declarative_catalog_accepts_yaml_text() -> None:
    yaml_text = """
    catalog: reports
    version: "1"
    environment: staging
    source_revision: rev-abc123
    actions:
      - id: reports.generate_batch
        kind: batch
        owner: reports-team
        description: Nightly batch report generation
        bindings: []
    """

    catalog = parse_declarative_catalog(yaml_text)

    assert catalog.catalog_name == "reports"
    assert len(catalog.actions) == 1
    assert catalog.actions[0].action_id == "reports.generate_batch"


@pytest.mark.parametrize(
    "field", ["environment", "source_revision", "catalog", "version"]
)
def test_parse_declarative_catalog_rejects_empty_metadata(field: str) -> None:
    with pytest.raises(ValidationError):
        parse_declarative_catalog(_document(**{field: "  "}))


def test_parse_declarative_catalog_rejects_unsupported_kind() -> None:
    document = _document()
    document["actions"] = [
        {
            "id": "reports.legacy_job",
            "kind": "cronjob",
            "owner": "reports-team",
        }
    ]

    with pytest.raises(ValidationError):
        parse_declarative_catalog(document)


def test_parse_declarative_catalog_rejects_empty_owner() -> None:
    document = _document()
    document["actions"] = [
        {
            "id": "reports.generate_batch",
            "kind": "batch",
            "owner": "   ",
        }
    ]

    with pytest.raises(ValidationError):
        parse_declarative_catalog(document)


@pytest.mark.parametrize(
    "unstable_id", ["Reports.Generate", "1reports.generate", "reports generate", ""]
)
def test_parse_declarative_catalog_rejects_unstable_action_id(unstable_id: str) -> None:
    document = _document()
    document["actions"] = [
        {"id": unstable_id, "kind": "batch", "owner": "reports-team"}
    ]

    with pytest.raises(ValidationError):
        parse_declarative_catalog(document)


def test_parse_declarative_catalog_rejects_duplicate_action_ids() -> None:
    document = _document()
    document["actions"] = [
        {"id": "reports.generate_batch", "kind": "batch", "owner": "reports-team"},
        {"id": "reports.generate_batch", "kind": "command", "owner": "reports-team"},
    ]

    with pytest.raises(ValidationError):
        parse_declarative_catalog(document)


def test_parse_declarative_catalog_rejects_duplicate_binding_within_action() -> None:
    document = _document()
    document["actions"] = [
        {
            "id": "reports.export",
            "kind": "http",
            "owner": "reports-team",
            "bindings": [
                {"method": "POST", "path_pattern": "/reports/export"},
                {"method": "post", "path_pattern": "/reports/export"},
            ],
        }
    ]

    with pytest.raises(ValidationError):
        parse_declarative_catalog(document)


def test_parse_declarative_catalog_rejects_colliding_bindings_across_actions() -> None:
    document = _document()
    document["actions"] = [
        {
            "id": "reports.export",
            "kind": "http",
            "owner": "reports-team",
            "bindings": [{"method": "POST", "path_pattern": "/reports/export"}],
        },
        {
            "id": "reports.export_v2",
            "kind": "http",
            "owner": "reports-team",
            "bindings": [{"method": "POST", "path_pattern": "/reports/export"}],
        },
    ]

    with pytest.raises(ValidationError):
        parse_declarative_catalog(document)


def test_import_declarative_catalog_creates_action_only_rows_without_endpoints(
    db_session,
) -> None:
    catalog = _catalog()

    result = import_declarative_catalog(db_session, catalog)

    assert result.created_actions == 5
    assert result.created_bindings == 1
    assert db_session.query(Action).count() == 5
    assert db_session.query(Endpoint).count() == 1

    batch_action = (
        db_session.query(Action).filter_by(name="reports.generate_batch").one()
    )
    assert db_session.query(Endpoint).filter_by(action_id=batch_action.id).count() == 0

    http_action = db_session.query(Action).filter_by(name="reports.export").one()
    endpoint = db_session.query(Endpoint).filter_by(action_id=http_action.id).one()
    assert endpoint.path_pattern == "/reports/export"
    assert endpoint.method == "POST"


def test_import_declarative_catalog_is_idempotent(db_session) -> None:
    catalog = _catalog()

    first = import_declarative_catalog(db_session, catalog)
    second = import_declarative_catalog(db_session, catalog)

    assert first.created_actions == 5
    assert first.created_bindings == 1
    assert second.created_actions == 0
    assert second.created_bindings == 0
    assert db_session.query(Action).count() == 5
    assert db_session.query(Endpoint).count() == 1


def test_import_declarative_catalog_preserves_existing_synthetic_and_rmi_rows(
    db_session,
) -> None:
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
    rmi_action = Action(name="rmi.v1.get.citizen.by-cpf", description="rmi import")
    db_session.add(rmi_action)
    db_session.flush()
    db_session.add(
        Endpoint(
            path_pattern="/citizen/:cpf",
            method="GET",
            action_id=rmi_action.id,
            description="rmi import",
        )
    )
    db_session.commit()

    catalog = _catalog()
    import_declarative_catalog(db_session, catalog)

    assert db_session.query(Action).filter_by(name="identity:health").count() == 1
    assert (
        db_session.query(Endpoint).filter_by(path_pattern="/api/v1/healthz").count()
        == 1
    )
    assert (
        db_session.query(Action).filter_by(name="rmi.v1.get.citizen.by-cpf").count()
        == 1
    )
    assert (
        db_session.query(Endpoint).filter_by(path_pattern="/citizen/:cpf").count() == 1
    )
    # 5 declarative actions + 2 pre-existing = 7; 1 declarative binding + 2 pre-existing = 3
    assert db_session.query(Action).count() == 7
    assert db_session.query(Endpoint).count() == 3


def test_import_declarative_catalog_rejects_binding_collision(db_session) -> None:
    existing_action = Action(name="different.action")
    db_session.add(existing_action)
    db_session.flush()
    db_session.add(
        Endpoint(
            path_pattern="/reports/export",
            method="POST",
            action_id=existing_action.id,
            description="unrelated binding",
        )
    )
    db_session.commit()
    catalog = _catalog()

    with pytest.raises(CatalogCollisionError):
        import_declarative_catalog(db_session, catalog)
