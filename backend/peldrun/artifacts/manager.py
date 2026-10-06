"""
backend/peldrun/artifacts/manager.py

PELDRUN Core Artifact Manager.
Maintains workspace deliverables manifest as the primary source of truth.
Supports direct mutation registration from tool executions and background reconciliation scanning.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from peldrun.artifacts.manifest import ArtifactManifest, ArtifactRef

logger = logging.getLogger("peldrun.artifacts.manager")


class ArtifactManager:
    """Manages tracking, indexing, revisioning, and validation of workspace deliverables."""

    def __init__(self, workspace_root: str):
        self.workspace_root = Path(workspace_root).resolve()
        self._manifest = ArtifactManifest(workspace_root=str(self.workspace_root))
        self._baseline_files: Set[str] = set()
        self._file_hashes: Dict[str, str] = {}
        self._revisions: Dict[str, int] = {}
        self._lock = asyncio.Lock()

    def snapshot_baseline(self) -> None:
        """Capture initial files present in workspace prior to run execution."""
        if not self.workspace_root.exists():
            return
        self._baseline_files = {
            str(p.resolve()) for p in self.workspace_root.rglob("*") if p.is_file()
        }
        for p in self.workspace_root.rglob("*"):
            if p.is_file() and ".peldrun" not in p.parts:
                try:
                    self._file_hashes[str(p.resolve())] = self._calculate_sha256(p)
                except Exception:
                    pass

    async def arecord_mutation(
        self,
        file_path: Union[str, Path],
        operation: str = "created",
        source_tool: Optional[str] = None,
    ) -> Optional[ArtifactRef]:
        """
        Directly record an authoritative artifact mutation into the manifest.
        Avoids redundant reconciliation scans by updating hashes and revisions immediately.
        """
        async with self._lock:
            p = Path(file_path)
            target = p.resolve() if p.is_absolute() else (self.workspace_root / p).resolve()

            if not target.is_file():
                if operation == "deleted":
                    rel = str(target.relative_to(self.workspace_root)).replace("\\", "/")
                    existing = self._manifest.get_by_path(rel)
                    if existing:
                        existing.operation = "deleted"
                        existing.updated_at = time.time()
                        self._file_hashes.pop(str(target), None)
                        return existing
                return None

            curr_hash = await asyncio.to_thread(self._calculate_sha256, target)
            resolved_str = str(target)
            rel_str = str(target.relative_to(self.workspace_root)).replace("\\", "/")

            prev_hash = self._file_hashes.get(resolved_str)
            existing_ref = self._manifest.get_by_path(rel_str)

            if existing_ref is not None:
                rev = existing_ref.revision + 1 if prev_hash != curr_hash else existing_ref.revision
                existing_ref.revision = rev
                existing_ref.size_bytes = target.stat().st_size
                existing_ref.sha256 = curr_hash
                existing_ref.operation = "updated" if operation != "created" else "created"
                existing_ref.source_tool = source_tool
                existing_ref.updated_at = time.time()
                self._revisions[resolved_str] = rev
                self._file_hashes[resolved_str] = curr_hash
                return existing_ref
            else:
                rev = 1
                ref = ArtifactRef.from_path(target, self.workspace_root, revision=rev)
                ref.sha256 = curr_hash
                ref.operation = operation
                ref.source_tool = source_tool
                self._revisions[resolved_str] = rev
                self._file_hashes[resolved_str] = curr_hash
                self._manifest.artifacts.append(ref)
                return ref

    def record_mutation(
        self,
        file_path: Union[str, Path],
        operation: str = "created",
        source_tool: Optional[str] = None,
    ) -> Optional[ArtifactRef]:
        """Synchronous wrapper for arecord_mutation."""
        p = Path(file_path)
        target = p.resolve() if p.is_absolute() else (self.workspace_root / p).resolve()

        if not target.is_file():
            return None

        curr_hash = self._calculate_sha256(target)
        resolved_str = str(target)
        rel_str = str(target.relative_to(self.workspace_root)).replace("\\", "/")

        existing_ref = self._manifest.get_by_path(rel_str)
        if existing_ref is not None:
            rev = existing_ref.revision + 1
            existing_ref.revision = rev
            existing_ref.size_bytes = target.stat().st_size
            existing_ref.sha256 = curr_hash
            existing_ref.operation = "updated"
            existing_ref.source_tool = source_tool
            existing_ref.updated_at = time.time()
            self._revisions[resolved_str] = rev
            self._file_hashes[resolved_str] = curr_hash
            return existing_ref
        else:
            rev = 1
            ref = ArtifactRef.from_path(target, self.workspace_root, revision=rev)
            ref.sha256 = curr_hash
            ref.operation = operation
            ref.source_tool = source_tool
            self._revisions[resolved_str] = rev
            self._file_hashes[resolved_str] = curr_hash
            self._manifest.artifacts.append(ref)
            return ref

    async def scan_mutations(self) -> List[Tuple[str, ArtifactRef]]:
        """
        Reconciliation mechanism scanning workspace for external or unrecorded mutations.
        Returns a list of tuples: (operation, ArtifactRef).
        """
        async with self._lock:
            if not self.workspace_root.exists():
                return []

            current_files = [p for p in self.workspace_root.rglob("*") if p.is_file()]
            mutations: List[Tuple[str, ArtifactRef]] = []
            current_resolved_set: Set[str] = set()

            for fpath in current_files:
                resolved_str = str(fpath.resolve())
                current_resolved_set.add(resolved_str)

                if ".peldrun" in fpath.parts:
                    continue

                curr_hash = await asyncio.to_thread(self._calculate_sha256, fpath)
                prev_hash = self._file_hashes.get(resolved_str)

                # Newly discovered file
                if prev_hash is None and resolved_str not in self._baseline_files:
                    rev = 1
                    self._revisions[resolved_str] = rev
                    self._file_hashes[resolved_str] = curr_hash

                    ref = ArtifactRef.from_path(fpath, self.workspace_root, revision=rev)
                    ref.sha256 = curr_hash
                    ref.operation = "created"

                    self._manifest.artifacts.append(ref)
                    mutations.append(("created", ref))
                    logger.debug("Reconciliation discovered new artifact: %s", ref.relative_path)

                # Existing file modified outside direct tool recording
                elif prev_hash is not None and prev_hash != curr_hash:
                    rev = self._revisions.get(resolved_str, 1) + 1
                    self._revisions[resolved_str] = rev
                    self._file_hashes[resolved_str] = curr_hash

                    existing_ref = self._manifest.get_by_path(
                        str(fpath.relative_to(self.workspace_root)).replace("\\", "/")
                    )
                    if existing_ref:
                        existing_ref.revision = rev
                        existing_ref.size_bytes = fpath.stat().st_size
                        existing_ref.sha256 = curr_hash
                        existing_ref.operation = "updated"
                        existing_ref.updated_at = time.time()
                        mutations.append(("updated", existing_ref))
                    else:
                        ref = ArtifactRef.from_path(fpath, self.workspace_root, revision=rev)
                        ref.sha256 = curr_hash
                        ref.operation = "updated"
                        self._manifest.artifacts.append(ref)
                        mutations.append(("updated", ref))

                    logger.debug("Reconciliation detected updated artifact: %s (rev %d)", fpath.name, rev)

            # Detect deleted artifacts
            for tracked_path, _ in list(self._file_hashes.items()):
                if tracked_path not in current_resolved_set:
                    try:
                        rel = str(Path(tracked_path).relative_to(self.workspace_root)).replace("\\", "/")
                        del_ref = self._manifest.get_by_path(rel)
                        if del_ref and del_ref.operation != "deleted":
                            del_ref.operation = "deleted"
                            del_ref.updated_at = time.time()
                            mutations.append(("deleted", del_ref))
                            self._file_hashes.pop(tracked_path, None)
                    except Exception:
                        pass

            self._manifest.updated_at = time.time()
            return mutations

    @staticmethod
    def _calculate_sha256(path: Path) -> str:
        hasher = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        return hasher.hexdigest()

    def get_manifest(self) -> ArtifactManifest:
        """Return current cumulative artifact manifest."""
        return self._manifest

    def format_deliverables_summary(self) -> str:
        """Format a human-readable Markdown summary of produced artifacts."""
        active_arts = [a for a in self._manifest.artifacts if a.operation != "deleted"]
        if not active_arts:
            return ""

        lines = ["### Deliverables Generated:"]
        for art in active_arts:
            size_kb = round(art.size_bytes / 1024, 2)
            lines.append(f"- **`{art.relative_path}`** ({art.artifact_type.value.upper()}, {size_kb} KB, rev {art.revision})")

        return "\n".join(lines)


__all__ = ["ArtifactManager"]