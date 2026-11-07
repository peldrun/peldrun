# backend/omweb/routers/skills.py
"""
Skills HTTP API.

Endpoints:
    GET    /api/skills                     — list skills (search & filters)
    GET    /api/skills/{skill_id}          — full detail (manifest + frontmatter)
    GET    /api/skills/{skill_id}/icon.svg — sanitized SVG icon
    GET    /api/skills/{skill_id}/preview  — instructions text (for the drawer)
    POST   /api/skills/install             — upload a .zip package
    POST   /api/skills/{skill_id}/toggle   — flip active/disabled
    DELETE /api/skills/{skill_id}          — remove a package

Design notes:
    * Every mutation delegates to ``skill_manager`` — this router contains
      no business logic, only request/response shaping and error mapping.
    * Error responses follow the same structured shape used by
      ``omweb.tools.bundles`` — ``{"error": str, "code": str, "details": {}}``
      — so the existing frontend error handlers already understand them.
    * Skill icons are served with a 1-hour browser cache header. The
      icon content is sanitized server-side (see
      ``omweb.skills.manifest.sanitize_svg``), so the browser never
      receives script-bearing or externally-referencing SVG.
    * Broken packages (present on disk but never successfully
      discovered) are exposed only when ``?include_broken=true`` is
      passed. Their error messages appear in a ``broken`` dict, never
      in the ``skills`` list.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field

from omweb.skills.errors import (
    SkillError,
    SkillLoadError,
    SkillNotFoundError,
    SkillSecurityError,
    SkillStorageError,
    SkillValidationError,
)
from omweb.skills.manager import skill_manager
from omweb.skills.registry import SkillRecord


logger = logging.getLogger("omweb.routers.skills")

router = APIRouter()


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class SkillSummaryResponse(BaseModel):
    """Public summary of a skill - mirrors ``SkillRecord.to_summary_dict``."""

    id: str
    version: str
    name: str
    display_name: str
    description: str
    short_description: str
    status: str
    enabled: bool
    is_builtin: bool
    user_editable: bool = True
    is_deprecated: bool
    chat_invocable: bool
    agent_assignable: bool
    workflow_node: bool
    has_icon_svg: bool
    has_icon_path: bool
    icon_path: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    author: Dict[str, Any] = Field(default_factory=dict)
    publisher_id: Optional[str] = None
    trust: str = "unverified"
    license: Optional[str] = None
    homepage: Optional[str] = None
    docs_url: Optional[str] = None
    release_date: Optional[str] = None
    stability: str = "stable"
    capabilities: Dict[str, Any] = Field(default_factory=dict)
    execution: Dict[str, Any] = Field(default_factory=dict)
    package_dir: str = ""
    # Router metadata (Progressive Disclosure). Defaults preserve the
    # existing API contract for non-router skills - the frontend gates
    # the ROUTER badge on ``has_sub_skills == true``.
    has_sub_skills: bool = False
    sub_skills_count: int = 0


class SubSkillSummary(BaseModel):
    """
    Public shape of a single sub-skill entry.

    Returned by ``GET /api/skills/{id}/sub-skills`` and embedded inside
    the detail response so the drawer can render the router without a
    second round-trip. Deliberately excludes ``instructions_file``
    from the public API surface - that path is an internal packaging
    detail and exposing it invites manual filesystem tinkering.
    """

    id: str
    description: str
    triggers: List[str] = Field(default_factory=list)
    capabilities: Dict[str, Any] = Field(default_factory=dict)


class SkillDetailResponse(SkillSummaryResponse):
    """Full detail - includes raw manifest and SKILL.md frontmatter."""

    manifest: Dict[str, Any] = Field(default_factory=dict)
    frontmatter: Dict[str, Any] = Field(default_factory=dict)
    # Full sub-skill list for router skills. Empty list for non-routers.
    sub_skills: List[SubSkillSummary] = Field(default_factory=list)


class SkillInstallResponse(BaseModel):
    """Response returned by ``POST /api/skills/install``."""

    status: str = "success"
    skill: SkillSummaryResponse
    was_upgrade: bool = False
    warnings: List[str] = Field(default_factory=list)


class SkillToggleResponse(BaseModel):
    """Response returned by ``POST /api/skills/{id}/toggle``."""

    status: str = "success"
    skill: SkillSummaryResponse


class SkillPreviewResponse(BaseModel):
    """Response returned by ``GET /api/skills/{id}/preview``."""

    status: str = "success"
    skill_id: str
    version: str
    instructions: str
    instructions_length: int
    frontmatter: Dict[str, Any] = Field(default_factory=dict)


class SkillInstructionsUpdateRequest(BaseModel):
    """
    Request body for ``PUT /api/skills/{id}/instructions``.

    Only the Markdown body (below the YAML frontmatter) is accepted.
    The frontmatter is preserved verbatim server-side from the
    existing SKILL.md — sending it in this payload is a client error.
    """

    instructions: str = Field(
        ...,
        min_length=1,
        description=(
            "The new Markdown body WITHOUT frontmatter. The existing "
            "frontmatter is preserved automatically."
        ),
    )


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------

def _skill_error_to_http(
    exc: SkillError, *, http_status: int = 400
) -> HTTPException:
    """
    Convert a ``SkillError`` into a structured ``HTTPException``.

    The detail payload keeps the ``{error, code, details}`` shape used
    by the tool bundles subsystem so the frontend's existing error
    handlers can render both uniformly.
    """
    return HTTPException(status_code=http_status, detail=exc.to_dict())


def _record_to_summary(record: SkillRecord) -> SkillSummaryResponse:
    """Convert a ``SkillRecord`` into the public summary model."""
    return SkillSummaryResponse(**record.to_summary_dict())


def _record_to_detail(record: SkillRecord) -> SkillDetailResponse:
    """
    Convert a ``SkillRecord`` into the full detail model.

    The raw ``sub_skills`` list on the record carries the internal
    ``instructions_file`` path; we strip it here so it never reaches
    the HTTP response (defense-in-depth: the schema would reject the
    extra key on ``SubSkillSummary`` anyway, but explicit is safer).
    """
    data = record.to_full_dict()
    data["sub_skills"] = [
        {
            "id": sub.get("id", ""),
            "description": sub.get("description", ""),
            "triggers": list(sub.get("triggers") or []),
            "capabilities": dict(sub.get("capabilities") or {}),
        }
        for sub in (data.get("sub_skills") or [])
    ]
    return SkillDetailResponse(**data)


# ---------------------------------------------------------------------------
# GET /api/skills
# ---------------------------------------------------------------------------

@router.get("", response_model=Dict[str, Any])
async def list_skills(
    search: Optional[str] = Query(
        None,
        description=(
            "Free-form search query. Matched case-insensitively against "
            "skill ID, name, display_name, tags, and description."
        ),
    ),
    include_disabled: bool = Query(
        True,
        description="Include skills whose status is 'disabled'.",
    ),
    include_deprecated: bool = Query(
        True,
        description="Include skills marked deprecated.",
    ),
    include_broken: bool = Query(
        False,
        description=(
            "Include packages that failed validation under a separate "
            "'broken' field. When false (default), only successfully "
            "discovered skills are returned."
        ),
    ),
    limit: int = Query(
        100,
        ge=1,
        le=500,
        description="Maximum number of skills to return (when 'search' is set).",
    ),
) -> Dict[str, Any]:
    """
    List installed skills, optionally filtered by a search query.

    Two request shapes are supported from a single endpoint:

        * ``GET /api/skills`` — full list, sorted by canonical ID.
        * ``GET /api/skills?search=code`` — ranked search results
          (score by exact-match, prefix, substring, tags, description).
          Disabled and deprecated skills are excluded by default in
          search mode; pass the include-* flags to override.

    The response shape is::

        {
          "status": "success",
          "skills": [ ...SkillSummaryResponse... ],
          "total":  <int>,
          "broken": { "<package>": "<error>", ... }   # only when include_broken=true
        }
    """
    raw_query = (search or "").strip()

    try:
        if raw_query:
            # Search mode: disabled/deprecated default to off unless
            # the caller explicitly enabled them.
            records = skill_manager.search_skills(
                raw_query,
                include_disabled=include_disabled and (raw_query != "" or True),
                include_deprecated=include_deprecated and (raw_query != "" or True),
                limit=limit,
            )
        else:
            records = skill_manager.list_skills(
                include_disabled=include_disabled,
                include_deprecated=include_deprecated,
            )
    except Exception as exc:
        logger.exception("Skill listing failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail={
                "error": f"Failed to list skills: {exc}",
                "code": "SKILL_LIST_FAILED",
                "details": {},
            },
        )

    payload: Dict[str, Any] = {
        "status": "success",
        "skills": [_record_to_summary(r).model_dump() for r in records],
        "total": len(records),
    }

    if include_broken:
        try:
            payload["broken"] = skill_manager.list_broken_skills()
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.debug("Failed to list broken skills: %s", exc)
            payload["broken"] = {}

    return payload


# ---------------------------------------------------------------------------
# GET /api/skills/{skill_id}
# ---------------------------------------------------------------------------

@router.get("/{skill_id}", response_model=Dict[str, Any])
async def get_skill(skill_id: str) -> Dict[str, Any]:
    """
    Return the full detail for a single skill.

    Includes the raw ``manifest`` and ``SKILL.md`` ``frontmatter`` —
    the UI needs both for the details drawer. The instructions body is
    NOT included here (it can be up to 1 MiB); use the ``/preview``
    endpoint to fetch it separately.
    """
    try:
        record = skill_manager.get_skill_or_raise(skill_id)
    except SkillNotFoundError as exc:
        raise _skill_error_to_http(exc, http_status=404)

    return {
        "status": "success",
        "skill": _record_to_detail(record).model_dump(),
    }


# ---------------------------------------------------------------------------
# GET /api/skills/{skill_id}/sub-skills
# ---------------------------------------------------------------------------

@router.get(
    "/{skill_id}/sub-skills",
    response_model=Dict[str, Any],
)
async def get_skill_sub_skills(skill_id: str) -> Dict[str, Any]:
    """
    Return the sub-skills of a router skill.

    The response always includes ``has_sub_skills`` and ``sub_skills``
    (possibly empty). Non-router skills return ``has_sub_skills: false``
    and an empty list - the endpoint never 404s just because the skill
    is not a router. It 404s only when the parent skill does not exist.

    Response shape::

        {
          "status": "success",
          "parent_id": "debugging",
          "has_sub_skills": true,
          "sub_skills": [
            {
              "id": "root_cause_tracing",
              "description": "...",
              "triggers": ["bug", "crash"],
              "capabilities": {"exclusive": false}
            },
            ...
          ],
          "count": 4
        }
    """
    try:
        record = skill_manager.get_skill_or_raise(skill_id)
    except SkillNotFoundError as exc:
        raise _skill_error_to_http(exc, http_status=404)

    sub_entries: List[Dict[str, Any]] = [
        {
            "id": str(sub.get("id") or ""),
            "description": str(sub.get("description") or ""),
            "triggers": list(sub.get("triggers") or []),
            "capabilities": dict(sub.get("capabilities") or {}),
        }
        for sub in record.sub_skills
        if isinstance(sub, dict) and sub.get("id")
    ]

    return {
        "status": "success",
        "parent_id": record.id,
        "has_sub_skills": bool(record.sub_skills),
        "sub_skills": sub_entries,
        "count": len(sub_entries),
    }


# ---------------------------------------------------------------------------
# GET /api/skills/{skill_id}/icon.svg
# ---------------------------------------------------------------------------

@router.get("/{skill_id}/icon.svg")
async def get_skill_icon(skill_id: str):
    """
    Serve the sanitized SVG icon for a skill.

    Resolution order (see ``loader.load_icon``):
        1. Inline ``icon_svg`` from the manifest (sanitized at load time).
        2. Relative ``icon`` file inside the package (sanitized now).
        3. 404 — frontend falls back to a Lucide icon.

    The response includes a 1-hour cache header. Browser and CDN caches
    will revalidate only when the skill is upgraded (which changes the
    ETag path via a fresh install timestamp).

    Security: the content is already sanitized server-side by
    ``manifest.sanitize_svg``. We additionally set
    ``Content-Type: image/svg+xml`` so browsers treat it as an image,
    not as inline HTML.
    """
    try:
        icon = skill_manager.get_skill_icon(skill_id)
    except SkillNotFoundError as exc:
        raise _skill_error_to_http(exc, http_status=404)
    except SkillSecurityError as exc:
        # Sanitization failure is a server-side problem (should never
        # happen for a successfully installed skill), but we surface
        # it clearly so operators can diagnose.
        raise _skill_error_to_http(exc, http_status=500)
    except SkillLoadError as exc:
        raise _skill_error_to_http(exc, http_status=500)

    if icon is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "Skill has no icon.",
                "code": "SKILL_ICON_NOT_FOUND",
                "details": {"skill_id": skill_id},
            },
        )

    return Response(
        content=icon["content"],
        media_type=icon.get("content_type", "image/svg+xml"),
        headers={
            "Cache-Control": "public, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )


# ---------------------------------------------------------------------------
# GET /api/skills/{skill_id}/preview
# ---------------------------------------------------------------------------

@router.get(
    "/{skill_id}/preview",
    response_model=Dict[str, Any],
)
async def preview_skill(skill_id: str) -> Dict[str, Any]:
    """
    Return the skill's instructions text for the details drawer.

    This is a read-only preview — it does not modify any state. The
    response includes the raw instructions (up to
    ``limits.max_instructions_bytes``), the frontmatter, and a length
    counter the frontend uses to decide whether to truncate.
    """
    try:
        record = skill_manager.get_skill_or_raise(skill_id)
        instructions = skill_manager.get_skill_instructions(skill_id)
    except SkillNotFoundError as exc:
        raise _skill_error_to_http(exc, http_status=404)
    except SkillLoadError as exc:
        raise _skill_error_to_http(exc, http_status=422)
    except SkillSecurityError as exc:
        raise _skill_error_to_http(exc, http_status=500)

    return {
        "status": "success",
        "skill_id": record.id,
        "version": record.version,
        "instructions": instructions,
        "instructions_length": len(instructions),
        "frontmatter": dict(record.frontmatter),
    }


# ---------------------------------------------------------------------------
# PUT /api/skills/{skill_id}/instructions
# ---------------------------------------------------------------------------

@router.put(
    "/{skill_id}/instructions",
    response_model=Dict[str, Any],
)
async def update_skill_instructions(
    skill_id: str,
    req: SkillInstructionsUpdateRequest,
) -> Dict[str, Any]:
    """
    Replace the SKILL.md body of a user-editable skill.

    The frontmatter block is NOT part of the payload and is preserved
    verbatim from the existing SKILL.md. The request is rejected when:

        * the skill does not exist (404),
        * the skill is not user-editable (403),
        * the body is empty, oversized, or fails re-parse (422),
        * the write itself fails (500).

    On success the registry is refreshed, so the next chat run that
    injects this skill picks up the new body immediately.
    """
    try:
        record = skill_manager.update_skill_instructions(
            skill_id=skill_id,
            new_body=req.instructions,
        )
    except SkillNotFoundError as exc:
        raise _skill_error_to_http(exc, http_status=404)
    except SkillSecurityError as exc:
        raise _skill_error_to_http(exc, http_status=403)
    except SkillValidationError as exc:
        raise _skill_error_to_http(exc, http_status=422)
    except SkillStorageError as exc:
        logger.exception(
            "Failed to persist instructions for skill %r: %s",
            skill_id,
            exc,
        )
        raise _skill_error_to_http(exc, http_status=500)
    except SkillError as exc:
        # Safety net: any future SkillError subclass that is not
        # explicitly mapped above becomes a 400. Prevents 500s from
        # leaking partial error shapes.
        logger.warning(
            "Unmapped SkillError during instructions update for %r: %s",
            skill_id,
            exc,
        )
        raise _skill_error_to_http(exc, http_status=400)

    return {
        "status": "success",
        "skill": _record_to_summary(record).model_dump(),
        "instructions_length": len(record.instructions),
    }


# ---------------------------------------------------------------------------
# POST /api/skills/install
# ---------------------------------------------------------------------------

@router.post("/install", response_model=Dict[str, Any])
async def install_skill(
    file: UploadFile = File(..., description="Skill .zip package"),
    source_filename: Optional[str] = Form(
        None,
        description=(
            "Optional original filename to record in the install "
            "metadata. Defaults to the uploaded file's name."
        ),
    ),
) -> Dict[str, Any]:
    """
    Install a skill package from an uploaded ``.zip`` archive.

    The zip MUST contain ``manifest.json`` and ``SKILL.md`` at its root
    (no wrapping directory). All package contents are validated and
    sanitized before any file is written to disk:

        * zip validation (path traversal, symlinks, size caps)
        * manifest schema validation
        * SKILL.md YAML frontmatter parse
        * SVG sanitization (if ``icon_svg`` is present)
        * version compatibility check (``peldrun_min_version``)

    A pre-existing skill with the same ID is atomically replaced. The
    previous package is preserved in a ``.trash_*`` directory until the
    rename completes, then deleted.
    """
    if not file.filename:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "Uploaded file has no filename.",
                "code": "SKILL_MISSING_FILENAME",
                "details": {},
            },
        )

    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "Only .zip skill packages are accepted.",
                "code": "SKILL_WRONG_FILE_TYPE",
                "details": {"filename": file.filename},
            },
        )

    try:
        content = await file.read()
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": f"Failed to read uploaded file: {exc}",
                "code": "SKILL_UPLOAD_READ_FAILED",
                "details": {},
            },
        )

    if not content:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "Uploaded skill package is empty.",
                "code": "SKILL_EMPTY_UPLOAD",
                "details": {},
            },
        )

    effective_source = (
        source_filename.strip()
        if isinstance(source_filename, str) and source_filename.strip()
        else file.filename
    )

    try:
        result = skill_manager.install_skill(
            content,
            source_filename=effective_source,
        )
    except SkillError as exc:
        logger.info(
            "Skill install rejected (%s): %s",
            exc.code,
            exc,
        )
        # Manifest, validation, and security errors are the caller's
        # problem (4xx). Storage errors indicate a server problem (5xx).
        status_code = (
            500
            if exc.code in (
                "SKILL_STORAGE_FAILED",
                "SKILL_EXTRACTION_FAILED",
            )
            else 400
        )
        raise _skill_error_to_http(exc, http_status=status_code)
    except Exception as exc:
        logger.exception("Unexpected skill install error: %s", exc)
        raise HTTPException(
            status_code=500,
            detail={
                "error": str(exc),
                "code": "SKILL_INSTALL_FAILED",
                "details": {"exception_type": type(exc).__name__},
            },
        )

    # Re-read the record so the response reflects the installed state
    # (including the normalized ID and any registry-derived fields).
    installed_record = skill_manager.get_skill(result.skill_id)
    if installed_record is None:
        # Extremely unlikely: install succeeded but refresh failed.
        # Return what we know from the storage result.
        logger.warning(
            "Skill %r installed but not visible in registry; "
            "returning storage-level summary.",
            result.skill_id,
        )
        return {
            "status": "success",
            "skill": {
                "id": result.skill_id,
                "version": result.version,
                "name": result.name,
                "description": result.description,
            },
            "was_upgrade": result.was_upgrade,
            "warnings": list(result.warnings),
        }

    return {
        "status": "success",
        "skill": _record_to_summary(installed_record).model_dump(),
        "was_upgrade": result.was_upgrade,
        "warnings": list(result.warnings),
    }


# ---------------------------------------------------------------------------
# POST /api/skills/{skill_id}/toggle
# ---------------------------------------------------------------------------

@router.post("/{skill_id}/toggle", response_model=Dict[str, Any])
async def toggle_skill(skill_id: str) -> Dict[str, Any]:
    """
    Flip a skill between ``active`` and ``disabled``.

    Disabling a skill:
        * prevents new ``/`` invocations immediately,
        * silently drops it from the current agent's injection set for
          the next run,
        * does NOT delete any files.

    The disabled state is persisted in ``skills_state.json`` alongside
    the skill package directory. It survives application restarts.
    """
    try:
        record = skill_manager.toggle_skill_status(skill_id)
    except SkillNotFoundError as exc:
        raise _skill_error_to_http(exc, http_status=404)
    except Exception as exc:
        logger.exception("Skill toggle failed for %r: %s", skill_id, exc)
        raise HTTPException(
            status_code=500,
            detail={
                "error": str(exc),
                "code": "SKILL_TOGGLE_FAILED",
                "details": {"skill_id": skill_id},
            },
        )

    return {
        "status": "success",
        "skill": _record_to_summary(record).model_dump(),
    }


# ---------------------------------------------------------------------------
# DELETE /api/skills/{skill_id}
# ---------------------------------------------------------------------------

@router.delete("/{skill_id}", response_model=Dict[str, Any])
async def delete_skill(skill_id: str) -> Dict[str, Any]:
    """
    Delete a skill package from disk.

    Accepts a canonical ID, an alias, or the package directory name —
    the manager resolves all three. Broken packages (present on disk
    but never successfully discovered) can also be deleted by
    directory name, so operators can always clean up a corrupt upload.

    Deletion is atomic: the package is moved to a ``.trash_*`` sibling
    before removal, so a crash mid-delete leaves it recoverable via
    the startup cleanup pass.
    """
    try:
        deleted = skill_manager.delete_skill(skill_id)
    except SkillNotFoundError as exc:
        raise _skill_error_to_http(exc, http_status=404)
    except SkillError as exc:
        raise _skill_error_to_http(exc, http_status=500)
    except Exception as exc:
        logger.exception("Skill delete failed for %r: %s", skill_id, exc)
        raise HTTPException(
            status_code=500,
            detail={
                "error": str(exc),
                "code": "SKILL_DELETE_FAILED",
                "details": {"skill_id": skill_id},
            },
        )

    if not deleted:
        # Race: skill was removed between our lookup and our delete.
        raise HTTPException(
            status_code=404,
            detail={
                "error": f"Skill {skill_id!r} not found.",
                "code": "SKILL_NOT_FOUND",
                "details": {"skill_id": skill_id},
            },
        )

    return {
        "status": "success",
        "skill_id": skill_id,
        "deleted": True,
    }


__all__ = ["router"]
