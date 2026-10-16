"""
Tool bundle HTTP endpoints.

Exposes the BundleManager to the frontend:

    POST   /upload-bundle                 upload + install a .zip bundle
    GET    /bundles                       list installed bundles
    GET    /bundles/{bundle_id}           detailed bundle metadata
    DELETE /bundles/{bundle_id}           remove a bundle (force optional)
    POST   /bundles/{bundle_id}/activate  rollback to a previous version
    GET    /bundles/{bundle_id}/health    filesystem health report

All endpoints return JSON. Errors are structured: { "error": ..., "code": ..., "details": {...} }.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from omweb.tools.bundles import (
    BundleError,
    BundleManifestError,
    BundleSecurityError,
    BundleStorageError,
    BundleValidationError,
)
from omweb.tools.bundles.manager import BundleManager

logger = logging.getLogger("omweb.routers.store.bundles")

router = APIRouter()


# ---------------------------------------------------------------------------
# Manager dependency
# ---------------------------------------------------------------------------

_manager: Optional[BundleManager] = None


def get_bundle_manager() -> BundleManager:
    """
    Return the process-wide BundleManager singleton, creating it lazily on
    first access. Kept outside of a FastAPI dependency so that other modules
    (tool_registry wiring at startup) can call it before routes are hit.
    """
    global _manager
    if _manager is None:
        from pathlib import Path

        backend_root = Path(__file__).resolve().parent.parent.parent.parent
        store_root = backend_root / "storage" / "store" / "tools"
        store_root.mkdir(parents=True, exist_ok=True)
        _manager = BundleManager(store_root)
        logger.info("BundleManager singleton initialized at %s", store_root)
    return _manager


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ActivateVersionRequest(BaseModel):
    version: str = Field(..., min_length=1, description="Version to activate")


class BundleHealthResponse(BaseModel):
    id: str
    version: str
    hash: str
    status: str
    checks: List[Dict[str, Any]]


# ---------------------------------------------------------------------------
# Error helper
# ---------------------------------------------------------------------------

def _error_payload(exc: Exception, *, http_status: int) -> HTTPException:
    """Normalize any BundleError into a structured HTTPException."""
    if isinstance(exc, BundleError):
        return HTTPException(status_code=http_status, detail=exc.to_dict())
    return HTTPException(
        status_code=500,
        detail={
            "error": str(exc),
            "code": "INTERNAL_ERROR",
            "details": {"exception_type": type(exc).__name__},
        },
    )


# ---------------------------------------------------------------------------
# 1. Upload + install
# ---------------------------------------------------------------------------

@router.post("/upload-bundle")
async def upload_bundle(
    file: UploadFile = File(..., description="Bundle .zip file"),
    install_requirements: bool = Form(True),
):
    """
    Upload and install a tool bundle.

    The uploaded file must be a .zip containing a valid `manifest.json` at
    its root plus the declared `entry_point` file. Any additional helper
    files are copied verbatim.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail={
            "error": "Missing filename",
            "code": "BUNDLE_MISSING_FILENAME",
            "details": {},
        })

    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail={
            "error": "Only .zip bundles are accepted by this endpoint.",
            "code": "BUNDLE_WRONG_FILE_TYPE",
            "details": {"filename": file.filename},
        })

    try:
        content = await file.read()
    except Exception as exc:
        raise HTTPException(status_code=400, detail={
            "error": f"Could not read uploaded file: {exc}",
            "code": "BUNDLE_UPLOAD_READ_FAILED",
            "details": {},
        })

    if not content:
        raise HTTPException(status_code=400, detail={
            "error": "Uploaded file is empty",
            "code": "BUNDLE_EMPTY_UPLOAD",
            "details": {},
        })

    manager = get_bundle_manager()

    try:
        result = manager.install_bundle(
            zip_bytes=content,
            source_filename=file.filename,
            install_requirements=install_requirements,
        )
    except (BundleValidationError, BundleManifestError, BundleSecurityError) as exc:
        logger.info("Bundle install rejected: %s", exc)
        raise _error_payload(exc, http_status=400)
    except BundleStorageError as exc:
        logger.warning("Bundle install storage error: %s", exc)
        raise _error_payload(exc, http_status=500)
    except BundleError as exc:
        logger.warning("Bundle install error: %s", exc)
        raise _error_payload(exc, http_status=400)
    except Exception as exc:
        logger.exception("Unexpected bundle install error")
        raise HTTPException(status_code=500, detail={
            "error": str(exc),
            "code": "BUNDLE_INSTALL_FAILED",
            "details": {"exception_type": type(exc).__name__},
        })

    return {
        "status": "success",
        "bundle": result.model_dump(),
        "registry": {"total_bundles": len(manager.list_bundles())},
    }


# ---------------------------------------------------------------------------
# 2. List bundles
# ---------------------------------------------------------------------------

@router.get("/bundles")
async def list_bundles():
    """Return every installed bundle with its active version and metadata."""
    manager = get_bundle_manager()
    return {
        "bundles": [entry.model_dump() for entry in manager.list_bundles()],
        "total": len(manager.list_bundles()),
    }


# ---------------------------------------------------------------------------
# 3. Bundle detail
# ---------------------------------------------------------------------------

@router.get("/bundles/{bundle_id}")
async def get_bundle_detail(bundle_id: str):
    manager = get_bundle_manager()
    entry = manager.get_bundle(bundle_id)
    if entry is None:
        raise HTTPException(status_code=404, detail={
            "error": f"Bundle {bundle_id!r} not found.",
            "code": "BUNDLE_NOT_FOUND",
            "details": {"bundle_id": bundle_id},
        })
    return {"bundle": entry.model_dump()}


# ---------------------------------------------------------------------------
# 4. Delete bundle
# ---------------------------------------------------------------------------

@router.delete("/bundles/{bundle_id}")
async def delete_bundle(
    bundle_id: str,
    force: bool = Query(False, description="Delete even if multiple versions exist"),
):
    manager = get_bundle_manager()
    try:
        deleted = manager.delete_bundle(bundle_id, force=force)
    except BundleError as exc:
        raise _error_payload(exc, http_status=400)

    if not deleted:
        raise HTTPException(status_code=404, detail={
            "error": f"Bundle {bundle_id!r} not found.",
            "code": "BUNDLE_NOT_FOUND",
            "details": {"bundle_id": bundle_id},
        })

    return {"status": "success", "bundle_id": bundle_id}


# ---------------------------------------------------------------------------
# 5. Activate version
# ---------------------------------------------------------------------------

@router.post("/bundles/{bundle_id}/activate")
async def activate_version(bundle_id: str, body: ActivateVersionRequest):
    manager = get_bundle_manager()
    try:
        ok = manager.activate_version(bundle_id, body.version)
    except BundleStorageError as exc:
        raise _error_payload(exc, http_status=500)

    if not ok:
        raise HTTPException(status_code=404, detail={
            "error": (
                f"Bundle {bundle_id!r} does not have version "
                f"{body.version!r} installed."
            ),
            "code": "BUNDLE_VERSION_NOT_FOUND",
            "details": {"bundle_id": bundle_id, "version": body.version},
        })

    return {
        "status": "success",
        "bundle_id": bundle_id,
        "active_version": body.version,
    }


# ---------------------------------------------------------------------------
# 6. Health
# ---------------------------------------------------------------------------

@router.get("/bundles/{bundle_id}/health")
async def bundle_health(bundle_id: str):
    manager = get_bundle_manager()
    report = manager.check_health(bundle_id)
    if report is None:
        raise HTTPException(status_code=404, detail={
            "error": f"Bundle {bundle_id!r} not found.",
            "code": "BUNDLE_NOT_FOUND",
            "details": {"bundle_id": bundle_id},
        })
    return {"health": report.model_dump()}
