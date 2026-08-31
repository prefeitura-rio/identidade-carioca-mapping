from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

import requests
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.models import Action, Endpoint

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

CatalogClassification = Literal[
    "business", "admin", "operational", "health", "metrics"
]
_HTTP_METHODS = frozenset({"delete", "get", "patch", "post", "put"})
_PATH_PARAMETER = re.compile(r"^\{(?P<name>[A-Za-z][A-Za-z0-9_]*)\}$")


class CatalogCollisionError(ValueError):
    pass


class _OpenApiInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: str


class _OpenApiServer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str


class _OpenApiOperation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    summary: str | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)


class _OpenApiDocument(BaseModel):
    model_config = ConfigDict(extra="ignore")

    info: _OpenApiInfo
    servers: list[_OpenApiServer]
    paths: dict[str, dict[str, object]]


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    action_name: str
    method: str
    path: str
    path_pattern: str
    classification: CatalogClassification
    description: str


@dataclass(frozen=True, slots=True)
class OpenApiCatalog:
    catalog_name: str
    api_version: str
    environment: str
    base_url: str
    source_revision: str
    entries: tuple[CatalogEntry, ...]


@dataclass(frozen=True, slots=True)
class CatalogImportResult:
    created_actions: int
    created_bindings: int


def parse_openapi_catalog(
    document: object,
    *,
    catalog_name: str,
    api_version: str,
    environment: str,
    server_url: str,
    source_revision: str,
) -> OpenApiCatalog:
    parsed = (
        _OpenApiDocument.model_validate_json(document)
        if isinstance(document, str)
        else _OpenApiDocument.model_validate(document)
    )
    base_url = _select_server(parsed.servers, server_url)
    operation_adapter = TypeAdapter(_OpenApiOperation)
    entries: list[CatalogEntry] = []

    for path in sorted(parsed.paths):
        path_item = parsed.paths[path]
        for method in sorted(_HTTP_METHODS & path_item.keys()):
            operation = operation_adapter.validate_python(path_item[method])
            classification = _classify(path, operation.tags)
            entries.append(
                CatalogEntry(
                    action_name=_action_name(catalog_name, api_version, method, path),
                    method=method.upper(),
                    path=path,
                    path_pattern=_resolver_path(path),
                    classification=classification,
                    description=_description(
                        catalog_name, operation, classification, source_revision
                    ),
                )
            )

    return OpenApiCatalog(
        catalog_name=catalog_name,
        api_version=api_version,
        environment=environment,
        base_url=base_url,
        source_revision=source_revision,
        entries=tuple(entries),
    )


def fetch_openapi_catalog(
    source_url: str,
    *,
    catalog_name: str,
    api_version: str,
    environment: str,
    server_url: str,
    source_revision: str,
) -> OpenApiCatalog:
    response = requests.get(source_url, timeout=(5, 30))
    response.raise_for_status()
    return parse_openapi_catalog(
        response.text,
        catalog_name=catalog_name,
        api_version=api_version,
        environment=environment,
        server_url=server_url,
        source_revision=source_revision,
    )


def import_openapi_catalog(db: Session, catalog: OpenApiCatalog) -> CatalogImportResult:
    actions_by_name: dict[str, Action | None] = {}
    endpoints_by_binding: dict[tuple[str, str], Endpoint | None] = {}

    for entry in catalog.entries:
        action = (
            db.query(Action).filter(Action.name == entry.action_name).one_or_none()
        )
        binding = (
            db.query(Endpoint)
            .filter(
                Endpoint.path_pattern == entry.path_pattern,
                Endpoint.method == entry.method,
            )
            .one_or_none()
        )
        if binding is not None and (action is None or binding.action_id != action.id):
            raise CatalogCollisionError(
                f"RMI binding collision for {entry.method} {entry.path}: "
                f"existing action_id={binding.action_id}"
            )
        actions_by_name[entry.action_name] = action
        endpoints_by_binding[(entry.path_pattern, entry.method)] = binding

    created_actions = 0
    for entry in catalog.entries:
        if actions_by_name[entry.action_name] is not None:
            continue
        action = Action(name=entry.action_name, description=entry.description)
        db.add(action)
        actions_by_name[entry.action_name] = action
        created_actions += 1

    db.flush()
    created_bindings = 0
    for entry in catalog.entries:
        if endpoints_by_binding[(entry.path_pattern, entry.method)] is not None:
            continue
        action = actions_by_name[entry.action_name]
        if action is None:
            raise RuntimeError(f"Missing action for {entry.action_name}")
        db.add(
            Endpoint(
                path_pattern=entry.path_pattern,
                method=entry.method,
                action_id=action.id,
                description=entry.description,
            )
        )
        created_bindings += 1

    db.commit()
    return CatalogImportResult(created_actions, created_bindings)


def _select_server(servers: list[_OpenApiServer], expected_url: str) -> str:
    matches = [
        server.url.rstrip("/")
        for server in servers
        if urlsplit(server.url).geturl().rstrip("/") == expected_url.rstrip("/")
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one OpenAPI server for {expected_url}, "
            f"found {len(matches)}"
        )
    return matches[0]


def _action_name(catalog_name: str, api_version: str, method: str, path: str) -> str:
    segments = [_normalize_segment(segment) for segment in path.split("/") if segment]
    path_name = ".".join(segments) or "root"
    return f"{catalog_name}.{api_version}.{method.lower()}.{path_name}"


def _normalize_segment(segment: str) -> str:
    parameter = _PATH_PARAMETER.fullmatch(segment)
    if parameter is not None:
        return f"by-{parameter.group('name').lower()}"
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", segment).strip("-")
    return normalized.lower() or "root"


def _resolver_path(path: str) -> str:
    return re.sub(r"\{([A-Za-z][A-Za-z0-9_]*)\}", r":\1", path)


def _classify(
    path: str, tags: list[str]
) -> CatalogClassification:
    normalized_path = path.lower().rstrip("/") or "/"
    normalized_tags = {tag.lower() for tag in tags}
    if normalized_path == "/metrics" or "metrics" in normalized_tags:
        return "metrics"
    if normalized_path == "/health" or "health" in normalized_tags:
        return "health"
    if normalized_path.startswith("/admin") or "admin" in normalized_tags:
        return "admin"
    if normalized_path.startswith("/operational") or "operational" in normalized_tags:
        return "operational"
    return "business"


def _description(
    catalog_name: str,
    operation: _OpenApiOperation,
    classification: CatalogClassification,
    source_revision: str,
) -> str:
    title = operation.summary or operation.description or "RMI operation"
    tags = ",".join(operation.tags) or "untagged"
    return f"{catalog_name} {classification}; source={source_revision}; tags={tags}; {title}"
