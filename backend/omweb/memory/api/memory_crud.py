# backend/omweb/memory/api/memory_crud.py
"""
PELDRUN Memory Platform - Memory CRUD & Search HTTP Router.

The primary read/write API surface for memory entries (P1.8 tasks
1.8.1 through 1.8.7):

    GET    /api/memory                    list with pagination
    POST   /api/memory                    create
    POST   /api/memory/search             search (preview)
    GET    /api/memory/{memory_id}        get single
    PATCH  /api/memory/{memory_id}        update
    DELETE /api/memory/{memory_id}        soft/hard delete
    POST   /api/memory/{memory_id}/promote  scope promotion
    POST   /api/memory/{memory_id}/pin      pin toggle

Scope enforcement: every specific-memory endpoint requires
EXPLICIT scope (scope_type + scope_id). List, search, and create
accept both explicit and contextual (chat_id/project_id) inputs.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status as http_status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from peldrun.memory.domain import (
    MemoryEntry,
    MemoryNotFoundError,
    MemoryProvenance,
    MemoryScope,
    MemoryScopeRef,
    MemorySettings,
    MemorySourceType,
    MemoryType,
    MemoryUnavailableError,
    MemoryValidationError,
    resolve_scope,
    resolve_scope_refs,
)
from peldrun.memory.services import (
    build_memory_context,
    format_memory_context,
    route_for_tool_invocation,
)

from omweb.memory import get_memory_service


logger = logging.getLogger("omweb.memory.api.crud")
router = APIRouter()


_VALID_STATUS_FILTERS: frozenset = frozenset({"active", "all"})

_ALLOWED_CREATE_SOURCE_TYPES: frozenset = frozenset(
    {
        MemorySourceType.EXPLICIT_USER.value,
        MemorySourceType.AGENT_TOOL.value,
        MemorySourceType.EXTRACTION.value,
    }
)


class _CreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(..., min_length=1)
    memory_type: str = Field(default=MemoryType.SEMANTIC.value)
    source_type: str = Field(
        default=MemorySourceType.EXPLICIT_USER.value,
    )
    scope_type: Optional[str] = None
    scope_id: Optional[str] = None
    chat_id: Optional[str] = None
    project_id: Optional[str] = None
    importance: int = Field(default=50, ge=0, le=100)
    tags: List[str] = Field(default_factory=list)
    is_pinned: bool = False


class _UpdateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: Optional[str] = Field(default=None, min_length=1)
    importance: Optional[int] = Field(default=None, ge=0, le=100)
    tags: Optional[List[str]] = None
    is_pinned: Optional[bool] = None
    memory_type: Optional[str] = None

    def to_update_dict(self) -> Dict[str, Any]:
        return self.model_dump(exclude_unset=True, exclude_none=True)


class _PromoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_scope_type: str = Field(default=MemoryScope.USER.value)
    target_scope_id: Optional[str] = None


class _PinBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pinned: bool = True
    importance: Optional[int] = Field(default=None, ge=0, le=100)


class _SearchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(..., min_length=0)
    scope_type: Optional[str] = None
    scope_id: Optional[str] = None
    chat_id: Optional[str] = None
    project_id: Optional[str] = None
    user_id: Optional[str] = None
    limit: int = Field(default=10, ge=1, le=50)


def _error_response(exc: BaseException, *, operation: str) -> JSONResponse:
    if isinstance(exc, MemoryValidationError):
        return JSONResponse(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            content={
                "status": "error", "operation": operation,
                "error": type(exc).__name__, "message": exc.message,
                "context": dict(exc.context),
            },
        )
    if isinstance(exc, MemoryNotFoundError):
        return JSONResponse(
            status_code=http_status.HTTP_404_NOT_FOUND,
            content={
                "status": "error", "operation": operation,
                "error": "MemoryNotFoundError", "message": exc.message,
                "context": dict(exc.context),
            },
        )
    if isinstance(exc, MemoryUnavailableError):
        logger.error("Memory CRUD %s backend failure: %s", operation, exc)
        return JSONResponse(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "error", "operation": operation,
                "error": "MemoryUnavailableError",
                "message": "Memory backend unavailable. Try again shortly.",
            },
        )
    logger.exception("Memory CRUD %s failed: %s", operation, exc)
    return JSONResponse(
        status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "status": "error", "operation": operation,
            "error": type(exc).__name__,
            "message": "Internal error during memory operation.",
        },
    )


def _get_service_or_500():
    try:
        return get_memory_service()
    except RuntimeError as exc:
        logger.error("MemoryService not initialized: %s", exc)
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "service_not_initialized",
                "message": (
                    "Memory service is not initialized. "
                    "Startup ordering bug."
                ),
            },
        ) from exc


def _build_explicit_scope_ref(
    scope_type: str, scope_id: str
) -> MemoryScopeRef:
    if not isinstance(scope_type, str) or not scope_type.strip():
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "missing_scope_type",
                "message": (
                    "scope_type is required. Expected one of: "
                    f"{sorted(s.value for s in MemoryScope)}"
                ),
            },
        )
    if not isinstance(scope_id, str) or not scope_id.strip():
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "missing_scope_id",
                "message": "scope_id is required (non-empty string).",
            },
        )
    try:
        resolved = MemoryScope(scope_type.strip().lower())
    except ValueError:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_scope_type",
                "message": (
                    f"unknown scope_type {scope_type!r}; expected one of "
                    f"{sorted(s.value for s in MemoryScope)}"
                ),
            },
        ) from None
    try:
        return MemoryScopeRef(
            scope_type=resolved, scope_id=scope_id.strip()
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_scope_ref", "message": str(exc),
            },
        ) from exc


def _resolve_single_scope_for_write(
    *,
    scope_type: Optional[str],
    scope_id: Optional[str],
    chat_id: Optional[str],
    project_id: Optional[str],
) -> MemoryScopeRef:
    if scope_type is not None or scope_id is not None:
        if scope_type is None or scope_id is None:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "incomplete_scope",
                    "message": "scope_type and scope_id must be provided together.",
                },
            )
        return _build_explicit_scope_ref(scope_type, scope_id)
    try:
        return resolve_scope(chat_id=chat_id, project_id=project_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "cannot_resolve_scope",
                "message": (
                    "either (scope_type, scope_id) or "
                    f"(chat_id / project_id) required: {exc}"
                ),
            },
        ) from exc


def _resolve_scope_refs_for_read(
    *,
    scope_type: Optional[str],
    scope_id: Optional[str],
    chat_id: Optional[str],
    project_id: Optional[str],
    user_id: Optional[str],
) -> List[str]:
    if scope_type is not None or scope_id is not None:
        if scope_type is None or scope_id is None:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "incomplete_scope",
                    "message": "scope_type and scope_id must be provided together.",
                },
            )
        return [str(_build_explicit_scope_ref(scope_type, scope_id))]
    return resolve_scope_refs(
        chat_id=chat_id, project_id=project_id, user_id=user_id,
    )


def _entry_to_dict(entry: MemoryEntry) -> Dict[str, Any]:
    payload = entry.model_dump(mode="json")
    payload["scope"] = f"{entry.scope_type.value}:{entry.scope_id}"
    return payload


# ---------------------------------------------------------------------------
# LIST
# ---------------------------------------------------------------------------


@router.get("", summary="List memory entries", response_model=None)
async def list_endpoint(
    scope_type: Optional[str] = Query(None),
    scope_id: Optional[str] = Query(None),
    chat_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: str = Query("all"),
    memory_type: Optional[str] = Query(None),
    is_pinned: Optional[bool] = Query(None),
) -> JSONResponse:
    normalized_status = status.strip().lower()
    if normalized_status not in _VALID_STATUS_FILTERS:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_status",
                "message": (
                    f"status {status!r} not supported; "
                    f"expected {sorted(_VALID_STATUS_FILTERS)}"
                ),
            },
        )
    scope_refs = _resolve_scope_refs_for_read(
        scope_type=scope_type, scope_id=scope_id,
        chat_id=chat_id, project_id=project_id, user_id=None,
    )
    if len(scope_refs) > 1:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "multiple_scopes_ambiguous",
                "message": (
                    "list requires a single explicit scope "
                    f"(scope_type + scope_id). Got: {scope_refs}"
                ),
            },
        )
    scope_ref = scope_refs[0]

    list_filters: Dict[str, Any] = {}
    if normalized_status == "active":
        list_filters["status"] = "active"
    if memory_type:
        try:
            list_filters["memory_type"] = MemoryType(
                memory_type.strip().lower()
            )
        except ValueError:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "invalid_memory_type",
                    "message": (
                        f"unknown memory_type {memory_type!r}; "
                        f"expected {sorted(t.value for t in MemoryType)}"
                    ),
                },
            ) from None
    if is_pinned is not None:
        list_filters["is_pinned"] = is_pinned

    try:
        service = _get_service_or_500()
        entries = await service.provider.list_by_scope(
            scope_ref=scope_ref, limit=limit,
            offset=offset, **list_filters,
        )
    except HTTPException:
        raise
    except Exception as exc:
        return _error_response(exc, operation="list")

    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok", "operation": "list",
            "scope": scope_ref, "limit": limit, "offset": offset,
            "status_filter": normalized_status,
            "count": len(entries),
            "memories": [_entry_to_dict(e) for e in entries],
        },
    )


# ---------------------------------------------------------------------------
# CREATE
# ---------------------------------------------------------------------------


@router.post("", summary="Create memory entry", response_model=None)
async def create_endpoint(body: _CreateBody) -> JSONResponse:
    normalized_source = body.source_type.strip().lower()
    if normalized_source not in _ALLOWED_CREATE_SOURCE_TYPES:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "forbidden_source_type",
                "message": (
                    f"source_type {body.source_type!r} not allowed. "
                    f"Permitted: {sorted(_ALLOWED_CREATE_SOURCE_TYPES)}. "
                    "For imports use POST /api/memory/import/apply."
                ),
            },
        )
    resolved_source = MemorySourceType(normalized_source)

    try:
        resolved_type = MemoryType(body.memory_type.strip().lower())
    except ValueError:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_memory_type",
                "message": (
                    f"unknown memory_type {body.memory_type!r}; "
                    f"expected {sorted(t.value for t in MemoryType)}"
                ),
            },
        ) from None

    scope_ref = _resolve_single_scope_for_write(
        scope_type=body.scope_type, scope_id=body.scope_id,
        chat_id=body.chat_id, project_id=body.project_id,
    )

    if (
        scope_ref.scope_type == MemoryScope.USER
        and resolved_source != MemorySourceType.EXPLICIT_USER
    ):
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "user_scope_requires_explicit",
                "message": (
                    f"user-scope entries require source_type="
                    f"'explicit_user', got {resolved_source.value!r}"
                ),
            },
        )

    try:
        candidate = MemoryEntry.create(
            content=body.content,
            memory_type=resolved_type,
            scope_type=scope_ref.scope_type,
            scope_id=scope_ref.scope_id,
            provenance=MemoryProvenance(
                source_type=resolved_source,
                source_chat_id=body.chat_id,
            ),
            importance=body.importance,
            tags=body.tags,
            is_pinned=body.is_pinned,
        )
    except (ValueError, TypeError) as exc:
        return _error_response(
            MemoryValidationError(
                f"Invalid memory entry: {exc}",
                field_name="content", cause=exc,
            ),
            operation="create",
        )

    try:
        service = _get_service_or_500()
        created = await service.provider.create(candidate)
    except HTTPException:
        raise
    except Exception as exc:
        return _error_response(exc, operation="create")

    return JSONResponse(
        status_code=http_status.HTTP_201_CREATED,
        content={
            "status": "ok", "operation": "create",
            "memory": _entry_to_dict(created),
        },
    )


# ---------------------------------------------------------------------------
# SEARCH (before /{memory_id})
# ---------------------------------------------------------------------------


@router.post("/search", summary="Search memories", response_model=None)
async def search_endpoint(body: _SearchBody) -> JSONResponse:
    scope_refs = _resolve_scope_refs_for_read(
        scope_type=body.scope_type, scope_id=body.scope_id,
        chat_id=body.chat_id, project_id=body.project_id,
        user_id=body.user_id,
    )
    if not scope_refs:
        return JSONResponse(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            content={
                "status": "error", "operation": "search",
                "error": "no_scopes",
                "message": "No scopes resolved for search.",
            },
        )

    route = route_for_tool_invocation(body.query)

    try:
        service = _get_service_or_500()
        effective_settings: MemorySettings = service.settings.model_copy(
            update={"default_limit": body.limit}
        )
        context = await build_memory_context(
            query=body.query,
            scope_refs=scope_refs,
            route=route,
            provider=service.provider,
            settings=effective_settings,
            # The HTTP search endpoint exists PRIMARILY for UI
            # previews ("what do I have related to this message?").
            # When FTS returns no keyword matches, showing recent
            # memories is far more useful than an empty list --
            # the caller wants to know what memories EXIST, not
            # just what shares keywords with the query.
            #
            # The agent-facing MemorySearchTool deliberately does
            # NOT set this flag -- strict keyword matching is the
            # right semantic for the agent's "find what I asked
            # for" contract.
            allow_recency_fallback=True,
        )
    except HTTPException:
        raise
    except Exception as exc:
        return _error_response(exc, operation="search")

    formatted = format_memory_context(context)
    results = [_entry_to_dict(entry) for entry in context.memories]

    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok", "operation": "search",
            "query": body.query, "scope_refs": list(scope_refs),
            "router_decision": context.router_decision,
            "ranker_version": context.ranker_version,
            "count": len(results),
            "used_tokens": context.used_tokens,
            "token_budget": context.token_budget,
            "dropped_for_budget": context.dropped_for_budget,
            "results": results,
            "formatted": formatted,
        },
    )


# ---------------------------------------------------------------------------
# GET single
# ---------------------------------------------------------------------------


@router.get("/{memory_id}", summary="Get memory by id", response_model=None)
async def get_endpoint(
    memory_id: str,
    scope_type: Optional[str] = Query(None),
    scope_id: Optional[str] = Query(None),
) -> JSONResponse:
    if scope_type is None or scope_id is None:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "scope_required",
                "message": (
                    "this endpoint requires explicit scope: "
                    "scope_type and scope_id are mandatory."
                ),
            },
        )
    scope_ref_str = str(_build_explicit_scope_ref(scope_type, scope_id))

    try:
        service = _get_service_or_500()
        entry = await service.provider.get(memory_id, scope_ref_str)
    except HTTPException:
        raise
    except Exception as exc:
        return _error_response(exc, operation="get")

    if entry is None:
        return JSONResponse(
            status_code=http_status.HTTP_404_NOT_FOUND,
            content={
                "status": "error", "operation": "get",
                "error": "not_found",
                "message": (
                    f"memory {memory_id!r} not found in scope "
                    f"{scope_ref_str!r}"
                ),
            },
        )
    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok", "operation": "get",
            "memory": _entry_to_dict(entry),
        },
    )


# ---------------------------------------------------------------------------
# UPDATE
# ---------------------------------------------------------------------------


@router.patch("/{memory_id}", summary="Update memory", response_model=None)
async def update_endpoint(
    memory_id: str,
    body: _UpdateBody,
    scope_type: Optional[str] = Query(None),
    scope_id: Optional[str] = Query(None),
) -> JSONResponse:
    if scope_type is None or scope_id is None:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "scope_required",
                "message": (
                    "this endpoint requires explicit scope: "
                    "scope_type and scope_id are mandatory."
                ),
            },
        )
    scope_ref_str = str(_build_explicit_scope_ref(scope_type, scope_id))

    update_dict = body.to_update_dict()
    if not update_dict:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "empty_update",
                "message": "PATCH body must include at least one field.",
            },
        )

    try:
        service = _get_service_or_500()
        existing = await service.provider.get(memory_id, scope_ref_str)
        if existing is None:
            return JSONResponse(
                status_code=http_status.HTTP_404_NOT_FOUND,
                content={
                    "status": "error", "operation": "update",
                    "error": "not_found",
                    "message": (
                        f"memory {memory_id!r} not found in scope "
                        f"{scope_ref_str!r}"
                    ),
                },
            )

        change_fields: Dict[str, Any] = {}
        if "content" in update_dict:
            change_fields["content"] = update_dict["content"]
        if "importance" in update_dict:
            change_fields["importance"] = int(update_dict["importance"])
        if "tags" in update_dict:
            change_fields["tags"] = list(update_dict["tags"])
        if "is_pinned" in update_dict:
            change_fields["is_pinned"] = bool(update_dict["is_pinned"])
        if "memory_type" in update_dict:
            try:
                change_fields["memory_type"] = MemoryType(
                    str(update_dict["memory_type"]).strip().lower()
                )
            except ValueError:
                raise HTTPException(
                    status_code=http_status.HTTP_400_BAD_REQUEST,
                    detail={
                        "error": "invalid_memory_type",
                        "message": (
                            "unknown memory_type "
                            f"{update_dict['memory_type']!r}"
                        ),
                    },
                ) from None

        updated = existing.model_copy(update=change_fields)
        persisted = await service.provider.update(updated)
    except HTTPException:
        raise
    except Exception as exc:
        return _error_response(exc, operation="update")

    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok", "operation": "update",
            "applied_fields": sorted(update_dict.keys()),
            "memory": _entry_to_dict(persisted),
        },
    )


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------


@router.delete("/{memory_id}", summary="Delete memory", response_model=None)
async def delete_endpoint(
    memory_id: str,
    scope_type: Optional[str] = Query(None),
    scope_id: Optional[str] = Query(None),
    hard: bool = Query(False),
) -> JSONResponse:
    if scope_type is None or scope_id is None:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "scope_required",
                "message": (
                    "this endpoint requires explicit scope: "
                    "scope_type and scope_id are mandatory."
                ),
            },
        )
    scope_ref_str = str(_build_explicit_scope_ref(scope_type, scope_id))

    try:
        service = _get_service_or_500()
        if hard:
            deleted = await service.provider.purge(
                memory_id, scope_ref_str
            )
        else:
            deleted = await service.provider.delete(
                memory_id, scope_ref_str
            )
    except HTTPException:
        raise
    except Exception as exc:
        return _error_response(exc, operation="delete")

    if not deleted:
        return JSONResponse(
            status_code=http_status.HTTP_404_NOT_FOUND,
            content={
                "status": "error", "operation": "delete",
                "error": "not_found",
                "message": (
                    f"memory {memory_id!r} not found in scope "
                    f"{scope_ref_str!r} (or already in target state)"
                ),
                "hard": hard,
            },
        )
    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok", "operation": "delete",
            "memory_id": memory_id, "scope": scope_ref_str, "hard": hard,
        },
    )


# ---------------------------------------------------------------------------
# PROMOTE
# ---------------------------------------------------------------------------


@router.post(
    "/{memory_id}/promote", summary="Promote memory", response_model=None,
)
async def promote_endpoint(
    memory_id: str,
    body: _PromoteBody,
    scope_type: Optional[str] = Query(None),
    scope_id: Optional[str] = Query(None),
) -> JSONResponse:
    if scope_type is None or scope_id is None:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "scope_required",
                "message": (
                    "promote requires SOURCE explicit scope: "
                    "scope_type and scope_id are mandatory."
                ),
            },
        )
    source_ref = _build_explicit_scope_ref(scope_type, scope_id)
    source_ref_str = str(source_ref)

    try:
        target_scope_type = MemoryScope(
            body.target_scope_type.strip().lower()
        )
    except ValueError:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_target_scope_type",
                "message": (
                    f"unknown target_scope_type "
                    f"{body.target_scope_type!r}"
                ),
            },
        ) from None

    if target_scope_type != MemoryScope.USER:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "unsupported_target_scope",
                "message": (
                    "promote currently supports only user scope. "
                    f"Got {target_scope_type.value!r}."
                ),
            },
        )

    target_scope_id = (
        body.target_scope_id.strip()
        if isinstance(body.target_scope_id, str) and body.target_scope_id.strip()
        else "default"
    )
    try:
        target_ref = MemoryScopeRef(
            scope_type=target_scope_type, scope_id=target_scope_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_target_scope_id", "message": str(exc),
            },
        ) from exc

    try:
        service = _get_service_or_500()
        source_entry = await service.provider.get(
            memory_id, source_ref_str
        )
        if source_entry is None:
            return JSONResponse(
                status_code=http_status.HTTP_404_NOT_FOUND,
                content={
                    "status": "error", "operation": "promote",
                    "error": "not_found",
                    "message": (
                        f"source memory {memory_id!r} not found in "
                        f"scope {source_ref_str!r}"
                    ),
                },
            )

        promoted_provenance = MemoryProvenance(
            source_type=MemorySourceType.EXPLICIT_USER,
            imported_from=f"promoted_from:{source_ref_str}",
        )
        promoted_candidate = MemoryEntry.create(
            content=source_entry.content,
            memory_type=source_entry.memory_type,
            scope_type=target_scope_type,
            scope_id=target_scope_id,
            provenance=promoted_provenance,
            importance=source_entry.importance,
            confidence=source_entry.confidence,
            tags=list(source_entry.tags),
            is_pinned=source_entry.is_pinned,
        )
        promoted = await service.provider.create(promoted_candidate)
    except HTTPException:
        raise
    except Exception as exc:
        return _error_response(exc, operation="promote")

    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok", "operation": "promote",
            "source_memory_id": memory_id,
            "source_scope": source_ref_str,
            "target_scope": str(target_ref),
            "promoted": _entry_to_dict(promoted),
        },
    )


# ---------------------------------------------------------------------------
# PIN
# ---------------------------------------------------------------------------


@router.post(
    "/{memory_id}/pin", summary="Pin/unpin memory", response_model=None,
)
async def pin_endpoint(
    memory_id: str,
    body: _PinBody,
    scope_type: Optional[str] = Query(None),
    scope_id: Optional[str] = Query(None),
) -> JSONResponse:
    if scope_type is None or scope_id is None:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "scope_required",
                "message": (
                    "this endpoint requires explicit scope: "
                    "scope_type and scope_id are mandatory."
                ),
            },
        )
    scope_ref_str = str(_build_explicit_scope_ref(scope_type, scope_id))

    try:
        service = _get_service_or_500()
        existing = await service.provider.get(memory_id, scope_ref_str)
        if existing is None:
            return JSONResponse(
                status_code=http_status.HTTP_404_NOT_FOUND,
                content={
                    "status": "error", "operation": "pin",
                    "error": "not_found",
                    "message": (
                        f"memory {memory_id!r} not found in scope "
                        f"{scope_ref_str!r}"
                    ),
                },
            )

        updates: Dict[str, Any] = {"is_pinned": bool(body.pinned)}
        if body.importance is not None:
            updates["importance"] = int(body.importance)
        updated = existing.model_copy(update=updates)
        persisted = await service.provider.update(updated)
    except HTTPException:
        raise
    except Exception as exc:
        return _error_response(exc, operation="pin")

    applied = ["is_pinned"]
    if body.importance is not None:
        applied.append("importance")

    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok", "operation": "pin",
            "memory_id": memory_id, "scope": scope_ref_str,
            "pinned": persisted.is_pinned,
            "importance": int(persisted.importance),
            "applied_fields": applied,
            "memory": _entry_to_dict(persisted),
        },
    )


__all__ = ["router"]
