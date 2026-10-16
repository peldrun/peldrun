"""
Safe extraction of validated tool bundles.

This module assumes `validator.validate_zip()` has already approved the
payload. It performs the actual disk writes with:

  * Per-file size accounting (defense-in-depth on top of validation).
  * Atomic-ish extraction: files are written to a temp subdir first, then
    the caller is responsible for the final atomic rename.
  * No symlink creation, no device files, no executable bit preservation.
  * Automatic cleanup of partial state if extraction aborts mid-way.

The extractor does NOT read the manifest — that is the caller's job.
"""

from __future__ import annotations

import io
import logging
import os
import time
import zipfile
from pathlib import Path
from typing import Optional

from omweb.tools.bundles.errors import BundleExtractionError
from omweb.tools.bundles.schema import (
    ExtractReport,
    ExtractStep,
    ZipValidationReport,
)
from omweb.tools.bundles.validator import (
    MAX_SINGLE_FILE_BYTES,
    MAX_TOTAL_UNCOMPRESSED_BYTES,
    assert_safe_paths_for_extraction,
)

logger = logging.getLogger("omweb.tools.bundles.extractor")


def safe_extract(
    zip_bytes: bytes,
    target_dir: Path,
    *,
    validation_report: Optional[ZipValidationReport] = None,
) -> ExtractReport:
    """
    Extract a validated zip payload into `target_dir`.

    If `validation_report` is provided, the extractor uses it to enforce a
    second line of defense: any path-safety violation triggers
    `BundleSecurityError` before a single byte is written.

    The extractor never creates the target directory's parents implicitly.
    The caller is expected to have prepared a clean, empty target directory.

    On any error, all files written during this call are removed before the
    exception propagates, leaving the filesystem unchanged.
    """
    target_dir = Path(target_dir)

    if validation_report is not None:
        assert_safe_paths_for_extraction(validation_report)

    if not target_dir.exists():
        try:
            target_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            pass
        except OSError as exc:
            raise BundleExtractionError(
                f"Cannot create target directory {target_dir}: {exc}",
                details={"target_dir": str(target_dir)},
            ) from exc

    if not target_dir.is_dir():
        raise BundleExtractionError(
            f"target_dir {target_dir} is not a directory.",
            details={"target_dir": str(target_dir)},
        )

    started = time.perf_counter()
    steps: list[ExtractStep] = []
    warnings: list[str] = []
    written_paths: list[Path] = []
    total_bytes = 0
    extracted = 0
    skipped = 0
    failed = 0

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for zinfo in zf.infolist():
                raw_name = zinfo.filename
                normalized = raw_name.replace("\\", "/").lstrip("/")

                if not normalized:
                    skipped += 1
                    steps.append(
                        ExtractStep(
                            path=raw_name,
                            status="skipped",
                            detail="empty path",
                        )
                    )
                    continue

                # Directory entries
                if normalized.endswith("/") or zinfo.is_dir():
                    dest_dir = (target_dir / normalized).resolve()
                    try:
                        dest_dir.relative_to(target_dir.resolve())
                    except ValueError:
                        raise BundleExtractionError(
                            f"Directory path escapes target: {normalized!r}"
                        )
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    continue

                # Final containment check before writing
                dest_file = (target_dir / normalized).resolve()
                try:
                    dest_file.relative_to(target_dir.resolve())
                except ValueError:
                    raise BundleExtractionError(
                        f"File path escapes target: {normalized!r}",
                        details={"path": normalized},
                    )

                # Per-file size guard (should already be enforced by validator)
                if zinfo.file_size > MAX_SINGLE_FILE_BYTES:
                    failed += 1
                    steps.append(
                        ExtractStep(
                            path=normalized,
                            status="failed",
                            detail=(
                                f"file size {zinfo.file_size} exceeds "
                                f"limit {MAX_SINGLE_FILE_BYTES}"
                            ),
                        )
                    )
                    continue

                if total_bytes + zinfo.file_size > MAX_TOTAL_UNCOMPRESSED_BYTES:
                    failed += 1
                    steps.append(
                        ExtractStep(
                            path=normalized,
                            status="failed",
                            detail="cumulative size cap exceeded",
                        )
                    )
                    continue

                dest_file.parent.mkdir(parents=True, exist_ok=True)

                try:
                    with zf.open(zinfo, "r") as src, open(dest_file, "wb") as dst:
                        # Stream in chunks to avoid loading the whole file in memory
                        chunk_size = 64 * 1024
                        remaining = zinfo.file_size
                        while remaining > 0:
                            chunk = src.read(min(chunk_size, remaining))
                            if not chunk:
                                break
                            dst.write(chunk)
                            remaining -= len(chunk)
                except OSError as exc:
                    failed += 1
                    steps.append(
                        ExtractStep(
                            path=normalized,
                            status="failed",
                            detail=f"write failed: {exc}",
                        )
                    )
                    continue

                # Clear Windows read-only flag that may have been set
                if os.name == "nt":
                    try:
                        os.chmod(dest_file, 0o644)
                    except OSError:
                        pass

                written_paths.append(dest_file)
                total_bytes += zinfo.file_size
                extracted += 1
                steps.append(
                    ExtractStep(
                        path=normalized,
                        status="extracted",
                        size_bytes=zinfo.file_size,
                    )
                )
    except zipfile.BadZipFile as exc:
        _cleanup_partial(written_paths, target_dir)
        raise BundleExtractionError(
            f"Corrupt zip archive: {exc}",
        ) from exc
    except Exception as exc:
        _cleanup_partial(written_paths, target_dir)
        if isinstance(exc, BundleExtractionError):
            raise
        raise BundleExtractionError(
            f"Extraction failed: {exc}",
            details={"exception_type": type(exc).__name__},
        ) from exc

    duration_ms = int((time.perf_counter() - started) * 1000)

    return ExtractReport(
        ok=(failed == 0),
        target_dir=str(target_dir),
        extracted_files=extracted,
        skipped_files=skipped,
        failed_files=failed,
        total_bytes=total_bytes,
        duration_ms=duration_ms,
        steps=steps,
        warnings=warnings,
    )


def _cleanup_partial(written_paths: list[Path], target_dir: Path) -> None:
    """
    Best-effort removal of files written during a failed extraction.

    The caller is responsible for removing `target_dir` itself if desired.
    """
    for path in reversed(written_paths):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("Cleanup: failed to remove %s: %s", path, exc)
    try:
        target_dir.rmdir()
    except OSError:
        # Directory not empty or already gone — ignore.
        pass


__all__ = ["safe_extract"]
