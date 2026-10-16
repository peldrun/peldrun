"""
Exception hierarchy for the PELDRUN Tool Bundles subsystem.

Every exception carries:
  * A machine-readable `code` (stable across releases).
  * A structured `details` dict (safe to serialize into HTTP responses).

Callers should catch `BundleError` for a catch-all and inspect `.code` when
they need to branch on the specific failure mode.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class BundleError(Exception):
    """
    Base class for every bundle-related failure.

    Attributes:
        code: Stable identifier, e.g. "BUNDLE_MANIFEST_INVALID".
        details: Structured context (bundle_id, field, path, ...).
    """

    DEFAULT_CODE = "BUNDLE_ERROR"

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code or self.DEFAULT_CODE
        self.details: Dict[str, Any] = details or {}

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for HTTP responses and structured logging."""
        return {
            "error": str(self),
            "code": self.code,
            "details": self.details,
        }


class BundleValidationError(BundleError):
    """The uploaded payload does not conform to the bundle format."""

    DEFAULT_CODE = "BUNDLE_VALIDATION_FAILED"


class BundleManifestError(BundleError):
    """The manifest.json is missing, malformed, or semantically invalid."""

    DEFAULT_CODE = "BUNDLE_MANIFEST_INVALID"


class BundleExtractionError(BundleError):
    """The zip could not be extracted safely (corrupt, oversized, unsafe)."""

    DEFAULT_CODE = "BUNDLE_EXTRACTION_FAILED"


class BundleStorageError(BundleError):
    """A storage-layer operation failed (blob write, install, registry)."""

    DEFAULT_CODE = "BUNDLE_STORAGE_FAILED"


class BundleLoadError(BundleError):
    """The bundle's Python code could not be imported or instantiated."""

    DEFAULT_CODE = "BUNDLE_LOAD_FAILED"


class BundleSecurityError(BundleError):
    """
    A security policy was violated.

    Examples:
        * Path traversal inside the zip.
        * Absolute path escape.
        * Symlink inside the archive.
        * Disallowed capability or dependency.
    """

    DEFAULT_CODE = "BUNDLE_SECURITY_VIOLATION"


__all__ = [
    "BundleError",
    "BundleValidationError",
    "BundleManifestError",
    "BundleExtractionError",
    "BundleStorageError",
    "BundleLoadError",
    "BundleSecurityError",
]
