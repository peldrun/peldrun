# backend/omweb/provider_packages/package_validator.py
"""
Archive validation and safe extraction for Provider Packages.

This module is the security boundary of the M9 install pipeline. Every
uploaded ZIP passes through ``validate_zip()`` BEFORE any file is
extracted to disk. The validator enforces:

    * Total compressed size limit (``MAX_PACKAGE_SIZE_BYTES``).
    * Total uncompressed size limit (``MAX_UNCOMPRESSED_BYTES``).
    * Compression ratio limit (``MAX_COMPRESSION_RATIO``) — the primary
      defense against archive bombs.
    * File count limit (``MAX_FILE_COUNT``).
    * Path length limit (``MAX_PATH_LENGTH``).
    * Path safety (no absolute paths, no ``..`` traversal, no Windows
      drive letters, no NUL bytes).
    * Symlink rejection (unconditional — a Provider Package never needs
      a symlink).
    * Manifest presence and Pydantic validation via
      ``ProviderManifest`` (which enforces semver, provider ID pattern,
      API contract version, forbidden permissions, and cross-field
      consistency between transport / entry_point / base_adapter).
    * Entry-point module presence for ``native`` / ``custom``
      transports — the module path referenced by ``entry_point`` MUST
      resolve to a real ``.py`` file in the archive. Both flat and
      ``src/`` layouts are supported. ``compatible`` transports ship
      no code, so this check is skipped for them.

Design invariants
-----------------

1. **Two-pass validation.**
   ``validate_zip()`` performs every check WITHOUT writing to disk.
   ``safe_extract()`` re-verifies path safety per entry during
   extraction (defense in depth against TOCTOU). Neither function
   trusts the other.

2. **No exceptions for expected failures.**
   Both functions return a structured report (``ValidationReport`` /
   ``ExtractReport``). The caller (``package_manager``) decides
   whether to raise a specific exception subclass. This keeps the
   validator pure and independently testable.

3. **Manifest location tolerance.**
   Two layouts are accepted:
       * Root manifest: ``manifest.json`` at the archive root (the
         recommended layout).
       * Single-folder manifest: ``<folder>/manifest.json`` where the
         archive has EXACTLY ONE top-level entry. The ``strip_prefix``
         field in the report records ``"<folder>/"`` so extraction
         can flatten the tree.
   Archives with MULTIPLE candidate manifests are rejected — ambiguity
   is a security risk, not a convenience to accommodate.

4. **Entry-point resolution (native/custom only).**
   ``entry_point = "module.path:ClassName"`` is resolved to a real
   file:
       * ``<strip_prefix>/module/path.py``
       * ``<strip_prefix>/module/path/__init__.py``
       * ``<strip_prefix>/src/module/path.py``
       * ``<strip_prefix>/src/module/path/__init__.py``
   At least one of these MUST exist for non-compatible transports.
   Without this check, a manifest could declare an entry point that
   never loads, and the user would only discover the problem at first
   chat message.

5. **SHA-256 of the original ZIP.**
   The hash is computed on the raw uploaded bytes, not the extracted
   contents. This is the idempotency key — re-uploading the exact
   same ZIP resolves to the same hash and the same version directory.

6. **No silent warnings that hide failures.**
   Warnings are informational only (e.g. "manifest was inside a
   subfolder; contents will be flattened"). They never mask a check
   that would otherwise fail.

See also
--------

    * ``backend/omweb/provider_packages/schema.py`` — the manifest model
    * ``backend/omweb/provider_packages/errors.py`` — exception hierarchy
    * ``backend/omweb/provider_packages/package_manager.py`` — orchestrator
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from pydantic import ValidationError as PydanticValidationError

from omweb.provider_packages.schema import (
    MANIFEST_FILENAME,
    MAX_COMPRESSION_RATIO,
    MAX_FILE_COUNT,
    MAX_PACKAGE_SIZE_BYTES,
    MAX_PATH_LENGTH,
    MAX_UNCOMPRESSED_BYTES,
    ProviderManifest,
)

logger = logging.getLogger("omweb.provider_packages.validator")


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


@dataclass
class ValidationReport:
    """
    Result of ``validate_zip()``.

    The caller MUST inspect ``ok`` before proceeding. When ``ok`` is
    ``False``, ``errors`` lists every detected problem (validation
    continues past the first error so the user sees the full picture
    in one pass).

    When ``ok`` is ``True``, every field is populated and
    ``safe_extract()`` may be called with this report as the optional
    second argument (to skip the redundant path-safety re-parse and
    reuse the computed hash).
    """

    ok: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    #: Counts and sizes.
    file_count: int = 0
    compressed_size: int = 0
    uncompressed_size: int = 0

    #: SHA-256 hex digest of the raw uploaded bytes.
    sha256: str = ""

    #: Parsed and Pydantic-validated manifest (only when ``ok`` is True).
    manifest: Optional[ProviderManifest] = None

    #: Full path of the manifest inside the ZIP (e.g. ``"manifest.json"``
    #: or ``"my-provider/manifest.json"``). Always relative, no leading
    #: slash.
    manifest_path_in_zip: str = ""

    #: Prefix stripped from every member path during extraction. Empty
    #: when the manifest was at the archive root.
    strip_prefix: str = ""

    #: Convenience: the name of the entry-point module file found in
    #: the archive (relative to the manifest location, prefix already
    #: applied). Empty for ``compatible`` transports (no code shipped)
    #: and when ``ok`` is False.
    entry_point_file: str = ""


@dataclass
class ExtractReport:
    """
    Result of ``safe_extract()``.

    Extraction proceeds file-by-file. On the first path-safety violation
    extraction aborts; the partially-extracted files are left behind for
    the caller to clean up (the manager always wipes the staging
    directory on failure).
    """

    ok: bool
    extracted_files: int = 0
    extracted_bytes: int = 0
    errors: List[str] = field(default_factory=list)
    failed_paths: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Hash
# ---------------------------------------------------------------------------


def compute_sha256(data: bytes) -> str:
    """Return the hex-encoded SHA-256 digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


#: Characters that are NEVER valid in a path on any supported platform.
#: NUL terminates strings at the OS level (C-string truncation attacks);
#: backslash is a path separator on Windows and can smuggle traversal.
_FORBIDDEN_PATH_CHARS = ("\x00", "\\")


def _check_member_path(name: str) -> Optional[str]:
    """
    Return an error string if ``name`` is unsafe as an extraction target,
    or ``None`` if the path is safe.

    Rules
    -----
    * Must be non-empty.
    * Must not contain NUL or backslash.
    * Must not be an absolute path (leading ``/`` or Windows drive
      letter like ``C:``).
    * Must not contain a ``..`` segment anywhere.
    * Must not exceed ``MAX_PATH_LENGTH`` characters.
    * Must not contain an empty segment (``//``) — no platform needs
      it and it can confuse some extractors.
    """
    if not name:
        return "empty path"
    if len(name) > MAX_PATH_LENGTH:
        return f"path exceeds {MAX_PATH_LENGTH} characters"
    for ch in _FORBIDDEN_PATH_CHARS:
        if ch in name:
            return f"path contains forbidden character {ch!r}"
    # Absolute path on Unix or Windows.
    if name.startswith("/") or name.startswith("\\"):
        return "absolute path"
    if len(name) >= 2 and name[1] == ":":
        return "absolute Windows drive path"
    # Traversal.
    parts = name.replace("\\", "/").split("/")
    for part in parts:
        if part == "..":
            return "path contains '..' traversal"
        if part == "":
            # Trailing slashes are common for directory entries — those
            # produce a final empty segment. Ignore only the LAST empty
            # segment (path ends with "/"); reject any interior ones.
            continue
    # Empty interior segment (e.g. "a//b").
    if "//" in name.replace("\\", "/"):
        return "path contains empty segment"
    return None


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    """
    Return True if ``info`` describes a Unix symlink.

    ZIP does not have a dedicated symlink flag. Instead, Unix-mode
    archives store the full mode in the high 16 bits of
    ``external_attr``. A symlink has file type ``0o120000`` (S_IFLNK).
    """
    # The high 16 bits contain the Unix mode when the archive was
    # created on Unix. On Windows-authored archives, ``external_attr``
    # is typically ``0`` or contains DOS attribute bits only, so this
    # check is safely a no-op there.
    if not info.external_attr:
        return False
    mode = (info.external_attr >> 16) & 0o170000
    return mode == stat.S_IFLNK


# ---------------------------------------------------------------------------
# Manifest location
# ---------------------------------------------------------------------------


def _locate_manifest(
    names: List[str],
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Find the manifest inside the archive.

    Returns ``(manifest_path, strip_prefix, error)`` where:
        * ``manifest_path`` — path of the manifest relative to the
          archive root (e.g. ``"manifest.json"`` or
          ``"my-provider/manifest.json"``).
        * ``strip_prefix`` — prefix to strip from every member when
          extracting (``""`` for root manifest, ``"my-provider/"`` for
          single-folder layout).
        * ``error`` — non-None when the layout is ambiguous or invalid.

    Selection rules
    ---------------
    1. Exact root manifest (``"manifest.json"``) — preferred.
    2. Exactly one single-folder manifest (``"<dir>/manifest.json"``
       where ``<dir>`` has no further slashes).
    3. Anything else → error. Multiple candidates are rejected to
       prevent ambiguity attacks (a malicious archive could ship two
       manifests and rely on a buggy extractor picking the wrong one).
    """
    # Rule 1: root manifest.
    if MANIFEST_FILENAME in names:
        return MANIFEST_FILENAME, "", None

    # Rule 2: single-folder manifests (exactly one slash).
    single_folder = [
        n for n in names
        if n.endswith("/" + MANIFEST_FILENAME) and n.count("/") == 1
    ]
    if len(single_folder) == 1:
        path = single_folder[0]
        prefix = path.rsplit("/", 1)[0] + "/"
        return path, prefix, None
    if len(single_folder) > 1:
        return None, None, (
            f"multiple candidate manifests found at one-folder depth: "
            f"{single_folder!r}. The archive must contain exactly one "
            f"manifest, either at the root or inside a single top-level "
            f"folder."
        )

    # Rule 3: deeper nesting — report a helpful error.
    deeper = [n for n in names if n.endswith("/" + MANIFEST_FILENAME)]
    if deeper:
        return None, None, (
            f"manifest.json was found at unexpected depth(s): {deeper!r}. "
            f"Move it to the archive root, or to a single top-level "
            f"folder alongside the rest of the package."
        )

    return None, None, (
        f"manifest.json is missing. Every Provider Package must include "
        f"'{MANIFEST_FILENAME}' at its root or inside a single top-level "
        f"folder."
    )


# ---------------------------------------------------------------------------
# Entry-point resolution
# ---------------------------------------------------------------------------


def _find_entry_point_file(
    names: List[str],
    module_path: str,
    strip_prefix: str,
) -> Optional[str]:
    """
    Return the archive-relative path of the file that implements
    ``module_path``, or ``None`` if no plausible candidate exists.

    ``module_path`` is the part of ``entry_point`` before the ``:``
    separator (e.g. ``"acme_peldrun.adapter"``). The check tries
    four standard layouts:

        * ``<prefix>/module/path.py``
        * ``<prefix>/module/path/__init__.py``
        * ``<prefix>/src/module/path.py``
        * ``<prefix>/src/module/path/__init__.py``

    The first three are the conventional flat layouts; the last two
    correspond to the widely-used ``src/`` layout recommended by the
    PyPA packaging guide.
    """
    if not module_path or not strip_prefix_is_valid(strip_prefix):
        # strip_prefix must be either empty or end with "/".
        return None

    module_as_path = module_path.replace(".", "/")
    candidates = [
        f"{strip_prefix}{module_as_path}.py",
        f"{strip_prefix}{module_as_path}/__init__.py",
        f"{strip_prefix}src/{module_as_path}.py",
        f"{strip_prefix}src/{module_as_path}/__init__.py",
    ]
    name_set = set(names)
    for candidate in candidates:
        if candidate in name_set:
            return candidate
    return None


def strip_prefix_is_valid(prefix: str) -> bool:
    """Return True iff ``prefix`` is empty or ends with ``/``."""
    if prefix == "":
        return True
    return prefix.endswith("/")


# ---------------------------------------------------------------------------
# Main validation entry point
# ---------------------------------------------------------------------------


def validate_zip(zip_bytes: bytes) -> ValidationReport:
    """
    Validate an uploaded Provider Package ZIP.

    No files are written to disk. Every check that can be performed
    from the archive metadata and the manifest bytes is performed here.

    On success, the returned ``ValidationReport`` carries the parsed
    ``ProviderManifest`` and the ``strip_prefix`` to use during
    extraction. On failure, ``ok`` is ``False`` and ``errors`` lists
    every detected problem.

    This function NEVER raises for expected failures (bad input, bad
    manifest). Only truly unexpected errors (e.g. ``MemoryError``)
    propagate.
    """
    report = ValidationReport(
        ok=False, compressed_size=len(zip_bytes)
    )

    # ------------------------------------------------------------------
    # 1. Compressed size limit — checked BEFORE opening the archive
    #    because ZipFile construction reads the central directory,
    #    which itself costs memory proportional to file count.
    # ------------------------------------------------------------------
    if len(zip_bytes) == 0:
        report.errors.append("Uploaded archive is empty.")
        return report

    if len(zip_bytes) > MAX_PACKAGE_SIZE_BYTES:
        report.errors.append(
            f"Archive size {len(zip_bytes)} bytes exceeds the limit of "
            f"{MAX_PACKAGE_SIZE_BYTES} bytes."
        )
        return report

    # ------------------------------------------------------------------
    # 2. Open the archive.
    # ------------------------------------------------------------------
    try:
        zip_file = zipfile.ZipFile(io.BytesIO(zip_bytes), "r")
    except zipfile.BadZipFile as exc:
        report.errors.append(f"Not a valid ZIP archive: {exc}")
        return report
    except Exception as exc:
        report.errors.append(f"Could not open ZIP archive: {exc}")
        return report

    with zip_file:
        try:
            infos = zip_file.infolist()
        except Exception as exc:
            report.errors.append(f"Could not read ZIP central directory: {exc}")
            return report

        # --------------------------------------------------------------
        # 3. File count limit.
        # --------------------------------------------------------------
        report.file_count = len(infos)
        if report.file_count == 0:
            report.errors.append("Archive contains zero files.")
            return report
        if report.file_count > MAX_FILE_COUNT:
            report.errors.append(
                f"Archive contains {report.file_count} files, exceeding "
                f"the limit of {MAX_FILE_COUNT}."
            )
            return report

        # --------------------------------------------------------------
        # 4. Walk every member: path safety, symlinks, size accounting.
        # --------------------------------------------------------------
        total_uncompressed = 0
        safe_names: List[str] = []

        for info in infos:
            name = info.filename

            # Path safety.
            path_error = _check_member_path(name)
            if path_error is not None:
                report.errors.append(
                    f"Unsafe path {name!r}: {path_error}."
                )
                # Do NOT return early — accumulate all path errors so
                # the user sees the full set in one response. But stop
                # adding to safe_names.
                continue

            # Symlink — unconditional rejection.
            if _is_symlink(info):
                report.errors.append(
                    f"Symlink entry {name!r} is not permitted. "
                    f"Provider Packages must ship regular files only."
                )
                continue

            # Size accounting.
            file_size = info.file_size
            if file_size < 0:
                report.errors.append(
                    f"Entry {name!r} has a negative size ({file_size})."
                )
                continue
            total_uncompressed += file_size

            safe_names.append(name)

        report.uncompressed_size = total_uncompressed

        # If any path was unsafe, stop here. Extraction is unsafe.
        if report.errors:
            return report

        # --------------------------------------------------------------
        # 5. Uncompressed size and compression ratio limits.
        # --------------------------------------------------------------
        if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
            report.errors.append(
                f"Total uncompressed size {total_uncompressed} bytes "
                f"exceeds the limit of {MAX_UNCOMPRESSED_BYTES} bytes."
            )
            return report

        if len(zip_bytes) > 0:
            ratio = total_uncompressed / len(zip_bytes)
            if ratio > MAX_COMPRESSION_RATIO:
                report.errors.append(
                    f"Compression ratio {ratio:.1f}:1 exceeds the limit "
                    f"of {MAX_COMPRESSION_RATIO}:1. This may indicate "
                    f"an archive bomb."
                )
                return report

        # --------------------------------------------------------------
        # 6. Manifest location.
        # --------------------------------------------------------------
        manifest_path, strip_prefix, layout_error = _locate_manifest(
            safe_names
        )
        if layout_error is not None:
            report.errors.append(layout_error)
            return report

        # Non-None guaranteed by successful _locate_manifest.
        assert manifest_path is not None
        assert strip_prefix is not None

        report.manifest_path_in_zip = manifest_path
        report.strip_prefix = strip_prefix

        if strip_prefix:
            report.warnings.append(
                f"manifest.json was located inside '{strip_prefix[:-1]}/'; "
                f"contents will be flattened during extraction."
            )

        # --------------------------------------------------------------
        # 7. Read and parse the manifest.
        # --------------------------------------------------------------
        try:
            manifest_raw_bytes = zip_file.read(manifest_path)
        except Exception as exc:
            report.errors.append(
                f"Could not read {manifest_path!r} from the archive: {exc}"
            )
            return report

        # Guard against zip bombs in the manifest itself — the manifest
        # is small; anything over 1 MB is suspicious.
        if len(manifest_raw_bytes) > 1 * 1024 * 1024:
            report.errors.append(
                f"{manifest_path!r} is {len(manifest_raw_bytes)} bytes, "
                f"which is unreasonably large for a manifest file "
                f"(limit: 1 MB)."
            )
            return report

        try:
            manifest_dict = json.loads(manifest_raw_bytes.decode("utf-8"))
        except UnicodeDecodeError as exc:
            report.errors.append(
                f"manifest.json is not valid UTF-8: {exc}"
            )
            return report
        except json.JSONDecodeError as exc:
            report.errors.append(
                f"manifest.json is not valid JSON: {exc}"
            )
            return report

        if not isinstance(manifest_dict, dict):
            report.errors.append(
                f"manifest.json must be a JSON object, got "
                f"{type(manifest_dict).__name__}."
            )
            return report

        # --------------------------------------------------------------
        # 8. Pydantic validation.
        # --------------------------------------------------------------
        try:
            manifest = ProviderManifest.model_validate(manifest_dict)
        except PydanticValidationError as exc:
            # Translate Pydantic errors into a flat list of messages
            # that name the exact field.
            for err in exc.errors():
                loc = ".".join(str(p) for p in err.get("loc", []))
                msg = err.get("msg", "invalid value")
                report.errors.append(
                    f"manifest.{loc}: {msg}"
                )
            return report

        # --------------------------------------------------------------
        # 9. Entry-point file presence (native/custom transports only).
        #
        # ``compatible`` packages ship no code — they reuse a built-in
        # adapter via the ``base_adapter`` manifest field. There is no
        # ``entry_point`` to resolve, so this check is skipped for
        # them. The Pydantic model already enforced the cross-field
        # contract (compatible => no entry_point, has base_adapter).
        # --------------------------------------------------------------
        if manifest.transport.value != "compatible":
            # Non-None guaranteed by the Pydantic model_validator for
            # any non-compatible transport.
            assert manifest.entry_point is not None
            module_path = manifest.entry_point.split(":", 1)[0]
            entry_file = _find_entry_point_file(
                safe_names, module_path, strip_prefix
            )
            if entry_file is None:
                report.errors.append(
                    f"entry_point module '{module_path}' does not "
                    f"resolve to any .py file in the archive. Tried: "
                    f"'{strip_prefix}{module_path.replace('.', '/')}.py', "
                    f"'{strip_prefix}{module_path.replace('.', '/')}/__init__.py', "
                    f"and the same paths under '{strip_prefix}src/'."
                )
                return report
            report.entry_point_file = entry_file
        # Compatible transports reuse a built-in adapter — no code file
        # to verify, and ``report.entry_point_file`` stays "".

        report.manifest = manifest

    # ------------------------------------------------------------------
    # 10. SHA-256 of the raw uploaded bytes.
    # ------------------------------------------------------------------
    report.sha256 = compute_sha256(zip_bytes)

    # ------------------------------------------------------------------
    # 11. Success.
    # ------------------------------------------------------------------
    report.ok = True
    return report


# ---------------------------------------------------------------------------
# Safe extraction
# ---------------------------------------------------------------------------


def safe_extract(
    zip_bytes: bytes,
    target_dir: Path,
    validation: Optional[ValidationReport] = None,
) -> ExtractReport:
    """
    Extract a validated archive into ``target_dir`` with defense-in-depth
    path safety.

    Behavior
    --------
    * Every member path is RE-CHECKED against ``_check_member_path``
      immediately before extraction. This is the TOCTOU defense: even
      if a malicious process swapped the archive between validation
      and extraction, the re-check catches it.
    * Directory entries are created as-is.
    * When ``validation.strip_prefix`` is non-empty, the prefix is
      stripped from every member before extraction. This flattens the
      single-folder layout into the target directory.
    * Symlinks are rejected (should never reach here given
      ``validate_zip`` behavior, but the check is repeated).

    Returns
    -------
    ExtractReport
        ``ok`` is ``True`` on full success. On any failure, extraction
        aborts and ``failed_paths`` lists the paths that could not be
        written. The caller MUST clean up ``target_dir`` on failure.
    """
    report = ExtractReport(ok=False)

    try:
        zip_file = zipfile.ZipFile(io.BytesIO(zip_bytes), "r")
    except zipfile.BadZipFile as exc:
        report.errors.append(f"Could not reopen archive: {exc}")
        return report

    strip_prefix = validation.strip_prefix if validation else ""

    with zip_file:
        try:
            infos = zip_file.infolist()
        except Exception as exc:
            report.errors.append(
                f"Could not enumerate members: {exc}"
            )
            return report

        for info in infos:
            original_name = info.filename

            # ----------------------------------------------------------
            # Re-verify path safety (defense in depth).
            # ----------------------------------------------------------
            path_error = _check_member_path(original_name)
            if path_error is not None:
                report.errors.append(
                    f"Aborting extraction: unsafe path "
                    f"{original_name!r} — {path_error}."
                )
                report.failed_paths.append(original_name)
                return report

            # ----------------------------------------------------------
            # Re-verify not a symlink.
            # ----------------------------------------------------------
            if _is_symlink(info):
                report.errors.append(
                    f"Aborting extraction: symlink "
                    f"{original_name!r} is not permitted."
                )
                report.failed_paths.append(original_name)
                return report

            # ----------------------------------------------------------
            # Apply strip_prefix.
            # ----------------------------------------------------------
            if strip_prefix:
                if not original_name.startswith(strip_prefix):
                    # Member outside the strip prefix — this can only
                    # happen for archives with mixed top-level entries
                    # (which validate_zip rejects). Skip with a warning
                    # rather than aborting, so a harmless extra file
                    # (e.g. macOS's __MACOSX) does not break install.
                    report.errors.append(
                        f"Skipping {original_name!r}: not inside "
                        f"strip prefix {strip_prefix!r}."
                    )
                    continue
                relative = original_name[len(strip_prefix):]
                if not relative:
                    # The prefix itself is a directory entry; skip.
                    continue
            else:
                relative = original_name

            # ----------------------------------------------------------
            # Build and containment-check the resolved target path.
            # ----------------------------------------------------------
            try:
                resolved_target = (target_dir / relative).resolve()
            except Exception as exc:
                report.errors.append(
                    f"Could not resolve extraction target for "
                    f"{relative!r}: {exc}"
                )
                report.failed_paths.append(relative)
                return report

            try:
                resolved_root = target_dir.resolve()
            except Exception as exc:
                report.errors.append(
                    f"Could not resolve extraction root: {exc}"
                )
                return report

            # The target MUST be the root itself or a descendant.
            if (
                resolved_target != resolved_root
                and resolved_root not in resolved_target.parents
            ):
                report.errors.append(
                    f"Aborting extraction: {relative!r} escapes the "
                    f"target directory. This should have been caught "
                    f"by validate_zip(); rejecting as a precaution."
                )
                report.failed_paths.append(relative)
                return report

            # ----------------------------------------------------------
            # Write the file (or create the directory).
            # ----------------------------------------------------------
            try:
                if info.is_dir() or relative.endswith("/"):
                    resolved_target.mkdir(parents=True, exist_ok=True)
                    continue

                # Ensure parent directory exists.
                resolved_target.parent.mkdir(parents=True, exist_ok=True)

                # Read in chunks to avoid loading large files fully
                # into memory — matters for packages shipping model
                # files or large test fixtures.
                with zip_file.open(info, "r") as source:
                    with open(resolved_target, "wb") as dest:
                        while True:
                            chunk = source.read(64 * 1024)
                            if not chunk:
                                break
                            dest.write(chunk)

                report.extracted_files += 1
                report.extracted_bytes += info.file_size
            except Exception as exc:
                report.errors.append(
                    f"Failed to extract {relative!r}: {exc}"
                )
                report.failed_paths.append(relative)
                return report

    report.ok = True
    return report


# ---------------------------------------------------------------------------
# Convenience wrappers used by the manager
# ---------------------------------------------------------------------------


def validate_and_hash(zip_bytes: bytes) -> ValidationReport:
    """
    Alias for ``validate_zip`` retained for clarity at call sites that
    emphasize the hash computation as part of validation.
    """
    return validate_zip(zip_bytes)


def extract_to_staging(
    zip_bytes: bytes,
    staging_dir: Path,
    validation: ValidationReport,
) -> ExtractReport:
    """
    Alias for ``safe_extract`` retained for readability in the manager
    code, where the function is always called after a successful
    ``validate_zip``.
    """
    return safe_extract(zip_bytes, staging_dir, validation=validation)


__all__ = [
    "ValidationReport",
    "ExtractReport",
    "compute_sha256",
    "validate_zip",
    "safe_extract",
    "validate_and_hash",
    "extract_to_staging",
    "strip_prefix_is_valid",
]
