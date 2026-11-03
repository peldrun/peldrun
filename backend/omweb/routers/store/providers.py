# backend/omweb/routers/store/providers.py
"""
Provider Package HTTP API (M9 administrative layer).

Exposes the ``ProviderPackageManager`` and ``ProviderPackageCatalog``
over REST so the Store's "Providers" section (M10) can:

    * Browse the catalog of installable packages.
    * Upload a ZIP and install it.
    * Install a package from the catalog with one click.
    * Enable / disable, activate a specific version, uninstall.
    * Inspect health and view the full manifest.

Routing order
-------------

FastAPI matches routes in registration order. All literal single-segment
paths (``/catalog``, ``/upload``) MUST be declared BEFORE the catch-all
``/{provider_id}`` route, otherwise the latter would swallow them and
interpret ``"catalog"`` or ``"upload"`` as a provider ID. This mirrors
the ordering discipline established in ``store_rtr.py`` for
``/tools/bundles`` vs ``/tools/{tool_id}``.

Error mapping
-------------

Every ``ProviderPackageError`` subclass is translated into a structured
HTTP response via ``_to_http_exception``:

    NotFoundError        → 404
    ConflictError        → 409
    SecurityError        → 400  (code: PROVIDER_PACKAGE_SECURITY_VIOLATION)
    ValidationError      → 400
    ManifestError        → 400
    CatalogError         → 400
    EnvironmentError     → 500
    StorageError         → 500
    ProviderPackageError → 500  (fallback for any unhandled subclass)

The response body is always ``{"detail": {"error", "code", "details"}}``
so the frontend can render a precise error banner without parsing
strings.

Naming note
-----------

The path segment ``{provider_id}`` in ``/install-from-catalog`` refers to
the CATALOG ITEM's ``id``, not an already-installed package's
``provider_id``. The two often coincide (catalog item ``deepseek``
installs a package whose manifest ``id`` is also ``deepseek``), but they
are distinct namespaces: the catalog ID is what ``catalog.get_item()``
accepts, while the installed package's ``id`` comes from the manifest.

Mounting
--------

``backend/omweb/main.py``::

    from omweb.routers.store import providers as store_providers
    app.include_router(
        store_providers.router,
        prefix="/api/store/providers",
        tags=["store", "providers"],
    )

See also
--------

    * ``backend/omweb/provider_packages/package_manager.py``
    * ``backend/omweb/provider_packages/catalog.py``
    * ``backend/omweb/provider_packages/errors.py``
    * ``backend/omweb/routers/store/bundles.py`` — parallel tool-bundle router
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from omweb.provider_packages.catalog import (
    get_provider_package_catalog,
    installability_reason,
    is_installable,
)
from omweb.provider_packages.errors import (
    ProviderPackageCatalogError,
    ProviderPackageConflictError,
    ProviderPackageEnvironmentError,
    ProviderPackageError,
    ProviderPackageManifestError,
    ProviderPackageNotFoundError,
    ProviderPackageSecurityError,
    ProviderPackageStorageError,
    ProviderPackageValidationError,
)
from omweb.provider_packages.package_manager import (
    provider_package_manager,
)
from omweb.provider_packages.schema import (
    MANIFEST_FILENAME,
    ProviderPackageEntry,
)


logger = logging.getLogger("omweb.routers.store.providers")

router = APIRouter()


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def _to_http_exception(exc: ProviderPackageError) -> HTTPException:
    """
    Translate a ``ProviderPackageError`` into an ``HTTPException`` with
    the correct status code and a structured ``detail`` body.

    The body is always ``exc.to_dict()`` — ``{error, code, details}`` —
    so the frontend can render field-level errors without parsing
    free-text messages.
    """
    # 404 — package or version not found.
    if isinstance(exc, ProviderPackageNotFoundError):
        return HTTPException(status_code=404, detail=exc.to_dict())

    # 409 — install conflict (builtin collision, hash mismatch).
    if isinstance(exc, ProviderPackageConflictError):
        return HTTPException(status_code=409, detail=exc.to_dict())

    # 400 — client supplied something the server rejected.
    # SecurityError is deliberately a 400 (not 500): the user must fix
    # their package. The distinct code lets the UI show a stronger
    # warning banner than a plain validation error.
    if isinstance(
        exc,
        (
            ProviderPackageSecurityError,
            ProviderPackageValidationError,
            ProviderPackageManifestError,
            ProviderPackageCatalogError,
        ),
    ):
        return HTTPException(status_code=400, detail=exc.to_dict())

    # 500 — server-side issue (disk, venv, pip).
    if isinstance(
        exc,
        (
            ProviderPackageEnvironmentError,
            ProviderPackageStorageError,
        ),
    ):
        return HTTPException(status_code=500, detail=exc.to_dict())

    # 500 — anything else is unexpected; still structured.
    return HTTPException(status_code=500, detail=exc.to_dict())


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _entry_to_dict(entry: ProviderPackageEntry) -> Dict[str, Any]:
    """Serialize a ``ProviderPackageEntry`` for the API response."""
    return entry.model_dump()


def _try_read_manifest(
    provider_id: str, version: str
) -> Optional[Dict[str, Any]]:
    """
    Best-effort read of the on-disk ``manifest.json`` for a specific
    installed version. Returns ``None`` when the manifest file cannot
    be read (e.g. it was deleted after install, or the entry is
    inconsistent with the filesystem).

    Never raises — a missing manifest is a degraded state, not an API
    error, and the caller (``GET /{provider_id}?include_manifest=true``)
    is expected to render the entry even without the manifest.
    """
    try:
        manager_root = provider_package_manager.root
        manifest_path = (
            manager_root
            / provider_id
            / "versions"
            / version
            / MANIFEST_FILENAME
        )
        if not manifest_path.is_file():
            return None
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except Exception as exc:
        logger.debug(
            "Could not read manifest for %r@%r: %s",
            provider_id,
            version,
            exc,
        )
        return None


def _describe_catalog_permissions(
    raw_permissions: Dict[str, Any],
) -> List[str]:
    """
    Return human-readable warnings for a catalog item's permission block.

    Used by the UI to render the install confirmation dialog. Kept
    inline (rather than routing through ``permissions.PermissionSet``)
    because the catalog item carries the raw manifest dict, not a
    validated ``PackagePermissions`` model, and constructing the latter
    just to compute warnings would duplicate work.
    """
    warnings: List[str] = []
    network = raw_permissions.get("network")
    if network == "connection_endpoint":
        warnings.append(
            "May make outbound HTTP requests to the connection's "
            "configured endpoint. It cannot reach other hosts."
        )
    secrets = raw_permissions.get("secrets") or []
    if isinstance(secrets, list) and "connection.api_key" in secrets:
        warnings.append(
            "May read the API key for THIS connection only. It cannot "
            "access other connections' credentials."
        )
    return warnings


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ActivateVersionRequest(BaseModel):
    """Body for ``POST /{provider_id}/activate``."""

    version: str = Field(
        ...,
        min_length=1,
        description="The previously installed version to activate.",
    )


# ===========================================================================
# Listing endpoints — MUST come before the catch-all /{provider_id}
# ===========================================================================


@router.get("")
@router.get("/")
async def list_installed_providers(
    enabled_only: bool = Query(
        False,
        description="When true, return only providers whose enabled flag is set.",
    ),
) -> Dict[str, Any]:
    """
    List every installed Provider Package.

    Returns the full registry entry per provider — enough for the UI to
    render the card (name, description, version, transport, features,
    permissions, tags). Use ``GET /{provider_id}?include_manifest=true``
    to also fetch the raw manifest.
    """
    entries = provider_package_manager.list_packages(
        enabled_only=enabled_only
    )
    return {
        "status": "ok",
        "providers": [_entry_to_dict(e) for e in entries],
        "total": len(entries),
    }


# ---------------------------------------------------------------------------
# Catalog — literal paths, declared before /{provider_id}
# ---------------------------------------------------------------------------


@router.get("/catalog")
async def list_catalog_items() -> Dict[str, Any]:
    """
    Return every item in the merged Provider Package catalog.

    Each item includes:

        * The catalog metadata (name, description, version, features).
        * ``installable``: whether the item can be installed right now
          (requires a valid ``download_url`` AND ``checksum_sha256``).
        * ``installability_reason``: the human explanation of the above
          flag — designed to render directly in a badge tooltip.
        * ``permission_warnings``: the concrete strings to show in the
          install confirmation dialog.
    """
    catalog = get_provider_package_catalog()
    items = catalog.list_items()

    result: List[Dict[str, Any]] = []
    for item in items:
        raw = item.model_dump()
        raw["installable"] = is_installable(item)
        raw["installability_reason"] = installability_reason(item)
        raw["permission_warnings"] = _describe_catalog_permissions(
            item.permissions or {}
        )
        result.append(raw)

    return {
        "status": "ok",
        "catalog": result,
        "total": len(result),
    }


@router.get("/catalog/{item_id}")
async def get_catalog_item(item_id: str) -> Dict[str, Any]:
    """
    Return a single catalog item by ID.

    Same shape as one element of ``GET /catalog``. Returns 404 when the
    item does not exist — this lets the frontend request the details
    for a card the user clicked without re-fetching the entire catalog.
    """
    catalog = get_provider_package_catalog()
    item = catalog.get_item(item_id)
    if item is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": f"Catalog item '{item_id}' does not exist.",
                "code": "PROVIDER_PACKAGE_CATALOG_NOT_FOUND",
                "details": {"item_id": item_id},
            },
        )

    raw = item.model_dump()
    raw["installable"] = is_installable(item)
    raw["installability_reason"] = installability_reason(item)
    raw["permission_warnings"] = _describe_catalog_permissions(
        item.permissions or {}
    )
    return {"status": "ok", "item": raw}


@router.post("/catalog/refresh")
async def refresh_catalog() -> Dict[str, Any]:
    """
    Reload the catalog from disk.

    Re-reads ``builtin_catalog.json`` and (when configured) the custom
    catalog at ``PELDRUN_PROVIDER_CATALOG_PATH``. Does NOT perform any
    network call — the catalog file is a local artifact by design (see
    ``catalog.py`` for the rationale).

    Useful when an operator edits the custom catalog without restarting
    the backend, and in tests that swap the catalog file on disk.
    """
    catalog = get_provider_package_catalog()
    count = catalog.reload()
    return {
        "status": "ok",
        "total": count,
        "message": f"Catalog reloaded: {count} item(s).",
    }


# ---------------------------------------------------------------------------
# Upload — literal path, declared before /{provider_id}
# ---------------------------------------------------------------------------


@router.post("/upload")
async def upload_provider_package(
    file: UploadFile = File(
        ...,
        description="Provider Package .zip archive",
    ),
    install_dependencies: bool = Form(
        True,
        description=(
            "When false, the isolated venv is still created for "
            "isolation but pip is skipped. Useful for offline installs "
            "of a package whose dependencies are already vendored."
        ),
    ),
) -> Dict[str, Any]:
    """
    Upload and install a Provider Package from a ZIP archive.

    The install pipeline validates the archive, extracts it to a
    staging directory, creates an isolated venv, and registers the
    package. On any failure the response carries the exact error code
    and structured details so the user can fix the package.

    Returns the ``ProviderPackageInstallResult`` shape (same as
    ``install-from-catalog``) so the frontend can reuse the same
    progress-timeline UI for both entry points.
    """
    filename = (file.filename or "provider.zip").strip()
    if not filename.lower().endswith(".zip"):
        raise HTTPException(
            status_code=400,
            detail={
                "error": (
                    "Provider Packages must be uploaded as .zip archives."
                ),
                "code": "PROVIDER_PACKAGE_WRONG_FILE_TYPE",
                "details": {"filename": filename},
            },
        )

    try:
        content = await file.read()
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": f"Could not read uploaded file: {exc}",
                "code": "PROVIDER_PACKAGE_UPLOAD_READ_FAILED",
                "details": {},
            },
        )

    if not content:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "Uploaded file is empty.",
                "code": "PROVIDER_PACKAGE_EMPTY_UPLOAD",
                "details": {"filename": filename},
            },
        )

    try:
        result = await provider_package_manager.install_bundle(
            zip_bytes=content,
            source_filename=filename,
            install_dependencies=install_dependencies,
            source="uploaded",
        )
    except ProviderPackageError as exc:
        logger.info(
            "Provider package upload rejected: %s (%s)",
            exc.message,
            exc.code,
        )
        raise _to_http_exception(exc)
    except Exception as exc:
        logger.exception("Unexpected provider package install error")
        raise HTTPException(
            status_code=500,
            detail={
                "error": f"Unexpected install failure: {exc}",
                "code": "PROVIDER_PACKAGE_INSTALL_FAILED",
                "details": {"exception_type": type(exc).__name__},
            },
        )

    return {
        "status": "ok",
        "provider": result.model_dump(),
        "registry": {
            "total_providers": len(
                provider_package_manager.list_packages()
            ),
        },
    }


# ===========================================================================
# Catch-all endpoints — declared AFTER all literal paths
# ===========================================================================


@router.post("/{provider_id}/install-from-catalog")
async def install_from_catalog(provider_id: str) -> Dict[str, Any]:
    """
    Download, verify, and install a catalog item.

    ``provider_id`` in the URL is the CATALOG ITEM's ``id``, not an
    already-installed package's provider_id — see the module docstring
    for the naming note.

    Pre-validates the item (exists, installable) so the common failure
    modes return a proper 404 or 400 before any network call. Genuine
    download or install errors are then mapped by ``_to_http_exception``.
    """
    catalog = get_provider_package_catalog()

    item = catalog.get_item(provider_id)
    if item is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": f"Catalog item '{provider_id}' does not exist.",
                "code": "PROVIDER_PACKAGE_CATALOG_NOT_FOUND",
                "details": {"item_id": provider_id},
            },
        )

    if not is_installable(item):
        raise HTTPException(
            status_code=400,
            detail={
                "error": installability_reason(item),
                "code": "PROVIDER_PACKAGE_NOT_INSTALLABLE",
                "details": {"item_id": provider_id},
            },
        )

    try:
        result = await catalog.install_from_catalog(provider_id)
    except ProviderPackageError as exc:
        logger.info(
            "Catalog install failed for %r: %s (%s)",
            provider_id,
            exc.message,
            exc.code,
        )
        raise _to_http_exception(exc)
    except Exception as exc:
        logger.exception(
            "Unexpected catalog install error for %r", provider_id
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": f"Unexpected catalog install failure: {exc}",
                "code": "PROVIDER_PACKAGE_INSTALL_FAILED",
                "details": {"exception_type": type(exc).__name__},
            },
        )

    return {
        "status": "ok",
        "provider": result.model_dump(),
        "registry": {
            "total_providers": len(
                provider_package_manager.list_packages()
            ),
        },
    }


@router.get("/{provider_id}")
async def get_provider_detail(
    provider_id: str,
    include_manifest: bool = Query(
        False,
        description=(
            "When true, also return the raw manifest.json from the "
            "active version's install directory. Default false to "
            "keep the endpoint cheap for grid rendering."
        ),
    ),
) -> Dict[str, Any]:
    """
    Return the registry entry for a single installed provider.

    The entry carries everything the card and the details drawer need
    (features, permissions, tags, transport, active version, installed
    versions for rollback). Set ``include_manifest=true`` to also fetch
    the raw manifest from disk.
    """
    entry = provider_package_manager.get_package(provider_id)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": f"Provider package '{provider_id}' is not installed.",
                "code": "PROVIDER_PACKAGE_NOT_FOUND",
                "details": {"provider_id": provider_id},
            },
        )

    response: Dict[str, Any] = {
        "status": "ok",
        "provider": _entry_to_dict(entry),
    }

    if include_manifest:
        manifest = _try_read_manifest(provider_id, entry.active_version)
        if manifest is not None:
            response["manifest"] = manifest
        else:
            response["manifest"] = None
            response["manifest_unavailable_reason"] = (
                "The manifest.json file for the active version could "
                "not be read. The package may need to be reinstalled."
            )

    return response


@router.delete("/{provider_id}")
async def uninstall_provider(provider_id: str) -> Dict[str, Any]:
    """
    Uninstall a Provider Package.

    Removes every installed version, every isolated venv, the active
    pointer, the provider directory (when empty), and the registry
    entry. Idempotent: uninstalling an already-removed package returns
    404 (matching the semantics of ``GET /{provider_id}``) so the
    frontend can distinguish "not found" from a real failure.
    """
    try:
        removed = provider_package_manager.uninstall(provider_id)
    except ProviderPackageError as exc:
        raise _to_http_exception(exc)
    except Exception as exc:
        logger.exception(
            "Unexpected error uninstalling provider %r", provider_id
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": f"Unexpected uninstall failure: {exc}",
                "code": "PROVIDER_PACKAGE_UNINSTALL_FAILED",
                "details": {"exception_type": type(exc).__name__},
            },
        )

    if not removed:
        raise HTTPException(
            status_code=404,
            detail={
                "error": f"Provider package '{provider_id}' is not installed.",
                "code": "PROVIDER_PACKAGE_NOT_FOUND",
                "details": {"provider_id": provider_id},
            },
        )

    return {
        "status": "ok",
        "provider_id": provider_id,
        "removed": True,
    }


@router.post("/{provider_id}/enable")
async def enable_provider(provider_id: str) -> Dict[str, Any]:
    """
    Enable a previously disabled provider package.

    Enabling is orthogonal to which version is active — the active
    pointer is not touched. A package whose active version's files are
    missing will still be enabled here; use ``GET /{provider_id}/health``
    to check filesystem consistency.
    """
    try:
        entry = provider_package_manager.enable(provider_id)
    except ProviderPackageError as exc:
        raise _to_http_exception(exc)

    return {"status": "ok", "provider": _entry_to_dict(entry)}


@router.post("/{provider_id}/disable")
async def disable_provider(provider_id: str) -> Dict[str, Any]:
    """
    Disable a provider package without uninstalling it.

    A disabled package remains registered (so the UI can re-enable it
    later) but is skipped by ``provider_factory.create``. Nothing on
    disk is deleted.
    """
    try:
        entry = provider_package_manager.disable(provider_id)
    except ProviderPackageError as exc:
        raise _to_http_exception(exc)

    return {"status": "ok", "provider": _entry_to_dict(entry)}


@router.post("/{provider_id}/activate")
async def activate_provider_version(
    provider_id: str, body: ActivateVersionRequest
) -> Dict[str, Any]:
    """
    Switch the active version to a previously installed one.

    Used for both rollback (activate an older version) and roll-forward
    (activate a newer version that was installed earlier). The version
    MUST already exist in ``installed_versions``; activating a version
    that was never installed returns 404 (not 400), because from the
    caller's perspective it is indistinguishable from "not found".

    Verifies the version directory and (for native/custom transports)
    the venv exist on disk BEFORE switching the pointer. A missing file
    is a 500 — the user must reinstall that version first.
    """
    try:
        entry = provider_package_manager.activate_version(
            provider_id, body.version
        )
    except ProviderPackageError as exc:
        raise _to_http_exception(exc)

    return {
        "status": "ok",
        "provider": _entry_to_dict(entry),
        "active_version": entry.active_version,
    }


@router.get("/{provider_id}/health")
async def get_provider_health(provider_id: str) -> Dict[str, Any]:
    """
    Run filesystem health checks on a package's active version.

    Returns the ``ProviderPackageHealthReport`` shape:

        * ``status``: ``healthy`` / ``degraded`` / ``broken``.
        * ``checks``: per-check detail (active pointer, version dir,
          manifest, entry point, venv).

    The endpoint does not launch a subprocess — those checks belong to
    the plugin host (phase 2). This makes the endpoint cheap enough to
    poll from the UI.
    """
    try:
        report = provider_package_manager.check_health(provider_id)
    except ProviderPackageError as exc:
        raise _to_http_exception(exc)

    if report is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": f"Provider package '{provider_id}' is not installed.",
                "code": "PROVIDER_PACKAGE_NOT_FOUND",
                "details": {"provider_id": provider_id},
            },
        )

    return {"status": "ok", "health": report.model_dump()}


__all__ = ["router"]
