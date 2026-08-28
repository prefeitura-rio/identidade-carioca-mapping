"""
Mapping management service with OpenTelemetry tracing.
Implements endpoint-to-action mapping operations with regex pattern matching.
"""

import re
from typing import Any

from opentelemetry import trace
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import Action, Endpoint, User
from app.services.audit import AuditService
from app.services.base import BaseService
from app.services.cache import CacheService
from app.services.cerbos import CerbosService
from app.services.pattern_matching import path_pattern_to_regex
from app.services.redis_mapping import RedisMappingService


class MappingService(BaseService):
    """Service for mapping management operations."""

    def __init__(self):
        super().__init__("mapping")
        self.cerbos_service = CerbosService()
        self.cache_service = CacheService()
        self.redis_mapping_service = RedisMappingService()
        self.audit_service = AuditService()

    def resolve_mapping(
        self, db: Session, path: str, method: str
    ) -> dict[str, Any] | None:
        """
        Resolve path and method to action from the Redis mapping projection only.

        `db` is accepted (and unused here) purely for call-site/interface
        stability with the router and the rest of `MappingService`'s CRUD
        methods, which still need a session. PostgreSQL is never queried by
        this method: a Redis miss means "no mapping in the current
        projection" and returns None immediately. PostgreSQL access is
        confined to the dedicated rebuild path
        (`sync_all_mappings_to_redis`/`MappingProjectionRebuilder`) and to
        the CRUD endpoints - a resolver-side cache miss can therefore never
        mask a stale or partially-rebuilt projection behind a fallback
        query or a fresh write.
        """
        del db
        with self.trace_operation(
            "resolve_mapping",
            {
                "mapping.path": path,
                "mapping.method": method,
                "mapping.operation": "resolve",
            },
        ) as span:
            try:
                redis_result = self.redis_mapping_service.resolve_mapping_fast(
                    method, path
                )
                if not redis_result:
                    span.set_attribute("mapping.redis_hit", False)
                    span.set_attribute("mapping.no_match", True)
                    return None

                span.set_attribute("mapping.redis_hit", True)
                span.set_attribute(
                    "mapping.matched_action", redis_result.get("action_name")
                )
                span.set_attribute("mapping.mapping_id", redis_result.get("id"))

                # Convert Redis format to expected format for backward compatibility
                return {
                    "mapping_id": redis_result.get("id"),
                    "action": redis_result.get("action_name"),
                    "path_pattern": redis_result.get("path_pattern"),
                    "method": redis_result.get("method"),
                    "description": redis_result.get("description"),
                }

            except Exception as e:
                span.record_exception(e)
                span.set_attribute("mapping.error", str(e))
                span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))
                raise

    def create_mapping(
        self,
        db: Session,
        path_pattern: str,
        method: str,
        action_id: int,
        description: str | None,
        created_by: User,
    ) -> Endpoint:
        """
        Create a new endpoint mapping.
        Implements mapping creation with validation and permission checking.
        """
        with self.trace_operation(
            "create_mapping",
            {
                "mapping.path_pattern": path_pattern,
                "mapping.method": method,
                "mapping.action_id": action_id,
                "mapping.created_by": created_by.subject,
                "mapping.operation": "create",
            },
        ) as span:
            try:
                # Validate the regex pattern
                try:
                    pattern = path_pattern_to_regex(path_pattern)
                    re.compile(pattern)
                except re.error as e:
                    span.set_attribute("mapping.invalid_pattern", True)
                    raise ValueError(f"Invalid path pattern: {e}")

                # Validate that the action exists
                action = db.query(Action).filter(Action.id == action_id).first()
                if not action:
                    span.set_attribute("mapping.action_not_found", True)
                    raise ValueError(f"Action with ID {action_id} not found")

                span.set_attribute("mapping.action_name", action.name)

                # Check if mapping already exists
                existing = (
                    db.query(Endpoint)
                    .filter(
                        Endpoint.path_pattern == path_pattern, Endpoint.method == method
                    )
                    .first()
                )

                if existing:
                    span.set_attribute("mapping.already_exists", True)
                    raise ValueError(
                        f"Mapping for pattern '{path_pattern}' and method '{method}' already exists"
                    )

                # Create the endpoint mapping
                endpoint = Endpoint(
                    path_pattern=path_pattern,
                    method=method,
                    action_id=action.id,
                    description=description,
                    created_by=created_by.id,
                )

                db.add(endpoint)
                db.commit()
                db.refresh(endpoint)

                # Store in Redis mapping persistence
                mapping_data = {
                    "id": endpoint.id,
                    "method": endpoint.method,
                    "path_pattern": endpoint.path_pattern,
                    "action_id": endpoint.action_id,
                    "action_name": action.name,
                    "description": endpoint.description,
                    "created_at": endpoint.created_at.isoformat()
                    if endpoint.created_at
                    else None,
                    "updated_at": endpoint.updated_at.isoformat()
                    if endpoint.updated_at
                    else None,
                }
                redis_stored = self.redis_mapping_service.store_mapping(mapping_data)
                span.set_attribute("mapping.redis_stored", redis_stored)

                # Invalidate old mapping cache after creation
                self.cache_service.invalidate_mapping_cache()
                span.set_attribute("mapping.cache_invalidated", True)

                span.set_attribute("mapping.id", endpoint.id)
                span.set_attribute("mapping.created", True)

                # Log successful mapping creation
                self.audit_service.safe_log_operation(
                    db=db,
                    actor_subject=created_by.subject,
                    operation="create_mapping",
                    target_type="mapping",
                    target_id=f"mapping:{endpoint.id}",
                    request_payload={
                        "path_pattern": path_pattern,
                        "method": method,
                        "action_id": action_id,
                        "action_name": action.name,
                        "description": description,
                    },
                    result={
                        "mapping_id": endpoint.id,
                        "action_id": action.id,
                        "action_created": span.attributes.get(
                            "mapping.action_created", False
                        ),
                    },
                    success=True,
                    actor_user_id=created_by.id,
                )

                return endpoint

            except Exception as e:
                span.record_exception(e)
                span.set_attribute("mapping.error", str(e))
                span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))
                db.rollback()

                # Log failed mapping creation
                # Try to get action name if action lookup succeeded
                action_name = None
                try:
                    if "action" in locals():
                        action_name = action.name
                except Exception:
                    pass

                self.audit_service.safe_log_operation(
                    db=db,
                    actor_subject=created_by.subject,
                    operation="create_mapping",
                    target_type="mapping",
                    target_id=f"mapping:{method}:{path_pattern}",
                    request_payload={
                        "path_pattern": path_pattern,
                        "method": method,
                        "action_id": action_id,
                        "action_name": action_name,
                        "description": description,
                    },
                    result={"error": str(e)},
                    success=False,
                    actor_user_id=created_by.id,
                )
                raise

    def update_mapping(
        self,
        db: Session,
        mapping_id: int,
        path_pattern: str | None,
        method: str | None,
        action_id: int | None,
        description: str | None,
        updated_by: User,
    ) -> Endpoint:
        """
        Update an existing endpoint mapping.
        """
        with self.trace_operation(
            "update_mapping",
            {
                "mapping.id": mapping_id,
                "mapping.updated_by": updated_by.subject,
                "mapping.operation": "update",
            },
        ) as span:
            try:
                # Find the mapping
                endpoint = db.query(Endpoint).filter(Endpoint.id == mapping_id).first()
                if not endpoint:
                    span.set_attribute("mapping.not_found", True)
                    raise ValueError(f"Mapping with ID {mapping_id} not found")

                # Update fields if provided
                updated = False

                if path_pattern is not None:
                    # Validate the regex pattern
                    try:
                        pattern = path_pattern_to_regex(path_pattern)
                        re.compile(pattern)
                    except re.error as e:
                        span.set_attribute("mapping.invalid_pattern", True)
                        raise ValueError(f"Invalid path pattern: {e}")
                    endpoint.path_pattern = path_pattern
                    updated = True

                if method is not None:
                    endpoint.method = method
                    updated = True

                if action_id is not None:
                    # Validate that the action exists
                    action = db.query(Action).filter(Action.id == action_id).first()
                    if not action:
                        span.set_attribute("mapping.action_not_found", True)
                        raise ValueError(f"Action with ID {action_id} not found")
                    endpoint.action_id = action_id
                    span.set_attribute("mapping.new_action_name", action.name)
                    updated = True

                if description is not None:
                    endpoint.description = description
                    updated = True

                if updated:
                    endpoint.updated_at = db.execute(text("SELECT now()")).scalar()
                    db.commit()
                    db.refresh(endpoint)

                    # Update in Redis mapping persistence
                    mapping_data = {
                        "id": endpoint.id,
                        "method": endpoint.method,
                        "path_pattern": endpoint.path_pattern,
                        "action_id": endpoint.action_id,
                        "action_name": endpoint.action.name,
                        "description": endpoint.description,
                        "created_at": endpoint.created_at.isoformat()
                        if endpoint.created_at
                        else None,
                        "updated_at": endpoint.updated_at.isoformat()
                        if endpoint.updated_at
                        else None,
                    }
                    redis_stored = self.redis_mapping_service.store_mapping(
                        mapping_data
                    )
                    span.set_attribute("mapping.redis_updated", redis_stored)

                    # Invalidate old mapping cache after update
                    self.cache_service.invalidate_mapping_cache()
                    span.set_attribute("mapping.cache_invalidated", True)

                    # Log successful mapping update
                    self.audit_service.safe_log_operation(
                        db=db,
                        actor_subject=updated_by.subject,
                        operation="update_mapping",
                        target_type="mapping",
                        target_id=f"mapping:{mapping_id}",
                        request_payload={
                            "mapping_id": mapping_id,
                            "path_pattern": path_pattern,
                            "method": method,
                            "action_id": action_id,
                            "action_name": endpoint.action.name,
                            "description": description,
                        },
                        result={
                            "mapping_updated": True,
                            "fields_updated": {
                                "path_pattern": path_pattern is not None,
                                "method": method is not None,
                                "action_id": action_id is not None,
                                "description": description is not None,
                            },
                        },
                        success=True,
                        actor_user_id=updated_by.id,
                    )

                span.set_attribute("mapping.updated", updated)

                return endpoint

            except Exception as e:
                span.record_exception(e)
                span.set_attribute("mapping.error", str(e))
                span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))
                db.rollback()

                # Log failed mapping update
                # Try to get action name if action lookup succeeded
                action_name = None
                try:
                    if "action" in locals():
                        action_name = action.name
                except Exception:
                    pass

                self.audit_service.safe_log_operation(
                    db=db,
                    actor_subject=updated_by.subject,
                    operation="update_mapping",
                    target_type="mapping",
                    target_id=f"mapping:{mapping_id}",
                    request_payload={
                        "mapping_id": mapping_id,
                        "path_pattern": path_pattern,
                        "method": method,
                        "action_id": action_id,
                        "action_name": action_name,
                        "description": description,
                    },
                    result={"error": str(e)},
                    success=False,
                    actor_user_id=updated_by.id,
                )
                raise

    def delete_mapping(self, db: Session, mapping_id: int, deleted_by: User) -> bool:
        """
        Delete an endpoint mapping.
        """
        with self.trace_operation(
            "delete_mapping",
            {
                "mapping.id": mapping_id,
                "mapping.deleted_by": deleted_by.subject,
                "mapping.operation": "delete",
            },
        ) as span:
            try:
                # Find the mapping
                endpoint = db.query(Endpoint).filter(Endpoint.id == mapping_id).first()
                if not endpoint:
                    span.set_attribute("mapping.not_found", True)
                    return True  # Idempotent operation

                # Store method before deletion for Redis cleanup
                method = endpoint.method

                db.delete(endpoint)
                db.commit()

                # Remove from Redis mapping persistence
                redis_removed = self.redis_mapping_service.remove_mapping(
                    mapping_id, method
                )
                span.set_attribute("mapping.redis_removed", redis_removed)

                # Invalidate old mapping cache after deletion
                self.cache_service.invalidate_mapping_cache()
                span.set_attribute("mapping.cache_invalidated", True)

                # Log successful mapping deletion
                self.audit_service.safe_log_operation(
                    db=db,
                    actor_subject=deleted_by.subject,
                    operation="delete_mapping",
                    target_type="mapping",
                    target_id=f"mapping:{mapping_id}",
                    request_payload={"mapping_id": mapping_id},
                    result={
                        "mapping_deleted": True,
                        "path_pattern": endpoint.path_pattern,
                        "method": endpoint.method,
                    },
                    success=True,
                    actor_user_id=deleted_by.id,
                )

                span.set_attribute("mapping.deleted", True)
                return True

            except Exception as e:
                span.record_exception(e)
                span.set_attribute("mapping.error", str(e))
                span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))
                db.rollback()

                # Log failed mapping deletion
                self.audit_service.safe_log_operation(
                    db=db,
                    actor_subject=deleted_by.subject,
                    operation="delete_mapping",
                    target_type="mapping",
                    target_id=f"mapping:{mapping_id}",
                    request_payload={"mapping_id": mapping_id},
                    result={"error": str(e)},
                    success=False,
                    actor_user_id=deleted_by.id,
                )
                raise

    def list_mappings(
        self, db: Session, action_filter: str | None = None
    ) -> list[Endpoint]:
        """
        List all endpoint mappings with optional filtering.
        """
        with self.trace_operation(
            "list_mappings",
            {"mapping.operation": "list", "mapping.action_filter": action_filter},
        ) as span:
            try:
                query = db.query(Endpoint).join(Action)

                if action_filter:
                    query = query.filter(Action.name.ilike(f"%{action_filter}%"))

                mappings = query.order_by(Endpoint.path_pattern, Endpoint.method).all()

                span.set_attribute("mapping.count", len(mappings))

                return mappings

            except Exception as e:
                span.record_exception(e)
                span.set_attribute("mapping.error", str(e))
                span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))
                raise

    def sync_all_mappings_to_redis(self, db: Session) -> bool:
        """
        Sync all mappings from database to Redis.
        This method should be called during application startup or when Redis needs to be rebuilt.

        Args:
            db: Database session

        Returns:
            bool: True if successful, False otherwise
        """
        with self.trace_operation(
            "sync_all_mappings_to_redis",
            {"mapping.operation": "sync_all_to_redis"},
        ) as span:
            try:
                # Get all endpoints with their actions
                endpoints = db.query(Endpoint).join(Action).all()

                # Convert to mapping data format
                mappings_data = []
                for endpoint in endpoints:
                    mapping_data = {
                        "id": endpoint.id,
                        "method": endpoint.method,
                        "path_pattern": endpoint.path_pattern,
                        "action_id": endpoint.action_id,
                        "action_name": endpoint.action.name,
                        "description": endpoint.description,
                        "created_at": endpoint.created_at.isoformat()
                        if endpoint.created_at
                        else None,
                        "updated_at": endpoint.updated_at.isoformat()
                        if endpoint.updated_at
                        else None,
                    }
                    mappings_data.append(mapping_data)

                # Sync to Redis
                success = self.redis_mapping_service.sync_all_mappings_from_db(
                    mappings_data
                )

                span.set_attribute("mapping.total_mappings", len(mappings_data))
                span.set_attribute("mapping.sync_successful", success)

                return success

            except Exception as e:
                span.record_exception(e)
                span.set_attribute("mapping.error", str(e))
                span.set_attribute("mapping.sync_successful", False)
                return False
