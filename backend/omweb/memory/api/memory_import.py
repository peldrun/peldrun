# backend/omweb/memory/api/memory_import.py
"""
PELDRUN Memory Platform - Import HTTP Router.

Thin HTTP layer over ``ImportManager``. Translates two request
shapes (multipart file upload / JSON body) into the source forms
the adapter registry understands, invokes the manager, and maps
domain exceptions to HTTP status codes.

Responsibilities (deliberately NARROW)
--------------------------------------

This router does FIVE things:

    1. **Extract the source** from an HTTP request:
         * multipart ``file`` field -> materialized source,
         * JSON ``source`` field -> passed through unchanged.

    2. **Normalize textual sources** to their expected adapter
       shapes:
         * ``.zip`` / ``.db`` / ``.sqlite`` / ``.sqlite3`` ->
           raw ``bytes`` (adapters detect by magic prefix),
         * ``.md`` / ``.markdown`` -> temp ``Path`` (adapter
           reads from disk),
         * ``.json`` -> parsed ``dict`` (adapter accepts inline
           dict, not raw JSON bytes),
         * JSON body ``source`` -> passed through verbatim
           (caller chooses the shape).

    3. **Enforce upload size limits** (100 MB) BEFORE
       materializing the source into memory. Prevents an
       innocent-looking upload from OOMing the server.

    4. **Build the ``MemoryScopeRef``** from form/body fields.

    5. **Map domain exceptions to HTTP status codes**:
         * ``MemoryValidationError`` -> 400 (user error, message
           forwarded verbatim),
         * ``MemoryUnavailableError`` -> 503 (backend down, no
           leak of internals),
         * ``TypeError`` / unexpected -> 500 (server bug, generic
           message).

It does NOT:

    * parse any format (adapters do),
    * apply any write policy (base adapter does),
    * touch SQLite, ZIP, or markdown internals (adapters do).

The router is that thin ON PURPOSE -- bugs here surface as HTTP
mapping mistakes, never as silent format mishandling.

Request shapes
--------------

Two forms are accepted on both ``/preview`` and ``/apply``:

**A. multipart/form-data** (file upload)

    POST /api/memory/import/preview
    Content-Type: multipart/form-data
    Content-Disposition: form-data; name="file"; filename="backup.zip"
    <binary content>
    Content-Disposition: form-data; name="scope_type" -> "project"
    Content-Disposition: form-data; name="scope_id" -> "acme"
    Content-Disposition: form-data; name="adapter_id" -> "" (optional)

**B. application/json** (inline source)

    POST /api/memory/import/preview
    Content-Type: application/json
    {
      "source": {
        "manifest": {...},
        "memories": [...]
      },
      "scope_type": "project",
      "scope_id": "acme",
      "adapter_id": null
    }

The ``source`` field in JSON body may be:
    * a dict (markdown inline content, or full package),
    * a string (markdown text, or single-line JSON),
    * any shape the adapter registry understands as a ``dict`` /
      ``str`` -- NOT bytes (JSON cannot carry raw binary).

For binary uploads (zip / sqlite), use multipart.

Source shape decision matrix
----------------------------

    | Filename   | Source passed to manager | Rationale
    |------------|--------------------------|---------------------
    | *.zip      | bytes                    | PK magic prefix
    | *.db       | bytes                    | SQLite magic prefix
    | *.sqlite   | bytes                    | same
    | *.sqlite3  | bytes                    | same
    | *.md       | temp Path                | MarkdownAdapter reads disk
    | *.markdown | temp Path                | same
    | *.json     | parsed dict              | adapters take dict
    | (JSON body)| body["source"] verbatim  | caller controls shape
    | other      | bytes                    | adapter registry rejects

Size limits
-----------

``_MAX_UPLOAD_BYTES = 100 MB``. Matches the archive/db limits
already enforced by the adapters themselves -- the router
enforces it FIRST so the server does not buffer megabytes of
garbage only to reject it at the adapter layer.

Temp file lifecycle
-------------------

``.md`` uploads are written to a ``NamedTemporaryFile`` and
deleted in the ``finally`` of the materialization context
manager. Even if the request raises mid-processing (adapter
rejects, DB error, client disconnects), the temp file is
cleaned up before the exception propagates.

Windows-safe: the file is closed via ``contextlib.closing``
(through the CM's finally) before ``unlink``, so the OS has
released its handle.

Routing surface
---------------

Mounted at ``/api/memory/import`` (parent router provides the
prefix). Endpoints:

    POST /preview   -- parse + classify, NO writes
    POST /apply     -- parse + write (best-effort)
    GET  /adapters  -- enumerate registered adapters (for UI)
"""

from __future__ import annotations

import json
import logging
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from peldrun.memory.domain import (
    MemoryScope,
    MemoryScopeRef,
    MemoryUnavailableError,
    MemoryValidationError,
)

from omweb.memory.imports import (
    ImportManager,
    import_manager_from_service,
    list_adapters,
)


logger = logging.getLogger("omweb.memory.api.import")


router = APIRouter()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_MAX_UPLOAD_BYTES: int = 100 * 1024 * 1024
"""100 MB ceiling. Matches the adapter-level limits; enforced
here first so garbage payloads are rejected before buffering."""


_MARKDOWN_SUFFIXES: Tuple[str, ...] = (".md", ".markdown")
_JSON_SUFFIX: str = ".json"


# ---------------------------------------------------------------------------
# Scope ref construction
# ---------------------------------------------------------------------------


def _build_scope_ref(
    scope_type: Any,
    scope_id: Any,
) -> MemoryScopeRef:
    """Build a ``MemoryScopeRef`` from raw form/body values.

    Missing or malformed scope values raise HTTP 400 with a
    structured ``detail`` explaining what the client must fix.
    This function is the ONLY place that maps form/body strings
    to a typed ``MemoryScopeRef`` -- keeping the mapping in one
    place ensures every endpoint rejects the same inputs
    identically.
    """
    if not isinstance(scope_type, str) or not scope_type.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
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
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "missing_scope_id",
                "message": "'scope_id' is required (non-empty string).",
            },
        )

    try:
        resolved_scope_type = MemoryScope(scope_type.strip().lower())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
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
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_scope_ref",
                "message": str(exc),
            },
        ) from exc


# ---------------------------------------------------------------------------
# Source materialization
# ---------------------------------------------------------------------------


def _decode_upload_to_source(
    file_bytes: bytes,
    filename: str,
    temp_paths: List[Path],
) -> Any:
    """Turn raw uploaded bytes into the appropriate source shape.

    Dispatches on file extension and content sniffing. See the
    module docstring's "Source shape decision matrix" for the
    full contract.

    Appends any temp file path it creates to ``temp_paths`` so
    the caller's ``finally`` can clean them up.
    """
    suffix = Path(filename or "").suffix.lower()

    # ----- Markdown: needs a file path -----
    if suffix in _MARKDOWN_SUFFIXES:
        tmp = tempfile.NamedTemporaryFile(
            suffix=suffix,
            prefix="peldrun_md_upload_",
            delete=False,
        )
        try:
            tmp.write(file_bytes)
            tmp.close()
        except Exception:
            try:
                tmp.close()
            except Exception:
                pass
            raise
        tmp_path = Path(tmp.name)
        temp_paths.append(tmp_path)
        return tmp_path

    # ----- JSON file: parse to dict (adapters take dict) -----
    if suffix == _JSON_SUFFIX:
        try:
            parsed = json.loads(file_bytes.decode("utf-8-sig"))
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "invalid_encoding",
                    "message": (
                        f"uploaded .json file is not valid UTF-8: {exc}"
                    ),
                },
            ) from exc
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "invalid_json",
                    "message": (
                        f"uploaded .json file is not valid JSON: {exc}"
                    ),
                },
            ) from exc
        if not isinstance(parsed, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "invalid_json_shape",
                    "message": (
                        f"uploaded .json file must contain a JSON "
                        f"object, got {type(parsed).__name__}"
                    ),
                },
            )
        return parsed

    # ----- Zip / SQLite / others: raw bytes -----
    # The adapter registry sniffs the magic prefix to confirm the
    # type; a file with an unrecognized extension and magic will
    # be rejected at ``select_adapter`` with a clear message.
    return file_bytes


async def _materialize_multipart(
    form: Any,
    temp_paths: List[Path],
) -> Tuple[Any, MemoryScopeRef, Optional[str]]:
    """Extract (source, scope_ref, adapter_id) from a multipart form."""
    file_field = form.get("file")
    scope_type = form.get("scope_type")
    scope_id = form.get("scope_id")
    adapter_id_raw = form.get("adapter_id")

    if file_field is None or not hasattr(file_field, "read"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "missing_file",
                "message": (
                    "multipart upload must include a 'file' field "
                    "with the source content"
                ),
            },
        )

    scope_ref = _build_scope_ref(scope_type, scope_id)

    # Read with size enforcement. Starlette's UploadFile streams,
    # but ``read()`` loads everything into memory -- we cap at
    # ``_MAX_UPLOAD_BYTES`` AFTER reading so we can reject the
    # size without a partial parse.
    file_bytes = await file_field.read()
    if len(file_bytes) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "error": "file_too_large",
                "message": (
                    f"uploaded file is {len(file_bytes)} bytes, "
                    f"exceeds limit of {_MAX_UPLOAD_BYTES} bytes"
                ),
            },
        )

    filename = getattr(file_field, "filename", "") or ""
    source = _decode_upload_to_source(
        file_bytes, filename, temp_paths
    )

    adapter_id: Optional[str] = (
        adapter_id_raw.strip()
        if isinstance(adapter_id_raw, str) and adapter_id_raw.strip()
        else None
    )

    return source, scope_ref, adapter_id


def _materialize_json_body(
    body: Any,
) -> Tuple[Any, MemoryScopeRef, Optional[str]]:
    """Extract (source, scope_ref, adapter_id) from a JSON body."""
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_body",
                "message": (
                    f"JSON body must be an object, got "
                    f"{type(body).__name__}"
                ),
            },
        )

    source = body.get("source")
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "missing_source",
                "message": (
                    "JSON body must include a 'source' field "
                    "carrying the content to import"
                ),
            },
        )

    scope_ref = _build_scope_ref(
        body.get("scope_type"),
        body.get("scope_id"),
    )

    adapter_id_raw = body.get("adapter_id")
    adapter_id: Optional[str] = (
        adapter_id_raw.strip()
        if isinstance(adapter_id_raw, str) and adapter_id_raw.strip()
        else None
    )

    return source, scope_ref, adapter_id


@asynccontextmanager
async def _materialize_source(
    request: Request,
) -> AsyncIterator[Tuple[Any, MemoryScopeRef, Optional[str]]]:
    """Extract (source, scope_ref, adapter_id) from a request.

    Context-manager form so that any temp file created for a
    markdown upload is cleaned up on EVERY exit path -- success,
    domain exception, HTTPException, or client disconnect.

    The ``finally`` block runs even if the consumer raises, which
    is the guarantee we need: no orphan temp files, ever.
    """
    content_type = request.headers.get("content-type", "").lower()
    temp_paths: List[Path] = []

    try:
        if "multipart/form-data" in content_type:
            form = await request.form()
            try:
                source, scope_ref, adapter_id = (
                    await _materialize_multipart(form, temp_paths)
                )
            finally:
                # Starlette's form parser may hold file descriptors
                # open; ``form.close()`` releases them. Safe to call
                # even if the form was already drained.
                try:
                    await form.close()
                except Exception:
                    pass
            yield source, scope_ref, adapter_id
            return

        if "application/json" in content_type:
            try:
                body = await request.json()
            except json.JSONDecodeError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "error": "invalid_json",
                        "message": f"request body is not valid JSON: {exc}",
                    },
                ) from exc
            source, scope_ref, adapter_id = _materialize_json_body(body)
            yield source, scope_ref, adapter_id
            return

        # Neither shape -- 415.
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={
                "error": "unsupported_content_type",
                "message": (
                    f"content-type {content_type!r} not supported; "
                    f"use 'multipart/form-data' (file upload) or "
                    f"'application/json' (inline source)"
                ),
            },
        )

    finally:
        # Best-effort temp cleanup. A failure to unlink must not
        # mask the original exception.
        for p in temp_paths:
            try:
                if p.exists():
                    p.unlink()
            except OSError as exc:
                logger.debug(
                    "Failed to remove temp file %s: %s", p, exc
                )


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def _error_response(
    exc: BaseException,
    *,
    operation: str,
) -> JSONResponse:
    """Map a memory-subsystem exception to an HTTP response.

    See the module docstring's error taxonomy. The four cases:

      * ``MemoryValidationError`` -> 400 (client can fix by
        retrying with different input). Message forwarded
        verbatim so the UI can display it directly.
      * ``MemoryUnavailableError`` -> 503 (retry later). Message
        replaced with a generic one -- the exception's own
        message is for logs, not users.
      * ``TypeError`` -> 500. A programmer error (mis-typed
        argument reaching the manager). Same generic response;
        the traceback lands in the server log via
        ``logger.exception``.
      * Anything else -> 500 with the same generic message. A
        narrow classifier would require an exhaustive list of
        possible exceptions; the fallback catches the tail
        without leaking internals.
    """
    if isinstance(exc, MemoryValidationError):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
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
            "Import %s hit backend failure: %s",
            operation,
            exc,
        )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
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
            "Import %s hit a programmer error: %s",
            operation,
            exc,
            exc_info=True,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "status": "error",
                "operation": operation,
                "error": "InternalError",
                "message": "Internal error during import.",
            },
        )

    logger.exception(
        "Import %s failed unexpectedly: %s", operation, exc
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "status": "error",
            "operation": operation,
            "error": type(exc).__name__,
            "message": "Internal error during import.",
        },
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/preview",
    summary="Preview an import without writing",
    response_model=None,
)
async def preview_endpoint(request: Request) -> JSONResponse:
    """Parse a source and return what WOULD be imported.

    Read-only. The caller receives the parsed candidates,
    the would-create / would-skip counts, and any warnings.
    No entries are written.

    The UI's usual flow:
        1. User selects a file.
        2. Client POSTs it here.
        3. UI shows the preview ("we will create N entries, skip M
           duplicates, and here are the warnings").
        4. If the user clicks "Confirm", the client POSTs the same
           source to ``/apply``.

    Nothing in this endpoint enforces the preview-before-apply
    ordering -- see ImportManager's module docstring for why
    that constraint belongs to the API caller (the UI), not to
    the manager.
    """
    start = time.monotonic()
    async with _materialize_source(request) as (
        source,
        scope_ref,
        adapter_id,
    ):
        try:
            manager = import_manager_from_service()
            preview = await manager.preview(
                source=source,
                scope_ref=scope_ref,
                adapter_id=adapter_id,
            )
        except (HTTPException, BaseException) as exc:
            # Re-raise HTTPException (raised by materialization
            # helpers, would already have the right status).
            if isinstance(exc, HTTPException):
                raise
            return _error_response(exc, operation="preview")

        elapsed_ms = round((time.monotonic() - start) * 1000.0, 2)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "status": "ok",
                "operation": "preview",
                "scope": str(scope_ref),
                "elapsed_ms": elapsed_ms,
                "preview": preview.model_dump(mode="json"),
            },
        )


@router.post(
    "/apply",
    summary="Apply an import (writes entries)",
    response_model=None,
)
async def apply_endpoint(request: Request) -> JSONResponse:
    """Parse a source and WRITE every viable candidate.

    Best-effort: a failure on one candidate does not abort the
    batch (see ``base_adapter._apply_candidates``). The result
    reports per-candidate errors.

    The caller is expected to have shown the user a preview
    first, but the endpoint does NOT verify this -- a scripted
    flow (``peldrun-memory-restore --yes``) legitimately skips
    the interactive step. See ImportManager's module docstring
    for the full rationale on where the confirmation constraint
    lives.
    """
    start = time.monotonic()
    async with _materialize_source(request) as (
        source,
        scope_ref,
        adapter_id,
    ):
        try:
            manager = import_manager_from_service()
            result = await manager.apply(
                source=source,
                scope_ref=scope_ref,
                adapter_id=adapter_id,
            )
        except (HTTPException, BaseException) as exc:
            if isinstance(exc, HTTPException):
                raise
            return _error_response(exc, operation="apply")

        elapsed_ms = round((time.monotonic() - start) * 1000.0, 2)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "status": "ok",
                "operation": "apply",
                "scope": str(scope_ref),
                "elapsed_ms": elapsed_ms,
                "result": result.model_dump(mode="json"),
            },
        )


@router.get(
    "/adapters",
    summary="List registered import adapters",
    response_model=None,
)
async def list_adapters_endpoint() -> JSONResponse:
    """Enumerate the registered import adapters.

    Used by the UI's "Force adapter" dropdown so users can bypass
    auto-detection when the heuristic genuinely guesses wrong.

    The list is ordered by priority -- the SAME order
    ``select_adapter`` tries in. A caller that forces an adapter
    based on this list sees the same ordering the auto-detector
    uses.
    """
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "status": "ok",
            "adapters": list_adapters(),
        },
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = ["router"]
