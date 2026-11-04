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

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
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
from omweb.provider_packages.certificate_manager import (
    certificate_manager,
)
from omweb.provider_packages.schema import (
    MANIFEST_FILENAME,
    CertificateSpec,
    ProviderPackageEntry,
)


logger = logging.getLogger("omweb.routers.store.providers")

router = APIRouter()

# ---------------------------------------------------------------------------
# Async Install Job Tracking (In-memory)
# ---------------------------------------------------------------------------
# We use an in-memory dict to track installation jobs. This prevents HTTP
# timeouts during the long-running `pip install` phase. The frontend polls
# `/install-status/{job_id}` to get real-time progress and final result.
INSTALL_JOBS: Dict[str, Dict[str, Any]] = {}

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
# Certificate helpers
# ---------------------------------------------------------------------------


def _extract_certificate_specs(
    manifest: Optional[Dict[str, Any]],
) -> List[CertificateSpec]:
    """
    Extract and validate the ``CertificateSpec`` entries from a raw
    ``manifest.json`` dict.

    The manifest was already validated at install time, so every entry
    SHOULD parse cleanly. This helper still logs and skips malformed
    entries — a single broken spec must not break the entire listing
    endpoint. The skip is a defense-in-depth measure against a
    hand-edited manifest or a corrupted file.
    """
    if not isinstance(manifest, dict):
        return []

    raw_certs = manifest.get("certificates")
    if not isinstance(raw_certs, list):
        return []

    result: List[CertificateSpec] = []
    for raw in raw_certs:
        if not isinstance(raw, dict):
            continue
        try:
            result.append(CertificateSpec.model_validate(raw))
        except Exception as exc:
            logger.warning(
                "Skipping invalid certificate spec in manifest: %s", exc
            )
    return result


def _get_certificate_spec_or_404(
    provider_id: str,
    certificate_id: str,
) -> CertificateSpec:
    """
    Return the ``CertificateSpec`` declared by a package's active
    manifest for the given ``certificate_id``, or raise an
    ``HTTPException``.

    Raises
    ------
    HTTPException(404)
        When the package is not installed, the manifest is unreadable,
        or the certificate id is not declared by the manifest.
    """
    entry = provider_package_manager.get_package(provider_id)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": (
                    f"Provider package '{provider_id}' is not installed."
                ),
                "code": "PROVIDER_PACKAGE_NOT_FOUND",
                "details": {"provider_id": provider_id},
            },
        )

    manifest = _try_read_manifest(provider_id, entry.active_version)
    if manifest is None:
        raise HTTPException(
            status_code=500,
            detail={
                "error": (
                    f"Could not read manifest.json for "
                    f"'{provider_id}' v{entry.active_version}."
                ),
                "code": "PROVIDER_PACKAGE_MANIFEST_UNREADABLE",
                "details": {
                    "provider_id": provider_id,
                    "version": entry.active_version,
                },
            },
        )

    specs = _extract_certificate_specs(manifest)
    for spec in specs:
        if spec.id == certificate_id:
            return spec

    raise HTTPException(
        status_code=404,
        detail={
            "error": (
                f"Certificate '{certificate_id}' is not declared by "
                f"provider '{provider_id}'."
            ),
            "code": "PROVIDER_CERTIFICATE_NOT_FOUND",
            "details": {
                "provider_id": provider_id,
                "certificate_id": certificate_id,
                "declared_certificates": [s.id for s in specs],
            },
        },
    )


def _compute_certificate_summary(
    provider_id: str,
    specs: List[CertificateSpec],
) -> tuple[bool, str]:
    """
    Compute the aggregate certificate status for the store card badge.

    Reads the certificate vault (cheap, no filesystem access beyond a
    single ``is_file()`` per required certificate) and decides:

        * ``none_required``  — the package declares no certificates.
        * ``all_installed``  — every REQUIRED certificate has a record
                               with status ``installed`` AND the file
                               is still present on disk.
        * ``some_missing``   — at least one REQUIRED certificate is
                               missing, invalid, pending, or its file
                               was deleted manually.
        * ``unknown``        — unexpected state (should be rare).

    Unlike ``certificate_manager.check_status()``, this helper does
    NOT write to the vault. The list endpoint is called on every page
    load, so it must be free of side effects. Full reconciliation
    happens on demand via ``GET /{id}/certificates`` (which calls
    ``check_status`` per certificate).
    """
    if not specs:
        return (False, "none_required")

    required_specs = [s for s in specs if s.required]
    if not required_specs:
        # Only optional certificates are declared — nothing to block on.
        return (True, "all_installed")

    try:
        records_list = certificate_manager.list_records(provider_id)
    except Exception as exc:
        logger.warning(
            "Could not read certificate records for %r: %s",
            provider_id,
            exc,
        )
        return (True, "unknown")

    records_by_id = {r.certificate_id: r for r in records_list}

    for spec in required_specs:
        record = records_by_id.get(spec.id)
        if record is None:
            return (True, "some_missing")
        if record.status != "installed":
            return (True, "some_missing")

        # Defense-in-depth: verify the file is still present. A user
        # who deleted the file manually must see "missing" here, not
        # a false green badge.
        if not record.installed_path:
            return (True, "some_missing")
        try:
            if not Path(record.installed_path).is_file():
                return (True, "some_missing")
        except Exception:
            return (True, "some_missing")

    return (True, "all_installed")


def _enrich_entry_with_certificate_status(
    entry: ProviderPackageEntry,
) -> Dict[str, Any]:
    """
    Serialize a registry entry with the two certificate-status fields
    the store card needs.

    The base registry entry stores only what was known at install time
    (name, description, features, ...). It does NOT carry the live
    certificate status because that depends on the filesystem and can
    change without touching the registry.

    This helper reads the on-disk ``manifest.json`` to discover the
    declared certificates, then delegates to
    ``_compute_certificate_summary`` for the aggregate status.

    Two new keys are added to the serialized dict:

        * ``has_certificates``   — boolean or null (null when the
                                   manifest could not be read).
        * ``certificates_status`` — one of ``none_required``,
                                    ``all_installed``, ``some_missing``,
                                    ``unknown``.

    Packages that do not declare certificates get ``has_certificates:
    False`` and ``certificates_status: "none_required"`` — the card
    will not render a certificate badge or button for them.
    """
    entry_dict = entry.model_dump()

    manifest = _try_read_manifest(entry.id, entry.active_version)
    if manifest is None:
        # Manifest unreadable — cannot determine certificate state.
        # Treat as "unknown" so the UI shows a neutral state rather
        # than a misleading green or amber badge.
        entry_dict["has_certificates"] = None
        entry_dict["certificates_status"] = "unknown"
        return entry_dict

    specs = _extract_certificate_specs(manifest)
    has_certs, cert_status = _compute_certificate_summary(
        entry.id, specs
    )
    entry_dict["has_certificates"] = has_certs
    entry_dict["certificates_status"] = cert_status
    return entry_dict


# ---------------------------------------------------------------------------
# Catalog install-state enrichment
# ---------------------------------------------------------------------------


def _enrich_catalog_item_with_install_state(
    raw: Dict[str, Any],
    installed_by_id: Dict[str, ProviderPackageEntry],
) -> Dict[str, Any]:
    """
    Add install-state fields to a serialized catalog item.

    The catalog item is a portable description of a package: it
    declares the provider's identity, branding, developer metadata,
    and download URL — but it is unaware of whether that package is
    already installed on *this* PELDRUN instance. The UI needs that
    information to render the correct action ("Install" vs
    "Enable / Configure" vs "Already active") and to display the
    installed version when it differs from the catalog's advertised
    version.

    The fields added here are computed at RESPONSE time by joining
    the catalog item against the installed registry. They are NOT
    part of the catalog's on-disk format — the JSON file remains
    identical across every PELDRUN instance, keeping it portable.
    Each instance derives its own install state at render time.

    Fields added
    ------------
    * ``is_installed`` (bool)
        True when the item's ``id`` matches an installed package.
    * ``is_enabled`` (bool)
        True only when the package is installed AND enabled. Always
        ``false`` when the package is not installed (there is nothing
        to enable).
    * ``installed_version`` (str | None)
        The installed package's ``active_version``, or ``None`` when
        not installed. Used by the UI to show "Installed v1.0.0" vs
        the catalog's ``version`` when they differ.
    * ``installed_versions`` (list[str])
        Every installed version, sorted descending. Empty when not
        installed. Used by the details modal to offer rollback /
        roll-forward version selection.
    * ``has_active_connection`` (bool)
        Same computation as ``ProviderPackageEntry.has_active_connection``
        in the installed list. Present only when ``is_installed`` is
        true. Lets the catalog card show "Not configured" without a
        second fetch.
    * ``active_connection_name`` (str | None)
        Name of the active connection when one exists. Used by the
        card to render the green "Connected" badge.

    Parameters
    ----------
    raw
        The catalog item already serialized to a dict (``item.model_dump()``),
        including the ``installable`` / ``installability_reason`` /
        ``permission_warnings`` fields that the caller has already added.
    installed_by_id
        Mapping of installed provider IDs to their registry entries.
        Callers build this once per request so the join is O(1) per
        item.
    """
    item_id = raw.get("id")
    installed = installed_by_id.get(item_id) if item_id else None

    if installed is None:
        raw["is_installed"] = False
        raw["is_enabled"] = False
        raw["installed_version"] = None
        raw["installed_versions"] = []
        raw["has_active_connection"] = False
        raw["active_connection_name"] = None
        return raw

    raw["is_installed"] = True
    raw["is_enabled"] = bool(installed.enabled)
    raw["installed_version"] = installed.active_version
    raw["installed_versions"] = sorted(
        [v.version for v in installed.installed_versions],
        reverse=True,
    )

    # Derive the connection status from the vault. This mirrors the
    # logic used by the installed-packages list endpoint so the card
    # renders identically regardless of which tab the user is on.
    try:
        from omweb.routers.config_rtr import (
            get_active_connection_for_package,
        )

        active_conn = get_active_connection_for_package(installed.id)
    except Exception:
        active_conn = None

    if active_conn is not None:
        raw["has_active_connection"] = True
        raw["active_connection_name"] = active_conn.get("name") or None
    else:
        raw["has_active_connection"] = False
        raw["active_connection_name"] = None

    return raw


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


class InstallCertificateRequest(BaseModel):
    """
    Body for ``POST /{provider_id}/certificates/{certificate_id}/install``.

    Both fields are optional — sending an empty body is valid and uses
    the package's declared defaults.
    """

    destination: Optional[str] = Field(
        default=None,
        description=(
            "Absolute path override for the installed file. When "
            "None, the manager uses CertificateSpec.default_destination, "
            "or a managed path under the provider's storage directory."
        ),
    )
    allow_overwrite: bool = Field(
        default=True,
        description=(
            "When false, an existing file at the destination causes a "
            "conflict error instead of being overwritten."
        ),
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
    providers = [
        _enrich_entry_with_certificate_status(e) for e in entries
    ]
    return {
        "status": "ok",
        "providers": providers,
        "total": len(providers),
    }


# ---------------------------------------------------------------------------
# Catalog — literal paths, declared before /{provider_id}
# ---------------------------------------------------------------------------


@router.get("/catalog")
async def list_catalog_items() -> Dict[str, Any]:
    """
    Return every item in the merged Provider Package catalog.

    Each item includes:

        * **Catalog metadata** — name, description, version, transport,
          features, permissions, tags, branding (``brand_color`` /
          ``icon`` / ``icon_type``), developer info (``developer`` +
          legacy ``author`` / ``author_url`` / ``homepage``), project
          links (``repository_url`` / ``license`` / ``docs_url`` /
          ``changelog_url`` / ``screenshots``), declared certificates,
          and connection fields.

        * **Installability** — ``installable`` (bool),
          ``installability_reason`` (str), ``permission_warnings``
          (list[str]).

        * **Install state** — ``is_installed``, ``is_enabled``,
          ``installed_version``, ``installed_versions``,
          ``has_active_connection``, ``active_connection_name``.

    The install-state fields are computed at response time by joining
    the catalog against the installed registry. They are NOT part of
    the catalog file itself — the on-disk JSON remains portable and
    identical across every PELDRUN instance.

    The remote catalog (when ``PELDRUN_PROVIDER_CATALOG_URL`` is set)
    is refreshed before listing if the on-disk cache has expired. The
    refresh is best-effort: a network failure leaves the previous
    cache intact and this endpoint still returns the merged view —
    the UI is never blocked by an unreachable GitHub.
    """
    catalog = get_provider_package_catalog()

    # Best-effort remote refresh. TTL-guarded (24h default) so this
    # is a no-op on the hot path. Failures are logged inside the
    # catalog and do not raise.
    try:
        await catalog.ensure_fresh()
    except Exception as exc:
        logger.warning(
            "Catalog refresh failed (non-fatal; serving cached view): %s",
            exc,
        )

    items = catalog.list_items()

    # One registry read for the whole response — O(1) per item when
    # joining.
    installed_by_id: Dict[str, ProviderPackageEntry] = {
        p.id: p for p in provider_package_manager.list_packages()
    }

    result: List[Dict[str, Any]] = []
    for item in items:
        raw = item.model_dump()
        raw["installable"] = is_installable(item)
        raw["installability_reason"] = installability_reason(item)
        raw["permission_warnings"] = _describe_catalog_permissions(
            item.permissions or {}
        )
        result.append(
            _enrich_catalog_item_with_install_state(raw, installed_by_id)
        )

    return {
        "status": "ok",
        "catalog": result,
        "total": len(result),
    }


@router.get("/catalog/status")
async def get_catalog_status() -> Dict[str, Any]:
    """
    Return a snapshot of the catalog state for diagnostics.

    Exposes the per-source item counts (builtin / custom / extra /
    remote), whether a remote URL is configured, the age of the
    remote cache, and the last fetch error (if any). Intended for
    operators who need to confirm that a configured
    ``PELDRUN_PROVIDER_CATALOG_URL`` is actually contributing items.

    No side effects — this endpoint does NOT trigger a refresh. Use
    ``POST /catalog/refresh`` for that.

    Route order note
    ----------------
    This handler MUST be registered BEFORE ``GET /catalog/{item_id}``
    — otherwise FastAPI matches ``/catalog/status`` against the
    ``{item_id}`` pattern first and dispatches here with
    ``item_id="status"``.
    """
    catalog = get_provider_package_catalog()
    return {
        "status": "ok",
        "catalog": catalog.get_status(),
    }


@router.get("/catalog/{item_id}")
async def get_catalog_item(item_id: str) -> Dict[str, Any]:
    """
    Return a single catalog item by ID.

    Same shape as one element of ``GET /catalog`` — including the
    install-state fields (``is_installed`` / ``is_enabled`` /
    ``installed_version`` / ``installed_versions`` /
    ``has_active_connection`` / ``active_connection_name``) computed
    by joining against the installed registry.

    Returns 404 when the item does not exist — this lets the
    frontend request the details for a card the user clicked without
    re-fetching the entire catalog.

    Refreshes the remote catalog first (TTL-guarded), so a user who
    just published a new item to GitHub does not need to wait for the
    listing endpoint to run before this one sees it.
    """
    catalog = get_provider_package_catalog()

    try:
        await catalog.ensure_fresh()
    except Exception as exc:
        logger.warning(
            "Catalog refresh failed (non-fatal): %s", exc
        )

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

    # Single-item lookup — build the join map for just this entry.
    installed = provider_package_manager.get_package(item_id)
    installed_by_id: Dict[str, ProviderPackageEntry] = (
        {item_id: installed} if installed is not None else {}
    )

    raw = item.model_dump()
    raw["installable"] = is_installable(item)
    raw["installability_reason"] = installability_reason(item)
    raw["permission_warnings"] = _describe_catalog_permissions(
        item.permissions or {}
    )
    return {
        "status": "ok",
        "item": _enrich_catalog_item_with_install_state(
            raw, installed_by_id
        ),
    }


@router.post("/catalog/refresh")
async def refresh_catalog(
    force_remote: bool = Query(
        True,
        description=(
            "When true (default) and PELDRUN_PROVIDER_CATALOG_URL is "
            "configured, bypass the 24h TTL and refetch from the "
            "remote URL. When false, only the local files (builtin + "
            "custom) are re-read, matching the pre-remote behavior."
        ),
    ),
) -> Dict[str, Any]:
    """
    Reload the catalog from disk AND (optionally) from the remote URL.

    Two-phase reload:

        1. **Local reload** — re-reads ``builtin_catalog.json`` and
           (when configured) the custom catalog at
           ``PELDRUN_PROVIDER_CATALOG_PATH``. Sync, fast, always
           succeeds.

        2. **Remote refresh** — when ``force_remote`` is true AND
           ``PELDRUN_PROVIDER_CATALOG_URL`` is set, bypasses the TTL
           and fetches from the remote URL. Best-effort: on network
           failure the previous cache is preserved and the response
           reports the error.

    Used by operators to pull a newly-published catalog entry
    immediately without waiting 24 hours, and by tests that swap a
    catalog file on disk.
    """
    catalog = get_provider_package_catalog()

    # Phase 1 — local reload (always runs, sync, cannot fail).
    local_count = catalog.reload()

    # Phase 2 — remote refresh (opt-in, async, best-effort).
    remote_ok: Optional[bool] = None
    if force_remote:
        try:
            remote_ok = await catalog.refresh()
        except Exception as exc:
            logger.warning(
                "Remote catalog refresh raised (non-fatal): %s", exc
            )
            remote_ok = False

    status = catalog.get_status()

    return {
        "status": "ok",
        "total": local_count,
        "remote_refresh_attempted": force_remote,
        "remote_refresh_ok": remote_ok,
        "catalog_status": status,
        "message": (
            f"Catalog reloaded: {local_count} item(s)"
            + (
                f"; remote refresh "
                f"{'succeeded' if remote_ok else 'failed or skipped'}"
                if force_remote
                else ""
            )
            + "."
        ),
    }


# NOTE: ``GET /catalog/status`` is registered ABOVE, immediately after
# ``GET /catalog`` and BEFORE ``GET /catalog/{item_id}``. That ordering
# is required: FastAPI matches routes in registration order, and the
# ``{item_id}`` pattern would otherwise swallow ``/catalog/status``
# with ``item_id="status"``. Do not move it back here.


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

    # ------------------------------------------------------------------
    # Create a background job and return immediately.
    #
    # Why async:
    #   `pip install` can take 30-60s on a cold cache. Synchronous
    #   endpoints time out at the HTTP proxy layer (~30s default in
    #   Next.js). Moving the install to a background task fixes the
    #   timeout: the request returns in <100ms, the actual work
    #   continues in the event loop, and the frontend polls
    #   `/install-status/{job_id}` for progress.
    #
    # The job state lives in `INSTALL_JOBS` (module-level dict). Jobs
    # survive until server restart. This is intentional — the payload
    # is small and bounded (500 log lines max), and persisting across
    # restarts adds complexity without real value for a one-shot
    # admin action.
    # ------------------------------------------------------------------
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    INSTALL_JOBS[job_id] = {
        "job_id": job_id,
        "status": "running",
        "filename": filename,
        "logs": ["[system] Upload received. Starting install pipeline..."],
        "result": None,
        "error": None,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
    }

    # Best-effort cleanup: if we have accumulated a lot of jobs, drop
    # the oldest completed ones. Never drop a running job.
    if len(INSTALL_JOBS) >= 50:
        completed = [
            (jid, j["started_at"])
            for jid, j in INSTALL_JOBS.items()
            if j["status"] in ("success", "failed")
        ]
        completed.sort(key=lambda pair: pair[1])
        excess = len(INSTALL_JOBS) - 49
        for jid, _ in completed[:excess]:
            INSTALL_JOBS.pop(jid, None)

    # Fire-and-forget: the task reference is held by the event loop
    # until completion, so we do not need to store it separately.
    asyncio.create_task(
        _run_install_background(
            job_id=job_id,
            content=content,
            filename=filename,
            install_dependencies=install_dependencies,
        )
    )

    return {
        "status": "accepted",
        "job_id": job_id,
        "message": (
            "Install started. Poll /install-status/{job_id} for "
            "progress and the final result."
        ),
    }


async def _run_install_background(
    *,
    job_id: str,
    content: bytes,
    filename: str,
    install_dependencies: bool,
) -> None:
    """
    Background worker for a single install job.

    Runs in the FastAPI event loop, but is not awaited by any HTTP
    request. Mutates ``INSTALL_JOBS[job_id]`` in place so the
    ``/install-status`` endpoint can report progress.

    All exceptions are caught and stored on the job. An unhandled
    exception would otherwise be logged by asyncio and silently
    dropped, leaving the frontend polling forever.
    """
    job = INSTALL_JOBS.get(job_id)
    if job is None:
        # Should never happen — the job is inserted in the endpoint
        # before the task is spawned. Logged for safety.
        logger.warning("Install job %s disappeared; aborting.", job_id)
        return

    def _log(message: str) -> None:
        # Cap the log buffer so a runaway pip cannot exhaust memory.
        # Old lines are dropped from the FRONT, so the tail (which is
        # most useful for diagnosis) survives.
        if len(job["logs"]) >= 500:
            job["logs"].pop(0)
        job["logs"].append(message)

    def _progress_callback(message: str) -> None:
        # The dependency_installer invokes this from within the
        # pip subprocess (on the event loop). Truncate each line so
        # the JSON payload stays bounded.
        try:
            _log(message[:2000])
        except Exception:
            # A broken callback must never break the install.
            pass

    try:
        _log("[system] Validating and extracting package...")

        result = await provider_package_manager.install_bundle(
            zip_bytes=content,
            source_filename=filename,
            install_dependencies=install_dependencies,
            source="uploaded",
            progress_callback=_progress_callback,
        )

        job["status"] = "success"
        job["result"] = result.model_dump()
        job["finished_at"] = datetime.now(timezone.utc).isoformat()
        _log(
            f"[system] Installation completed successfully "
            f"({result.id}@{result.version})."
        )

        # Register the freshly-installed package in the runtime
        # factory registry so "Test Connection" and "Scan Models" work
        # immediately, without requiring a separate scan round-trip.
        try:
            from peldrun.llm.providers import (
                ensure_external_providers_discovered,
            )

            ensure_external_providers_discovered()
        except Exception as exc:
            logger.debug(
                "Post-install provider discovery failed (non-fatal): %s",
                exc,
            )

    except ProviderPackageError as exc:
        job["status"] = "failed"
        job["error"] = exc.to_dict()
        job["finished_at"] = datetime.now(timezone.utc).isoformat()
        _log(f"[error] {exc.message}")
        logger.info(
            "Background install for job %s failed: %s (%s)",
            job_id,
            exc.message,
            exc.code,
        )

    except Exception as exc:
        job["status"] = "failed"
        job["error"] = {
            "error": f"Unexpected install failure: {exc}",
            "code": "PROVIDER_PACKAGE_INSTALL_FAILED",
            "details": {"exception_type": type(exc).__name__},
        }
        job["finished_at"] = datetime.now(timezone.utc).isoformat()
        _log(f"[error] Unexpected failure: {exc}")
        logger.exception(
            "Unexpected background install failure for job %s", job_id
        )


@router.get("/install-status/{job_id}")
async def get_install_status(job_id: str) -> Dict[str, Any]:
    """
    Return the status of an asynchronous install job.

    The frontend polls this every 1-2 seconds while a job is
    ``running``. Once the job reaches ``success`` or ``failed``,
    polling stops and the UI processes the result (or shows the
    error).

    The full log buffer is returned so the UI can render a live
    timeline. Log entries are capped at 500 server-side; the oldest
    are dropped when the cap is exceeded.
    """
    job = INSTALL_JOBS.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": f"Install job '{job_id}' not found.",
                "code": "INSTALL_JOB_NOT_FOUND",
                "details": {"job_id": job_id},
            },
        )

    return {
        "status": "ok",
        "job": {
            "job_id": job["job_id"],
            "status": job["status"],
            "filename": job["filename"],
            "logs": list(job["logs"]),
            "result": job["result"],
            "error": job["error"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
        },
    }


# ===========================================================================
# Catch-all endpoints — declared AFTER all literal paths
# ===========================================================================


@router.post("/{provider_id}/install-from-catalog")
async def install_from_catalog(provider_id: str) -> Dict[str, Any]:
    """
    Download, verify, and install a catalog item — asynchronously.

    Returns ``{"status": "accepted", "job_id": "...", "message": "..."}``
    immediately. The actual download + install runs in a background
    task and its progress is exposed via ``GET /install-status/{job_id}``
    — the SAME polling contract the manual-upload endpoint uses. The
    frontend therefore renders catalog installs through the identical
    progress modal (phase bar + live log), eliminating the silent
    failures the previous synchronous version produced.

    ``provider_id`` in the URL is the CATALOG ITEM's ``id``, not an
    already-installed package's provider_id — see the module docstring
    for the naming note.

    Pre-validates the item (exists, installable) so the common failure
    modes return a proper 404 or 400 before any background task is
    spawned.
    """
    catalog = get_provider_package_catalog()

    # Best-effort refresh so an item published to the remote catalog
    # is discoverable here even if the listing endpoint was not called
    # first (e.g. direct API callers, or a race between page load and
    # install click).
    try:
        await catalog.ensure_fresh()
    except Exception:
        pass

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

    job_id = f"job_{uuid.uuid4().hex[:12]}"
    display = item.display_name or item.name or item.id
    INSTALL_JOBS[job_id] = {
        "job_id": job_id,
        "status": "running",
        "filename": f"{item.id}-{item.version}.zip",
        "logs": [
            f"[system] Starting catalog install for "
            f"{display} v{item.version}..."
        ],
        "result": None,
        "error": None,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
    }

    if len(INSTALL_JOBS) >= 50:
        completed = [
            (jid, j["started_at"])
            for jid, j in INSTALL_JOBS.items()
            if j["status"] in ("success", "failed")
        ]
        completed.sort(key=lambda pair: pair[1])
        excess = len(INSTALL_JOBS) - 49
        for jid, _ in completed[:excess]:
            INSTALL_JOBS.pop(jid, None)

    asyncio.create_task(
        _run_catalog_install_background(job_id=job_id, item_id=item.id)
    )

    return {
        "status": "accepted",
        "job_id": job_id,
        "message": (
            "Catalog install started. Poll /install-status/{job_id} "
            "for progress and the final result."
        ),
    }


async def _run_catalog_install_background(
    *,
    job_id: str,
    item_id: str,
) -> None:
    """
    Background worker for a catalog-item install job.

    Mirrors ``_run_install_background`` (upload path) exactly: it
    mutates ``INSTALL_JOBS[job_id]`` in place so the frontend's
    polling loop can display the same progress timeline for catalog
    installs as it does for manual ZIP uploads.

    All exceptions are caught and stored on the job. An unhandled
    exception would otherwise be logged by asyncio and silently
    dropped, leaving the frontend polling forever.
    """
    job = INSTALL_JOBS.get(job_id)
    if job is None:
        logger.warning(
            "Catalog install job %s disappeared; aborting.", job_id
        )
        return

    def _log(message: str) -> None:
        if len(job["logs"]) >= 500:
            job["logs"].pop(0)
        job["logs"].append(message)

    def _progress_callback(message: str) -> None:
        try:
            _log(message[:2000])
        except Exception:
            pass

    try:
        catalog = get_provider_package_catalog()
        result = await catalog.install_from_catalog(
            item_id,
            progress_callback=_progress_callback,
        )

        job["status"] = "success"
        job["result"] = result.model_dump()
        job["finished_at"] = datetime.now(timezone.utc).isoformat()
        _log(
            f"[system] Installation completed successfully "
            f"({result.id}@{result.version})."
        )

        # Register the freshly-installed catalog package in the
        # runtime factory registry so "Test Connection" works
        # immediately after install completes.
        try:
            from peldrun.llm.providers import (
                ensure_external_providers_discovered,
            )

            ensure_external_providers_discovered()
        except Exception as exc:
            logger.debug(
                "Post-catalog-install provider discovery failed "
                "(non-fatal): %s",
                exc,
            )

    except ProviderPackageError as exc:
        job["status"] = "failed"
        job["error"] = exc.to_dict()
        job["finished_at"] = datetime.now(timezone.utc).isoformat()
        _log(f"[error] {exc.message}")
        logger.info(
            "Background catalog install for job %s failed: %s (%s)",
            job_id,
            exc.message,
            exc.code,
        )

    except Exception as exc:
        job["status"] = "failed"
        job["error"] = {
            "error": f"Unexpected install failure: {exc}",
            "code": "PROVIDER_PACKAGE_INSTALL_FAILED",
            "details": {"exception_type": type(exc).__name__},
        }
        job["finished_at"] = datetime.now(timezone.utc).isoformat()
        _log(f"[error] Unexpected failure: {exc}")
        logger.exception(
            "Unexpected background catalog install failure for job %s",
            job_id,
        )


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
        "provider": _enrich_entry_with_certificate_status(entry),
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


# ---------------------------------------------------------------------------
# Certificates — install, verify, list, delete
# ---------------------------------------------------------------------------
#
# Certificates are declared by the package's manifest
# (``ProviderManifest.certificates``). The Certificate Manager UI opens
# a dedicated modal per package that offers:
#
#   * List: all declared certs with their current status.
#   * Install: download + verify + write to disk (one click).
#   * Test: re-check status without re-downloading.
#   * Delete: remove the file and the record.
#
# Installation happens in the MAIN backend process (not the worker
# subprocess), because the worker's network allowlist restricts
# outbound requests to the connection's ``base_url`` host. The CA host
# is a different host, so the download must happen outside the worker.
# See ``certificate_manager.py`` for the full rationale.


@router.get("/{provider_id}/certificates")
async def list_provider_certificates(
    provider_id: str,
) -> Dict[str, Any]:
    """
    Return every certificate declared by a package's active manifest,
    with its current status.

    Status is reconciled live against the filesystem, so a user who
    manually deleted the CA file will see ``missing`` here without
    needing to click Test.

    The response contains one entry per declared certificate — even
    those that have never been installed. A never-installed certificate
    appears with status ``pending``.
    """
    entry = provider_package_manager.get_package(provider_id)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": (
                    f"Provider package '{provider_id}' is not installed."
                ),
                "code": "PROVIDER_PACKAGE_NOT_FOUND",
                "details": {"provider_id": provider_id},
            },
        )

    manifest = _try_read_manifest(provider_id, entry.active_version)
    specs = _extract_certificate_specs(manifest)

    reports: List[Dict[str, Any]] = []
    for spec in specs:
        try:
            report = await certificate_manager.check_status(
                provider_id, spec
            )
            reports.append(report.to_dict())
        except Exception as exc:
            # A failure to check one certificate must not fail the
            # whole endpoint — the UI still needs to show the others.
            logger.warning(
                "Status check failed for certificate %r/%r: %s",
                provider_id,
                spec.id,
                exc,
            )
            reports.append({
                "provider_id": provider_id,
                "certificate_id": spec.id,
                "display_name": spec.display_name,
                "status": "invalid",
                "error_message": f"Status check failed: {exc}",
                "required": spec.required,
                "download_url": spec.download_url,
                "docs_url": spec.docs_url,
                "installed_path": None,
                "expected_sha256": spec.download_sha256,
                "actual_sha256": None,
                "size_bytes": 0,
                "installed_at": None,
                "last_checked_at": None,
            })

    return {
        "status": "ok",
        "provider_id": provider_id,
        "certificates": reports,
        "total": len(reports),
    }


@router.post("/{provider_id}/certificates/{certificate_id}/install")
async def install_provider_certificate(
    provider_id: str,
    certificate_id: str,
    body: Optional[InstallCertificateRequest] = None,
) -> Dict[str, Any]:
    """
    Download, verify, and install a certificate.

    Sending an empty body is valid — the package's declared defaults
    are used. A non-empty body may override ``destination`` (useful for
    operators who want the CA at ``/etc/ssl/certs/...``) and
    ``allow_overwrite``.

    Returns the saved vault record plus a fresh status report, so the
    UI can update the card and the modal without a second round-trip.
    """
    spec = _get_certificate_spec_or_404(provider_id, certificate_id)

    # Convert the optional destination string to a Path. Reject
    # relative paths — the caller must be explicit, since the resolved
    # path depends on the backend's CWD otherwise.
    destination: Optional[Path] = None
    if body and body.destination:
        candidate = Path(body.destination)
        if not candidate.is_absolute():
            raise HTTPException(
                status_code=400,
                detail={
                    "error": (
                        "destination must be an absolute path when "
                        "provided."
                    ),
                    "code": "PROVIDER_CERTIFICATE_INVALID_DESTINATION",
                    "details": {"destination": body.destination},
                },
            )
        destination = candidate

    allow_overwrite = body.allow_overwrite if body else True

    try:
        record = await certificate_manager.install(
            provider_id=provider_id,
            spec=spec,
            destination=destination,
            allow_overwrite=allow_overwrite,
        )
    except ProviderPackageError as exc:
        logger.info(
            "Certificate install failed for %r/%r: %s (%s)",
            provider_id,
            certificate_id,
            exc.message,
            exc.code,
        )
        raise _to_http_exception(exc)
    except Exception as exc:
        logger.exception(
            "Unexpected certificate install error for %r/%r",
            provider_id,
            certificate_id,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": (
                    f"Unexpected certificate install failure: {exc}"
                ),
                "code": "PROVIDER_CERTIFICATE_INSTALL_FAILED",
                "details": {"exception_type": type(exc).__name__},
            },
        )

    # Best-effort fresh status — if this fails, return the record
    # alone rather than fail the endpoint.
    try:
        report = await certificate_manager.check_status(
            provider_id, spec
        )
        status_report: Optional[Dict[str, Any]] = report.to_dict()
    except Exception as exc:
        logger.warning(
            "Post-install status check failed for %r/%r: %s",
            provider_id,
            certificate_id,
            exc,
        )
        status_report = None

    return {
        "status": "ok",
        "provider_id": provider_id,
        "certificate_id": certificate_id,
        "record": record.model_dump(),
        "status_report": status_report,
    }


@router.post("/{provider_id}/certificates/{certificate_id}/test")
async def test_provider_certificate(
    provider_id: str,
    certificate_id: str,
) -> Dict[str, Any]:
    """
    Re-check the status of a certificate without re-downloading.

    Updates the vault record's ``status`` and ``last_checked_at``
    fields, then returns the full status report. Used by the UI's
    "Test" button in the Certificate Manager modal.
    """
    spec = _get_certificate_spec_or_404(provider_id, certificate_id)

    try:
        report = await certificate_manager.check_status(
            provider_id, spec
        )
    except ProviderPackageError as exc:
        raise _to_http_exception(exc)
    except Exception as exc:
        logger.exception(
            "Unexpected certificate check error for %r/%r",
            provider_id,
            certificate_id,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": f"Status check failed: {exc}",
                "code": "PROVIDER_CERTIFICATE_CHECK_FAILED",
                "details": {"exception_type": type(exc).__name__},
            },
        )

    return {
        "status": "ok",
        "provider_id": provider_id,
        "certificate_id": certificate_id,
        "status_report": report.to_dict(),
    }


@router.delete("/{provider_id}/certificates/{certificate_id}")
async def delete_provider_certificate(
    provider_id: str,
    certificate_id: str,
    delete_file: bool = Query(
        True,
        description=(
            "When true (default), the installed file is also deleted "
            "from disk. When false, only the vault record is removed "
            "— useful for preserving a manually-installed certificate."
        ),
    ),
) -> Dict[str, Any]:
    """
    Delete a certificate: the vault record, and optionally the file
    on disk.

    Idempotent — deleting a certificate that was never installed
    returns ``removed: false`` without an error. The UI uses that
    distinction to decide whether to show "Certificate removed" or
    "Nothing to remove".
    """
    try:
        removed = await certificate_manager.delete(
            provider_id=provider_id,
            certificate_id=certificate_id,
            delete_file=delete_file,
        )
    except ProviderPackageError as exc:
        raise _to_http_exception(exc)
    except Exception as exc:
        logger.exception(
            "Unexpected certificate delete error for %r/%r",
            provider_id,
            certificate_id,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": f"Unexpected certificate delete failure: {exc}",
                "code": "PROVIDER_CERTIFICATE_DELETE_FAILED",
                "details": {"exception_type": type(exc).__name__},
            },
        )

    return {
        "status": "ok",
        "provider_id": provider_id,
        "certificate_id": certificate_id,
        "removed": removed,
    }


# ===========================================================================
# Provider Connections (M9/M10)
# ===========================================================================
#
# A Provider Package can have MULTIPLE saved connections. Exactly one
# is ``is_active`` — the runtime uses that one for dispatch.
#
# The actual storage logic lives in ``config_rtr.py`` (functions
# ``list_provider_connections``, ``save_provider_connection``, etc.).
# This module exposes them over HTTP and adds two behaviors that are
# specific to the Provider Packages flow:
#
#   1. **Secret masking in responses.** Password and secret_file
#      fields are returned as ``••••••••`` so the API response never
#      contains the plaintext credential. The stored value is always
#      intact; only the response representation is masked.
#
#   2. **Certificate path injection for ``test``.** Before probing a
#      connection, we look for any certificate the package has
#      installed and inject its path into the matching ``file``
#      connection field (e.g. ``ca_bundle_path``). Without this, the
#      adapter would use the system trust store and fail with an SSL
#      error even though the user "installed the certificate"
#      earlier via the Certificate Manager.

from omweb.routers.config_rtr import (
    delete_provider_connection as _vault_delete_connection,
    get_provider_connection as _vault_get_connection,
    list_provider_connections as _vault_list_connections,
    save_provider_connection as _vault_save_connection,
    set_active_provider_connection as _vault_set_active,
    set_provider_connection_enabled as _vault_set_enabled,
)


def _mask_connection_fields(
    connection: Dict[str, Any],
    manifest: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Return a copy of ``connection`` with password / secret_file fields
    replaced by the ``••••••••`` placeholder.

    Masking is based on the manifest's ``connection_fields`` spec. A
    field whose ``type`` is ``"password"`` or ``"secret_file"`` is
    always masked when its value is a non-empty string.

    The masked copy is what the API returns. The actual stored value
    in ``models_vault.json`` remains the real credential — masking is
    a presentation concern only.
    """
    result = dict(connection)
    fields = dict(result.get("fields") or {})

    if manifest is None:
        # Manifest unreadable — we cannot reliably identify secret
        # fields. Return the fields as-is rather than risk masking
        # something the user needs to see, OR leaking a secret as
        # plaintext. We choose "show plaintext" here because this
        # branch only triggers on a corrupted manifest, which is a
        # state where the user must repair before proceeding anyway.
        result["fields"] = fields
        return result

    raw_specs = manifest.get("connection_fields") or []
    secret_keys: set = set()
    if isinstance(raw_specs, list):
        for spec in raw_specs:
            if not isinstance(spec, dict):
                continue
            if spec.get("type") in ("password", "secret_file"):
                key = spec.get("key")
                if isinstance(key, str) and key:
                    secret_keys.add(key)

    for key in secret_keys:
        val = fields.get(key)
        if isinstance(val, str) and val:
            fields[key] = "\u2022" * 8

    result["fields"] = fields
    return result


def _inject_installed_certificates(
    provider_id: str,
    fields: Dict[str, Any],
    manifest: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Populate ``file`` connection fields with the installed certificate
    paths.

    GigaChat (and any future provider that requires a custom CA) has
    a chicken-and-egg problem: the user installs the CA via the
    Certificate Manager, but the ``ca_bundle_path`` connection field
    is separate from the certificate record. Without this helper, the
    adapter would receive an empty ``ca_bundle_path`` and fall back to
    the system trust store, which fails on the Russian Root CA.

    Precedence rules for each installed certificate:

        1. If ``spec.required_for_fields`` lists specific field keys,
           use those exact keys.
        2. Else if the manifest declares EXACTLY ONE ``file`` /
           ``secret_file`` field, use it.
        3. Else if a field named ``ca_bundle_path`` exists, use it.

    Rules 2 and 3 are heuristics for GigaChat-style packages. A
    future provider can be explicit by setting
    ``required_for_fields`` in its manifest.
    """
    if not manifest or not fields:
        return dict(fields)

    cert_specs = _extract_certificate_specs(manifest)
    if not cert_specs:
        return dict(fields)

    try:
        records = certificate_manager.list_records(provider_id)
    except Exception:
        return dict(fields)

    records_by_cert_id = {r.certificate_id: r for r in records}

    raw_fields = manifest.get("connection_fields") or []
    file_field_keys = [
        f.get("key") for f in raw_fields
        if isinstance(f, dict)
        and f.get("type") in ("file", "secret_file")
        and isinstance(f.get("key"), str)
    ]

    result = dict(fields)
    for spec in cert_specs:
        record = records_by_cert_id.get(spec.id)
        if record is None or not record.installed_path:
            continue

        target_keys: list = list(spec.required_for_fields or [])

        if not target_keys and len(file_field_keys) == 1:
            target_keys = file_field_keys

        if not target_keys and "ca_bundle_path" in file_field_keys:
            target_keys = ["ca_bundle_path"]

        for key in target_keys:
            current = result.get(key)
            if not current:
                result[key] = record.installed_path

    return result


@router.get("/{provider_id}/connections")
async def list_connections(provider_id: str) -> Dict[str, Any]:
    """
    Return every saved connection for a provider package.

    Password and secret_file fields are masked in the response.
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

    manifest = _try_read_manifest(provider_id, entry.active_version)
    raw = _vault_list_connections(provider_id)
    masked = [_mask_connection_fields(c, manifest) for c in raw]

    return {
        "status": "ok",
        "provider_id": provider_id,
        "connections": masked,
        "total": len(masked),
    }


@router.post("/{provider_id}/connections")
async def create_connection(
    provider_id: str,
    body: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Create a new connection for a provider package.

    When ``is_active`` is ``true``, this connection becomes the one
    the runtime uses; all siblings are deactivated.
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

    # Ensure provider_package_id is set (the frontend should send it,
    # but we tolerate omission for defense in depth).
    payload = dict(body)
    payload["provider_package_id"] = provider_id

    try:
        saved = _vault_save_connection(payload)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to save connection for %r", provider_id)
        raise HTTPException(
            status_code=500,
            detail={
                "error": f"Could not save connection: {exc}",
                "code": "PROVIDER_CONNECTION_SAVE_FAILED",
                "details": {"exception_type": type(exc).__name__},
            },
        )

    manifest = _try_read_manifest(provider_id, entry.active_version)
    return {
        "status": "ok",
        "connection": _mask_connection_fields(saved, manifest),
    }


@router.put("/{provider_id}/connections/{connection_id}")
async def update_connection(
    provider_id: str,
    connection_id: str,
    body: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Update an existing connection.

    Any field omitted from ``body["fields"]`` is preserved from the
    current stored value — see the merge logic in
    ``config_rtr.save_provider_connection``.
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

    existing = _vault_get_connection(connection_id)
    if existing is None or existing.get("provider_package_id") != provider_id:
        raise HTTPException(
            status_code=404,
            detail={
                "error": f"Connection '{connection_id}' not found.",
                "code": "PROVIDER_CONNECTION_NOT_FOUND",
                "details": {"connection_id": connection_id},
            },
        )

    payload = dict(body)
    payload["id"] = connection_id
    payload["provider_package_id"] = provider_id

    try:
        saved = _vault_save_connection(payload)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            "Failed to update connection %r for %r",
            connection_id, provider_id,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": f"Could not update connection: {exc}",
                "code": "PROVIDER_CONNECTION_SAVE_FAILED",
                "details": {"exception_type": type(exc).__name__},
            },
        )

    manifest = _try_read_manifest(provider_id, entry.active_version)
    return {
        "status": "ok",
        "connection": _mask_connection_fields(saved, manifest),
    }


@router.delete("/{provider_id}/connections/{connection_id}")
async def delete_connection(
    provider_id: str,
    connection_id: str,
) -> Dict[str, Any]:
    """
    Delete a connection.
    """
    existing = _vault_get_connection(connection_id)
    if existing is None or existing.get("provider_package_id") != provider_id:
        raise HTTPException(
            status_code=404,
            detail={
                "error": f"Connection '{connection_id}' not found.",
                "code": "PROVIDER_CONNECTION_NOT_FOUND",
                "details": {"connection_id": connection_id},
            },
        )

    removed = _vault_delete_connection(connection_id)
    if not removed:
        raise HTTPException(
            status_code=404,
            detail={
                "error": f"Connection '{connection_id}' not found.",
                "code": "PROVIDER_CONNECTION_NOT_FOUND",
                "details": {"connection_id": connection_id},
            },
        )

    return {
        "status": "ok",
        "connection_id": connection_id,
        "removed": True,
    }


@router.post("/{provider_id}/connections/{connection_id}/enable")
async def enable_connection(
    provider_id: str,
    connection_id: str,
) -> Dict[str, Any]:
    """Enable a previously disabled connection."""
    existing = _vault_get_connection(connection_id)
    if existing is None or existing.get("provider_package_id") != provider_id:
        raise HTTPException(
            status_code=404,
            detail={
                "error": f"Connection '{connection_id}' not found.",
                "code": "PROVIDER_CONNECTION_NOT_FOUND",
                "details": {"connection_id": connection_id},
            },
        )

    saved = _vault_set_enabled(connection_id, True)
    if saved is None:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Connection enable operation returned no record.",
                "code": "PROVIDER_CONNECTION_ENABLE_FAILED",
                "details": {"connection_id": connection_id},
            },
        )

    entry = provider_package_manager.get_package(provider_id)
    manifest = _try_read_manifest(provider_id, entry.active_version) if entry else None
    return {
        "status": "ok",
        "connection": _mask_connection_fields(saved, manifest),
    }


@router.post("/{provider_id}/connections/{connection_id}/disable")
async def disable_connection(
    provider_id: str,
    connection_id: str,
) -> Dict[str, Any]:
    """Disable a connection without deleting it."""
    existing = _vault_get_connection(connection_id)
    if existing is None or existing.get("provider_package_id") != provider_id:
        raise HTTPException(
            status_code=404,
            detail={
                "error": f"Connection '{connection_id}' not found.",
                "code": "PROVIDER_CONNECTION_NOT_FOUND",
                "details": {"connection_id": connection_id},
            },
        )

    saved = _vault_set_enabled(connection_id, False)
    if saved is None:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Connection disable operation returned no record.",
                "code": "PROVIDER_CONNECTION_DISABLE_FAILED",
                "details": {"connection_id": connection_id},
            },
        )

    entry = provider_package_manager.get_package(provider_id)
    manifest = _try_read_manifest(provider_id, entry.active_version) if entry else None
    return {
        "status": "ok",
        "connection": _mask_connection_fields(saved, manifest),
    }


@router.post("/{provider_id}/connections/{connection_id}/activate")
async def activate_connection(
    provider_id: str,
    connection_id: str,
) -> Dict[str, Any]:
    """
    Mark this connection as the active one for the package.

    Deactivates every sibling connection of the same package.
    """
    existing = _vault_get_connection(connection_id)
    if existing is None or existing.get("provider_package_id") != provider_id:
        raise HTTPException(
            status_code=404,
            detail={
                "error": f"Connection '{connection_id}' not found.",
                "code": "PROVIDER_CONNECTION_NOT_FOUND",
                "details": {"connection_id": connection_id},
            },
        )

    saved = _vault_set_active(connection_id)
    if saved is None:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Connection activate operation returned no record.",
                "code": "PROVIDER_CONNECTION_ACTIVATE_FAILED",
                "details": {"connection_id": connection_id},
            },
        )

    entry = provider_package_manager.get_package(provider_id)
    manifest = _try_read_manifest(provider_id, entry.active_version) if entry else None
    return {
        "status": "ok",
        "connection": _mask_connection_fields(saved, manifest),
    }


@router.post("/{provider_id}/connections/{connection_id}/test")
async def test_connection(
    provider_id: str,
    connection_id: str,
) -> Dict[str, Any]:
    """
    Probe the connection by calling the provider's discovery endpoint.

    Uses the REAL stored credential values (not the masked
    placeholders). Injects installed certificate paths so the adapter
    can reach TLS-protected endpoints.

    Always returns HTTP 200. The ``ok`` field carries the probe
    result; ``message``, ``latency_ms``, ``code``, ``status_code``
    add detail.
    """
    import time as _time

    existing = _vault_get_connection(connection_id)
    if existing is None or existing.get("provider_package_id") != provider_id:
        raise HTTPException(
            status_code=404,
            detail={
                "error": f"Connection '{connection_id}' not found.",
                "code": "PROVIDER_CONNECTION_NOT_FOUND",
                "details": {"connection_id": connection_id},
            },
        )

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

    manifest = _try_read_manifest(provider_id, entry.active_version)

    # Inject installed certificate paths into file-based connection
    # fields (e.g. ``ca_bundle_path``). Without this, the adapter would
    # fall back to the system trust store, which does not include the
    # Russian Root CA even after the user installed it via the
    # Certificate Manager.
    raw_fields = dict(existing.get("fields") or {})
    fields = _inject_installed_certificates(provider_id, raw_fields, manifest)

    base_url = fields.pop("base_url", None)
    api_key = fields.pop("api_key", None)

    # ------------------------------------------------------------------
    # Build the plugin descriptor for the Provider Package.
    #
    # The factory returns ``PluginProviderAdapter`` for any registered
    # external package. That adapter REQUIRES a descriptor dict in
    # ``extra_params["_peldrun_plugin"]`` describing:
    #
    #   * transport ("native" / "custom")
    #   * venv_python (absolute path to the isolated interpreter)
    #   * entry_point (module:ClassName to import inside the worker)
    #   * package_dir (version directory, added to sys.path in the worker)
    #
    # The dispatcher does this same step when handling a real chat
    # message — we replicate it here so the Test button exercises the
    # EXACT same code path.
    # ------------------------------------------------------------------
    descriptor = provider_package_manager.get_active_adapter_descriptor(
        provider_id
    )
    if descriptor is None:
        return {
            "status": "ok",
            "ok": False,
            "message": (
                f"Cannot test connection: provider package "
                f"'{provider_id}' has no active adapter descriptor. "
                f"The package may be disabled, its files missing, or "
                f"its entry_point invalid. Check the Details view for "
                f"the health report."
            ),
            "latency_ms": 0.0,
        }

    # Convert the dataclass to a JSON-serializable dict. ``Path`` values
    # must become strings because the IPC layer between the supervisor
    # and the worker is JSON-Lines, and Path is not serializable.
    descriptor_dict = {
        "provider_id": descriptor.provider_id,
        "version": descriptor.version,
        "hash": descriptor.hash,
        "transport": descriptor.transport,
        "api_contract": descriptor.api_contract,
        "entry_point": descriptor.entry_point,
        "class_name": descriptor.class_name,
        "base_adapter": descriptor.base_adapter,
        "package_dir": str(descriptor.package_dir),
        "venv_python": (
            str(descriptor.venv_python)
            if descriptor.venv_python is not None
            else None
        ),
        "features": dict(descriptor.features or {}),
        "permissions": dict(descriptor.permissions or {}),
        "enabled": descriptor.enabled,
    }
    fields["_peldrun_plugin"] = descriptor_dict

    # Lazy-import PELDRUN internals so this module stays importable in
    # environments without the full runtime (e.g. unit tests that only
    # exercise validation).
    try:
        from peldrun.llm.contracts import ConnectionContext
        from peldrun.llm.providers import (
            ensure_external_providers_discovered,
            provider_factory,
        )
    except Exception as exc:
        logger.error("Could not import PELDRUN runtime: %s", exc)
        return {
            "status": "ok",
            "ok": False,
            "message": f"PELDRUN runtime not available: {exc}",
            "latency_ms": 0.0,
        }

    # Ensure any externally-installed provider packages are registered
    # in the runtime factory registry BEFORE we call ``create()``.
    #
    # The registry is populated lazily (on the first ``fetch-models``
    # call), but ``test_connection`` needs it too: a user who installs
    # a package from the catalog and immediately clicks "Test" has
    # never triggered a scan, so the package is on disk but NOT in the
    # in-memory registry — and ``provider_factory.create()`` raises
    # "Provider 'X' is not registered". Calling this here is idempotent
    # (a cheap no-op when already registered) and closes that gap.
    try:
        ensure_external_providers_discovered()
    except Exception as exc:
        logger.warning(
            "External provider discovery failed (non-fatal): %s", exc
        )

    connection = ConnectionContext(
        connection_id=connection_id,
        provider_id=provider_id,
        base_url=base_url or None,
        api_key=api_key or None,
        timeout=60.0,
        extra_params=fields,
    )

    started = _time.time()
    adapter = None
    try:
        adapter = provider_factory.create(provider_id, connection)
        result = await adapter.validate_connection(connection)

        latency = result.get("latency_ms")
        if not isinstance(latency, (int, float)):
            latency = round((_time.time() - started) * 1000.0, 2)

        return {
            "status": "ok",
            "ok": bool(result.get("ok")),
            "message": result.get("message", ""),
            "latency_ms": latency,
            "code": result.get("code"),
            "status_code": result.get("status_code"),
        }
    except Exception as exc:
        latency = round((_time.time() - started) * 1000.0, 2)
        logger.warning(
            "Connection test failed for %r/%r: %s",
            provider_id,
            connection_id,
            exc,
        )
        return {
            "status": "ok",
            "ok": False,
            "message": str(exc),
            "latency_ms": latency,
        }
    finally:
        if adapter is not None:
            # Diagnostic logging around close so a freeze (if any)
            # can be traced to the exact line.
            logger.info(
                "[test_connection] closing adapter for %r/%r",
                provider_id,
                connection_id,
            )
            try:
                await asyncio.wait_for(adapter.close(), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "[test_connection] adapter.close() timed out for "
                    "%r/%r after 10s — the worker may be wedged.",
                    provider_id,
                    connection_id,
                )
            except Exception as exc:
                logger.debug(
                    "[test_connection] adapter.close() raised: %s",
                    exc,
                )
            logger.info(
                "[test_connection] adapter closed for %r/%r",
                provider_id,
                connection_id,
            )


__all__ = ["router"]
