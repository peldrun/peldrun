# backend/omweb/memory/api/memory_settings.py
"""
PELDRUN Memory Platform - Memory Settings HTTP Router.

Read and modify the memory subsystem's configuration, both at
the global level and per-project. Every change is persisted to
disk (``memory_settings.json``) and applied to the running
``MemoryService`` immediately via hot-reload.

Responsibilities (deliberately NARROW)
--------------------------------------

    1. **Validate inputs** (partial dict on PATCH, per-project
       fields on PUT) with the SAME structured error envelope as
       the other memory routers.

    2. **Persist** via ``MemorySettingsManager.update()``, which
       handles atomic disk writes and re-validation.

    3. **Hot-reload** via ``MemoryService.reload_settings()``,
       which reads the new file and applies it to the lifecycle
       manager (rebuilding the provider only when necessary --
       see ``MemoryLifecycleManager.reload_settings`` for the
       surgical rules).

    4. **Shape responses** with the uniform
       ``{"status": "ok", "operation": ..., ...}`` envelope.

Mode policy -- NO mode check
----------------------------

Unlike the import/export routers, this router does NOT consult
``MemoryMode``. Rationale:

    * The MOST common settings operation is "re-enable memory"
      -- which requires the mode to be DISABLED at the time.
      Rejecting the request would create an unrecoverable
      trap: the operator disabled memory, then cannot
      re-enable it via the UI because the settings endpoint
      requires memory to be enabled.

    * Updating settings is not a MEMORY operation per se -- it
      is a CONFIGURATION operation. The memory subsystem is
      the TARGET of the update, not the MEDIUM for it.

    * Every other subsystem's settings endpoint (runtime,
      llm, etc.) follows the same rule: settings are editable
      regardless of what the settings enable.

``_settings_manager`` access
----------------------------

The update flow reaches the settings manager via
``service._settings_manager`` (private). Rationale:

    * ``MemoryService`` owns exactly ONE
      ``MemorySettingsManager``, whose lock serializes every
      update. Constructing a second manager against the same
      file would create a race (two locks protecting the same
      resource is not a lock at all).

    * ``MemoryService`` does not expose a public
      ``update_settings(**kwargs)`` method today. Adding one
      would require modifying a tested P1.7 file; the pragmatic
      P1 compromise is direct attribute access with a clear
      comment.

    * P2 can add ``MemoryService.update_settings(**kwargs)`` as
      a thin wrapper over ``_settings_manager.update()``, at
      which point this file's helper becomes a one-line call.
      The behavior is byte-identical either way.

Endpoints
---------

    GET    /api/memory/settings
        Return the CURRENT global settings snapshot.

    PATCH  /api/memory/settings
        Apply a partial update. Only explicitly-set fields are
        changed. The update is persisted and hot-reloaded.

    GET    /api/memory/projects/{project_id}/settings
        Return BOTH the effective settings for the project
        (base merged with any override) AND the raw override
        (empty if none).

    PUT    /api/memory/projects/{project_id}/settings
        Create or replace the project's settings override. The
        body is a partial MemorySettings; fields not present
        inherit from the global settings.

    DELETE /api/memory/projects/{project_id}/settings
        Remove the project's override (reverting the project to
        pure global settings). Idempotent.

PATCH semantics
---------------

``PATCH /api/memory/settings`` accepts a **partial** dict --
every field in ``_PATCHABLE_FIELDS`` is optional, and fields
not present are left untouched. This matches Pydantic's
``model_dump(exclude_unset=True)`` behavior that the settings
manager already uses.

Two important exclusions from the patchable set:

    * ``project_overrides`` -- managed via the dedicated
      ``/projects/{id}/settings`` endpoints. Sending it via
      PATCH would bypass the per-project merge and race
      handling.

    * Any field the caller sends as ``null`` is treated as
      "leave unchanged". Use the per-project DELETE endpoint
      to remove a field; use PUT to replace an entire override
      with fresh values.

Empty PATCH bodies are rejected with 400 -- a no-op update is
more likely a client bug than an intentional request. A caller
that wants to confirm the current settings uses GET.

Revalidation
------------

Every update goes through ``MemorySettings.model_validate``
INSIDE ``MemorySettingsManager.update``. A request that produces
invalid state (e.g. ``reserved_for_user_scope > max_tokens``,
``mode="bogus"``) raises ``pydantic.ValidationError``, which
this router maps to a structured 400. NO partial state is
persisted.

Extra-field validation -- KNOWN INCONSISTENCY (temporary)
---------------------------------------------------------

``_SettingsPatch`` and ``_ProjectSettingsBody`` declare
``extra="forbid"``. FastAPI's automatic request-body validation
catches this and returns **422** with FastAPI's default envelope
BEFORE this router's handler runs. Consequently, a request that
sends an unknown field receives a 422 with FastAPI's shape, not
the memory-API's uniform 400 envelope.

The fix lives at the APPLICATION level, not at this router:
``omweb/main.py`` (P1.8 file #6) will register an
``@app.exception_handler(RequestValidationError)`` that
normalizes every 422 from any router to the memory API's
400 envelope. Until that handler is wired, extra-field
requests produce 422 -- the rejection is still enforced (the
intent is preserved), only the response envelope is
inconsistent.

All OTHER error paths (business validation, mode rejection,
backend failure) already produce the uniform envelope today
because they raise HTTPException from within this router's
handler.

Content of a "settings" response
--------------------------------

The ``settings`` field of the response carries the FULL
``MemorySettings.model_dump(mode="json")``. This gives the UI
everything it needs to render the settings form in a single
round-trip: current mode, provider, all tunables, token
budget, extraction flags, and the per-project overrides map
(for the "projects with overrides" sidebar).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, status as http_status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from peldrun.memory.domain import (
    MemoryMode,
    MemorySettings,
    MemoryUnavailableError,
    MemoryValidationError,
)

from omweb.memory import get_memory_service


logger = logging.getLogger("omweb.memory.api.settings")


router = APIRouter()


# ---------------------------------------------------------------------------
# Patchable fields -- the whitelist
# ---------------------------------------------------------------------------


_PATCHABLE_FIELDS: frozenset = frozenset(
    {
        "mode",
        "provider_id",
        "default_limit",
        "min_relevance_score",
        "boost_pinned",
        "token_budget",
        "auto_extraction_enabled",
        "auto_extraction_min_turns",
    }
)
"""Fields a caller may update via ``PATCH /api/memory/settings``.

Deliberately excludes ``project_overrides`` -- that map is
managed by the per-project endpoints, not by a bulk PATCH. A
caller that wants to reset ALL overrides can PATCH with
``{"project_overrides": {}}``... except that would be sent
via the per-project path, not here. See the module docstring
for the rationale.
"""


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class _SettingsPatch(BaseModel):
    """Body of a ``PATCH /api/memory/settings`` request.

    All fields are optional. Any field not present is left
    unchanged in the resulting settings. ``None`` values are
    treated as "not present" -- Pydantic's ``exclude_unset``
    behavior -- so explicitly sending ``null`` has no effect.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Optional[str] = Field(
        default=None,
        description="MemoryMode value: disabled | read_only | read_write",
    )
    provider_id: Optional[str] = Field(
        default=None,
        description="Registered provider id (e.g. 'sqlite_fts')",
    )
    default_limit: Optional[int] = Field(
        default=None, ge=1, le=50,
        description="Max entries returned by the ranker",
    )
    min_relevance_score: Optional[float] = Field(
        default=None, ge=0.0, le=1.0,
        description="Minimum normalized relevance for injection",
    )
    boost_pinned: Optional[float] = Field(
        default=None, ge=1.0, le=10.0,
        description="Multiplier applied to pinned entries",
    )
    token_budget: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Partial or complete MemoryTokenBudget. Fields "
            "present override the current values; omitted "
            "fields are inherited."
        ),
    )
    auto_extraction_enabled: Optional[bool] = Field(
        default=None,
        description="Enable post-run extraction",
    )
    auto_extraction_min_turns: Optional[int] = Field(
        default=None, ge=1, le=100,
        description="Min conversation turns before extraction",
    )

    def to_update_dict(self) -> Dict[str, Any]:
        """Return only the explicitly-set, non-None fields.

        Excludes anything not in ``_PATCHABLE_FIELDS`` (defense
        in depth: even if a future field is added to this model,
        it will not reach the settings manager until the
        whitelist is updated too).
        """
        raw = self.model_dump(exclude_unset=True, exclude_none=True)
        return {
            k: v for k, v in raw.items()
            if k in _PATCHABLE_FIELDS
        }


class _ProjectSettingsBody(BaseModel):
    """Body of a ``PUT /api/memory/projects/{id}/settings`` request.

    Same semantics as ``_SettingsPatch``: every field is
    optional; present fields override the global settings for
    this project; absent fields inherit.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Optional[str] = None
    provider_id: Optional[str] = None
    default_limit: Optional[int] = Field(default=None, ge=1, le=50)
    min_relevance_score: Optional[float] = Field(
        default=None, ge=0.0, le=1.0,
    )
    boost_pinned: Optional[float] = Field(
        default=None, ge=1.0, le=10.0,
    )
    token_budget: Optional[Dict[str, Any]] = None
    auto_extraction_enabled: Optional[bool] = None
    auto_extraction_min_turns: Optional[int] = Field(
        default=None, ge=1, le=100,
    )

    def to_override_dict(self) -> Dict[str, Any]:
        """Return only explicitly-set fields, ready for override."""
        return self.model_dump(exclude_unset=True, exclude_none=True)


# ---------------------------------------------------------------------------
# Error mapping (same envelope as import/export/usage)
# ---------------------------------------------------------------------------


def _error_response(
    exc: BaseException,
    *,
    operation: str,
) -> JSONResponse:
    """Map a settings-layer exception to an HTTP response."""
    if isinstance(exc, MemoryValidationError):
        return JSONResponse(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            content={
                "status": "error",
                "operation": operation,
                "error": type(exc).__name__,
                "message": exc.message,
                "context": dict(exc.context),
            },
        )

    if isinstance(exc, MemoryUnavailableError):
        logger.error(
            "Memory settings %s hit backend failure: %s",
            operation,
            exc,
        )
        return JSONResponse(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "error",
                "operation": operation,
                "error": "MemoryUnavailableError",
                "message": (
                    "Memory backend unavailable. Settings change "
                    "was not persisted."
                ),
            },
        )

    logger.exception(
        "Memory settings %s failed unexpectedly: %s",
        operation,
        exc,
    )
    return JSONResponse(
        status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "status": "error",
            "operation": operation,
            "error": type(exc).__name__,
            "message": "Internal error during settings update.",
        },
    )


# ---------------------------------------------------------------------------
# Service access helpers
# ---------------------------------------------------------------------------


def _get_service_or_500():
    """Return the ``MemoryService`` singleton or raise HTTP 500.

    The service is initialized by the FastAPI ``lifespan`` hook
    (P1.7). When the request lands before that hook has run, the
    exception is a startup ordering bug -- 500, not 503,
    because the fix is "fix the startup sequence", not "retry".
    """
    try:
        return get_memory_service()
    except RuntimeError as exc:
        logger.error(
            "MemoryService is not initialized: %s", exc
        )
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "service_not_initialized",
                "message": (
                    "Memory service is not initialized. This is a "
                    "startup ordering bug; contact the operator."
                ),
            },
        ) from exc


def _settings_to_dict(settings: MemorySettings) -> Dict[str, Any]:
    """Serialize settings for a response body."""
    return settings.model_dump(mode="json")


def _pydantic_error_to_400(
    exc: BaseException,
) -> HTTPException:
    """Convert a Pydantic validation error into a structured 400."""
    # Pydantic's ValidationError has an ``errors()`` method;
    # we forward its structured output under ``detail.fields``
    # so the UI can highlight the offending field.
    try:
        errors = exc.errors()  # type: ignore[attr-defined]
    except Exception:
        errors = []

    fields: List[Dict[str, Any]] = []
    for err in errors:
        if not isinstance(err, dict):
            continue
        fields.append(
            {
                "loc": [str(x) for x in err.get("loc", [])],
                "msg": str(err.get("msg", "")),
                "type": str(err.get("type", "")),
            }
        )

    return HTTPException(
        status_code=http_status.HTTP_400_BAD_REQUEST,
        detail={
            "error": "invalid_settings",
            "message": "Settings validation failed.",
            "fields": fields,
        },
    )


# ---------------------------------------------------------------------------
# Global settings endpoints
# ---------------------------------------------------------------------------


@router.get(
    "",
    summary="Read global memory settings",
    response_model=None,
)
async def get_settings() -> JSONResponse:
    """Return the current global ``MemorySettings`` snapshot.

    Powers the "Memory Settings" page in the UI. Includes every
    field including ``project_overrides`` (which the sidebar
    uses to show which projects have custom settings).
    """
    try:
        service = _get_service_or_500()
    except HTTPException:
        raise

    try:
        settings = service.settings
    except Exception as exc:
        return _error_response(exc, operation="settings_get")

    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok",
            "operation": "settings_get",
            "settings": _settings_to_dict(settings),
        },
    )


@router.patch(
    "",
    summary="Partially update global memory settings",
    response_model=None,
)
async def patch_settings(body: _SettingsPatch) -> JSONResponse:
    """Apply a partial update to the global settings.

    Flow:

      1. Serialize the request to a kwargs dict
         (``exclude_unset`` drops anything the caller did not
         explicitly set).
      2. Persist + revalidate via
         ``MemoryService._settings_manager.update(**kwargs)``.
         The manager holds the ONLY lock on the settings file,
         so two concurrent PATCHes serialize cleanly.
      3. Reload via ``MemoryService.reload_settings()``, which
         reads the fresh file and re-applies it to the
         lifecycle (rebuilding the provider only when the
         provider_id or the DISABLED boundary changed).

    The response carries the FULL new settings object -- not
    just the diff -- so the UI can replace its local snapshot
    atomically.
    """
    update_dict = body.to_update_dict()
    if not update_dict:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "empty_update",
                "message": (
                    "PATCH body must include at least one field "
                    "to update. Use GET to read the current "
                    "settings."
                ),
            },
        )

    try:
        service = _get_service_or_500()
    except HTTPException:
        raise

    try:
        # ----- Persist + validate -----
        new_settings = await service._settings_manager.update(  # noqa: SLF001
            **update_dict
        )
    except Exception as exc:
        # Pydantic's ValidationError is not a MemoryValidationError;
        # catch it here and convert to a structured 400.
        try:
            from pydantic import ValidationError

            if isinstance(exc, ValidationError):
                raise _pydantic_error_to_400(exc)
        except ImportError:
            pass
        return _error_response(exc, operation="settings_update")

    # ----- Hot-reload onto the running service -----
    try:
        await service.reload_settings()
    except Exception as exc:
        # Settings file is now on disk but lifecycle reload
        # failed. The next request will observe the file
        # contents; the current one gets a 503 so the operator
        # knows the in-memory state is stale.
        logger.error(
            "Settings persisted but reload failed: %s", exc
        )
        return _error_response(exc, operation="settings_update")

    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok",
            "operation": "settings_update",
            "applied_fields": sorted(update_dict.keys()),
            "settings": _settings_to_dict(new_settings),
        },
    )


# ---------------------------------------------------------------------------
# Per-project settings endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{project_id}",
    summary="Read a project's memory settings",
    response_model=None,
)
async def get_project_settings(project_id: str) -> JSONResponse:
    """Return (effective, override) settings for a project.

    ``effective`` is what the memory subsystem ACTUALLY uses for
    this project: the global settings with any project override
    merged on top (via ``MemorySettings.effective_for_project``).

    ``override`` is the raw per-project delta, or ``{}`` when
    the project has no override. The UI uses this to render the
    "customize this project" form pre-filled with the current
    override values (not the effective ones).
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
    except HTTPException:
        raise

    try:
        base = service.settings
        effective = base.effective_for_project(clean_pid)
        override = base.project_overrides.get(clean_pid)
    except Exception as exc:
        return _error_response(exc, operation="project_settings_get")

    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok",
            "operation": "project_settings_get",
            "project_id": clean_pid,
            "effective": _settings_to_dict(effective),
            "override": (
                _settings_to_dict(override)
                if override is not None
                else {}
            ),
        },
    )


@router.put(
    "/projects/{project_id}",
    summary="Create or replace a project's memory settings override",
    response_model=None,
)
async def put_project_settings(
    project_id: str,
    body: _ProjectSettingsBody,
) -> JSONResponse:
    """Create or replace the project's override.

    The body is a PARTIAL ``MemorySettings``. Fields present
    override the global settings for this project. Fields
    absent inherit from global.

    The override REPLACES any previous override -- a PUT is a
    full replacement of the override slot, not a merge with it.
    This matches HTTP PUT semantics: the client sends the
    complete desired override state.

    Implementation:

      1. Build an override ``MemorySettings`` from the body's
         explicitly-set fields, validating via
         ``MemorySettings.model_validate`` so any invalid
         combination is rejected BEFORE the write.
      2. Attach the override to the base settings via
         ``MemorySettings.with_project_override``.
      3. Persist the FULL base+overrides structure to disk.
      4. Hot-reload onto the running service.

    Because ``_settings_manager.update()`` takes kwargs (field
    diffs), we pass ``project_overrides`` as a whole-replacement
    dict: the current overrides with the new one inserted.
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

    override_dict = body.to_override_dict()
    if not override_dict:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "empty_override",
                "message": (
                    "PUT body must include at least one field. "
                    "Use DELETE to remove the override entirely."
                ),
            },
        )

    try:
        service = _get_service_or_500()
    except HTTPException:
        raise

    # ----- Build the override MemorySettings -----
    #
    # ``MemorySettings.model_validate`` is the single source of
    # truth for validation -- no need to duplicate the rules
    # here. Any invalid combination (e.g. mode=bogus) raises
    # ValidationError, which we convert to a 400.
    #
    # The override intentionally carries ONLY the fields the
    # caller sent -- not inherited values. This is what
    # ``effective_for_project``'s delta-merge expects (see
    # ``MemorySettings.effective_for_project``).
    try:
        override = MemorySettings.model_validate(override_dict)
    except Exception as exc:
        try:
            from pydantic import ValidationError

            if isinstance(exc, ValidationError):
                raise _pydantic_error_to_400(exc)
        except ImportError:
            pass
        return _error_response(exc, operation="project_settings_put")

    if override.project_overrides:
        # Nested overrides are rejected by MemorySettings's own
        # validator; this branch is defense in depth.
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "nested_overrides",
                "message": (
                    "project_overrides cannot be nested inside a "
                    "project override."
                ),
            },
        )

    try:
        base = service.settings
        merged = base.with_project_override(clean_pid, override)
        # ``with_project_override`` returns a fresh
        # MemorySettings whose project_overrides map is the
        # base's map + {clean_pid: override}. Persisting THAT
        # (not the override alone) is what we need: the file
        # stores the FULL structure.
        new_settings = await service._settings_manager.update(  # noqa: SLF001
            project_overrides=merged.project_overrides
        )
    except Exception as exc:
        return _error_response(
            exc, operation="project_settings_put"
        )

    try:
        await service.reload_settings()
    except Exception as exc:
        logger.error(
            "Project override persisted but reload failed: %s",
            exc,
        )
        return _error_response(
            exc, operation="project_settings_put"
        )

    effective = new_settings.effective_for_project(clean_pid)

    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok",
            "operation": "project_settings_put",
            "project_id": clean_pid,
            "applied_fields": sorted(override_dict.keys()),
            "effective": _settings_to_dict(effective),
        },
    )


@router.delete(
    "/projects/{project_id}",
    summary="Remove a project's settings override",
    response_model=None,
)
async def delete_project_settings(project_id: str) -> JSONResponse:
    """Remove the project's override, reverting to global settings.

    Idempotent: removing an override that does not exist is a
    no-op that still returns 200 with the effective (= global)
    settings.
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
    except HTTPException:
        raise

    try:
        base = service.settings
        merged = base.without_project_override(clean_pid)
        new_settings = await service._settings_manager.update(  # noqa: SLF001
            project_overrides=merged.project_overrides
        )
    except Exception as exc:
        return _error_response(
            exc, operation="project_settings_delete"
        )

    try:
        await service.reload_settings()
    except Exception as exc:
        logger.error(
            "Project override removal persisted but reload "
            "failed: %s",
            exc,
        )
        return _error_response(
            exc, operation="project_settings_delete"
        )

    effective = new_settings.effective_for_project(clean_pid)

    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok",
            "operation": "project_settings_delete",
            "project_id": clean_pid,
            "effective": _settings_to_dict(effective),
        },
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = ["router"]
