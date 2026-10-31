# backend/omweb/skills/storage.py
"""
On-disk storage layer for installed skill packages.

Responsibilities:
    * Extract uploaded zip payloads safely by reusing the battle-tested
      ``omweb.tools.bundles.validator`` and ``omweb.tools.bundles.extractor``.
    * Normalize the ``id`` field in the manifest before schema validation,
      so authors can use ``code-review`` / ``Code Review`` / ``123-skill``
      forms and still get a valid package installed.
    * Atomically commit a validated package to its final location at
      ``<skills_root>/<skill_id>/``.
    * Track per-skill install metadata in
      ``<skills_root>/<skill_id>/.installed.json`` (version, hash,
      install time, source filename).
    * Remove a skill atomically via a trash-then-delete pattern.
    * Provide directory iteration for ``registry`` discovery.
    * Clean up leftover temp / trash dirs from crashed installs.

Design notes:
    * This module OWNS the on-disk layout of installed skills. The
      registry reads from it, the manager mutates through it, and
      everything else reads via the registry's in-memory cache.
    * We do NOT support multi-version rollback for skills (unlike tool
      bundles). A skill upgrade fully replaces the previous package.
      Skill content (SKILL.md, references) is not independently
      addressable at runtime the way tool code is — the instruction
      block is always the "current" one.
    * Reuses ``validate_zip`` + ``safe_extract`` from the tool bundles
      subsystem to avoid duplicating ~600 lines of security-critical
      archive handling. Those functions are package-agnostic — they
      check path traversal, symlinks, size caps, and entry counts, but
      do NOT assume any particular manifest schema.
    * The only bundle-specific behavior we do NOT reuse is
      ``parse_manifest`` (which validates the tool-bundle manifest
      schema). Skills use their own ``load_skill_package`` from
      ``omweb.skills.manifest``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import shutil
import time
from typing import Any, Dict, List, Optional, Tuple
import uuid

from omweb.skills.errors import (
    SkillExtractionError,
    SkillManifestError,
    SkillSecurityError,
    SkillStorageError,
    SkillValidationError,
)
from omweb.skills.manifest import (
    MANIFEST_FILENAME,
    ParsedSkillPackage,
    load_skill_package,
)
from omweb.tools.bundles.errors import (
    BundleError as _BundleError,
    BundleExtractionError as _BundleExtractionError,
    BundleManifestError as _BundleManifestError,
    BundleSecurityError as _BundleSecurityError,
    BundleValidationError as _BundleValidationError,
)
from omweb.tools.bundles.extractor import safe_extract
from omweb.tools.bundles.validator import validate_zip


logger = logging.getLogger("omweb.skills.storage")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INSTALLED_META_FILENAME = ".installed.json"
INSTALLED_META_SCHEMA_VERSION = 1
TEMP_PREFIX = ".tmp_"
TRASH_PREFIX = ".trash_"
HASH_HEX_LENGTH = 16


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def compute_skill_hash(zip_bytes: bytes) -> str:
    """
    Return ``sha256:<first 16 hex chars>`` of the given payload.

    The prefix matches the format used by the manifest's optional
    ``signature`` field so a future verifier can compare values without
    extra parsing.
    """
    full = hashlib.sha256(zip_bytes).hexdigest()
    return f"sha256:{full[:HASH_HEX_LENGTH]}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_rmtree(path: Path, retries: int = 3) -> bool:
    """
    Best-effort recursive removal with retries.

    On Windows, files can briefly appear locked due to antivirus scans
    or lingering handles. Three short retries cover the common cases
    without blocking a request for long.
    """
    if not path.exists():
        return True
    for attempt in range(retries):
        try:
            shutil.rmtree(path)
            return True
        except Exception as exc:
            if attempt < retries - 1:
                time.sleep(0.3 * (attempt + 1))
            else:
                logger.warning(
                    "Failed to remove %s after %d attempts: %s",
                    path,
                    retries,
                    exc,
                )
    return not path.exists()


def _retry_rename(src: Path, dst: Path, attempts: int = 3) -> None:
    """
    Retry ``os.rename`` a few times to tolerate transient Windows locks.

    Raises the underlying ``OSError`` after exhausting attempts so the
    caller can wrap it into a ``SkillStorageError`` with context.
    """
    last_exc: Optional[OSError] = None
    for attempt in range(attempts):
        try:
            os.rename(str(src), str(dst))
            return
        except OSError as exc:
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(0.15 * (attempt + 1))
    assert last_exc is not None
    raise last_exc


def _atomic_rename(src: Path, dst: Path) -> None:
    """
    Rename ``src`` -> ``dst`` with atomic-replace semantics on the same
    filesystem.

    If ``dst`` already exists:
        1. Move ``dst`` to a ``.trash_<uuid>`` sibling.
        2. Rename ``src`` into place.
        3. On success, remove the trash directory.
        4. On failure, restore ``dst`` from the trash so the previous
           install is preserved intact.

    Raises:
        SkillStorageError: on any filesystem failure that leaves the
            on-disk state uncertain.
    """
    parent = dst.parent
    parent.mkdir(parents=True, exist_ok=True)

    trash: Optional[Path] = None

    if dst.exists():
        trash = parent / f"{TRASH_PREFIX}{uuid.uuid4().hex[:12]}"
        try:
            _retry_rename(dst, trash)
        except OSError as exc:
            raise SkillStorageError(
                f"Failed to move existing skill dir aside: {exc}",
                details={"source": str(dst), "trash": str(trash)},
            ) from exc

    try:
        _retry_rename(src, dst)
    except OSError as exc:
        if trash is not None:
            try:
                _retry_rename(trash, dst)
            except Exception as restore_exc:
                logger.error(
                    "CRITICAL: failed to restore %s from %s after rename "
                    "failure: %s",
                    dst,
                    trash,
                    restore_exc,
                )
        raise SkillStorageError(
            f"Failed to commit skill package: {exc}",
            details={"source": str(src), "target": str(dst)},
        ) from exc

    if trash is not None:
        _safe_rmtree(trash)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SkillInstallResult:
    """Immutable result of a successful ``install_skill`` call."""

    skill_id: str
    version: str
    content_hash: str
    name: str
    description: str
    size_bytes: int
    file_count: int
    source_filename: str
    installed_at: str
    was_upgrade: bool
    warnings: Tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# SkillStorage
# ---------------------------------------------------------------------------

class SkillStorage:
    """
    Filesystem layout manager for installed skills.

    Owns the ``<skills_root>/`` directory tree. Every operation that
    touches disk lives here; the registry never writes and the manager
    never extracts.
    """

    def __init__(self, root_dir: Path) -> None:
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)

    # ── Read paths ────────────────────────────────────────────────────

    def skill_dir(self, skill_id: str) -> Path:
        """Absolute path to the skill's package directory."""
        return self.root_dir / skill_id

    def installed_metadata_path(self, skill_id: str) -> Path:
        """Absolute path to the skill's install-metadata sidecar."""
        return self.skill_dir(skill_id) / INSTALLED_META_FILENAME

    def skill_exists(self, skill_id: str) -> bool:
        """True when the skill package directory holds a manifest."""
        return (self.skill_dir(skill_id) / MANIFEST_FILENAME).is_file()

    def list_skill_dirs(self) -> List[Path]:
        """
        Return every directory that looks like an installed skill
        package (contains a ``manifest.json``).

        Excludes ``.tmp_*`` staging dirs, ``.trash_*`` cleanup dirs, and
        any hidden entry (``.`` prefix). Sorted by name for
        deterministic iteration.
        """
        if not self.root_dir.is_dir():
            return []
        out: List[Path] = []
        for entry in sorted(self.root_dir.iterdir()):
            if not entry.is_dir():
                continue
            name = entry.name
            if name.startswith("."):
                continue
            if not (entry / MANIFEST_FILENAME).is_file():
                continue
            out.append(entry)
        return out

    def read_installed_metadata(
        self, skill_id: str
    ) -> Optional[Dict[str, Any]]:
        """Read ``.installed.json`` for a skill, or ``None`` when absent."""
        path = self.installed_metadata_path(skill_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                "Failed to read install metadata for %r: %s", skill_id, exc
            )
            return None
        return data if isinstance(data, dict) else None

    # ── Temp dirs ─────────────────────────────────────────────────────

    def make_temp_dir(self) -> Path:
        """Allocate a fresh staging dir under ``<root>/.tmp_<uuid>/``."""
        temp = self.root_dir / f"{TEMP_PREFIX}{uuid.uuid4().hex[:12]}"
        temp.mkdir(parents=True, exist_ok=False)
        return temp

    def cleanup_orphan_temp_dirs(
        self, older_than_seconds: int = 3600
    ) -> int:
        """Remove ``.tmp_*`` staging dirs older than the given threshold."""
        if not self.root_dir.is_dir():
            return 0
        cutoff = time.time() - older_than_seconds
        removed = 0
        for entry in self.root_dir.iterdir():
            if not entry.is_dir():
                continue
            if not entry.name.startswith(TEMP_PREFIX):
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    if _safe_rmtree(entry):
                        removed += 1
            except OSError:
                continue
        if removed:
            logger.info(
                "Cleaned up %d orphan skill temp dir(s).", removed
            )
        return removed

    def cleanup_orphan_trash_dirs(self) -> int:
        """Remove leftover ``.trash_*`` dirs from a previous crash."""
        if not self.root_dir.is_dir():
            return 0
        removed = 0
        for entry in self.root_dir.iterdir():
            if not entry.is_dir():
                continue
            if not entry.name.startswith(TRASH_PREFIX):
                continue
            if _safe_rmtree(entry):
                removed += 1
        if removed:
            logger.info(
                "Cleaned up %d leftover skill trash dir(s).", removed
            )
        return removed

    # ── Install pipeline ──────────────────────────────────────────────

    def install_skill(
        self,
        zip_bytes: bytes,
        *,
        source_filename: str = "skill.zip",
    ) -> SkillInstallResult:
        """
        Run the full install pipeline: validate, extract, normalize,
        verify, and atomically commit.

        Any failure cleans up the staging directory before re-raising
        a SkillError subclass. On success, the previous package (if any)
        has been atomically replaced.
        """
        if not isinstance(zip_bytes, (bytes, bytearray)):
            raise SkillValidationError(
                f"zip_bytes must be bytes, got {type(zip_bytes).__name__}.",
                details={"reason": "invalid_payload_type"},
            )
        if not zip_bytes:
            raise SkillValidationError(
                "Uploaded skill archive is empty.",
                details={"reason": "empty_upload"},
            )

        started = time.perf_counter()
        warnings: List[str] = []

        # ── 1. Structural zip validation (shared with tool bundles)
        try:
            validation_report = validate_zip(zip_bytes)
        except _BundleValidationError as exc:
            raise SkillValidationError(
                f"Skill archive failed validation: {exc}",
                details=dict(exc.details),
            ) from exc

        if not validation_report.ok:
            raise SkillValidationError(
                "Skill archive failed structural validation.",
                details={"errors": list(validation_report.errors)},
            )
        if validation_report.warnings:
            warnings.extend(validation_report.warnings)

        # ── 2. Extract to a staging directory
        staging: Optional[Path] = self.make_temp_dir()
        try:
            try:
                extract_report = safe_extract(
                    zip_bytes,
                    staging,
                    validation_report=validation_report,
                )
            except _BundleExtractionError as exc:
                raise SkillExtractionError(
                    f"Failed to extract skill archive: {exc}",
                    details=dict(exc.details),
                ) from exc
            except _BundleSecurityError as exc:
                raise SkillSecurityError(
                    f"Skill archive violated security policy: {exc}",
                    details=dict(exc.details),
                ) from exc
            except _BundleManifestError as exc:
                raise SkillManifestError(
                    f"Skill archive manifest error: {exc}",
                    details=dict(exc.details),
                ) from exc
            except _BundleError as exc:
                raise SkillExtractionError(
                    f"Skill archive error: {exc}",
                    details=dict(exc.details),
                ) from exc

            if not extract_report.ok:
                raise SkillExtractionError(
                    "Skill archive extraction reported failures.",
                    details={
                        "failed_files": extract_report.failed_files,
                        "steps": [
                            s.model_dump()
                            for s in extract_report.steps
                            if s.status == "failed"
                        ],
                    },
                )

            # ── 3. Normalize manifest.id before schema validation
            manifest_path = staging / MANIFEST_FILENAME
            if not manifest_path.is_file():
                raise SkillManifestError(
                    f"Skill archive is missing {MANIFEST_FILENAME} at "
                    "the archive root. Note: skill packages must be "
                    "flat (no wrapping directory).",
                    details={"staging_dir": str(staging)},
                )

            original_id, normalized_id, rename_warning = (
                self._normalize_manifest_id_inplace(manifest_path)
            )
            if rename_warning:
                warnings.append(rename_warning)

            # ── 4. Full validation via load_skill_package
            parsed: ParsedSkillPackage = load_skill_package(staging)

            if parsed.manifest["id"] != normalized_id:
                # Defensive; load_skill_package re-reads the file we
                # just rewrote, so this should be unreachable.
                raise SkillManifestError(
                    "Internal error: normalized ID mismatch after rewrite.",
                    details={
                        "original": original_id,
                        "normalized": normalized_id,
                        "post_validation": parsed.manifest["id"],
                    },
                )

            # ── 5. Idempotency check (same version + hash -> no-op)
            was_upgrade = self.skill_exists(normalized_id)
            incoming_hash = compute_skill_hash(zip_bytes)

            if was_upgrade:
                existing_meta = self.read_installed_metadata(normalized_id)
                if (
                    isinstance(existing_meta, dict)
                    and existing_meta.get("version") == parsed.manifest["version"]
                    and existing_meta.get("content_hash") == incoming_hash
                ):
                    logger.info(
                        "Skill %r@%s already installed with identical "
                        "content hash; skipping reinstall.",
                        normalized_id,
                        parsed.manifest["version"],
                    )
                    _safe_rmtree(staging)
                    staging = None
                    return SkillInstallResult(
                        skill_id=normalized_id,
                        version=str(parsed.manifest["version"]),
                        content_hash=incoming_hash,
                        name=str(
                            existing_meta.get("name")
                            or parsed.manifest.get("name")
                            or normalized_id
                        ),
                        description=str(
                            existing_meta.get("description")
                            or parsed.manifest.get("description")
                            or ""
                        ),
                        size_bytes=int(
                            existing_meta.get("size_bytes")
                            or len(zip_bytes)
                        ),
                        file_count=int(
                            existing_meta.get("file_count")
                            or validation_report.total_files
                        ),
                        source_filename=str(
                            existing_meta.get("source_filename")
                            or source_filename
                        ),
                        installed_at=str(
                            existing_meta.get("installed_at") or _now_iso()
                        ),
                        was_upgrade=False,
                        warnings=tuple(warnings),
                    )

                # Warn when the version was reused but content differs.
                if (
                    isinstance(existing_meta, dict)
                    and existing_meta.get("version")
                    == parsed.manifest["version"]
                ):
                    warnings.append(
                        f"Reinstalling existing version "
                        f"{parsed.manifest['version']!r} with different "
                        f"content. Consider bumping the version."
                    )

            # ── 6. Write .installed.json sidecar into the staging dir
            installed_at = _now_iso()
            meta = {
                "schema_version": INSTALLED_META_SCHEMA_VERSION,
                "skill_id": normalized_id,
                "version": str(parsed.manifest["version"]),
                "name": str(
                    parsed.manifest.get("display_name")
                    or parsed.manifest.get("name")
                    or normalized_id
                ),
                "description": str(
                    parsed.manifest.get("description") or ""
                ),
                "content_hash": incoming_hash,
                "installed_at": installed_at,
                "source_filename": source_filename,
                "size_bytes": len(zip_bytes),
                "file_count": validation_report.total_files,
                "original_id": original_id,
            }
            (staging / INSTALLED_META_FILENAME).write_text(
                json.dumps(meta, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            # ── 7. Atomic commit
            target = self.skill_dir(normalized_id)
            _atomic_rename(staging, target)
            staging = None

            duration_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                "Installed skill %r@%s (hash=%s, %d files, %d bytes, "
                "%dms%s).",
                normalized_id,
                meta["version"],
                incoming_hash,
                meta["file_count"],
                meta["size_bytes"],
                duration_ms,
                ", upgrade" if was_upgrade else "",
            )

            return SkillInstallResult(
                skill_id=normalized_id,
                version=meta["version"],
                content_hash=incoming_hash,
                name=meta["name"],
                description=meta["description"],
                size_bytes=int(meta["size_bytes"]),
                file_count=int(meta["file_count"]),
                source_filename=source_filename,
                installed_at=installed_at,
                was_upgrade=was_upgrade,
                warnings=tuple(warnings),
            )

        finally:
            if staging is not None:
                _safe_rmtree(staging)

    # ── Delete ────────────────────────────────────────────────────────

    def delete_skill(self, skill_id: str) -> bool:
        """
        Remove a skill package from disk.

        Uses the trash-then-delete pattern so a crash mid-delete leaves
        the trash directory recoverable via
        ``cleanup_orphan_trash_dirs``.
        """
        target = self.skill_dir(skill_id)
        if not target.exists():
            return False

        trash = self.root_dir / f"{TRASH_PREFIX}{uuid.uuid4().hex[:12]}"
        try:
            _retry_rename(target, trash)
        except OSError as exc:
            raise SkillStorageError(
                f"Failed to move skill dir aside for deletion: {exc}",
                details={
                    "skill_id": skill_id,
                    "source": str(target),
                    "trash": str(trash),
                },
            ) from exc

        if not _safe_rmtree(trash):
            logger.warning(
                "Skill %r was moved aside but trash cleanup failed; "
                "will retry on next startup.",
                skill_id,
            )
        return True

    # ── Manifest normalization ────────────────────────────────────────

    @staticmethod
    def _normalize_manifest_id_inplace(
        manifest_path: Path,
    ) -> Tuple[str, str, Optional[str]]:
        """
        Read ``manifest.json``, normalize its ``id`` field in-place if
        needed, and return ``(original_id, normalized_id, warning)``.

        The rewrite is applied to the file on disk so that
        ``load_skill_package`` (which re-reads the file) sees the
        canonical form. This is what lets authors use
        ``code-review`` / ``Code Review`` / ``123-skill`` in their source
        repository while still ending up with a schema-valid package.

        Raises:
            SkillManifestError: on unreadable/invalid JSON, missing
                ``id``, or a non-string ``id``.
            SkillStorageError: when the rewrite itself cannot be
                persisted (filesystem failure).
        """
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SkillManifestError(
                f"manifest.json is not valid JSON: {exc}",
                details={"parse_error": str(exc)[:200]},
            ) from exc
        except OSError as exc:
            raise SkillManifestError(
                f"Failed to read manifest.json: {exc}",
                details={"path": str(manifest_path)},
            ) from exc

        if not isinstance(raw, dict):
            raise SkillManifestError(
                "manifest.json must contain a JSON object.",
                details={"type": type(raw).__name__},
            )

        original_id = raw.get("id")
        if not isinstance(original_id, str) or not original_id.strip():
            raise SkillManifestError(
                "manifest.json 'id' must be a non-empty string.",
                details={"field": "id"},
            )

        # Lazy import to avoid a circular dependency at module load time
        # (registry imports from manifest; manifest must not import from
        # registry at module scope).
        from omweb.skills.registry import normalize_skill_id

        normalized_id = normalize_skill_id(original_id)

        if normalized_id == original_id:
            return original_id, normalized_id, None

        raw["id"] = normalized_id
        try:
            manifest_path.write_text(
                json.dumps(raw, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            raise SkillStorageError(
                f"Failed to rewrite manifest.json with normalized ID: {exc}",
                details={
                    "original_id": original_id,
                    "normalized_id": normalized_id,
                },
            ) from exc

        warning = (
            f"Manifest 'id' was normalized from {original_id!r} to "
            f"{normalized_id!r}. Consider updating the source "
            f"repository to use the canonical form directly."
        )
        logger.info(warning)
        return original_id, normalized_id, warning


__all__ = [
    "INSTALLED_META_FILENAME",
    "INSTALLED_META_SCHEMA_VERSION",
    "SkillInstallResult",
    "SkillStorage",
    "compute_skill_hash",
]
