"""
High-level orchestration for tool bundle lifecycle.

`BundleManager` ties together the validator, extractor, storage layer, and
the persistent registry. It exposes the operations the API surface will
call:

    install_bundle(zip_bytes, filename) -> BundleInstallResult
    list_bundles()                       -> List[BundleEntry]
    get_bundle(bundle_id)                -> Optional[BundleEntry]
    delete_bundle(bundle_id, force)      -> bool
    activate_version(bundle_id, version) -> bool
    check_health(bundle_id)              -> BundleHealthReport

Installation flow:

    1. Validate the zip (structure + security).
    2. Extract into a staging directory.
    3. Read and validate the manifest.
    4. Compute the content hash.
    5. Commit the blob (idempotent).
    6. Update the registry (atomic).
    7. Point the active pointer at the new hash (atomic).

All state-changing operations are atomic and idempotent.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from omweb.tools.bundles.errors import (
    BundleError,
    BundleManifestError,
    BundleStorageError,
    BundleValidationError,
)
from omweb.tools.bundles.extractor import safe_extract
from omweb.tools.bundles.schema import (
    BundleEntry,
    BundleHealthReport,
    BundleInstallResult,
    BundleManifest,
    BundleRegistryIndex,
    BundleVersionRecord,
    HealthCheck,
    InstallReport,
    InstallStep,
)
from omweb.tools.bundles.storage import (
    ACTIVE_POINTER,
    MANIFEST_FILENAME,
    META_FILENAME,
    BundleStorage,
    compute_bundle_hash,
)
from omweb.tools.bundles.validator import (
    parse_manifest,
    validate_zip,
)

logger = logging.getLogger("omweb.tools.bundles.manager")

REGISTRY_FILENAME = "registry.json"


# ---------------------------------------------------------------------------
# BundleManager
# ---------------------------------------------------------------------------

class BundleManager:
    """Coordinates the full lifecycle of installed tool bundles."""

    def __init__(self, root_dir: Path) -> None:
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)

        self.storage = BundleStorage(self.root_dir)
        self.registry_file = self.root_dir / REGISTRY_FILENAME

        self._registry_cache: Optional[BundleRegistryIndex] = None

    # ── Registry I/O ──────────────────────────────────────────────────────

    def _load_registry(self) -> BundleRegistryIndex:
        """Read registry.json from disk, or synthesize a fresh one."""
        if self._registry_cache is not None:
            return self._registry_cache

        if not self.registry_file.exists():
            self._registry_cache = BundleRegistryIndex()
            return self._registry_cache

        try:
            raw = json.loads(self.registry_file.read_text(encoding="utf-8"))
            self._registry_cache = BundleRegistryIndex.model_validate(raw)
        except Exception as exc:
            logger.warning(
                "Registry file unreadable (%s); starting fresh.", exc
            )
            self._registry_cache = BundleRegistryIndex()

        return self._registry_cache

    def _save_registry(self, registry: BundleRegistryIndex) -> None:
        """Atomically persist the registry and update the cache."""
        registry.updated_at = datetime.now(timezone.utc).isoformat()
        tmp = self.registry_file.parent / (self.registry_file.name + ".tmp")
        tmp.write_text(
            json.dumps(registry.model_dump(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(str(tmp), str(self.registry_file))
        self._registry_cache = registry

    # ── Installation ──────────────────────────────────────────────────────

    def install_bundle(
        self,
        zip_bytes: bytes,
        source_filename: str = "bundle.zip",
        *,
        install_requirements: bool = True,
        requirements_timeout_seconds: int = 60,
    ) -> BundleInstallResult:
        """
        Install a bundle from a raw zip payload.

        This is the primary entry point used by the HTTP layer.

        Args:
            zip_bytes: Raw bytes of the uploaded .zip.
            source_filename: Original filename (for audit trail only).
            install_requirements: If True and requirements.txt is present,
                run `pip install -r requirements.txt` inside the bundle.
            requirements_timeout_seconds: Hard timeout for pip.

        Returns:
            A `BundleInstallResult` describing what was installed.

        Raises:
            BundleValidationError: if the zip fails validation.
            BundleManifestError: if the manifest is malformed.
            BundleStorageError: if disk operations fail.
        """
        started = time.perf_counter()
        steps: List[InstallStep] = []
        warnings: List[str] = []

        # ── 1. Validate ──────────────────────────────────────────────────
        t0 = time.perf_counter()
        validation_report = validate_zip(zip_bytes)
        steps.append(InstallStep(
            name="validate",
            status="ok" if validation_report.ok else "failed",
            duration_ms=int((time.perf_counter() - t0) * 1000),
            detail=f"{validation_report.total_files} files",
        ))
        if not validation_report.ok:
            raise BundleValidationError(
                "Zip payload failed validation.",
                details={"errors": validation_report.errors},
            )
        warnings.extend(validation_report.warnings)

        # ── 2. Extract to staging ────────────────────────────────────────
        staging_dir = self.storage.make_temp_dir()
        t0 = time.perf_counter()
        try:
            extract_report = safe_extract(
                zip_bytes, staging_dir,
                validation_report=validation_report,
            )
            steps.append(InstallStep(
                name="extract",
                status="ok" if extract_report.ok else "failed",
                duration_ms=int((time.perf_counter() - t0) * 1000),
                detail=f"{extract_report.extracted_files} files, "
                       f"{extract_report.total_bytes} bytes",
            ))
            if not extract_report.ok:
                raise BundleStorageError(
                    "Extraction failed.",
                    details={"failed_files": extract_report.failed_files},
                )
        except Exception:
            # Cleanup staging on any extraction failure
            import shutil
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise

        # ── 3. Parse manifest ────────────────────────────────────────────
        t0 = time.perf_counter()
        manifest_file = staging_dir / MANIFEST_FILENAME
        if not manifest_file.is_file():
            import shutil
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise BundleManifestError("manifest.json missing after extraction.")

        try:
            manifest_dict = json.loads(manifest_file.read_text(encoding="utf-8"))
            manifest: BundleManifest = parse_manifest(manifest_dict)
        except Exception as exc:
            import shutil
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise BundleManifestError(
                f"Manifest could not be parsed: {exc}",
            ) from exc

        steps.append(InstallStep(
            name="manifest",
            status="ok",
            duration_ms=int((time.perf_counter() - t0) * 1000),
            detail=f"{manifest.id}@{manifest.version}",
        ))

        # Verify entry point actually exists
        entry_file = staging_dir / manifest.entry_point
        if not entry_file.is_file():
            import shutil
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise BundleManifestError(
                f"entry_point {manifest.entry_point!r} not found in bundle.",
                details={"bundle_id": manifest.id},
            )

        # ── 4. Optional pip install ──────────────────────────────────────
        # Collect pip data into plain locals; the frozen InstallReport is
        # built exactly once at the end of the method.
        pip_steps: List[InstallStep] = []
        pip_installed: bool = False
        pip_skipped: bool = True

        req_file = staging_dir / "requirements.txt"
        if install_requirements and req_file.is_file():
            t0 = time.perf_counter()
            ok, detail = self._run_pip_install(
                req_file, timeout_seconds=requirements_timeout_seconds,
            )
            pip_duration_ms = int((time.perf_counter() - t0) * 1000)
            pip_installed = ok
            pip_skipped = False
            pip_steps.append(InstallStep(
                name="pip",
                status="ok" if ok else "failed",
                duration_ms=pip_duration_ms,
                detail=detail[:500],
            ))
            if not ok:
                warnings.append(f"pip install reported: {detail[:200]}")

        # ── 5. Compute hash ──────────────────────────────────────────────
        hash_short = compute_bundle_hash(zip_bytes)

        # ── 6. Commit blob (idempotent) ──────────────────────────────────
        t0 = time.perf_counter()
        meta: Dict[str, Any] = {
            "hash_short": hash_short,
            "bundle_id": manifest.id,
            "version": manifest.version,
            "name": manifest.name,
            "entry_point": manifest.entry_point,
            "class_name": manifest.class_name,
            "capabilities": [c.value for c in manifest.capabilities],
            "tags": manifest.tags,
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "source_filename": source_filename,
            "size_bytes": len(zip_bytes),
            "file_count": validation_report.total_files,
        }

        try:
            self.storage.commit_blob(staging_dir, hash_short, meta)
            steps.append(InstallStep(
                name="blob_commit",
                status="ok",
                duration_ms=int((time.perf_counter() - t0) * 1000),
                detail=f"hash={hash_short}",
            ))
        except Exception as exc:
            raise BundleStorageError(
                f"Failed to commit blob: {exc}",
                details={"hash": hash_short},
            ) from exc

        # ── 7. Update registry ───────────────────────────────────────────
        t0 = time.perf_counter()
        registry = self._load_registry()
        now_iso = datetime.now(timezone.utc).isoformat()

        version_record = BundleVersionRecord(
            version=manifest.version,
            hash=hash_short,
            installed_at=now_iso,
            size_bytes=len(zip_bytes),
            file_count=validation_report.total_files,
            entry_point=manifest.entry_point,
            class_name=manifest.class_name,
        )

        existing_entry = registry.bundles.get(manifest.id)
        if existing_entry is None:
            entry = BundleEntry(
                id=manifest.id,
                name=manifest.name,
                description=manifest.description,
                active_version=manifest.version,
                active_hash=hash_short,
                installed_versions=[version_record],
                safety_level=manifest.safety_level,
                capabilities=[c.value for c in manifest.capabilities],
                tags=manifest.tags,
                author=manifest.author,
                homepage=manifest.homepage,
            )
            registry.bundles[manifest.id] = entry
        else:
            # Version bump or re-install of existing version
            existing_versions = {v.version for v in existing_entry.installed_versions}
            if manifest.version not in existing_versions:
                existing_entry.installed_versions.append(version_record)
            else:
                # Replacing an existing version: update its record in place
                existing_entry.installed_versions = [
                    version_record if v.version == manifest.version else v
                    for v in existing_entry.installed_versions
                ]
            existing_entry.active_version = manifest.version
            existing_entry.active_hash = hash_short
            existing_entry.name = manifest.name
            existing_entry.description = manifest.description
            existing_entry.safety_level = manifest.safety_level
            existing_entry.capabilities = [c.value for c in manifest.capabilities]
            existing_entry.tags = manifest.tags
            existing_entry.author = manifest.author or existing_entry.author
            existing_entry.homepage = manifest.homepage or existing_entry.homepage

        self._save_registry(registry)
        steps.append(InstallStep(
            name="register",
            status="ok",
            duration_ms=int((time.perf_counter() - t0) * 1000),
            detail=f"version={manifest.version}",
        ))

        # ── 8. Point active pointer at new hash ──────────────────────────
        self.storage.set_active_hash(manifest.id, hash_short)

        # ── 9. Build the final, immutable InstallReport ──────────────────
        total_ms = int((time.perf_counter() - started) * 1000)
        install_report = InstallReport(
            requirements_installed=pip_installed,
            requirements_skipped=pip_skipped,
            duration_ms=total_ms,
            steps=steps + pip_steps,
            warnings=[],
        )

        return BundleInstallResult(
            id=manifest.id,
            version=manifest.version,
            hash=hash_short,
            name=manifest.name,
            description=manifest.description,
            size_bytes=len(zip_bytes),
            file_count=validation_report.total_files,
            capabilities=[c.value for c in manifest.capabilities],
            tags=manifest.tags,
            install_report=install_report,
            warnings=warnings,
        )

    # ── Queries ───────────────────────────────────────────────────────────

    def list_bundles(self) -> List[BundleEntry]:
        registry = self._load_registry()
        return list(registry.bundles.values())

    def get_bundle(self, bundle_id: str) -> Optional[BundleEntry]:
        registry = self._load_registry()
        return registry.bundles.get(bundle_id)

    # ── Deletion ──────────────────────────────────────────────────────────

    def delete_bundle(self, bundle_id: str, force: bool = False) -> bool:
        """
        Remove a bundle entirely.

        By default, this refuses if the bundle has more than one installed
        version (to prevent accidental loss of a rollback target). Use
        `force=True` to remove all versions.
        """
        registry = self._load_registry()
        entry = registry.bundles.get(bundle_id)
        if entry is None:
            return False

        if len(entry.installed_versions) > 1 and not force:
            raise BundleError(
                f"Bundle {bundle_id!r} has {len(entry.installed_versions)} "
                "installed versions. Pass force=True to remove all.",
                code="BUNDLE_DELETE_REFUSED",
                details={"versions": [v.version for v in entry.installed_versions]},
            )

        # Delete every blob this bundle ever pointed at
        for version_record in entry.installed_versions:
            self.storage.delete_blob(version_record.hash)

        # Delete the install pointer
        self.storage.delete_install(bundle_id)

        # Update registry
        del registry.bundles[bundle_id]
        self._save_registry(registry)
        return True

    # ── Version activation ────────────────────────────────────────────────

    def activate_version(self, bundle_id: str, version: str) -> bool:
        """Atomically switch the active pointer to a previously installed version."""
        registry = self._load_registry()
        entry = registry.bundles.get(bundle_id)
        if entry is None:
            return False

        record = entry.get_version(version)
        if record is None:
            return False

        if not self.storage.blob_exists(record.hash):
            raise BundleStorageError(
                f"Blob for {bundle_id}@{version} is missing.",
                details={"hash": record.hash},
            )

        self.storage.set_active_hash(bundle_id, record.hash)
        entry.active_version = version
        entry.active_hash = record.hash
        self._save_registry(registry)
        return True

    # ── Health ────────────────────────────────────────────────────────────

    def check_health(self, bundle_id: str) -> Optional[BundleHealthReport]:
        """Perform a series of filesystem checks on the bundle's active version."""
        registry = self._load_registry()
        entry = registry.bundles.get(bundle_id)
        if entry is None:
            return None

        checks: List[HealthCheck] = []
        active_hash = self.storage.get_active_hash(bundle_id)

        # 1. Active pointer matches registry
        checks.append(HealthCheck(
            name="active_pointer",
            passed=(active_hash == entry.active_hash),
            message=f"pointer={active_hash} registry={entry.active_hash}",
        ))

        # 2. Blob directory exists
        blob_present = self.storage.blob_exists(entry.active_hash)
        checks.append(HealthCheck(
            name="blob_present",
            passed=blob_present,
            message=f"hash={entry.active_hash}",
        ))

        # 3. Manifest readable inside the blob
        manifest_ok = False
        if blob_present:
            manifest_file = self.storage.blob_dir(entry.active_hash) / MANIFEST_FILENAME
            manifest_ok = manifest_file.is_file()
        checks.append(HealthCheck(
            name="manifest_present",
            passed=manifest_ok,
            message="manifest.json readable" if manifest_ok else "missing",
        ))

        # 4. Entry point file exists
        entry_point_ok = False
        if blob_present and manifest_ok:
            try:
                manifest_dict = json.loads(
                    (self.storage.blob_dir(entry.active_hash) / MANIFEST_FILENAME)
                    .read_text(encoding="utf-8")
                )
                entry_point = manifest_dict.get("entry_point", "")
                entry_point_ok = self.storage.blob_entry_point_exists(
                    entry.active_hash, entry_point,
                )
            except Exception:
                entry_point_ok = False
        checks.append(HealthCheck(
            name="entry_point_present",
            passed=entry_point_ok,
            message="ok" if entry_point_ok else "missing",
        ))

        # 5. Meta file present
        meta_ok = self.storage.read_blob_meta(entry.active_hash) is not None
        checks.append(HealthCheck(
            name="meta_present",
            passed=meta_ok,
            message="ok" if meta_ok else "missing",
        ))

        # Aggregate
        passed_count = sum(1 for c in checks if c.passed)
        if passed_count == len(checks):
            status = "healthy"
        elif passed_count == 0:
            status = "broken"
        else:
            status = "degraded"

        return BundleHealthReport(
            id=bundle_id,
            version=entry.active_version,
            hash=entry.active_hash,
            status=status,
            checks=checks,
        )

    # ── Cleanup ───────────────────────────────────────────────────────────

    def cleanup_orphans(self, older_than_seconds: int = 3600) -> Dict[str, int]:
        """
        Remove blobs no longer referenced by any registered bundle, and
        abandon staging directories from crashed installs.
        """
        registry = self._load_registry()
        active_hashes: Set[str] = {
            v.hash
            for entry in registry.bundles.values()
            for v in entry.installed_versions
        }

        orphaned = self.storage.list_orphaned_blobs(active_hashes)
        removed_blobs = 0
        for h in orphaned:
            if self.storage.delete_blob(h):
                removed_blobs += 1

        removed_temp = self.storage.cleanup_orphan_temp_dirs(
            older_than_seconds=older_than_seconds,
        )

        return {
            "removed_orphan_blobs": removed_blobs,
            "removed_temp_dirs": removed_temp,
            "active_blob_count": len(active_hashes),
        }

    # ── Internal helpers ──────────────────────────────────────────────────

    @staticmethod
    def _run_pip_install(
        requirements_file: Path,
        *,
        timeout_seconds: int = 60,
    ) -> tuple[bool, str]:
        """
        Run `pip install -r requirements.txt`.

        Returns (success, human_readable_detail). Never raises.
        """
        try:
            proc = subprocess.run(
                [
                    sys.executable, "-m", "pip", "install",
                    "--disable-pip-version-check",
                    "--no-input",
                    "-r", str(requirements_file),
                ],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            if proc.returncode == 0:
                tail = (proc.stdout or "").strip().splitlines()[-3:]
                return True, " | ".join(tail) or "installed"
            else:
                err = (proc.stderr or proc.stdout or "").strip()
                return False, err[-500:] if err else "pip exited with error"
        except subprocess.TimeoutExpired:
            return False, f"pip timed out after {timeout_seconds}s"
        except Exception as exc:
            return False, f"pip invocation failed: {exc}"


__all__ = ["BundleManager", "REGISTRY_FILENAME"]
