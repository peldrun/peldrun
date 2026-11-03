# backend/omweb/provider_packages/errors.py
"""
Exception hierarchy for the PELDRUN Provider Package subsystem.

Every error raised by the M9 administrative layer (validator, installer,
manager, registry) is an instance of ``ProviderPackageError``. This
gives the API layer a single ``except`` clause to translate into a
structured JSON response, while still preserving a stable ``code``
field per error type for the frontend to react to.

Design invariants
-----------------

1. **Stable codes.** ``code`` values are part of the public API
   contract. Renaming one is a breaking change. New codes are added,
   never removed.

2. **Structured details.** Every error carries an optional ``details``
   dict. The API returns it verbatim inside ``{"detail": {...}}`` so
   the frontend can render field-level errors (e.g. "dependencies[2]
   is invalid") without parsing a message string.

3. **Safe to log.** ``to_dict()`` returns only what the frontend needs.
   It never includes stack traces, absolute filesystem paths, or the
   content of the manifest's secrets — because there are none, but
   the discipline matters for future extensions.

4. **Security errors are distinguishable.** ``ProviderPackageSecurityError``
   is a subclass of ``ProviderPackageValidationError`` so a broad
   catch still works, but the distinct subclass lets the UI display
   a stronger warning (red banner, mandatory acknowledgment) for
   install attempts that trip a Zip Slip or archive-bomb guard.

See also
--------

    * ``backend/omweb/provider_packages/package_validator.py``
    * ``backend/omweb/provider_packages/package_manager.py``
"""

from __future__ import annotations

from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Base error
# ---------------------------------------------------------------------------


class ProviderPackageError(Exception):
    """
    Base class for every error raised by the Provider Package subsystem.

    Subclasses declare a stable ``code`` (part of the public API) and
    may attach a ``details`` dict for structured reporting. The
    ``message`` is human-readable and safe to display directly in the
    frontend toast.
    """

    code: str = "PROVIDER_PACKAGE_ERROR"

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code:
            # Per-instance override. Used when a subclass wants to
            # return a more specific code for a particular failure
            # (e.g. VALIDATION_FAILED vs MANIFEST_FIELD_INVALID).
            self.code = code
        self.details: Dict[str, Any] = details or {}

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for structured JSON responses."""
        return {
            "error": self.message,
            "code": self.code,
            "details": self.details,
        }

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


# ---------------------------------------------------------------------------
# Validation / manifest
# ---------------------------------------------------------------------------


class ProviderPackageValidationError(ProviderPackageError):
    """
    The uploaded archive failed structural validation.

    Examples:
        * Missing ``manifest.json``.
        * Invalid ``manifest.json`` JSON.
        * Required manifest field missing or malformed.
        * File count / size limit exceeded.
        * Entry point module not present in the archive.
    """

    code = "PROVIDER_PACKAGE_INVALID"


class ProviderPackageSecurityError(ProviderPackageValidationError):
    """
    The uploaded archive tripped a security guard.

    Subclass of ``ProviderPackageValidationError`` so a broad
    ``except ProviderPackageValidationError`` still catches it, but
    the distinct subclass lets the API return a stronger response
    (e.g. an install-blocked banner with a mandatory acknowledgment).

    Examples:
        * Zip Slip (a member path escapes the extraction root).
        * Absolute path in a member name.
        * Symlink entry (unconditional rejection).
        * Archive bomb (compression ratio or uncompressed size limit).
        * Path length overflow.
    """

    code = "PROVIDER_PACKAGE_SECURITY_VIOLATION"


class ProviderPackageManifestError(ProviderPackageValidationError):
    """
    The ``manifest.json`` content is structurally valid JSON but
    semantically invalid.

    Distinct from ``ProviderPackageValidationError`` so the frontend
    can point the user to the exact field: ``details["field"]`` names
    the offending field, ``details["expected"]`` describes the allowed
    shape, ``details["actual"]`` echoes what was submitted.
    """

    code = "PROVIDER_PACKAGE_MANIFEST_INVALID"


# ---------------------------------------------------------------------------
# Storage / registry
# ---------------------------------------------------------------------------


class ProviderPackageStorageError(ProviderPackageError):
    """
    A filesystem operation failed during install / activate / delete.

    Examples:
        * The target directory could not be created.
        * The extracted files could not be moved into place.
        * The registry file could not be written atomically.

    The underlying ``OSError`` (or similar) is chained via
    ``__cause__`` so the operator can see the real reason in logs.
    """

    code = "PROVIDER_PACKAGE_STORAGE_ERROR"


class ProviderPackageNotFoundError(ProviderPackageError):
    """
    A requested package (or version of a package) is not installed.

    Used by ``get_package``, ``activate_version``, ``uninstall`` and
    the health endpoint.
    """

    code = "PROVIDER_PACKAGE_NOT_FOUND"


class ProviderPackageConflictError(ProviderPackageError):
    """
    The install would conflict with an existing entry.

    Examples:
        * Reinstalling the same version with a different hash (this
          is refused unless ``allow_reinstall=True``).
        * Registering a provider ID that already exists as a BUILTIN
          provider — builtins cannot be overwritten by external
          packages, mirroring the guarantee established in
          ``peldrun.llm.providers.registry``.
    """

    code = "PROVIDER_PACKAGE_CONFLICT"


# ---------------------------------------------------------------------------
# Environment / dependencies
# ---------------------------------------------------------------------------


class ProviderPackageEnvironmentError(ProviderPackageError):
    """
    The isolated Python virtual environment could not be created, or
    a required dependency could not be installed.

    Examples:
        * The manifest declares a Python version incompatible with the
          running interpreter.
        * ``venv`` module unavailable.
        * ``pip install`` failed (invalid package name, network
          failure, dependency conflict).
        * ``pip install`` timed out.

    The pip stderr tail is preserved in ``details["pip_stderr"]`` so
    the operator can see the actual resolver output without needing
    to check server logs.
    """

    code = "PROVIDER_PACKAGE_ENVIRONMENT_ERROR"


# ---------------------------------------------------------------------------
# Runtime / activation
# ---------------------------------------------------------------------------


class ProviderPackagePermissionError(ProviderPackageError):
    """
    A requested runtime action is not permitted by the manifest's
    declared permissions, or the package is disabled.

    Examples:
        * Attempting to instantiate a disabled package.
        * Attempting to read a permission the manifest did not declare
          (defense-in-depth: the Supervisor checks this before every
          adapter call in phase 2).
    """

    code = "PROVIDER_PACKAGE_PERMISSION_DENIED"


class ProviderPackageIncompatibleError(ProviderPackageError):
    """
    The installed package declares an ``api_contract`` that this
    PELDRUN build no longer supports.

    Distinct from ``ProviderPackageManifestError`` (which catches this
    at install time) — this error is raised at ACTIVATION time when a
    package that was valid on a previous PELDRUN version becomes
    invalid after an upgrade.
    """

    code = "PROVIDER_PACKAGE_INCOMPATIBLE"


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


class ProviderPackageCatalogError(ProviderPackageError):
    """
    A catalog operation failed.

    Examples:
        * The requested catalog item does not exist.
        * The catalog item has no ``download_url`` (informational only).
        * The downloaded ZIP's SHA-256 does not match ``checksum_sha256``.
        * The download timed out or returned a non-200 status.
    """

    code = "PROVIDER_PACKAGE_CATALOG_ERROR"


__all__ = [
    "ProviderPackageError",
    "ProviderPackageValidationError",
    "ProviderPackageSecurityError",
    "ProviderPackageManifestError",
    "ProviderPackageStorageError",
    "ProviderPackageNotFoundError",
    "ProviderPackageConflictError",
    "ProviderPackageEnvironmentError",
    "ProviderPackagePermissionError",
    "ProviderPackageIncompatibleError",
    "ProviderPackageCatalogError",
]
