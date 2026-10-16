"""
Phase 2 unit tests: validator module.

Run:
    cd D:\\AI\\peldrun\\backend
    python -m pytest tests/test_bundle_validation.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pytest

from omweb.tools.bundles.errors import (
    BundleManifestError,
    BundleSecurityError,
    BundleValidationError,
)
from omweb.tools.bundles.schema import BundleManifest
from omweb.tools.bundles.validator import (
    MAX_FILE_COUNT,
    MAX_SINGLE_FILE_BYTES,
    MAX_ZIP_BYTES,
    assert_safe_paths_for_extraction,
    parse_manifest,
    validate_zip,
)
from tests.fixtures import bundle_factory as bf


# ---------------------------------------------------------------------------
# validate_zip — happy path
# ---------------------------------------------------------------------------

class TestValidateZipHappyPath:
    def test_valid_minimal(self):
        report = validate_zip(bf.make_valid_bundle())
        assert report.ok is True
        assert report.total_files == 2
        assert report.errors == []

    def test_valid_with_helpers(self):
        report = validate_zip(bf.make_bundle_with_helpers())
        assert report.ok is True
        assert report.total_files >= 5
        # README present → no warning
        assert all("README" not in w for w in report.warnings)

    def test_valid_with_requirements(self):
        report = validate_zip(bf.make_bundle_with_requirements())
        assert report.ok is True
        paths = {e.path for e in report.entries}
        assert "requirements.txt" in paths


# ---------------------------------------------------------------------------
# validate_zip — structural failures
# ---------------------------------------------------------------------------

class TestValidateZipStructural:
    def test_empty_upload(self):
        report = validate_zip(b"")
        assert report.ok is False
        assert any("empty" in e for e in report.errors)

    def test_not_a_zip(self):
        report = validate_zip(b"this is not a zip")
        assert report.ok is False
        assert any("valid zip" in e for e in report.errors)

    def test_missing_manifest(self):
        report = validate_zip(bf.make_bundle_missing_manifest())
        assert report.ok is False
        assert any("manifest.json" in e for e in report.errors)

    def test_zip_too_large(self):
        # Build a payload slightly over the limit
        payload = b"\x00" * (MAX_ZIP_BYTES + 1)
        report = validate_zip(payload)
        assert report.ok is False
        assert any("exceeds" in e and "size" in e for e in report.errors)

    def test_too_many_files(self):
        report = validate_zip(bf.make_bundle_too_many_files())
        assert report.ok is False
        assert any("too many" in e.lower() for e in report.errors)


# ---------------------------------------------------------------------------
# validate_zip — security
# ---------------------------------------------------------------------------

class TestValidateZipSecurity:
    def test_traversal_rejected(self):
        report = validate_zip(bf.make_bundle_with_traversal())
        assert report.ok is False
        assert any("traversal" in e for e in report.errors)

    def test_absolute_path_rejected(self):
        report = validate_zip(bf.make_bundle_with_absolute_path())
        assert report.ok is False
        assert any(
            "absolute" in e or "drive-letter" in e for e in report.errors
        )

    def test_symlink_rejected(self):
        report = validate_zip(bf.make_bundle_with_symlink())
        assert report.ok is False
        assert any("symlink" in e for e in report.errors)

    def test_forbidden_directory_rejected(self):
        report = validate_zip(bf.make_bundle_with_forbidden_dir())
        assert report.ok is False
        assert any("forbidden" in e for e in report.errors)

    def test_oversized_single_file(self):
        report = validate_zip(bf.make_bundle_with_single_oversized_file())
        assert report.ok is False
        assert any("size" in e and "exceeds" in e for e in report.errors)


# ---------------------------------------------------------------------------
# assert_safe_paths_for_extraction
# ---------------------------------------------------------------------------

class TestSafePathsAssertion:
    def test_valid_report_passes(self):
        report = validate_zip(bf.make_valid_bundle())
        # Should not raise
        assert_safe_paths_for_extraction(report)

    def test_traversal_report_raises(self):
        report = validate_zip(bf.make_bundle_with_traversal())
        with pytest.raises(BundleSecurityError):
            assert_safe_paths_for_extraction(report)

    def test_symlink_report_raises(self):
        report = validate_zip(bf.make_bundle_with_symlink())
        with pytest.raises(BundleSecurityError):
            assert_safe_paths_for_extraction(report)


# ---------------------------------------------------------------------------
# parse_manifest
# ---------------------------------------------------------------------------

class TestParseManifest:
    def test_valid_manifest(self):
        raw = bf.make_manifest()
        manifest = parse_manifest(raw)
        assert isinstance(manifest, BundleManifest)
        assert manifest.id == "test_tool"

    def test_invalid_manifest_raises_manifest_error(self):
        with pytest.raises(BundleManifestError):
            parse_manifest({"id": "BadID"})

    def test_extra_field_raises(self):
        raw = bf.make_manifest()
        raw["unknown_field"] = "x"
        with pytest.raises(BundleManifestError):
            parse_manifest(raw)


# ---------------------------------------------------------------------------
# Type safety
# ---------------------------------------------------------------------------

class TestTypeSafety:
    def test_validate_zip_rejects_non_bytes(self):
        with pytest.raises(BundleValidationError):
            validate_zip("not bytes")  # type: ignore[arg-type]

    def test_validate_zip_rejects_none(self):
        with pytest.raises(BundleValidationError):
            validate_zip(None)  # type: ignore[arg-type]
