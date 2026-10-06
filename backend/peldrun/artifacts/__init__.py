"""
PELDRUN Core Artifacts Subsystem.
Exports manifest definitions, structured artifact references, and the ArtifactManager.
"""

from peldrun.artifacts.manifest import (
    ArtifactManifest,
    ArtifactRef,
    ArtifactType,
)
from peldrun.artifacts.manager import ArtifactManager

__all__ = [
    "ArtifactType",
    "ArtifactRef",
    "ArtifactManifest",
    "ArtifactManager",
]