"""
PELDRUN Tool Bundles Subsystem.

A content-addressed, version-aware distribution system for agent tools.

Public API:
    BundleManager       — install/list/delete/activate operations
    BundleManifest      — declarative bundle metadata (schema-versioned)
    BundleEntry         — registry view of a single installed bundle
    BundleStorage       — content-addressed storage layer
    load_tool_from_bundle — import a Python tool from an installed blob
    validate_zip        — pre-flight validation of an uploaded zip
    safe_extract        — safe extraction to a target directory
    compute_bundle_hash — deterministic hash of an uploaded payload

Exceptions are exported for caller-side handling.
"""

from omweb.tools.bundles.errors import (
    BundleError,
    BundleExtractionError,
    BundleLoadError,
    BundleManifestError,
    BundleSecurityError,
    BundleStorageError,
    BundleValidationError,
)
from omweb.tools.bundles.extractor import safe_extract
from omweb.tools.bundles.loader import load_tool_from_bundle
from omweb.tools.bundles.manager import BundleManager
from omweb.tools.bundles.schema import (
    BundleCapability,
    BundleEntry,
    BundleHealthReport,
    BundleInstallResult,
    BundleManifest,
    BundleRegistryIndex,
    BundleVersionRecord,
    DependenciesSpec,
    ExtractReport,
    ExtractStep,
    HealthCheck,
    InstallReport,
    InstallStep,
    RuntimeSpec,
    ZipEntryInfo,
    ZipValidationReport,
)
from omweb.tools.bundles.storage import (
    ACTIVE_POINTER,
    BLOB_PREFIX,
    HASH_HEX_LENGTH,
    META_FILENAME,
    BundleStorage,
    compute_bundle_hash,
)
from omweb.tools.bundles.validator import (
    assert_safe_paths_for_extraction,
    parse_manifest,
    validate_zip,
)

__all__ = [
    # Errors
    "BundleError",
    "BundleValidationError",
    "BundleManifestError",
    "BundleExtractionError",
    "BundleStorageError",
    "BundleLoadError",
    "BundleSecurityError",
    # Manager & storage
    "BundleManager",
    "BundleStorage",
    "compute_bundle_hash",
    "HASH_HEX_LENGTH",
    "BLOB_PREFIX",
    "META_FILENAME",
    "ACTIVE_POINTER",
    # Loader
    "load_tool_from_bundle",
    # Schema
    "BundleManifest",
    "BundleCapability",
    "BundleVersionRecord",
    "BundleEntry",
    "BundleRegistryIndex",
    "BundleHealthReport",
    "BundleInstallResult",
    "DependenciesSpec",
    "HealthCheck",
    "InstallReport",
    "InstallStep",
    "RuntimeSpec",
    "ZipEntryInfo",
    "ZipValidationReport",
    "ExtractStep",
    "ExtractReport",
    # Functions
    "validate_zip",
    "parse_manifest",
    "assert_safe_paths_for_extraction",
    "safe_extract",
]
