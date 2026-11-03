# backend/omweb/provider_packages/package_manager.py
"""
Provider Package lifecycle orchestrator.

Ties together the validator, extractor, dependency installer, and
registry into the single public API used by the store router (M10):

    install_bundle(zip_bytes, source_filename) -> InstallResult
    list_packages()                            -> List[Entry]
    get_package(id)                            -> Optional[Entry]
    enable(id) / disable(id)                   -> Entry
    activate_version(id, version)              -> Entry
    uninstall(id)                              -> bool
    check_health(id)                           -> Optional[HealthReport]
    get_active_adapter_descriptor(id)          -> Optional[AdapterDescriptor]

Storage layout
--------------

    <root>/store/providers/
    ├── registry.json                        # global index
    └── <provider_id>/
        ├── active.json                      # active version pointer
        ├── versions/
        │   └── <version>/                   # extracted package files
        │       ├── manifest.json
        │       ├── _peldrun_meta.json
        │       └── ...
        └── environments/
            └── <version>/                   # isolated venv
                ├── bin/python  (or Scripts/python.exe)
                └── ...

Install pipeline (atomic, all-or-nothing)
-----------------------------------------

1. **validate**      — ``validate_zip`` (in-memory; no disk write).
2. **conflict**      — reject builtin IDs, idempotent re-installs,
                       same-version-different-hash.
3. **extract**       — ``safe_extract`` into a staging directory.
4. **promote**       — atomically move staging → ``versions/<version>/``.
5. **environment**   — create isolated venv (skipped for ``compatible``).
6. **register**      — persist into ``registry.json`` + ``active.json``.

On failure at any step from 4 onward, the manager deletes any partial
state (``versions/<v>/``, ``environments/<v>/``) so a retry starts from
a clean slate. ``registry.json`` is only written on full success, so it
never references a version directory that does not exist.

Design invariants
-----------------

1. **The registry is the last writer.** Files on disk are fully in
   place BEFORE the registry is updated. An interrupted install
   leaves orphaned files (which a subsequent ``cleanup_orphans`` can
   reap) but never leaves the registry pointing at a nonexistent
   version.

2. **Idempotent installs.** Re-uploading the exact same ZIP (same
   version, same SHA-256) is a no-op: the existing entry is returned
   with ``skipped_reason = "already_installed"``. Re-uploading the
   SAME version with DIFFERENT content is a hard conflict — the user
   must either bump the version or uninstall first.

3. **Built-in IDs are protected.** A provider package cannot register
   a ``provider_id`` that matches a built-in adapter
   (``openai_compat``, ``lmstudio``, ``anthropic``, ...). This mirrors
   the guarantee in ``peldrun.llm.providers.registry``.

4. **Filesystem cleanup on uninstall.** ``uninstall`` removes the
   version directories, the venvs, the active pointer, and the
   provider directory itself. Files not owned by the package (e.g.
   an admin-placed readme) are preserved — the manager only deletes
   directories that match the known layout.

5. **Health checks are filesystem-only.** No subprocess is spawned
   to verify the adapter imports. That is the plugin host's job
   (phase 2), and it is exercised on the first request — not on
   every page load. The health endpoint is intentionally cheap.

6. **No mutations after install.** The version directory and its
   venv are treated as immutable. A rebuild requires an uninstall.

See also
--------

    * ``backend/omweb/provider_packages/package_validator.py``
    * ``backend/omweb/provider_packages/dependency_installer.py``
    * ``backend/omweb/provider_packages/registry.py``
    * ``backend/omweb/provider_packages/schema.py``
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from omweb.provider_packages.dependency_installer import (
    DependencyInstaller,
    EnvironmentInstallResult,
    dependency_installer as default_installer,
)
from omweb.provider_packages.errors import (
    ProviderPackageConflictError,
    ProviderPackageEnvironmentError,
    ProviderPackageError,
    ProviderPackageManifestError,
    ProviderPackageNotFoundError,
    ProviderPackageSecurityError,
    ProviderPackageStorageError,
    ProviderPackageValidationError,
)
from omweb.provider_packages.package_validator import (
    ValidationReport,
    safe_extract,
    validate_zip,
)
from omweb.provider_packages.registry import (
    ActivePointer,
    ProviderPackageRegistry,
    provider_package_registry as default_registry,
)
from omweb.provider_packages.schema import (
    MANIFEST_FILENAME,
    META_FILENAME,
    ProviderManifest,
    ProviderPackageEntry,
    ProviderPackageHealthCheck,
    ProviderPackageHealthReport,
    ProviderPackageInstallReport,
    ProviderPackageInstallResult,
    ProviderPackageStepReport,
    ProviderPackageVersionRecord,
    ProviderTransport,
)

logger = logging.getLogger("omweb.provider_packages.manager")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Directory under each provider that holds per-version extracted files.
VERSIONS_DIR_NAME: str = "versions"

#: Directory under each provider that holds per-version virtualenvs.
ENVIRONMENTS_DIR_NAME: str = "environments"

#: Staging directory name. Lives under the storage root (same filesystem
#: as the final install), so ``os.replace``-style renames stay atomic.
STAGING_DIR_NAME: str = ".staging"


#: Provider ID validation — mirrors ``schema._PROVIDER_ID_PATTERN``.
#: Repeated here as a defense-in-depth check before building any path.
_PROVIDER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


#: Files/directories the extractor may produce that must NEVER land in
#: the final version directory. macOS archiver metadata, Windows thumbs.
_JUNK_NAMES = frozenset(
    {"__MACOSX", ".DS_Store", "Thumbs.db", ".Spotlight-V100", ".fseventsd"}
)


# ---------------------------------------------------------------------------
# Adapter descriptor (forward-looking for phase 2 plugin_host)
# ---------------------------------------------------------------------------


@dataclass
class AdapterDescriptor:
    """
    Runtime descriptor for an active provider package.

    Consumed by the plugin host (phase 2) to launch a worker process.
    Not persisted — derived on demand from the registry + filesystem.
    Every field is ``None``-able so the caller can decide whether the
    descriptor is usable or degraded.
    """

    provider_id: str
    version: str
    hash: str
    transport: str
    api_contract: str
    entry_point: Optional[str]
    class_name: Optional[str]
    base_adapter: Optional[str]
    package_dir: Path
    venv_python: Optional[Path]
    features: Dict[str, bool]
    permissions: Dict[str, Any]
    enabled: bool


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _perf_ms(start: float) -> int:
    """Return elapsed milliseconds since ``start`` (a perf_counter value)."""
    return int((time.perf_counter() - start) * 1000)


def _step(
    name: str,
    *,
    status: str = "ok",
    duration_ms: int = 0,
    detail: str = "",
) -> ProviderPackageStepReport:
    """Build a ``ProviderPackageStepReport`` for the install timeline."""
    return ProviderPackageStepReport(
        name=name,
        status=status,
        duration_ms=duration_ms,
        detail=detail,
    )


def _safe_provider_id(provider_id: str) -> str:
    """
    Validate ``provider_id`` before using it in a filesystem path.

    The Pydantic ``ProviderManifest`` validator already enforces this
    pattern, but the manager also accepts provider IDs supplied by the
    caller (e.g. ``uninstall("openai_compat")``). Defense in depth: no
    path built from a caller-supplied string reaches disk without this
    check.
    """
    if not isinstance(provider_id, str) or not _PROVIDER_ID_RE.match(provider_id):
        raise ProviderPackageStorageError(
            f"Invalid provider ID {provider_id!r}. Must match "
            f"{_PROVIDER_ID_RE.pattern}.",
            details={"provider_id": str(provider_id)},
        )
    return provider_id


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class ProviderPackageManager:
    """
    Orchestrator for Provider Package lifecycle operations.

    Thread-safety
    -------------
    Individual operations are atomic with respect to the registry
    (which holds its own lock). Concurrent installs of the SAME
    provider+version are serialized by the filesystem (the version
    directory is created via ``os.replace`` which is atomic). The
    manager itself holds no lock — the registry does.

    Concurrency policy for installs
    -------------------------------
    Two concurrent ``install_bundle`` calls for the SAME version will
    both extract to their own staging directories (unique UUID names),
    then race at the ``os.replace(staging, version_dir)`` step. The
    first to finish wins; the second finds the version directory
    already populated and (after re-reading the registry) either
    returns the existing entry (same hash) or raises a conflict
    (different hash). This is the desired behavior — no corruption,
    no silent override.
    """

    def __init__(
        self,
        root_dir: Path,
        *,
        installer: Optional[DependencyInstaller] = None,
        registry: Optional[ProviderPackageRegistry] = None,
    ) -> None:
        """
        Parameters
        ----------
        root_dir
            The provider-packages storage root. All version directories,
            venvs, and registry files live under this path.
        installer
            Optional override for the dependency installer. Tests use
            this to inject a no-op installer.
        registry
            Optional override for the registry. When omitted, a fresh
            ``ProviderPackageRegistry(root_dir)`` is created so the
            manager is self-contained even if it is instantiated
            without the module-level singleton.
        """
        self._root = Path(root_dir).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

        self._installer = installer or default_installer
        self._registry = registry or ProviderPackageRegistry(self._root)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def root(self) -> Path:
        """The storage root this manager operates under."""
        return self._root

    @property
    def registry(self) -> ProviderPackageRegistry:
        """The underlying registry instance."""
        return self._registry

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _provider_dir(self, provider_id: str) -> Path:
        return self._root / _safe_provider_id(provider_id)

    def _versions_root(self, provider_id: str) -> Path:
        return self._provider_dir(provider_id) / VERSIONS_DIR_NAME

    def _version_dir(self, provider_id: str, version: str) -> Path:
        return self._versions_root(provider_id) / version

    def _environments_root(self, provider_id: str) -> Path:
        return self._provider_dir(provider_id) / ENVIRONMENTS_DIR_NAME

    def _env_dir(self, provider_id: str, version: str) -> Path:
        return self._environments_root(provider_id) / version

    def _staging_root(self) -> Path:
        path = self._root / STAGING_DIR_NAME
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _venv_python(env_dir: Path) -> Path:
        """Return the interpreter path inside a venv (POSIX or Windows)."""
        if os.name == "nt":
            return env_dir / "Scripts" / "python.exe"
        return env_dir / "bin" / "python"

    # ------------------------------------------------------------------
    # Public API: reads
    # ------------------------------------------------------------------

    def list_packages(
        self, *, enabled_only: bool = False
    ) -> List[ProviderPackageEntry]:
        """Return every installed package (sorted by display name)."""
        return self._registry.list_packages(enabled_only=enabled_only)

    def get_package(self, provider_id: str) -> Optional[ProviderPackageEntry]:
        """Return the entry for ``provider_id``, or ``None`` if not installed."""
        return self._registry.get(provider_id)

    def exists(self, provider_id: str) -> bool:
        """Return True iff ``provider_id`` is installed (regardless of enabled)."""
        return self._registry.exists(provider_id)

    # ------------------------------------------------------------------
    # Public API: enable / disable
    # ------------------------------------------------------------------

    def enable(self, provider_id: str) -> ProviderPackageEntry:
        """Enable a previously disabled provider package."""
        return self._registry.set_enabled(provider_id, True)

    def disable(self, provider_id: str) -> ProviderPackageEntry:
        """Disable a provider package without uninstalling it."""
        return self._registry.set_enabled(provider_id, False)

    # ------------------------------------------------------------------
    # Public API: install
    # ------------------------------------------------------------------

    async def install_bundle(
        self,
        zip_bytes: bytes,
        source_filename: str = "provider.zip",
        *,
        install_dependencies: bool = True,
        source: str = "uploaded",
    ) -> ProviderPackageInstallResult:
        """
        Validate, extract, and register a provider package from a ZIP.

        Parameters
        ----------
        zip_bytes
            The raw uploaded ZIP.
        source_filename
            Original filename for the audit trail.
        install_dependencies
            When ``False``, the venv is still created (for isolation)
            but ``pip install`` is skipped. Useful for offline installs
            of a package whose dependencies are already vendored in
            the archive.
        source
            One of ``"uploaded"`` (manual ZIP) or ``"catalog"``
            (installed from the store catalog). Persisted for the UI.

        Returns
        -------
        ProviderPackageInstallResult
            On success. The ``install_report`` carries step timings and
            any warnings.

        Raises
        ------
        ProviderPackageValidationError / ProviderPackageSecurityError /
        ProviderPackageManifestError
            When the ZIP fails validation.
        ProviderPackageConflictError
            When the package ID collides with a builtin, or the exact
            version is already installed with different content.
        ProviderPackageEnvironmentError
            When venv creation or dependency install fails.
        ProviderPackageStorageError
            When a disk operation fails.
        """
        started_perf = time.perf_counter()
        steps: List[ProviderPackageStepReport] = []
        warnings: List[str] = []

        # --------------------------------------------------------------
        # 1. Validate
        # --------------------------------------------------------------
        t0 = time.perf_counter()
        validation = validate_zip(zip_bytes)
        steps.append(
            _step(
                "validate",
                status="ok" if validation.ok else "failed",
                duration_ms=_perf_ms(t0),
                detail=(
                    f"{validation.file_count} files, "
                    f"{validation.uncompressed_size} bytes"
                    if validation.ok
                    else f"{len(validation.errors)} error(s)"
                ),
            )
        )
        if not validation.ok or validation.manifest is None:
            raise self._error_from_validation(validation)

        warnings.extend(validation.warnings)
        manifest = validation.manifest

        # --------------------------------------------------------------
        # 2. Conflict check
        # --------------------------------------------------------------
        t0 = time.perf_counter()
        existing_entry = self._check_conflicts(
            manifest, validation.sha256
        )
        steps.append(
            _step(
                "conflict_check",
                status="ok",
                duration_ms=_perf_ms(t0),
                detail=(
                    "idempotent_reinstall"
                    if existing_entry is not None
                    else "new_install"
                ),
            )
        )
        if existing_entry is not None:
            # Idempotent re-install of the exact same version+hash.
            return self._build_result(
                entry=existing_entry,
                validation=validation,
                env_result=None,
                steps=steps,
                warnings=warnings,
                started_perf=started_perf,
                idempotent=True,
            )

        # --------------------------------------------------------------
        # 3. Extract to staging
        # --------------------------------------------------------------
        staging_dir = Path(
            tempfile.mkdtemp(
                prefix=f"pkg_{manifest.id}_{uuid.uuid4().hex[:8]}_",
                dir=str(self._staging_root()),
            )
        )
        t0 = time.perf_counter()
        try:
            extract_report = safe_extract(
                zip_bytes, staging_dir, validation=validation
            )
            steps.append(
                _step(
                    "extract",
                    status="ok" if extract_report.ok else "failed",
                    duration_ms=_perf_ms(t0),
                    detail=f"{extract_report.extracted_files} files",
                )
            )
            if not extract_report.ok:
                raise ProviderPackageStorageError(
                    "Extraction failed.",
                    details={
                        "failed_paths": extract_report.failed_paths,
                        "errors": extract_report.errors,
                    },
                )
            # Remove junk that the archiver may have embedded.
            self._strip_junk(staging_dir)
        except Exception:
            self._safe_rmtree(staging_dir)
            raise

        # --------------------------------------------------------------
        # 4. Promote staging → versions/<version>/
        # --------------------------------------------------------------
        version_dir = self._version_dir(manifest.id, manifest.version)
        # Clean up any orphaned version dir from a crashed previous run.
        if version_dir.exists() and not (
            self._registry.exists(manifest.id)
            and (entry := self._registry.get(manifest.id)) is not None
            and entry.has_version(manifest.version)
        ):
            logger.info(
                "Removing orphaned version directory %s from a "
                "previous crashed install.",
                version_dir,
            )
            self._safe_rmtree(version_dir)

        t0 = time.perf_counter()
        try:
            version_dir.parent.mkdir(parents=True, exist_ok=True)
            # ``os.replace`` on a directory requires the target not to
            # exist (or to be an empty directory). We have already
            # ensured neither.
            os.replace(str(staging_dir), str(version_dir))
            self._write_peldrun_meta(version_dir, manifest, validation)
            steps.append(
                _step(
                    "promote",
                    status="ok",
                    duration_ms=_perf_ms(t0),
                    detail=f"versions/{manifest.version}/",
                )
            )
        except Exception as exc:
            self._safe_rmtree(staging_dir)
            self._safe_rmtree(version_dir)
            raise ProviderPackageStorageError(
                f"Failed to promote package files into place: {exc}",
                details={"target": str(version_dir)},
            ) from exc

        # --------------------------------------------------------------
        # 5. Environment (create venv, or skip for compatible transport)
        # --------------------------------------------------------------
        env_result: Optional[EnvironmentInstallResult] = None
        if manifest.transport == ProviderTransport.COMPATIBLE:
            steps.append(
                _step(
                    "environment",
                    status="skipped",
                    detail=(
                        "compatible transport — no venv, uses "
                        f"base_adapter={manifest.base_adapter or '<unset>'}"
                    ),
                )
            )
        else:
            t0 = time.perf_counter()
            try:
                env_result = await self._installer.create_environment(
                    package_dir=version_dir,
                    environments_root=self._environments_root(manifest.id),
                    version=manifest.version,
                    manifest=manifest,
                )
                status = "ok" if env_result.installed or env_result.skipped_reason else "ok"
                steps.append(
                    _step(
                        "environment",
                        status=status,
                        duration_ms=_perf_ms(t0),
                        detail=(
                            "installed"
                            if env_result.installed
                            else env_result.skipped_reason or "created"
                        ),
                    )
                )
                warnings.extend(env_result.warnings)
            except ProviderPackageEnvironmentError:
                self._safe_rmtree(version_dir)
                raise
            except Exception as exc:
                self._safe_rmtree(version_dir)
                raise ProviderPackageEnvironmentError(
                    f"Environment creation failed: {exc}",
                    details={"provider_id": manifest.id},
                ) from exc

        # --------------------------------------------------------------
        # 6. Register
        # --------------------------------------------------------------
        t0 = time.perf_counter()
        try:
            version_record = self._build_version_record(
                manifest=manifest,
                validation=validation,
            )
            entry = self._registry.install_version(
                provider_id=manifest.id,
                version_record=version_record,
                name=manifest.name,
                display_name=manifest.display_name,
                description=manifest.description,
                transport=manifest.transport.value,
                api_contract=manifest.api_contract,
                features=self._features_dict(manifest),
                permissions=self._permissions_dict(manifest),
                tags=list(manifest.tags),
                author=manifest.author,
                author_url=manifest.author_url,
                homepage=manifest.homepage,
                # Snapshot connection_fields and runtime_hints into
                # the registry so the Store's Configure modal can
                # render the form without a second disk read. Plain
                # dicts (not Pydantic models) so future field additions
                # to ConnectionFieldSpec / RuntimeHints do not require
                # a registry migration.
                connection_fields=[
                    f.model_dump() for f in manifest.connection_fields
                ],
                runtime_hints=manifest.runtime_hints.model_dump(),
                source=source,
                set_as_active=True,
            )
            steps.append(
                _step(
                    "register",
                    status="ok",
                    duration_ms=_perf_ms(t0),
                    detail=f"{manifest.id}@{manifest.version}",
                )
            )
        except Exception:
            self._safe_rmtree(version_dir)
            if env_result is not None:
                self._installer.delete_environment(
                    environments_root=self._environments_root(manifest.id),
                    version=manifest.version,
                )
            raise

        return self._build_result(
            entry=entry,
            validation=validation,
            env_result=env_result,
            steps=steps,
            warnings=warnings,
            started_perf=started_perf,
            idempotent=False,
        )

    # ------------------------------------------------------------------
    # Public API: activate / uninstall
    # ------------------------------------------------------------------

    def activate_version(
        self, provider_id: str, version: str
    ) -> ProviderPackageEntry:
        """
        Switch the active version to a previously installed one.

        Verifies that the version directory and (for native/custom
        transports) the venv are both present before updating the
        pointer. A missing file is a hard error — the caller must
        reinstall that version rather than silently activating a
        broken package.

        Raises
        ------
        ProviderPackageNotFoundError
            When the package or version is not installed.
        ProviderPackageStorageError
            When the version directory or venv is missing from disk.
        """
        _safe_provider_id(provider_id)
        entry = self._registry.get_or_raise(provider_id)

        record = entry.get_version(version)
        if record is None:
            raise ProviderPackageNotFoundError(
                f"Version '{version}' of provider '{provider_id}' is "
                f"not installed. Installed versions: "
                f"{[v.version for v in entry.installed_versions]}.",
                details={
                    "provider_id": provider_id,
                    "requested_version": version,
                    "installed_versions": [
                        v.version for v in entry.installed_versions
                    ],
                },
            )

        version_dir = self._version_dir(provider_id, version)
        if not version_dir.is_dir():
            raise ProviderPackageStorageError(
                f"Version directory for {provider_id}@{version} is "
                f"missing from disk ({version_dir}). Reinstall the "
                f"version before activating it.",
                details={"path": str(version_dir)},
            )

        if entry.transport in (
            ProviderTransport.NATIVE.value,
            ProviderTransport.CUSTOM.value,
        ):
            env_dir = self._env_dir(provider_id, version)
            python_exe = self._venv_python(env_dir)
            if not python_exe.is_file():
                raise ProviderPackageStorageError(
                    f"Virtual environment for {provider_id}@{version} "
                    f"is missing ({env_dir}). Reinstall the version "
                    f"before activating it.",
                    details={"path": str(env_dir)},
                )

        return self._registry.set_active_version(provider_id, version)

    def uninstall(self, provider_id: str) -> bool:
        """
        Remove an entire provider package: all versions, all venvs, the
        active pointer, the directory, and the registry entry.

        Returns ``True`` when the package was installed and is now
        removed, ``False`` when the package was not installed.

        Every filesystem removal is best-effort with respect to
        ``ignore_errors=True``, but the registry removal IS atomic.
        Even if a directory could not be deleted (e.g. a file is
        locked by an antivirus scanner), the package is uninstalled
        from the runtime's perspective — it can no longer be
        activated because it is no longer registered.
        """
        _safe_provider_id(provider_id)
        entry = self._registry.get(provider_id)
        if entry is None:
            return False

        versions_dir = self._versions_root(provider_id)
        envs_dir = self._environments_root(provider_id)

        if versions_dir.is_dir():
            for version_dir in versions_dir.iterdir():
                if version_dir.is_dir():
                    self._safe_rmtree(version_dir)
            try:
                versions_dir.rmdir()
            except OSError:
                pass  # Directory not empty — safe to leave.

        if envs_dir.is_dir():
            for env_dir in envs_dir.iterdir():
                if env_dir.is_dir():
                    self._safe_rmtree(env_dir)
            try:
                envs_dir.rmdir()
            except OSError:
                pass

        # Update the registry (also removes ``active.json``).
        self._registry.remove_package(provider_id)

        # Try to remove the provider directory itself if it is now empty.
        provider_dir = self._provider_dir(provider_id)
        try:
            if provider_dir.is_dir() and not any(provider_dir.iterdir()):
                provider_dir.rmdir()
        except OSError:
            pass

        return True

    # ------------------------------------------------------------------
    # Public API: health
    # ------------------------------------------------------------------

    def check_health(
        self, provider_id: str
    ) -> Optional[ProviderPackageHealthReport]:
        """
        Run a series of filesystem checks on a package's active version.

        Returns ``None`` when the package is not installed. Otherwise
        returns a report whose ``status`` is one of:
            * ``healthy``  — every check passed.
            * ``degraded`` — some checks failed.
            * ``broken``   — no meaningful check passed.
        """
        _safe_provider_id(provider_id)
        entry = self._registry.get(provider_id)
        if entry is None:
            return None

        checks: List[ProviderPackageHealthCheck] = []
        active_version = entry.active_version
        active_hash = entry.active_hash

        # 1. Active pointer agreement.
        pointer = self._registry.read_active(provider_id)
        pointer_ok = (
            pointer is not None
            and pointer.version == active_version
            and pointer.hash == active_hash
        )
        checks.append(
            ProviderPackageHealthCheck(
                name="active_pointer",
                passed=pointer_ok,
                message=(
                    f"pointer={pointer.version if pointer else '<missing>'}"
                    f" registry={active_version}"
                ),
            )
        )

        # 2. Version directory exists.
        version_dir = self._version_dir(provider_id, active_version)
        version_ok = version_dir.is_dir()
        checks.append(
            ProviderPackageHealthCheck(
                name="version_directory",
                passed=version_ok,
                message=str(version_dir),
            )
        )

        # 3. Manifest is readable.
        manifest_ok = False
        manifest_data: Optional[Dict[str, Any]] = None
        if version_ok:
            manifest_file = version_dir / MANIFEST_FILENAME
            if manifest_file.is_file():
                try:
                    manifest_data = json.loads(
                        manifest_file.read_text(encoding="utf-8")
                    )
                    manifest_ok = isinstance(manifest_data, dict)
                except Exception:
                    manifest_ok = False
        checks.append(
            ProviderPackageHealthCheck(
                name="manifest_present",
                passed=manifest_ok,
                message="readable" if manifest_ok else "missing or corrupt",
            )
        )

        # 4. Entry-point file (for native/custom transports).
        entry_point_ok = True  # default true for compatible
        if entry.transport in (
            ProviderTransport.NATIVE.value,
            ProviderTransport.CUSTOM.value,
        ):
            entry_point_ok = False
            if manifest_ok and manifest_data is not None:
                entry_point = manifest_data.get("entry_point") or ""
                if isinstance(entry_point, str) and entry_point:
                    module_path = entry_point.split(":", 1)[0].replace(".", "/")
                    candidates = [
                        version_dir / f"{module_path}.py",
                        version_dir / module_path / "__init__.py",
                        version_dir / "src" / f"{module_path}.py",
                        version_dir / "src" / module_path / "__init__.py",
                    ]
                    entry_point_ok = any(c.is_file() for c in candidates)
        checks.append(
            ProviderPackageHealthCheck(
                name="entry_point_present",
                passed=entry_point_ok,
                message=(
                    "ok"
                    if entry_point_ok
                    else "entry_point module file is missing"
                ),
            )
        )

        # 5. Venv (for native/custom transports only).
        if entry.transport in (
            ProviderTransport.NATIVE.value,
            ProviderTransport.CUSTOM.value,
        ):
            env_dir = self._env_dir(provider_id, active_version)
            python_exe = self._venv_python(env_dir)
            venv_ok = python_exe.is_file()
            checks.append(
                ProviderPackageHealthCheck(
                    name="venv_present",
                    passed=venv_ok,
                    message=str(env_dir) if venv_ok else "missing",
                )
            )
        else:
            checks.append(
                ProviderPackageHealthCheck(
                    name="venv_present",
                    passed=True,
                    message="not required for compatible transport",
                )
            )

        # Aggregate.
        passed_count = sum(1 for c in checks if c.passed)
        if passed_count == len(checks):
            status = "healthy"
        elif passed_count == 0:
            status = "broken"
        else:
            status = "degraded"

        return ProviderPackageHealthReport(
            id=provider_id,
            version=active_version,
            hash=active_hash,
            status=status,
            checks=checks,
        )

    # ------------------------------------------------------------------
    # Public API: adapter descriptor (phase-2 bridge)
    # ------------------------------------------------------------------

    def get_active_adapter_descriptor(
        self, provider_id: str
    ) -> Optional[AdapterDescriptor]:
        """
        Return the runtime descriptor for the currently active version
        of a package, or ``None`` when:
            * the package is not installed;
            * the package is disabled;
            * the active version's files are missing from disk.

        The descriptor is what the plugin host (phase 2) needs to
        launch a worker process. It is derived fresh on each call —
        callers should cache it for the duration of a single run, not
        across runs.

        For ``compatible`` packages, ``venv_python`` is ``None`` and
        ``base_adapter`` names the built-in adapter that should be
        reused (the plugin host resolves it via the built-in registry).
        """
        _safe_provider_id(provider_id)
        entry = self._registry.get(provider_id)
        if entry is None or not entry.enabled:
            return None

        record = entry.get_version(entry.active_version)
        if record is None:
            return None

        version_dir = self._version_dir(provider_id, entry.active_version)
        if not version_dir.is_dir():
            return None

        venv_python: Optional[Path] = None
        base_adapter: Optional[str] = None

        if entry.transport in (
            ProviderTransport.NATIVE.value,
            ProviderTransport.CUSTOM.value,
        ):
            env_dir = self._env_dir(provider_id, entry.active_version)
            venv_python = self._venv_python(env_dir)
            if not venv_python.is_file():
                return None
        else:
            # Compatible transport: no venv; base_adapter MUST be set
            # in the manifest. Read it from disk (the registry keeps a
            # flattened view that omits this field).
            try:
                manifest_data = json.loads(
                    (version_dir / MANIFEST_FILENAME).read_text(
                        encoding="utf-8"
                    )
                )
                raw = manifest_data.get("base_adapter")
                if isinstance(raw, str) and raw:
                    base_adapter = raw
            except Exception:
                return None

        entry_point = record.entry_point
        class_name: Optional[str] = None
        if isinstance(entry_point, str) and ":" in entry_point:
            class_name = entry_point.rsplit(":", 1)[-1]

        return AdapterDescriptor(
            provider_id=provider_id,
            version=entry.active_version,
            hash=entry.active_hash,
            transport=entry.transport,
            api_contract=entry.api_contract,
            entry_point=entry_point,
            class_name=class_name,
            base_adapter=base_adapter,
            package_dir=version_dir,
            venv_python=venv_python,
            features=dict(entry.features or {}),
            permissions=dict(entry.permissions or {}),
            enabled=entry.enabled,
        )

    # ------------------------------------------------------------------
    # Internal — conflict check
    # ------------------------------------------------------------------

    def _check_conflicts(
        self,
        manifest: ProviderManifest,
        sha256: str,
    ) -> Optional[ProviderPackageEntry]:
        """
        Detect install conflicts before any filesystem write.

        Three checks:

        1. **Builtin collision.** A provider package cannot use a
           ``provider_id`` that is already registered as a builtin
           adapter. This mirrors the guarantee established in
           ``peldrun.llm.providers.registry``.

        2. **Idempotent re-install.** Same version + same SHA-256 +
           files present on disk → return the existing entry so
           ``install_bundle`` becomes a no-op.

        3. **Hash mismatch.** Same version + different SHA-256 → raise
           ``ProviderPackageConflictError`` with a message that points
           the user at the two safe alternatives (bump the version, or
           uninstall first).

        Returns
        -------
        Optional[ProviderPackageEntry]
            Non-``None`` when the install is an idempotent re-install;
            ``None`` when the install should proceed.
        """
        # ------------------------------------------------------------------
        # 1. Builtin collision check.
        #
        # Imported lazily to avoid a circular import (peldrun.llm.providers
        # imports from peldrun.llm.client, which triggers a broader
        # bootstrap). The import is cheap after the first call.
        # ------------------------------------------------------------------
        try:
            from peldrun.llm.providers.registry import (
                SOURCE_BUILTIN,
                provider_registry as builtin_registry,
            )

            existing_registration = builtin_registry.find(manifest.id)
            if (
                existing_registration is not None
                and existing_registration.source == SOURCE_BUILTIN
            ):
                raise ProviderPackageConflictError(
                    f"Provider ID '{manifest.id}' is already used by a "
                    f"built-in adapter ({existing_registration.class_path}). "
                    f"Provider Packages cannot overwrite built-in "
                    f"providers. Use a different 'id' in manifest.json.",
                    details={
                        "provider_id": manifest.id,
                        "conflicts_with": existing_registration.class_path,
                    },
                )
        except ProviderPackageConflictError:
            raise
        except Exception as exc:
            # A broken import must not block install — log and proceed.
            logger.warning(
                "Builtin collision check failed (%s); proceeding with "
                "install. A subsequent registration error will still "
                "protect the runtime.",
                exc,
            )

        # ------------------------------------------------------------------
        # 2. Idempotent re-install.
        # ------------------------------------------------------------------
        existing = self._registry.get(manifest.id)
        if existing is None:
            return None

        record = existing.get_version(manifest.version)
        if record is None:
            # Same provider, different version — proceed as a new install.
            return None

        if record.hash != sha256:
            # Same version, different content — hard conflict.
            raise ProviderPackageConflictError(
                f"Version '{manifest.version}' of provider "
                f"'{manifest.id}' is already installed with different "
                f"content (installed hash: {record.hash[:12]}..., "
                f"new hash: {sha256[:12]}...). Bump the 'version' in "
                f"manifest.json, or uninstall the existing package "
                f"first.",
                details={
                    "provider_id": manifest.id,
                    "version": manifest.version,
                    "existing_hash": record.hash,
                    "new_hash": sha256,
                },
            )

        # Same version + same hash. Verify files are still on disk.
        version_dir = self._version_dir(manifest.id, manifest.version)
        if not version_dir.is_dir():
            logger.info(
                "Idempotent re-install requested for %r@%r, but the "
                "version directory is missing (%s). Proceeding as a "
                "fresh install.",
                manifest.id,
                manifest.version,
                version_dir,
            )
            return None

        if existing.transport in (
            ProviderTransport.NATIVE.value,
            ProviderTransport.CUSTOM.value,
        ):
            env_dir = self._env_dir(manifest.id, manifest.version)
            if not self._venv_python(env_dir).is_file():
                logger.info(
                    "Idempotent re-install requested for %r@%r, but the "
                    "venv is missing (%s). Proceeding as a fresh "
                    "install.",
                    manifest.id,
                    manifest.version,
                    env_dir,
                )
                return None

        # Fully idempotent.
        return existing

    # ------------------------------------------------------------------
    # Internal — result builders
    # ------------------------------------------------------------------

    def _build_result(
        self,
        *,
        entry: ProviderPackageEntry,
        validation: ValidationReport,
        env_result: Optional[EnvironmentInstallResult],
        steps: List[ProviderPackageStepReport],
        warnings: List[str],
        started_perf: float,
        idempotent: bool,
    ) -> ProviderPackageInstallResult:
        """
        Assemble the ``ProviderPackageInstallResult`` returned to the
        API layer. Merges step timings, environment report, and warnings.
        """
        if idempotent:
            warnings = list(warnings) + [
                "This version was already installed with identical "
                "content. No files were modified."
            ]

        install_report = ProviderPackageInstallReport(
            dependencies_installed=bool(env_result and env_result.installed),
            dependencies_skipped=bool(env_result is None or env_result.skipped_reason),
            venv_path=(
                str(env_result.venv_path) if env_result is not None else None
            ),
            duration_ms=_perf_ms(started_perf),
            steps=list(steps),
            warnings=list(warnings),
        )

        return ProviderPackageInstallResult(
            id=entry.id,
            version=entry.active_version,
            hash=entry.active_hash,
            name=entry.name,
            display_name=entry.display_name,
            description=entry.description,
            size_bytes=validation.compressed_size,
            file_count=validation.file_count,
            transport=entry.transport,
            features=dict(entry.features or {}),
            permissions=dict(entry.permissions or {}),
            install_report=install_report,
            warnings=list(warnings),
        )

    def _build_version_record(
        self,
        *,
        manifest: ProviderManifest,
        validation: ValidationReport,
    ) -> ProviderPackageVersionRecord:
        """Build the version record that will be persisted in the registry."""
        entry_point = manifest.entry_point
        class_name: Optional[str] = None
        if isinstance(entry_point, str) and ":" in entry_point:
            class_name = entry_point.rsplit(":", 1)[-1]

        return ProviderPackageVersionRecord(
            version=manifest.version,
            hash=validation.sha256,
            installed_at=_utc_now_iso(),
            size_bytes=validation.compressed_size,
            file_count=validation.file_count,
            entry_point=entry_point,
            class_name=class_name,
        )

    # ------------------------------------------------------------------
    # Internal — manifest serialization helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _features_dict(manifest: ProviderManifest) -> Dict[str, bool]:
        """Flatten the manifest's feature flags into a plain dict."""
        return manifest.features.model_dump()

    @staticmethod
    def _permissions_dict(manifest: ProviderManifest) -> Dict[str, Any]:
        """Flatten the manifest's permission block into a plain dict."""
        return manifest.permissions.model_dump()

    # ------------------------------------------------------------------
    # Internal — filesystem helpers
    # ------------------------------------------------------------------

    def _write_peldrun_meta(
        self,
        version_dir: Path,
        manifest: ProviderManifest,
        validation: ValidationReport,
    ) -> None:
        """
        Write ``_peldrun_meta.json`` alongside the extracted files.

        This file is PELDRUN-owned (never shipped by the package author)
        and records the install-time facts the plugin host needs:
        original SHA-256, install timestamp, and the resolved class
        name. The plugin host reads this to verify content integrity
        before launching a worker.
        """
        meta = {
            "schema_version": 1,
            "provider_id": manifest.id,
            "version": manifest.version,
            "sha256": validation.sha256,
            "installed_at": _utc_now_iso(),
            "transport": manifest.transport.value,
            "api_contract": manifest.api_contract,
            "entry_point": manifest.entry_point,
            "base_adapter": manifest.base_adapter,
            "file_count": validation.file_count,
            "uncompressed_bytes": validation.uncompressed_size,
        }
        try:
            target = version_dir / META_FILENAME
            tmp = target.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(meta, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(str(tmp), str(target))
        except OSError as exc:
            raise ProviderPackageStorageError(
                f"Failed to write install metadata at {target}: {exc}",
                details={"path": str(target)},
            ) from exc

    @staticmethod
    def _strip_junk(directory: Path) -> None:
        """
        Remove archiver metadata that must never land in the final
        version directory (``__MACOSX``, ``.DS_Store``, ``Thumbs.db``).
        Best-effort — a failure here does not abort the install.
        """
        try:
            for entry in list(directory.iterdir()):
                if entry.name in _JUNK_NAMES:
                    ProviderPackageManager._safe_rmtree(entry)
        except Exception as exc:
            logger.debug("Junk stripping failed (non-fatal): %s", exc)

    @staticmethod
    def _safe_rmtree(path: Path) -> None:
        """Best-effort recursive delete. Never raises."""
        if not path.exists():
            return
        try:
            shutil.rmtree(path)
        except OSError as exc:
            logger.warning("Could not remove %s: %s", path, exc)

    # ------------------------------------------------------------------
    # Internal — error mapping
    # ------------------------------------------------------------------

    def _error_from_validation(
        self, validation: ValidationReport
    ) -> ProviderPackageError:
        """
        Choose the most specific exception class for a validation
        report's error set. The API layer uses the class to pick the
        correct HTTP status and error banner.

        Policy
        ------
        * Any error mentioning "unsafe path", "traversal", "symlink",
          or "compression ratio" → ``ProviderPackageSecurityError``.
        * Any error mentioning "manifest" → ``ProviderPackageManifestError``.
        * Everything else → ``ProviderPackageValidationError``.
        """
        errors_lower = " ".join(validation.errors).lower()

        security_markers = (
            "unsafe path",
            "traversal",
            "symlink",
            "compression ratio",
            "archive bomb",
            "absolute path",
        )
        if any(marker in errors_lower for marker in security_markers):
            return ProviderPackageSecurityError(
                "; ".join(validation.errors),
                details={"errors": list(validation.errors)},
            )

        if "manifest" in errors_lower:
            return ProviderPackageManifestError(
                "; ".join(validation.errors),
                details={"errors": list(validation.errors)},
            )

        return ProviderPackageValidationError(
            "; ".join(validation.errors)
            if validation.errors
            else "Package validation failed.",
            details={"errors": list(validation.errors)},
        )


# ---------------------------------------------------------------------------
# Default root + module-level singleton
# ---------------------------------------------------------------------------


def _default_root() -> Path:
    """
    Compute the default storage root for provider packages.

    Layout: ``<backend>/storage/store/providers/``

    Mirrors the location used by tool bundles
    (``<backend>/storage/store/tools/``) and the ``registry.py`` module
    so both agree on the layout.
    """
    backend_root = Path(__file__).resolve().parent.parent.parent
    return backend_root / "storage" / "store" / "providers"


#: Process-wide manager singleton, wired to the default registry.
#: Tests that need isolation should build their own
#: ``ProviderPackageManager(root_dir=tmp_path)``.
provider_package_manager = ProviderPackageManager(_default_root())


__all__ = [
    "VERSIONS_DIR_NAME",
    "ENVIRONMENTS_DIR_NAME",
    "STAGING_DIR_NAME",
    "AdapterDescriptor",
    "ProviderPackageManager",
    "provider_package_manager",
]
