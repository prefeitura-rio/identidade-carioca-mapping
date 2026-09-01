"""Generic declarative action-catalog adapter for non-OpenAPI systems.

Unlike :mod:`app.catalog.openapi` (which derives actions from an OpenAPI
document), this adapter consumes a small, source-system-agnostic YAML/JSON
shape for systems that expose commands, batch jobs, events, or UI-only
actions with no HTTP surface at all::

    catalog: reports
    version: "1"
    environment: staging
    source_revision: abc123
    actions:
      - id: reports.generate_batch
        kind: batch
        owner: reports-team
        description: Nightly batch report generation
        bindings: []
      - id: reports.export
        kind: http
        owner: reports-team
        bindings:
          - method: POST
            path_pattern: /reports/export
            description: Trigger a report export

Bindings are optional per action: action-only entries (``bindings: []``)
create an :class:`~app.models.Action` row with no
:class:`~app.models.Endpoint` row at all. This module never performs a live
network fetch; callers are responsible for obtaining the raw document text
or an already-parsed mapping.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models import Action, Endpoint

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

ActionKind = Literal["http", "command", "batch", "event", "ui"]

_HTTP_METHODS = frozenset({"DELETE", "GET", "PATCH", "POST", "PUT"})
_STABLE_ACTION_ID = re.compile(r"^[a-z][a-z0-9]*(?:[_.:-][a-z0-9]+)*$")


class CatalogCollisionError(ValueError):
    """A declarative binding collides with an existing endpoint bound elsewhere."""


class _BindingDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: str
    path_pattern: str
    description: str | None = None

    @field_validator("method")
    @classmethod
    def _validate_method(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in _HTTP_METHODS:
            raise ValueError(f"Unsupported binding method: {value!r}")
        return normalized

    @field_validator("path_pattern")
    @classmethod
    def _validate_path_pattern(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped.startswith("/"):
            raise ValueError(f"Binding path_pattern must start with '/': {value!r}")
        return stripped


class _ActionDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: ActionKind
    owner: str
    description: str | None = None
    bindings: list[_BindingDocument] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if not _STABLE_ACTION_ID.fullmatch(value):
            raise ValueError(f"Action id is not a stable identifier: {value!r}")
        return value

    @field_validator("owner")
    @classmethod
    def _validate_owner(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Action owner must not be empty")
        return stripped

    @model_validator(mode="after")
    def _validate_no_self_colliding_bindings(self) -> Self:
        seen: set[tuple[str, str]] = set()
        for binding in self.bindings:
            key = (binding.method, binding.path_pattern)
            if key in seen:
                raise ValueError(
                    f"Duplicate binding within action {self.id!r}: "
                    f"{binding.method} {binding.path_pattern}"
                )
            seen.add(key)
        return self


class _CatalogDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    catalog: str
    version: str
    environment: str
    source_revision: str
    actions: list[_ActionDocument]

    @field_validator("catalog", "version", "environment", "source_revision")
    @classmethod
    def _validate_non_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Catalog metadata fields must not be empty")
        return stripped

    @model_validator(mode="after")
    def _validate_unique_actions_and_bindings(self) -> Self:
        seen_ids: set[str] = set()
        for action in self.actions:
            if action.id in seen_ids:
                raise ValueError(f"Duplicate action id in catalog: {action.id!r}")
            seen_ids.add(action.id)

        owner_by_binding: dict[tuple[str, str], str] = {}
        for action in self.actions:
            for binding in action.bindings:
                key = (binding.method, binding.path_pattern)
                owner = owner_by_binding.get(key)
                if owner is not None and owner != action.id:
                    raise ValueError(
                        f"Colliding binding {binding.method} {binding.path_pattern}: "
                        f"claimed by both {owner!r} and {action.id!r}"
                    )
                owner_by_binding[key] = action.id
        return self


@dataclass(frozen=True, slots=True)
class CatalogBinding:
    method: str
    path_pattern: str
    description: str | None


@dataclass(frozen=True, slots=True)
class CatalogAction:
    action_id: str
    kind: ActionKind
    owner: str
    description: str
    bindings: tuple[CatalogBinding, ...]


@dataclass(frozen=True, slots=True)
class DeclarativeCatalog:
    catalog_name: str
    version: str
    environment: str
    source_revision: str
    actions: tuple[CatalogAction, ...]


@dataclass(frozen=True, slots=True)
class CatalogImportResult:
    created_actions: int
    created_bindings: int


def parse_declarative_catalog(document: object) -> DeclarativeCatalog:
    """Parse a generic action-catalog document (YAML or JSON text, or an
    already-decoded mapping) into a typed :class:`DeclarativeCatalog`.

    YAML is a superset of JSON, so a single ``yaml.safe_load`` call handles
    both text formats; a pre-parsed mapping is validated as-is.
    """
    raw = yaml.safe_load(document) if isinstance(document, str) else document
    parsed = _CatalogDocument.model_validate(raw)

    actions = tuple(
        CatalogAction(
            action_id=action.id,
            kind=action.kind,
            owner=action.owner,
            description=_build_description(parsed, action),
            bindings=tuple(
                CatalogBinding(
                    method=binding.method,
                    path_pattern=binding.path_pattern,
                    description=binding.description,
                )
                for binding in action.bindings
            ),
        )
        for action in parsed.actions
    )
    return DeclarativeCatalog(
        catalog_name=parsed.catalog,
        version=parsed.version,
        environment=parsed.environment,
        source_revision=parsed.source_revision,
        actions=actions,
    )


def import_declarative_catalog(
    db: Session, catalog: DeclarativeCatalog
) -> CatalogImportResult:
    """Idempotently import a parsed catalog into the `actions`/`endpoints`
    tables.

    Actions are matched by stable name; bindings are matched by
    ``(path_pattern, method)``. A binding that already exists in the
    database but is bound to a different action raises
    :class:`CatalogCollisionError` instead of silently reassigning it.
    """
    actions_by_id: dict[str, Action | None] = {}
    endpoints_by_binding: dict[tuple[str, str], Endpoint | None] = {}

    for action in catalog.actions:
        existing_action = (
            db.query(Action).filter(Action.name == action.action_id).one_or_none()
        )
        actions_by_id[action.action_id] = existing_action
        for binding in action.bindings:
            existing_binding = (
                db.query(Endpoint)
                .filter(
                    Endpoint.path_pattern == binding.path_pattern,
                    Endpoint.method == binding.method,
                )
                .one_or_none()
            )
            if existing_binding is not None and (
                existing_action is None
                or existing_binding.action_id != existing_action.id
            ):
                raise CatalogCollisionError(
                    f"Declarative binding collision for "
                    f"{binding.method} {binding.path_pattern}: "
                    f"existing action_id={existing_binding.action_id}"
                )
            endpoints_by_binding[(binding.path_pattern, binding.method)] = (
                existing_binding
            )

    created_actions = 0
    for action in catalog.actions:
        if actions_by_id[action.action_id] is not None:
            continue
        new_action = Action(name=action.action_id, description=action.description)
        db.add(new_action)
        actions_by_id[action.action_id] = new_action
        created_actions += 1

    db.flush()

    created_bindings = 0
    for action in catalog.actions:
        resolved_action = actions_by_id[action.action_id]
        if resolved_action is None:
            raise RuntimeError(f"Missing action for {action.action_id}")
        for binding in action.bindings:
            if endpoints_by_binding[(binding.path_pattern, binding.method)] is not None:
                continue
            db.add(
                Endpoint(
                    path_pattern=binding.path_pattern,
                    method=binding.method,
                    action_id=resolved_action.id,
                    description=binding.description or action.description,
                )
            )
            created_bindings += 1

    db.commit()
    return CatalogImportResult(created_actions, created_bindings)


def _build_description(catalog: _CatalogDocument, action: _ActionDocument) -> str:
    title = action.description or f"{action.kind} action"
    return (
        f"{catalog.catalog} {action.kind}; owner={action.owner}; "
        f"environment={catalog.environment}; source={catalog.source_revision}; {title}"
    )
