"""
backend/peldrun/artifacts/manifest.py

PELDRUN Core Artifact Manifest Models.
Defines typed references, classifications, revisions, and serializable manifests
for deliverables produced during agent execution loops.
"""

from __future__ import annotations

import mimetypes
import time
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field


class ArtifactType(str, Enum):
    """Classification of deliverables produced by agent workflows."""
    FILE = "file"
    CODE = "code"
    DOCUMENT = "document"
    IMAGE = "image"
    DATA = "data"
    ARCHIVE = "archive"


class ArtifactRef(BaseModel):
    """Structured descriptor of a single generated artifact with revision tracking."""
    model_config = ConfigDict(extra="allow")

    artifact_id: str = Field(default_factory=lambda: str(uuid4()), description="Unique artifact identifier")
    name: str = Field(..., description="File name of the artifact")
    relative_path: str = Field(..., description="Canonical relative path from workspace root")
    artifact_type: ArtifactType = Field(default=ArtifactType.FILE, description="Type categorization")
    mime_type: str = Field(default="application/octet-stream", description="Detected MIME type")
    size_bytes: int = Field(default=0, description="Size in bytes")
    revision: int = Field(default=1, ge=1, description="Monotonically increasing version counter")
    operation: str = Field(default="created", description="Latest operation: created, updated, deleted, moved")
    created_at: float = Field(default_factory=time.time, description="Creation timestamp")
    updated_at: float = Field(default_factory=time.time, description="Last update timestamp")
    sha256: Optional[str] = Field(default=None, description="SHA256 content checksum")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Arbitrary auxiliary metrics")

    @classmethod
    def from_path(cls, file_path: Path, workspace_root: Path, revision: int = 1) -> ArtifactRef:
        """Construct an ArtifactRef directly from a filesystem file path."""
        rel = file_path.resolve().relative_to(workspace_root.resolve())
        rel_str = str(rel).replace("\\", "/")
        ext = file_path.suffix.lower()

        mime, _ = mimetypes.guess_type(str(file_path))
        mime = mime or "application/octet-stream"

        # Categorize by extension
        if ext in (".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".json", ".sql", ".sh"):
            art_type = ArtifactType.CODE
        elif ext in (".md", ".txt", ".pdf", ".docx", ".rtf"):
            art_type = ArtifactType.DOCUMENT
        elif ext in (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"):
            art_type = ArtifactType.IMAGE
        elif ext in (".csv", ".parquet", ".xlsx", ".tsv"):
            art_type = ArtifactType.DATA
        elif ext in (".zip", ".tar", ".gz", ".7z"):
            art_type = ArtifactType.ARCHIVE
        else:
            art_type = ArtifactType.FILE

        stat = file_path.stat()
        return cls(
            name=file_path.name,
            relative_path=rel_str,
            artifact_type=art_type,
            mime_type=mime,
            size_bytes=stat.st_size,
            revision=revision,
            operation="created",
            created_at=stat.st_ctime,
            updated_at=stat.st_mtime,
        )


class ArtifactManifest(BaseModel):
    """Cumulative collection of deliverables produced within an execution run."""
    model_config = ConfigDict(extra="ignore")

    workspace_root: str = Field(..., description="Root directory of the workspace")
    artifacts: List[ArtifactRef] = Field(default_factory=list, description="Indexed artifacts")
    updated_at: float = Field(default_factory=time.time, description="Timestamp of last scan/update")

    def get_by_name(self, name: str) -> Optional[ArtifactRef]:
        for art in self.artifacts:
            if art.name == name:
                return art
        return None

    def get_by_path(self, relative_path: str) -> Optional[ArtifactRef]:
        norm = relative_path.replace("\\", "/").strip("/")
        for art in self.artifacts:
            if art.relative_path.replace("\\", "/").strip("/") == norm:
                return art
        return None

    def get_relative_paths(self) -> List[str]:
        return [art.relative_path for art in self.artifacts]