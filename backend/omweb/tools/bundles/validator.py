"""
Pre-flight validation of uploaded tool bundles.

This module performs *static* checks against a zip payload and its manifest
before any file is written to disk. It is intentionally stateless and has no
side effects: given the same bytes it always produces the same report.

Security invariants enforced here:
  * No absolute paths inside the archive.
  * No `..` path traversal segments.
  * No symbolic links.
  * No oversized files, no archive bombs.
  * No forbidden directory names (`__pycache__`, `.git`, ...).
"""

from __future__ import annotations

import io
import logging
import zipfile
from pathlib import PurePosixPath
from typing import List, Tuple

from omweb.tools.bundles.errors import (
    BundleManifestError,
    BundleSecurityError,
    BundleValidationError,
)
from omweb.tools.bundles.schema import (
    BundleManifest,
    ZipEntryInfo,
    ZipValidationReport,
)

logger = logging.getLogger("omweb.tools.bundles.validator")


# ---------------------------------------------------------------------------
# Limits (module-level so extractor.py can reuse them)
# ---------------------------------------------------------------------------

MAX_ZIP_BYTES = 20 * 1024 * 1024            # 20 MB compressed
MAX_TOTAL_UNCOMPRESSED_BYTES = 100 * 1024 * 1024  # 100 MB extracted
MAX_SINGLE_FILE_BYTES = 5 * 1024 * 1024     # 5 MB per file
MAX_FILE_COUNT = 500
MAX_PATH_DEPTH = 10
MAX_PATH_LENGTH = 200

FORBIDDEN_TOP_LEVEL_DIRS = {
    "__MACOSX",
    "__pycache__",
    ".git",
    ".svn",
    ".hg",
    ".venv",
    "venv",
    "env",
    "node_modules",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}

FORBIDDEN_FILE_SUFFIXES = (
    ".pyc",
    ".pyo",
    ".pyd",
    ".so",
    ".dll",
    ".dylib",
    ".exe",
)

# Unix mode constants for zip external attributes
_S_IFMT = 0xF000
_S_IFLNK = 0xA000


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_symlink(zinfo: zipfile.ZipInfo) -> bool:
    """Detect symlinks via the UNIX external attributes embedded in the zip."""
    unix_mode = zinfo.external_attr >> 16
    return (unix_mode & _S_IFMT) == _S_IFLNK


def _check_path_safety(raw_path: str) -> Tuple[List[str], str]:
    """
    Check the raw zip entry path for safety violations and return
    (errors, normalized_path).

    The absolute-prefix check MUST run against the raw path, *before*
    normalization strips the leading slash. Otherwise `/etc/passwd` would
    silently collapse to the safe-looking `etc/passwd`.

    Normalization is only used to prepare a canonical relative path for
    the remaining structural checks and for storage.
    """
    errors: List[str] = []

    if not raw_path:
        return ["empty path"], ""

    # ── Absolute / drive-letter checks on the RAW name ─────────────
    if raw_path.startswith("/") or raw_path.startswith("\\"):
        errors.append(f"absolute path: {raw_path!r}")

    if len(raw_path) >= 2 and raw_path[1] == ":":
        errors.append(f"drive-letter path: {raw_path!r}")

    # ── Normalize for the remaining structural checks ──────────────
    normalized = raw_path.replace("\\", "/").lstrip("/")

    if len(normalized) > MAX_PATH_LENGTH:
        errors.append(
            f"path too long ({len(normalized)} > {MAX_PATH_LENGTH}): "
            f"{normalized!r}"
        )

    parts = [p for p in normalized.split("/") if p not in ("", ".")]

    if any(p == ".." for p in parts):
        errors.append(f"traversal segment in path: {normalized!r}")

    if len(parts) > MAX_PATH_DEPTH:
        errors.append(
            f"path depth {len(parts)} exceeds limit {MAX_PATH_DEPTH}: "
            f"{normalized!r}"
        )

    for part in parts:
        if part in FORBIDDEN_TOP_LEVEL_DIRS:
            errors.append(
                f"forbidden segment {part!r} in path: {normalized!r}"
            )
            break

    if normalized.lower().endswith(FORBIDDEN_FILE_SUFFIXES):
        errors.append(f"forbidden file type: {normalized!r}")

    return errors, normalized


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_zip(zip_bytes: bytes) -> ZipValidationReport:
    """
    Validate an uploaded zip payload.

    Returns a `ZipValidationReport` with `.ok=True` if and only if every
    check passed. On failure, `.errors` contains human-readable descriptions
    of every violation that was detected (not just the first one, so the
    operator can fix multiple issues in one iteration).

    This function never raises for user-input problems. It only raises
    `BundleValidationError` for programmer errors (e.g. passing a non-bytes
    object).
    """
    if not isinstance(zip_bytes, (bytes, bytearray)):
        raise BundleValidationError(
            f"zip_bytes must be bytes, got {type(zip_bytes).__name__}."
        )

    errors: List[str] = []
    warnings: List[str] = []
    entries: List[ZipEntryInfo] = []

    if len(zip_bytes) == 0:
        return ZipValidationReport(ok=False, errors=["empty upload"])
    if len(zip_bytes) > MAX_ZIP_BYTES:
        return ZipValidationReport(
            ok=False,
            errors=[
                f"zip size {len(zip_bytes)} exceeds max "
                f"{MAX_ZIP_BYTES} bytes"
            ],
        )

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        return ZipValidationReport(
            ok=False, errors=[f"not a valid zip archive: {exc}"]
        )

    with zf:
        namelist = zf.namelist()
        if not namelist:
            return ZipValidationReport(ok=False, errors=["zip is empty"])

        if len(namelist) > MAX_FILE_COUNT:
            errors.append(
                f"too many entries: {len(namelist)} > {MAX_FILE_COUNT}"
            )

        total_uncompressed = 0
        total_compressed = 0
        file_count = 0
        dir_count = 0
        manifest_seen = False

        for zinfo in zf.infolist():
            raw_name = zinfo.filename

            # Directory entries end with "/"
            is_dir = raw_name.endswith("/") or zinfo.is_dir()

            if is_dir:
                dir_count += 1
                continue

            file_count += 1

            # Reject symlinks before touching the path further.
            # Use the raw name in the error message so the operator sees
            # exactly what was rejected.
            if _is_symlink(zinfo):
                errors.append(f"symlink not allowed: {raw_name!r}")
                continue

            # Path safety checks operate on the raw path. They return a
            # normalized relative path for downstream storage/reporting.
            path_errors, normalized = _check_path_safety(raw_name)
            if path_errors:
                errors.extend(path_errors)
                continue

            # Per-file size
            if zinfo.file_size > MAX_SINGLE_FILE_BYTES:
                errors.append(
                    f"file {normalized!r} size {zinfo.file_size} "
                    f"exceeds {MAX_SINGLE_FILE_BYTES} bytes"
                )
                continue

            total_uncompressed += zinfo.file_size
            total_compressed += zinfo.compress_size

            if normalized == "manifest.json":
                manifest_seen = True

            entries.append(
                ZipEntryInfo(
                    path=normalized,
                    size_bytes=zinfo.file_size,
                    compressed_bytes=zinfo.compress_size,
                    is_dir=False,
                    is_symlink=False,
                )
            )

        # Total uncompressed size cap
        if total_uncompressed > MAX_TOTAL_UNCOMPRESSED_BYTES:
            errors.append(
                f"total uncompressed size {total_uncompressed} exceeds "
                f"{MAX_TOTAL_UNCOMPRESSED_BYTES} bytes"
            )

        # Compression ratio (zip bomb heuristic)
        if total_compressed > 0:
            ratio = total_uncompressed / total_compressed
            if ratio > 100:
                warnings.append(
                    f"high compression ratio ({ratio:.1f}x) — verify contents"
                )

        if not manifest_seen:
            errors.append("missing required file: manifest.json at zip root")

        # Informational warnings
        if not any(e.path.lower() == "readme.md" for e in entries):
            warnings.append("no README.md at bundle root")

    ok = not errors
    return ZipValidationReport(
        ok=ok,
        total_files=file_count,
        total_directories=dir_count,
        total_uncompressed_bytes=total_uncompressed,
        total_compressed_bytes=total_compressed,
        entries=entries,
        errors=errors,
        warnings=warnings,
    )


def parse_manifest(manifest_dict: object) -> BundleManifest:
    """
    Convert a raw dict (typically loaded from manifest.json) into a
    `BundleManifest`, translating Pydantic errors into `BundleManifestError`
    with actionable context.
    """
    try:
        return BundleManifest.model_validate(manifest_dict)
    except Exception as exc:
        # Pydantic v2 raises ValidationError; we normalize it.
        raise BundleManifestError(
            f"Invalid manifest: {exc}",
            details={"raw_error": str(exc)[:500]},
        ) from exc


def assert_safe_paths_for_extraction(report: ZipValidationReport) -> None:
    """
    Raise `BundleSecurityError` if the report contains any path-safety
    violations. Called by the extractor to enforce a second line of defense.
    """
    safety_markers = (
        "absolute path",
        "traversal segment",
        "drive-letter path",
        "symlink not allowed",
    )
    for err in report.errors:
        if any(marker in err for marker in safety_markers):
            raise BundleSecurityError(
                "Refusing to extract an archive with unsafe paths.",
                details={"reason": err},
            )


__all__ = [
    "MAX_ZIP_BYTES",
    "MAX_TOTAL_UNCOMPRESSED_BYTES",
    "MAX_SINGLE_FILE_BYTES",
    "MAX_FILE_COUNT",
    "MAX_PATH_DEPTH",
    "MAX_PATH_LENGTH",
    "FORBIDDEN_TOP_LEVEL_DIRS",
    "FORBIDDEN_FILE_SUFFIXES",
    "validate_zip",
    "parse_manifest",
    "assert_safe_paths_for_extraction",
]
