"""
Content-addressed storage layer for installed tool bundles.

Responsibilities:
  * Compute deterministic hashes of uploaded zip payloads.
  * Store extracted bundles in an immutable blob directory keyed by hash.
  * Maintain per-bundle "active version" pointers as atomic text files.
  * Provide orphan detection and cleanup utilities.

This module is intentionally free of business logic. It never validates
manifests, never imports Python, and never touches the global registry.
Callers (BundleManager) orchestrate those concerns.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Set

from omweb.tools.bundles.errors import BundleStorageError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HASH_HEX_LENGTH = 16
BLOB_PREFIX = "sha256_"
TEMP_PREFIX = ".tmp_"
META_FILENAME = "_bundle_meta.json"
ACTIVE_POINTER = "active.txt"
MANIFEST_FILENAME = "manifest.json"


# ---------------------------------------------------------------------------
# Hash
# ---------------------------------------------------------------------------

def compute_bundle_hash(zip_bytes: bytes) -> str:
    """
    Return the first HASH_HEX_LENGTH hex chars of SHA-256(zip_bytes).

    16 hex chars = 64 bits = ~10^19 distinct values. Collision probability
    is negligible for the expected namespace size (thousands of bundles).
    A longer hash is stored in `_bundle_meta.json` for full verification.
    """
    return hashlib.sha256(zip_bytes).hexdigest()[:HASH_HEX_LENGTH]


# ---------------------------------------------------------------------------
# BundleStorage
# ---------------------------------------------------------------------------

class BundleStorage:
    """File-system layout manager for the content-addressed blob store."""

    def __init__(self, root_dir: Path) -> None:
        self.root_dir = Path(root_dir)
        self.blobs_dir = self.root_dir / "_blobs"
        self.installs_dir = self.root_dir / "_installs"

        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        self.installs_dir.mkdir(parents=True, exist_ok=True)

    # ── Blob paths ────────────────────────────────────────────────────────

    def blob_dir(self, hash_short: str) -> Path:
        return self.blobs_dir / f"{BLOB_PREFIX}{hash_short}"

    def blob_exists(self, hash_short: str) -> bool:
        return (self.blob_dir(hash_short) / MANIFEST_FILENAME).is_file()

    def blob_entry_point_exists(self, hash_short: str, entry_point: str) -> bool:
        return (self.blob_dir(hash_short) / entry_point).is_file()

    # ── Temp dirs ─────────────────────────────────────────────────────────

    def make_temp_dir(self) -> Path:
        """
        Allocate a fresh staging directory under `_blobs/`.

        The `.tmp_` prefix ensures these directories are never mistaken for
        real blobs (which always start with `sha256_`).
        """
        tmp = self.blobs_dir / f"{TEMP_PREFIX}{uuid.uuid4().hex[:12]}"
        tmp.mkdir(parents=True, exist_ok=False)
        return tmp

    # ── Blob commit ───────────────────────────────────────────────────────

    def commit_blob(
        self,
        staging_dir: Path,
        hash_short: str,
        meta: Dict[str, object],
    ) -> Path:
        """
        Atomically move a staging directory into place as a blob.

        Behavior:
          * If the blob already exists (same hash), the staging dir is
            discarded and the existing blob is returned. This guarantees
            idempotency for repeated uploads of the same payload.
          * Otherwise, `_bundle_meta.json` is written into the staging dir
            and the whole directory is moved atomically to the final path.
        """
        target = self.blob_dir(hash_short)

        if target.exists():
            # Blob already committed — drop staging, return existing
            shutil.rmtree(staging_dir, ignore_errors=True)
            return target

        # Write meta before the atomic move so the target is complete
        # the instant it becomes visible.
        meta_file = staging_dir / META_FILENAME
        self._atomic_write_json(meta_file, meta)

        try:
            os.replace(str(staging_dir), str(target))
        except OSError as exc:
            # Fallback for cross-volume moves (rare). Not atomic, but safe.
            try:
                shutil.move(str(staging_dir), str(target))
            except Exception as move_err:
                raise BundleStorageError(
                    f"Failed to commit blob {hash_short}: {move_err}",
                    details={"hash": hash_short},
                ) from move_err

        return target

    # ── Blob metadata ─────────────────────────────────────────────────────

    def read_blob_meta(self, hash_short: str) -> Optional[Dict[str, object]]:
        meta_file = self.blob_dir(hash_short) / META_FILENAME
        if not meta_file.is_file():
            return None
        try:
            return json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            return None

    # ── Blob listing / deletion ───────────────────────────────────────────

    def list_blobs(self) -> List[str]:
        """Return the short hash of every committed blob."""
        result: List[str] = []
        if not self.blobs_dir.exists():
            return result
        for entry in self.blobs_dir.iterdir():
            if entry.is_dir() and entry.name.startswith(BLOB_PREFIX):
                result.append(entry.name[len(BLOB_PREFIX):])
        return result

    def list_orphaned_blobs(self, active_hashes: Set[str]) -> List[str]:
        """
        Return blobs that are not referenced by any active install.

        `active_hashes` is the set of short hashes currently pointed to by
        any bundle's `active.txt`.
        """
        return [h for h in self.list_blobs() if h not in active_hashes]

    def delete_blob(self, hash_short: str) -> bool:
        target = self.blob_dir(hash_short)
        if not target.exists():
            return False
        shutil.rmtree(target, ignore_errors=True)
        return True

    # ── Active pointer ────────────────────────────────────────────────────

    def install_dir(self, bundle_id: str) -> Path:
        return self.installs_dir / bundle_id

    def get_active_hash(self, bundle_id: str) -> Optional[str]:
        pointer = self.install_dir(bundle_id) / ACTIVE_POINTER
        if not pointer.is_file():
            return None
        try:
            value = pointer.read_text(encoding="utf-8").strip()
            return value or None
        except OSError:
            return None

    def set_active_hash(self, bundle_id: str, hash_short: str) -> None:
        """Atomically point a bundle at the given blob hash."""
        install_dir = self.install_dir(bundle_id)
        install_dir.mkdir(parents=True, exist_ok=True)
        pointer = install_dir / ACTIVE_POINTER
        tmp = pointer.parent / (pointer.name + ".tmp")
        tmp.write_text(hash_short, encoding="utf-8")
        os.replace(str(tmp), str(pointer))

    def delete_install(self, bundle_id: str) -> bool:
        target = self.install_dir(bundle_id)
        if not target.exists():
            return False
        shutil.rmtree(target, ignore_errors=True)
        return True

    def list_installs(self) -> List[str]:
        if not self.installs_dir.exists():
            return []
        return [
            entry.name
            for entry in self.installs_dir.iterdir()
            if entry.is_dir()
        ]

    # ── Cleanup ───────────────────────────────────────────────────────────

    def cleanup_orphan_temp_dirs(self, older_than_seconds: int = 3600) -> int:
        """
        Remove `.tmp_*` staging directories left behind by crashed installs.

        Only directories older than `older_than_seconds` are removed, so
        concurrent installs in flight are never disturbed.
        """
        import time

        if not self.blobs_dir.exists():
            return 0

        cutoff = time.time() - older_than_seconds
        removed = 0
        for entry in self.blobs_dir.iterdir():
            if not entry.is_dir() or not entry.name.startswith(TEMP_PREFIX):
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
        return removed

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _atomic_write_json(target: Path, data: Dict[str, object]) -> None:
        tmp = target.parent / (target.name + ".tmp")
        tmp.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(str(tmp), str(target))


__all__ = [
    "HASH_HEX_LENGTH",
    "BLOB_PREFIX",
    "TEMP_PREFIX",
    "META_FILENAME",
    "ACTIVE_POINTER",
    "compute_bundle_hash",
    "BundleStorage",
]
