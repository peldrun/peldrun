"""
Phase 2 unit tests: extractor module.

Run:
    cd D:\\AI\\peldrun\\backend
    python -m pytest tests/test_bundle_extraction.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pytest

from omweb.tools.bundles.errors import (
    BundleExtractionError,
    BundleSecurityError,
)
from omweb.tools.bundles.extractor import safe_extract
from omweb.tools.bundles.validator import validate_zip
from tests.fixtures import bundle_factory as bf


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestSafeExtractHappyPath:
    def test_extracts_minimal_bundle(self, tmp_path: Path):
        payload = bf.make_valid_bundle()
        target = tmp_path / "bundle"
        report = safe_extract(payload, target)

        assert report.ok is True
        assert report.extracted_files == 2
        assert report.failed_files == 0

        manifest_file = target / "manifest.json"
        tool_file = target / "tool.py"
        assert manifest_file.is_file()
        assert tool_file.is_file()

    def test_extracts_helpers_recursively(self, tmp_path: Path):
        payload = bf.make_bundle_with_helpers()
        target = tmp_path / "bundle"
        report = safe_extract(payload, target)

        assert report.ok is True
        assert (target / "helpers" / "util.py").is_file()
        assert (target / "helpers" / "__init__.py").is_file()

    def test_extracts_with_validation_report(self, tmp_path: Path):
        payload = bf.make_valid_bundle()
        report = validate_zip(payload)
        target = tmp_path / "bundle"

        extract_report = safe_extract(
            payload, target, validation_report=report
        )
        assert extract_report.ok is True

    def test_creates_target_dir_if_missing(self, tmp_path: Path):
        payload = bf.make_valid_bundle()
        target = tmp_path / "new" / "sub" / "bundle"
        # parent doesn't exist either
        report = safe_extract(payload, target)
        assert report.ok is True
        assert target.is_dir()

    def test_per_file_sizes_reported(self, tmp_path: Path):
        payload = bf.make_valid_bundle()
        target = tmp_path / "bundle"
        report = safe_extract(payload, target)

        # Every step has an accurate size
        for step in report.steps:
            if step.status == "extracted":
                actual = (target / step.path).stat().st_size
                assert step.size_bytes == actual


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

class TestSafeExtractSecurity:
    def test_rejects_traversal_with_report(self, tmp_path: Path):
        payload = bf.make_bundle_with_traversal()
        report = validate_zip(payload)
        target = tmp_path / "bundle"

        with pytest.raises(BundleSecurityError):
            safe_extract(payload, target, validation_report=report)

        # No file should have been written
        assert not (target / "manifest.json").exists()

    def test_rejects_symlink_with_report(self, tmp_path: Path):
        payload = bf.make_bundle_with_symlink()
        report = validate_zip(payload)
        target = tmp_path / "bundle"

        with pytest.raises(BundleSecurityError):
            safe_extract(payload, target, validation_report=report)

    def test_no_files_outside_target_on_traversal(self, tmp_path: Path):
        # Even without a validation report, containment must hold
        payload = bf.make_bundle_with_traversal()
        target = tmp_path / "bundle"

        with pytest.raises((BundleExtractionError, BundleSecurityError)):
            safe_extract(payload, target)

        # Sibling file must not exist
        assert not (tmp_path / "evil.txt").exists()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestSafeExtractErrors:
    def test_rejects_corrupt_zip(self, tmp_path: Path):
        target = tmp_path / "bundle"
        with pytest.raises(BundleExtractionError):
            safe_extract(b"not a zip", target)

    def test_target_is_file_not_dir(self, tmp_path: Path):
        file_path = tmp_path / "iam_a_file"
        file_path.write_text("x")
        payload = bf.make_valid_bundle()

        with pytest.raises(BundleExtractionError):
            safe_extract(payload, file_path)

    def test_partial_cleanup_on_corrupt(self, tmp_path: Path):
        # A truncated zip that starts valid but fails mid-stream
        payload = bf.make_valid_bundle()
        truncated = payload[: len(payload) // 2]
        target = tmp_path / "bundle"

        with pytest.raises(BundleExtractionError):
            safe_extract(truncated, target)

        # Target either removed or empty of extracted files
        if target.exists():
            files = [p for p in target.rglob("*") if p.is_file()]
            assert files == []


# ---------------------------------------------------------------------------
# Report structure
# ---------------------------------------------------------------------------

class TestExtractReportShape:
    def test_report_has_duration(self, tmp_path: Path):
        payload = bf.make_valid_bundle()
        report = safe_extract(payload, tmp_path / "bundle")
        assert report.duration_ms >= 0

    def test_report_tracks_total_bytes(self, tmp_path: Path):
        payload = bf.make_valid_bundle()
        report = safe_extract(payload, tmp_path / "bundle")
        assert report.total_bytes > 0
        assert report.total_bytes == sum(
            s.size_bytes for s in report.steps if s.status == "extracted"
        )
