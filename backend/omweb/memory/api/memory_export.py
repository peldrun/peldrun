# backend/omweb/memory/api/memory_export.py
"""
PELDRUN Memory Platform - Export HTTP Router.

Thin HTTP layer over ``package_writer`` + the memory provider.
Reads entries from a scope, builds a ``peldrun-memory-package``,
and returns it to the caller -- as a zip download or as raw JSON.

Responsibilities (deliberately NARROW)
--------------------------------------

This router does FOUR things:

    1. **Validate query params** (scope_type, scope_id, format,
       status, limit). Programmer errors and user errors are
       distinguished by the same taxonomy as the import router.

    2. **Fetch entries** from the memory provider, with a hard
       cap of 10k entries per export. Larger exports are
       truncated with a response header indicating so.

    3. **Build the package payload** via
       ``package_writer.build_package_payload``.

    4. **Serialize** to the caller's requested format and set
       the correct ``Content-Type`` / ``Content-Disposition``
       headers.

It does NOT:

    * parse any format (there is no parsing on the read path),
    * apply any write policy (the caller is READing),
    * touch SQLite or ZIP internals (writer does),
    * orchestrate multi-step flows (there is no preview/apply
      split on export -- the user either downloads or does not).

Read-only -- and mode-consistent with import
--------------------------------------------

Export is a READ operation. Its ``MemoryMode`` policy mirrors
import's:

    **DISABLED**
        Rejected with 400. There is nothing to export -- the
        memory subsystem is off.

    **READ_ONLY**
        Allowed. Export is a read; READ_ONLY explicitly permits
        reads.

    **READ_WRITE**
        Allowed with no special-casing.

Notice that this differs from the IMPORT router's policy for
``READ_ONLY``: imports are WRITES that get an exemption, exports
are READS that need no exemption. Both end up allowed, but the
reasoning is different, and the code that enforces the policy
is the same in both routers (a single mode comparison before
proceeding).

Response formats
----------------

Two ``format=`` values are accepted:

    **zip** (default)
        Returns ``application/zip`` with
        ``Content-Disposition: attachment; filename="..."``.
        The zip contains ``manifest.json`` + ``memories.jsonl``
        in the ``peldrun-memory-package`` format. This is the
        round-trip format -- a user who saves this file can
        re-upload it via ``POST /api/memory/import/apply`` and
        get back the exact same entries.

    **json**
        Returns ``application/json`` with the raw package data
        as a SINGLE object:

            {
              "manifest": {...},
              "memories": [{...}, {...}]
            }

        This is the shape ``JsonAdapter`` accepts as an inline
        dict source -- so the caller can feed the response body
        back to the import endpoint via the JSON request shape
        (``{"source": <this_body>, ...}``) without writing it
        to disk.

        Useful for:
          * browser devtools inspection,
          * scripts that want to filter/transform before
            re-importing,
          * automated tests that need deterministic byte output.

Query parameters
----------------

    ``scope_type`` (required)
        One of ``user`` / ``project`` / ``chat`` / ``agent``.

    ``scope_id`` (required)
        The concrete owner of the scope.

    ``format`` (optional, default ``zip``)
        ``zip`` or ``json``. Any other value is 400.

    ``status`` (optional, default ``all``)
        ``active`` -- only retrievable entries (what would be
        injected into a prompt).
        ``all`` -- every status including ``archived`` and
        ``superseded``.
        Any other value is 400.

    ``limit`` (optional, default 10000, max 10000)
        Maximum entries to export. Capped at
        ``_MAX_EXPORT_ENTRIES``; the cap exists so a single
        request cannot return a multi-hundred-MB archive or
        an unbounded JSON document.

Truncation signaling
--------------------

When ``limit`` (or the hard cap) causes entries to be dropped,
the response carries:

    ``X-Export-Truncated: true``
    ``X-Export-Total-Matched: <n>``

The ``<n>`` is the actual count of matching entries BEFORE
truncation. A caller that needs everything should export in
smaller slices by varying ``status`` / re-running with higher
``limit`` -- but the 10k cap is deliberate and documented in
``package_writer``'s module docstring (the preview UI must
render each entry, and a 1M-entry response is unusable to a
browser).

Filename generation
-------------------

``memory-export-<scope_type>-<scope_id>-<YYYYMMDD>.<ext>``

``scope_id`` is sanitized for filename use: characters that are
invalid on Windows (``\\ / : * ? " < > |``) and control chars
are replaced with ``_``. The ``MemoryScopeRef`` validator already
rejects ``:`` and control chars, but ``/`` and ``\\`` are legal
scope_ids (a path-like project id is common) and MUST be
sanitized before use as a filename.

Content-Disposition header
--------------------------

RFC 6266 with both an ASCII ``filename`` and a UTF-8
``filename*`` fallback for non-ASCII scope_ids:

    attachment; filename="memory-export-project-acme-20261112.zip";
                filename*=UTF-8''memory-export-project-acme-20261112.zip

The ``filename*`` variant uses percent-encoding so a scope_id
containing, say, Arabic characters downloads under its real
name in modern browsers. Older clients fall back to the ASCII
form (which has already been sanitized).

Error mapping
-------------

Same taxonomy as the import router:

    * ``MemoryValidationError`` -> 400 (client error, message
      forwarded verbatim),
    * ``MemoryUnavailableError`` -> 503 (backend down),
    * ``TypeError`` / unexpected -> 500 (server bug).

The mode check is intentionally raised as
``MemoryValidationError`` (400) rather than 503: the fix is
"enable memory in settings", not "retry later".
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, status as http_status
from fastapi.responses import JSONResponse, Response

from peldrun.memory.domain import (
    MemoryMode,
    MemoryScope,
    MemoryScopeRef,
    MemoryUnavailableError,
    MemoryValidationError,
)

from omweb.memory.imports import (
    build_package_payload,
    write_package_to_zip_bytes,
)
from omweb.memory import get_memory_service


logger = logging.getLogger("omweb.memory.api.export")


router = APIRouter()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_MAX_EXPORT_ENTRIES: int = 10_000
"""Hard ceiling on entries returned by a single export.

Matches the adapter-level limit in ``sqlite_adapter`` so that
export and import round-trip with the same shape. A larger
export would produce a multi-hundred-MB zip that neither the
browser nor the download endpoint can usefully handle."""


_ACCEPTED_FORMATS: frozenset = frozenset({"zip", "json"})
_ACCEPTED_STATUS_FILTERS: frozenset = frozenset({"active", "all"})


#: Characters that must not appear in a filename on Windows.
#: ``:`` and control chars are already rejected by MemoryScopeRef,
#: but ``/`` and ``\\`` are legal in scope_ids (path-like project
#: ids) and must be sanitized here.
_FILENAME_INVALID_RE: re.Pattern = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')


# ---------------------------------------------------------------------------
# Scope ref construction
# ---------------------------------------------------------------------------


def _build_scope_ref(
    scope_type: str,
    scope_id: str,
) -> MemoryScopeRef:
    """Build a ``MemoryScopeRef`` from query params.

    Same validation rules as the import router's
    ``_build_scope_ref``. Kept as a local function (not shared)
    because the two routers import different helper modules --
    sharing would require a third module for two ~30-line
    functions.
    """
    if not isinstance(scope_type, str) or not scope_type.strip():
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "missing_scope_type",
                "message": (
                    "'scope_type' is required. Expected one of: "
                    f"{sorted(s.value for s in MemoryScope)}"
                ),
            },
        )

    if not isinstance(scope_id, str) or not scope_id.strip():
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "missing_scope_id",
                "message": "'scope_id' is required (non-empty string).",
            },
        )

    try:
        resolved_scope_type = MemoryScope(scope_type.strip().lower())
    except ValueError:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_scope_type",
                "message": (
                    f"unknown scope_type {scope_type!r}; "
                    f"expected one of "
                    f"{sorted(s.value for s in MemoryScope)}"
                ),
            },
        ) from None

    try:
        return MemoryScopeRef(
            scope_type=resolved_scope_type,
            scope_id=scope_id.strip(),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_scope_ref",
                "message": str(exc),
            },
        ) from exc


# ---------------------------------------------------------------------------
# Format / status validation
# ---------------------------------------------------------------------------


def _validate_format(format_value: str) -> str:
    """Validate the ``format`` query parameter.

    Returns the canonical lowercase value. Any value outside
    ``_ACCEPTED_FORMATS`` raises 400 with a helpful message.
    """
    if not isinstance(format_value, str):
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_format",
                "message": (
                    f"'format' must be one of "
                    f"{sorted(_ACCEPTED_FORMATS)}"
                ),
            },
        )
    normalized = format_value.strip().lower()
    if normalized not in _ACCEPTED_FORMATS:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_format",
                "message": (
                    f"unknown format {format_value!r}; "
                    f"expected one of {sorted(_ACCEPTED_FORMATS)}"
                ),
            },
        )
    return normalized


def _validate_status_filter(status_value: str) -> str:
    """Validate the ``status`` query parameter."""
    if not isinstance(status_value, str):
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_status",
                "message": (
                    f"'status' must be one of "
                    f"{sorted(_ACCEPTED_STATUS_FILTERS)}"
                ),
            },
        )
    normalized = status_value.strip().lower()
    if normalized not in _ACCEPTED_STATUS_FILTERS:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_status",
                "message": (
                    f"unknown status {status_value!r}; "
                    f"expected one of "
                    f"{sorted(_ACCEPTED_STATUS_FILTERS)}"
                ),
            },
        )
    return normalized


def _validate_limit(limit_value: int) -> int:
    """Clamp ``limit`` to ``_MAX_EXPORT_ENTRIES``."""
    if not isinstance(limit_value, int):
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_limit",
                "message": "'limit' must be an integer.",
            },
        )
    if limit_value < 1:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_limit",
                "message": "'limit' must be at least 1.",
            },
        )
    return min(limit_value, _MAX_EXPORT_ENTRIES)


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------


def _sanitize_for_filename(text: str) -> str:
    """Replace filename-invalid chars with ``_``.

    The output is guaranteed to be a valid filename on Windows,
    macOS, and Linux.
    """
    sanitized = _FILENAME_INVALID_RE.sub("_", text or "")
    # Strip leading/trailing whitespace and dots -- some
    # filesystems silently truncate those.
    sanitized = sanitized.strip(" .")
    return sanitized or "unnamed"


def _build_filename(
    scope_ref: MemoryScopeRef,
    fmt: str,
    *,
    now: Optional[datetime] = None,
) -> str:
    """Generate the download filename.

    Format: ``memory-export-<type>-<id>-<YYYYMMDD>.<ext>``
    """
    if now is None:
        now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y%m%d")
    ext = "zip" if fmt == "zip" else "json"
    safe_id = _sanitize_for_filename(scope_ref.scope_id)
    safe_type = _sanitize_for_filename(scope_ref.scope_type.value)
    return f"memory-export-{safe_type}-{safe_id}-{date_str}.{ext}"


def _content_disposition(filename: str) -> str:
    """Build an RFC 6266 ``Content-Disposition`` value.

    Provides both the ASCII-safe ``filename`` and the
    UTF-8 ``filename*`` variant. Modern browsers prefer the
    latter (which preserves non-ASCII characters); older
    clients fall back to the former (already sanitized by the
    caller, so it is byte-safe).
    """
    ascii_fallback = filename.encode("ascii", "replace").decode(
        "ascii"
    ).replace("?", "_")
    utf8_encoded = quote(filename, safe="")
    return (
        f'attachment; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{utf8_encoded}"
    )


# ---------------------------------------------------------------------------
# Error mapping (mirrors the import router)
# ---------------------------------------------------------------------------


def _error_response(
    exc: BaseException,
    *,
    operation: str,
) -> JSONResponse:
    """Map a memory-subsystem exception to an HTTP response."""
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
            "Export %s hit backend failure: %s", operation, exc
        )
        return JSONResponse(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "error",
                "operation": operation,
                "error": "MemoryUnavailableError",
                "message": (
                    "Memory backend unavailable. Try again shortly."
                ),
            },
        )

    if isinstance(exc, TypeError):
        logger.error(
            "Export %s hit a programmer error: %s",
            operation,
            exc,
            exc_info=True,
        )
        return JSONResponse(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "status": "error",
                "operation": operation,
                "error": "InternalError",
                "message": "Internal error during export.",
            },
        )

    logger.exception(
        "Export %s failed unexpectedly: %s", operation, exc
    )
    return JSONResponse(
        status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "status": "error",
            "operation": operation,
            "error": type(exc).__name__,
            "message": "Internal error during export.",
        },
    )


# ---------------------------------------------------------------------------
# Mode enforcement
# ---------------------------------------------------------------------------


def _enforce_mode_export(mode: MemoryMode) -> None:
    """Reject export when the memory subsystem is DISABLED.

    READ_ONLY and READ_WRITE both permit export -- export is a
    READ. See the module docstring for the (different-from-import)
    rationale.
    """
    if mode == MemoryMode.DISABLED:
        raise MemoryValidationError(
            "Memory is disabled; cannot export. Enable memory (set "
            "mode to 'read_only' or 'read_write') in Settings "
            "before exporting.",
            field_name="mode",
            context={"mode": mode.value},
        )


# ---------------------------------------------------------------------------
# Core export function
# ---------------------------------------------------------------------------


async def _fetch_entries_for_export(
    scope_ref: MemoryScopeRef,
    limit: int,
    status_filter: str,
) -> tuple:
    """Fetch entries, with truncation signaling.

    Returns
    -------
    Tuple[list[MemoryEntry], int, bool]
        ``(entries, total_matched_before_truncation, truncated)``

    Raises
    ------
    MemoryUnavailableError
        When the backend is down.
    """
    service = get_memory_service()
    provider = service.provider

    # Fetch one extra so we can detect "there were more rows"
    # without a second COUNT query. If the extra row is
    # present, we know we truncated and can report the count as
    # ``limit + 1 or more``.
    fetch_limit = limit + 1

    try:
        entries = await provider.search(
            query="",
            scope_refs=[str(scope_ref)],
            limit=fetch_limit,
            status=status_filter,
        )
    except MemoryUnavailableError:
        raise

    total_matched = len(entries)
    truncated = False
    if total_matched > limit:
        truncated = True
        entries = entries[:limit]

    return entries, total_matched, truncated


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get(
    "",
    summary="Export memory entries for a scope",
    response_model=None,
)
async def export_endpoint(
    # ``Optional[str] = Query(None)`` instead of ``str = Query(...)``
    # -- the plain required form makes FastAPI return 422 with its
    # OWN error shape BEFORE our handler runs. That defeats the
    # consistent error envelope this router (and its twin, the
    # import router) otherwise produce. Passing ``None`` through
    # to ``_build_scope_ref`` lets the handler raise the same
    # structured 400 response the import router raises, so
    # clients that parse one router's errors can parse the other's
    # identically.
    scope_type: Optional[str] = Query(
        None,
        description="MemoryScope value (user|project|chat|agent)",
    ),
    scope_id: Optional[str] = Query(
        None, description="The concrete owner of the scope"
    ),
    format: str = Query(
        "zip",
        description="Output format: 'zip' (default) or 'json'",
    ),
    status: str = Query(
        "all",
        description="Status filter: 'active' or 'all' (default)",
    ),
    limit: int = Query(
        _MAX_EXPORT_ENTRIES,
        description=(
            f"Maximum entries to export (1-{_MAX_EXPORT_ENTRIES})."
        ),
    ),
) -> Response:
    """Export a scope's memory entries as a package or JSON.

    See the module docstring for the full parameter and response
    contract.

    Returns
    -------
    ``Response``
        * ``format=zip`` -> ``application/zip`` with
          ``Content-Disposition: attachment; filename="..."``.
        * ``format=json`` -> ``application/json`` with the raw
          ``{"manifest": {...}, "memories": [...]}`` object.
        * Error cases -> ``JSONResponse`` with the mapped status
          code and structured error body.
    """
    # ----- Validate everything BEFORE touching the backend ----------
    #
    # All query-param validation happens first so a malformed
    # request is rejected without a wasted DB query. This is
    # especially important when the caller is doing many
    # iterations (polling a specific scope), because a typo in
    # ``format`` should fail fast at the HTTP layer, not in the
    # middle of a read.
    scope_ref = _build_scope_ref(scope_type, scope_id)
    fmt = _validate_format(format)
    status_filter = _validate_status_filter(status)
    effective_limit = _validate_limit(limit)

    try:
        service = get_memory_service()
        _enforce_mode_export(service.settings.mode)
    except MemoryValidationError as exc:
        return _error_response(exc, operation="export")
    except RuntimeError as exc:
        # get_memory_service() raises RuntimeError when the
        # process-wide singleton is missing. Treat as a
        # programmer/startup error -- 500, not 503.
        return _error_response(exc, operation="export")

    try:
        entries, total_matched, truncated = (
            await _fetch_entries_for_export(
                scope_ref=scope_ref,
                limit=effective_limit,
                status_filter=status_filter,
            )
        )
    except (MemoryValidationError, MemoryUnavailableError) as exc:
        return _error_response(exc, operation="export")
    except Exception as exc:
        return _error_response(exc, operation="export")

    # ----- Build payload -------------------------------------------
    manifest_data, memory_lines = build_package_payload(entries)

    headers = {
        "X-Export-Scope": str(scope_ref),
        "X-Export-Entries": str(len(entries)),
        "X-Export-Truncated": "true" if truncated else "false",
        "X-Export-Total-Matched": str(total_matched),
    }

    # ----- JSON response ------------------------------------------
    if fmt == "json":
        filename = _build_filename(scope_ref, fmt="json")
        return JSONResponse(
            status_code=http_status.HTTP_200_OK,
            content={
                "manifest": manifest_data,
                "memories": memory_lines,
            },
            headers={
                **headers,
                "Content-Disposition": _content_disposition(filename),
            },
        )

    # ----- Zip response -------------------------------------------
    #
    # ``write_package_to_zip_bytes`` is synchronous and runs in
    # the request's thread. The zip is bounded by
    # ``_MAX_EXPORT_ENTRIES`` entries, so the CPU cost is
    # predictable (~100-200ms for 10k typical entries). Thread
    # offload was considered but rejected: the request handler
    # is already inside an async task, and the GIL serializes
    # the CPU work anyway -- offloading to a thread would add
    # overhead without freeing the loop.
    try:
        zip_bytes = write_package_to_zip_bytes(entries)
    except Exception as exc:
        return _error_response(exc, operation="export")

    filename = _build_filename(scope_ref, fmt="zip")
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            **headers,
            "Content-Disposition": _content_disposition(filename),
            "Content-Length": str(len(zip_bytes)),
        },
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = ["router"]
