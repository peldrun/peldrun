"""
PELDRUN Core Artifact Manager.
Scans workspace directories, tracks deliverables created during execution runs,
computes checksums, and exposes formatted manifests for events and memory context.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from peldrun.artifacts.manifest import ArtifactManifest, ArtifactRef

logger = logging.getLogger("peldrun.artifacts.manager")


class ArtifactManager:
    """Manages tracking, indexing, and validation of deliverables within an isolated workspace."""

    def __init__(self, workspace_root: str):
        self.workspace_root = Path(workspace_root).resolve()
        self._manifest = ArtifactManifest(workspace_root=str(self.workspace_root))
        self._baseline_files: Set[str] = set()
        self._lock = asyncio.Lock()

    def snapshot_baseline(self) -> None:
        """Capture initial files present in workspace prior to run execution."""
        if not self.workspace_root.exists():
            return
        self._baseline_files = {
            str(p.resolve()) for p in self.workspace_root.rglob("*") if p.is_file()
        }

    async def scan_new_artifacts(self, compute_hashes: bool = False) -> List[ArtifactRef]:
        """
        Scan workspace for newly created files since baseline snapshot.
        Updates internal manifest and returns newly discovered artifacts.
        """
        async with self._lock:
            if not self.workspace_root.exists():
                return []

            current_files = [p for p in self.workspace_root.rglob("*") if p.is_file()]
            new_refs: List[ArtifactRef] = []
            existing_paths = {art.relative_path for art in self._manifest.artifacts}

            for fpath in current_files:
                resolved_str = str(fpath.resolve())
                # Skip baseline or hidden metadata directory
                if resolved_str in self._baseline_files or ".peldrun" in fpath.parts:
                    continue

                ref = ArtifactRef.from_path(fpath, self.workspace_root)
                if ref.relative_path not in existing_paths:
                    if compute_hashes:
                        ref.sha256 = await asyncio.to_thread(self._calculate_sha256, fpath)

                    self._manifest.artifacts.append(ref)
                    new_refs.append(ref)
                    logger.debug("Discovered new artifact: %s (%s)", ref.relative_path, ref.artifact_type.value)

            return new_refs

    @staticmethod
    def _calculate_sha256(path: Path) -> str:
        hasher = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        return hasher.hexdigest()

    def get_manifest(self) -> ArtifactManifest:
        """Return the current cumulative artifact manifest."""
        return self._manifest

    def format_deliverables_summary(self) -> str:
        """Format a human-readable Markdown summary of produced artifacts."""
        if not self._manifest.artifacts:
            return ""

        lines = ["### Deliverables Generated:"]
        for art in self._manifest.artifacts:
            size_kb = round(art.size_bytes / 1024, 2)
            lines.append(f"- **`{art.relative_path}`** ({art.artifact_type.value.upper()}, {size_kb} KB)")

        return "\n".join(lines)