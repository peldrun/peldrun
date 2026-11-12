# backend/omweb/memory/api/memory_projects.py
"""
PELDRUN Memory Platform - Project Memory Aggregation Router.

Aggregate views and bulk operations over memories in project
scopes. Complements:

    * ``memory_crud.py``    -- per-entry operations,
    * ``memory_settings.py``-- per-project configuration (settings
                              overrides, NOT memory content).

This module answers "how much memory does each project have?"
and provides safe bulk operations for housekeeping:

    GET    /api/memory/projects
        List all projects that have memory, with per-project
        counts (total / active / archived / superseded) and the
        most recent update timestamp. Feeds the "Projects with
        memory" sidebar in the UI.

    GET    /api/memory/projects/{project_id}/stats
        Detailed statistics for one project: counts by status,
        memory_type distribution, importance range, top tags,
        and the oldest / newest entry timestamps. Feeds the
        "project detail" page.

    POST   /api/memory/projects/{project_id}/bulk-archive
        Archive every ACTIVE memory in the project in a single
        SQL operation. Idempotent (archived entries are skipped
        by the WHERE clause). Returns the number of rows
        transitioned to ``status=archived``.

    POST   /api/memory/projects/{project_id}/bulk-purge
        PERMANENTLY DELETE every memory in the project. Requires
        ``?confirm=yes`` as a low-friction guard against
        accidental curl invocations. Returns the number of rows
        purged. The FTS index is cleaned automatically by the
        delete trigger.

Mode enforcement
----------------

Read endpoints (``list``, ``stats``) require
``MemoryMode.can_read`` (READ_ONLY or READ_WRITE). Write
endpoints (``bulk-archive``, ``bulk-purge``) require
``MemoryMode.can_write`` (READ_WRITE only).

Rationale: bulk-archive modifies existing data, and the plan's
READ_ONLY contract explicitly says writes are rejected except
imports. A user who wants to clean up their memory should
switch to READ_WRITE, run the operation, and optionally switch
back. The mode switch is a single PATCH to
``/api/memory/settings``, so the friction is minimal.

Destructive operations -- extra guards
--------------------------------------

``bulk-purge`` is unrecoverable. Three layers of protection:

    1. Mode gate: requires READ_WRITE.
    2. Explicit confirmation: ``?confirm=yes`` (any case).
    3. Response includes the count purged so the caller can
       verify the intended project was affected.

The UI SHOULD show a two-step dialog ("Are you sure? Type the
project name to confirm") on top of these -- the query param
guards against accidental direct calls, not against a
determined user who has already clicked through a dialog.

Response envelope
-----------------

All responses use the memory-API's uniform envelope::

    {"status": "ok", "operation": "...", ...}

Errors follow the same pattern as ``memory_crud``. FastAPI's
default 422 (from body-validation failures) is normalized to
400 in ``omweb/main.py`` (P1.8 file #6).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, status as http_status
from fastapi.responses import JSONResponse

from peldrun.memory.domain import (
    MemoryMode,
    MemoryUnavailableError,
    MemoryValidationError,
)

from omweb.memory import get_memory_service


logger = logging.getLogger("omweb.memory.api.projects")
router = APIRouter()


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
    if isinstance(exc, MemoryUnavailableError):
        logger.error("Memory projects %s backend failure: %s", operation, exc)
        return JSONResponse(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "error", "operation": operation,
                "error": "MemoryUnavailableError",
                "message": "Memory backend unavailable. Try again shortly.",
            },
        )
    logger.exception("Memory projects %s failed: %s", operation, exc)
    return JSONResponse(
        status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "status": "error", "operation": operation,
            "error": type(exc).__name__,
            "message": "Internal error during project operation.",
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


def _enforce_read_mode(service) -> None:
    """Raise 403-like 400 when mode forbids reads."""
    if not service.settings.mode.can_read:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "mode_disabled",
                "message": (
                    "Memory reads are disabled "
                    f"(mode={service.settings.mode.value}). "
                    "Enable memory first."
                ),
            },
        )


def _enforce_write_mode(service) -> None:
    """Raise when mode forbids writes."""
    if not service.settings.mode.can_write:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "mode_read_only",
                "message": (
                    "Memory writes are disabled "
                    f"(mode={service.settings.mode.value}). "
                    "Set mode to 'read_write' to perform this "
                    "operation."
                ),
            },
        )


# ---------------------------------------------------------------------------
# LIST PROJECTS
# ---------------------------------------------------------------------------


@router.get(
    "", summary="List projects with memory", response_model=None,
)
async def list_projects_endpoint() -> JSONResponse:
    """Return a per-project summary of memories.

    Only projects with at least one memory appear. A project
    whose memories were all purged (or never existed) is absent
    -- the UI shows "no memories yet" rather than a zero row.
    """
    try:
        service = _get_service_or_500()
        _enforce_read_mode(service)
    except HTTPException:
        raise

    try:
        summaries = await service.provider.list_project_summaries()
    except Exception as exc:
        return _error_response(exc, operation="list_projects")

    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok", "operation": "list_projects",
            "count": len(summaries),
            "projects": summaries,
        },
    )


# ---------------------------------------------------------------------------
# PROJECT STATS
# ---------------------------------------------------------------------------


@router.get(
    "/{project_id}/stats",
    summary="Project-level memory statistics",
    response_model=None,
)
async def project_stats_endpoint(project_id: str) -> JSONResponse:
    """Return detailed statistics for one project's memory."""
    if not isinstance(project_id, str) or not project_id.strip():
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_project_id",
                "message": "project_id must be a non-empty string.",
            },
        )
    clean_pid = project_id.strip()

    try:
        service = _get_service_or_500()
        _enforce_read_mode(service)
    except HTTPException:
        raise

    try:
        stats = await service.provider.get_project_stats(clean_pid)
    except Exception as exc:
        return _error_response(exc, operation="project_stats")

    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok", "operation": "project_stats",
            "stats": stats,
        },
    )


# ---------------------------------------------------------------------------
# BULK ARCHIVE
# ---------------------------------------------------------------------------


@router.post(
    "/{project_id}/bulk-archive",
    summary="Archive all active memories in a project",
    response_model=None,
)
async def bulk_archive_endpoint(project_id: str) -> JSONResponse:
    """Transition every ACTIVE memory in the project to ``archived``.

    Idempotent: already-archived entries are skipped by the SQL
    WHERE clause. The response reports how many rows were
    transitioned on THIS call (0 if none were active).
    """
    if not isinstance(project_id, str) or not project_id.strip():
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_project_id",
                "message": "project_id must be a non-empty string.",
            },
        )
    clean_pid = project_id.strip()

    try:
        service = _get_service_or_500()
        _enforce_write_mode(service)
    except HTTPException:
        raise

    try:
        archived_count = await service.provider.bulk_archive(clean_pid)
    except Exception as exc:
        return _error_response(exc, operation="bulk_archive")

    logger.info(
        "Bulk archived %d memories in project %r.",
        archived_count, clean_pid,
    )
    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok", "operation": "bulk_archive",
            "project_id": clean_pid,
            "archived_count": int(archived_count),
        },
    )


# ---------------------------------------------------------------------------
# BULK PURGE (destructive)
# ---------------------------------------------------------------------------


@router.post(
    "/{project_id}/bulk-purge",
    summary="PERMANENTLY delete all memories in a project",
    response_model=None,
)
async def bulk_purge_endpoint(
    project_id: str,
    confirm: str = Query(
        ...,
        description=(
            "Must be 'yes' (case-insensitive) to proceed. "
            "Prevents accidental direct calls."
        ),
    ),
) -> JSONResponse:
    """PERMANENTLY delete every memory in the project.

    Unrecoverable. Requires ``?confirm=yes``. The FTS index is
    cleaned automatically. The response reports how many rows
    were purged.
    """
    if not isinstance(project_id, str) or not project_id.strip():
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_project_id",
                "message": "project_id must be a non-empty string.",
            },
        )
    clean_pid = project_id.strip()

    if (confirm or "").strip().lower() != "yes":
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "confirmation_required",
                "message": (
                    "bulk-purge is irreversible. Pass "
                    "'?confirm=yes' to proceed."
                ),
                "hint": "confirm=yes",
            },
        )

    try:
        service = _get_service_or_500()
        _enforce_write_mode(service)
    except HTTPException:
        raise

    try:
        purged_count = await service.provider.bulk_purge(clean_pid)
    except Exception as exc:
        return _error_response(exc, operation="bulk_purge")

    logger.warning(
        "Bulk PURGED %d memories in project %r (irreversible).",
        purged_count, clean_pid,
    )
    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok", "operation": "bulk_purge",
            "project_id": clean_pid,
            "purged_count": int(purged_count),
            "warning": (
                "This operation is irreversible. The memories "
                "have been permanently removed."
            ),
        },
    )


__all__ = ["router"]
