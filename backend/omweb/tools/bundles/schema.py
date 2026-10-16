"""
Pydantic v2 schemas for the PELDRUN Tool Bundles subsystem.

Every schema is versioned via `schema_version` to allow future evolution
without breaking previously published bundles.

Design invariants:
  * All models are frozen (immutable) where mutation is not required.
  * All validators emit actionable error messages.
  * No mutable default arguments (Pydantic enforces this via `default_factory`).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


# ---------------------------------------------------------------------------
# Constants and patterns
# ---------------------------------------------------------------------------

SCHEMA_VERSION_CURRENT = "1.0.0"
SUPPORTED_SCHEMA_MAJOR = "1"

_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_SEMVER_PATTERN = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:-(?P<prerelease>[a-z0-9.\-]+))?$"
)
_ENTRY_POINT_PATTERN = re.compile(r"^[A-Za-z0-9_./\-]+\.py$")
_PYTHON_VERSION_PATTERN = re.compile(r"^\d+\.\d+$")
_PIP_REQUIREMENT_PATTERN = re.compile(
    r"^[A-Za-z][A-Za-z0-9_\-.]*"
    r"(?:\[[A-Za-z0-9_,\-]+\])?"
    r"(?:[<>=!~]=?[A-Za-z0-9_.\-*]+)?"
    r"(?:,[<>=!~]=?[A-Za-z0-9_.\-*]+)*$"
)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class BundleCapability(str, Enum):
    """
    Declared capability of a bundle. Used for policy enforcement and for
    surfacing risk to operators before activation.
    """

    WORKSPACE_READ = "workspace.read"
    WORKSPACE_WRITE = "workspace.write"
    WORKSPACE_SCOPED = "workspace.scoped"
    NETWORK = "network"
    SHELL = "shell"
    SYSTEM = "system"
    AI_LLM = "ai.llm"


# ---------------------------------------------------------------------------
# Nested specs
# ---------------------------------------------------------------------------

class RuntimeSpec(BaseModel):
    """Python runtime compatibility window."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    python_min: str = Field(
        default="3.11",
        description="Minimum supported Python version, e.g. '3.11'.",
    )
    python_max: Optional[str] = Field(
        default=None,
        description="Maximum supported Python version (inclusive). None = open-ended.",
    )

    @field_validator("python_min", "python_max", mode="before")
    @classmethod
    def _validate_python_version(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = str(v).strip()
        if not _PYTHON_VERSION_PATTERN.match(v):
            raise ValueError(
                f"Python version must be 'MAJOR.MINOR' (e.g. '3.11'), got {v!r}."
            )
        return v


class DependenciesSpec(BaseModel):
    """Declarative pip dependencies."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pip: List[str] = Field(
        default_factory=list,
        description="List of PEP 508 requirement strings, e.g. ['pypdf2>=3.0.0'].",
    )

    @field_validator("pip", mode="before")
    @classmethod
    def _validate_pip_list(cls, v: object) -> List[str]:
        if v is None:
            return []
        if not isinstance(v, list):
            raise ValueError("dependencies.pip must be a list of strings.")
        cleaned: List[str] = []
        for idx, req in enumerate(v):
            req_str = str(req).strip()
            if not req_str:
                continue
            if not _PIP_REQUIREMENT_PATTERN.match(req_str):
                raise ValueError(
                    f"dependencies.pip[{idx}] = {req_str!r} is not a valid "
                    "PEP 508 requirement string."
                )
            cleaned.append(req_str)
        return cleaned


# ---------------------------------------------------------------------------
# The Manifest
# ---------------------------------------------------------------------------

class BundleManifest(BaseModel):
    """
    Declarative manifest for a tool bundle.

    This is the payload the operator uploads inside `manifest.json`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(
        ...,
        description="Bundle manifest schema version, e.g. '1.0.0'.",
    )
    id: str = Field(
        ...,
        description="Bundle identifier: lowercase, 3-64 chars, [a-z0-9_].",
    )
    version: str = Field(
        ...,
        description="Bundle release version (semver), e.g. '1.0.0'.",
    )
    name: str = Field(
        ...,
        description="Human-readable name, 1-120 chars.",
    )
    description: str = Field(
        ...,
        description="Short description, 1-500 chars.",
    )
    entry_point: str = Field(
        ...,
        description="Relative path to the Python entry file, e.g. 'tool.py'.",
    )
    class_name: Optional[str] = Field(
        default=None,
        description="Explicit class to instantiate. If None, first found is used.",
    )
    safety_level: Literal["safe", "read_only", "dangerous"] = Field(
        default="safe",
        description="Safety tier declared by the author.",
    )
    capabilities: List[BundleCapability] = Field(
        default_factory=list,
        description="Declared capabilities for policy enforcement.",
    )
    author: Optional[str] = Field(default=None, description="Bundle author name.")
    license: Optional[str] = Field(default=None, description="SPDX license identifier.")
    homepage: Optional[str] = Field(default=None, description="Project homepage URL.")
    tags: List[str] = Field(
        default_factory=list,
        description="Free-form search tags.",
    )
    runtime: RuntimeSpec = Field(default_factory=RuntimeSpec)
    dependencies: DependenciesSpec = Field(default_factory=DependenciesSpec)
    checksum_sha256: Optional[str] = Field(
        default=None,
        description="Optional integrity checksum of the entire bundle zip.",
    )

    # ---------------------------------------------------------------
    # Validators
    # ---------------------------------------------------------------

    @field_validator("schema_version", mode="before")
    @classmethod
    def _validate_schema_version(cls, v: object) -> str:
        v_str = str(v).strip()
        if not _SEMVER_PATTERN.match(v_str):
            raise ValueError(
                f"schema_version must be semver, got {v_str!r}."
            )
        major = v_str.split(".", 1)[0]
        if major != SUPPORTED_SCHEMA_MAJOR:
            raise ValueError(
                f"Unsupported schema_version major {major!r}. "
                f"This server supports major={SUPPORTED_SCHEMA_MAJOR}."
            )
        return v_str

    @field_validator("id", mode="before")
    @classmethod
    def _validate_id(cls, v: object) -> str:
        v_str = str(v).strip()
        if not _ID_PATTERN.match(v_str):
            raise ValueError(
                f"id must match {_ID_PATTERN.pattern}, got {v_str!r}."
            )
        return v_str

    @field_validator("version", mode="before")
    @classmethod
    def _validate_version(cls, v: object) -> str:
        v_str = str(v).strip()
        if not _SEMVER_PATTERN.match(v_str):
            raise ValueError(
                f"version must be semver (e.g. '1.0.0'), got {v_str!r}."
            )
        return v_str

    @field_validator("name", mode="before")
    @classmethod
    def _validate_name(cls, v: object) -> str:
        v_str = str(v).strip()
        if not (1 <= len(v_str) <= 120):
            raise ValueError("name must be 1-120 characters.")
        return v_str

    @field_validator("description", mode="before")
    @classmethod
    def _validate_description(cls, v: object) -> str:
        v_str = str(v).strip()
        if not (1 <= len(v_str) <= 500):
            raise ValueError("description must be 1-500 characters.")
        return v_str

    @field_validator("entry_point", mode="before")
    @classmethod
    def _validate_entry_point(cls, v: object) -> str:
        v_str = str(v).strip().replace("\\", "/")
        if not _ENTRY_POINT_PATTERN.match(v_str):
            raise ValueError(
                f"entry_point must be a relative .py path, got {v_str!r}."
            )
        if v_str.startswith("/") or ".." in v_str.split("/"):
            raise ValueError(
                f"entry_point must not be absolute or contain '..': {v_str!r}."
            )
        return v_str

    @field_validator("class_name", mode="before")
    @classmethod
    def _validate_class_name(cls, v: object) -> Optional[str]:
        if v is None:
            return None
        v_str = str(v).strip()
        if not v_str:
            return None
        if not v_str.isidentifier():
            raise ValueError(
                f"class_name must be a valid Python identifier, got {v_str!r}."
            )
        return v_str

    @field_validator("tags", mode="before")
    @classmethod
    def _validate_tags(cls, v: object) -> List[str]:
        if v is None:
            return []
        if not isinstance(v, list):
            raise ValueError("tags must be a list of strings.")
        cleaned: List[str] = []
        for tag in v:
            t = str(tag).strip().lower()
            if t and t not in cleaned:
                cleaned.append(t[:32])
        return cleaned

    @field_validator("checksum_sha256", mode="before")
    @classmethod
    def _validate_checksum(cls, v: object) -> Optional[str]:
        if v is None:
            return None
        v_str = str(v).strip().lower()
        if not v_str:
            return None
        if len(v_str) != 64 or not all(c in "0123456789abcdef" for c in v_str):
            raise ValueError(
                "checksum_sha256 must be a 64-char lowercase hex string."
            )
        return v_str

    @model_validator(mode="after")
    def _cross_field_checks(self) -> "BundleManifest":
        # Runtime window sanity
        if self.runtime.python_max:
            if self.runtime.python_min == self.runtime.python_max:
                pass  # single-version window is legal
            else:
                try:
                    min_parts = tuple(int(x) for x in self.runtime.python_min.split("."))
                    max_parts = tuple(int(x) for x in self.runtime.python_max.split("."))
                    if min_parts > max_parts:
                        raise ValueError(
                            "runtime.python_min must be <= runtime.python_max."
                        )
                except ValueError as exc:
                    raise ValueError(f"Invalid runtime window: {exc}") from exc

        # Dangerous bundles must declare at least one capability
        if self.safety_level == "dangerous" and not self.capabilities:
            raise ValueError(
                "Bundles marked safety_level='dangerous' must declare at "
                "least one capability for policy enforcement."
            )
        return self


# ---------------------------------------------------------------------------
# Registry-side records
# ---------------------------------------------------------------------------

class BundleVersionRecord(BaseModel):
    """One immutable installed version of a bundle."""

    model_config = ConfigDict(extra="forbid")

    version: str
    hash: str = Field(..., description="SHA-256 truncated to 16 hex chars.")
    installed_at: str = Field(..., description="ISO 8601 timestamp (UTC).")
    size_bytes: int = Field(..., ge=0)
    file_count: int = Field(..., ge=0)
    entry_point: str
    class_name: Optional[str] = None


class BundleEntry(BaseModel):
    """Registry-side view of a single bundle with all its versions."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    description: str
    active_version: str
    active_hash: str
    installed_versions: List[BundleVersionRecord] = Field(default_factory=list)
    status: Literal["active", "disabled", "broken"] = "active"
    safety_level: str = "safe"
    capabilities: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    author: Optional[str] = None
    homepage: Optional[str] = None

    def get_version(self, version: str) -> Optional[BundleVersionRecord]:
        """Return the matching version record, or None."""
        for v in self.installed_versions:
            if v.version == version:
                return v
        return None


class BundleRegistryIndex(BaseModel):
    """Root of `registry.json` — the source of truth for installed bundles."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION_CURRENT
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="ISO 8601 timestamp of last write.",
    )
    bundles: Dict[str, BundleEntry] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Health & Install reports
# ---------------------------------------------------------------------------

class HealthCheck(BaseModel):
    """One concrete check performed during a health probe."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    passed: bool
    message: str = ""


class BundleHealthReport(BaseModel):
    """Aggregate health result for a single bundle."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    version: str
    hash: str
    status: Literal["healthy", "degraded", "broken"]
    checks: List[HealthCheck] = Field(default_factory=list)
    checked_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class InstallStep(BaseModel):
    """One step in the install pipeline."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    status: Literal["ok", "skipped", "failed"]
    duration_ms: int = 0
    detail: str = ""


class InstallReport(BaseModel):
    """Full report of what happened during an install."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requirements_installed: bool = False
    requirements_skipped: bool = False
    duration_ms: int = 0
    steps: List[InstallStep] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class BundleInstallResult(BaseModel):
    """Public response returned by `BundleManager.install_bundle(...)`."""

    model_config = ConfigDict(extra="forbid")

    id: str
    version: str
    hash: str
    name: str
    description: str
    size_bytes: int
    file_count: int
    capabilities: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    install_report: InstallReport = Field(default_factory=InstallReport)
    warnings: List[str] = Field(default_factory=list)

# ---------------------------------------------------------------------------
# Zip / Extraction reports
# ---------------------------------------------------------------------------

class ZipEntryInfo(BaseModel):
    """Info about one entry discovered inside an uploaded zip."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(..., description="Normalized POSIX-style relative path.")
    size_bytes: int = Field(..., ge=0)
    compressed_bytes: int = Field(..., ge=0)
    is_dir: bool = False
    is_symlink: bool = False


class ZipValidationReport(BaseModel):
    """Result of validating a zip payload prior to extraction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    total_files: int = 0
    total_directories: int = 0
    total_uncompressed_bytes: int = 0
    total_compressed_bytes: int = 0
    entries: List[ZipEntryInfo] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class ExtractStep(BaseModel):
    """One file-level step inside an extraction report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    status: Literal["extracted", "skipped", "failed"]
    size_bytes: int = 0
    detail: str = ""


class ExtractReport(BaseModel):
    """Result of a safe extraction to a target directory."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    target_dir: str
    extracted_files: int = 0
    skipped_files: int = 0
    failed_files: int = 0
    total_bytes: int = 0
    duration_ms: int = 0
    steps: List[ExtractStep] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


__all__ = [
    "SCHEMA_VERSION_CURRENT",
    "SUPPORTED_SCHEMA_MAJOR",
    "BundleCapability",
    "RuntimeSpec",
    "DependenciesSpec",
    "BundleManifest",
    "BundleVersionRecord",
    "BundleEntry",
    "BundleRegistryIndex",
    "HealthCheck",
    "BundleHealthReport",
    "InstallStep",
    "InstallReport",
    "BundleInstallResult",
    # Phase 2 additions
    "ZipEntryInfo",
    "ZipValidationReport",
    "ExtractStep",
    "ExtractReport",
]
