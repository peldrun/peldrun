"""
Phase 1 unit tests: errors + schema for the Tool Bundles subsystem.

Run:
    cd D:\\AI\\peldrun\\backend
    python -m pytest tests/test_bundles_phase1.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pytest
from pydantic import ValidationError

from omweb.tools.bundles import (
    BundleCapability,
    BundleEntry,
    BundleError,
    BundleInstallResult,
    BundleManifest,
    BundleRegistryIndex,
    BundleSecurityError,
    BundleValidationError,
    BundleVersionRecord,
    DependenciesSpec,
    RuntimeSpec,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class TestErrors:
    def test_base_error_serializes(self):
        err = BundleError("something broke", details={"key": "value"})
        payload = err.to_dict()
        assert payload["error"] == "something broke"
        assert payload["code"] == "BUNDLE_ERROR"
        assert payload["details"] == {"key": "value"}

    def test_subclass_default_codes(self):
        assert BundleValidationError("x").code == "BUNDLE_VALIDATION_FAILED"
        assert BundleSecurityError("x").code == "BUNDLE_SECURITY_VIOLATION"

    def test_subclass_inheritance(self):
        err = BundleValidationError("bad input")
        assert isinstance(err, BundleError)


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

class TestManifest:
    def _base_manifest(self, **overrides):
        base = {
            "schema_version": "1.0.0",
            "id": "word_count",
            "version": "1.0.0",
            "name": "Word Count",
            "description": "Counts words in a workspace file.",
            "entry_point": "tool.py",
        }
        base.update(overrides)
        return base

    def test_valid_minimal(self):
        m = BundleManifest(**self._base_manifest())
        assert m.id == "word_count"
        assert m.safety_level == "safe"
        assert m.capabilities == []
        assert m.runtime.python_min == "3.11"

    def test_invalid_schema_major(self):
        with pytest.raises(ValidationError) as ei:
            BundleManifest(**self._base_manifest(schema_version="2.0.0"))
        assert "Unsupported schema_version major" in str(ei.value)

    def test_invalid_id_uppercase(self):
        with pytest.raises(ValidationError):
            BundleManifest(**self._base_manifest(id="WordCount"))

    def test_invalid_id_too_short(self):
        with pytest.raises(ValidationError):
            BundleManifest(**self._base_manifest(id="ab"))

    def test_invalid_version_not_semver(self):
        with pytest.raises(ValidationError):
            BundleManifest(**self._base_manifest(version="v1"))

    def test_entry_point_rejects_absolute(self):
        with pytest.raises(ValidationError):
            BundleManifest(**self._base_manifest(entry_point="/etc/passwd.py"))

    def test_entry_point_rejects_traversal(self):
        with pytest.raises(ValidationError):
            BundleManifest(**self._base_manifest(entry_point="../../evil.py"))

    def test_entry_point_rejects_non_py(self):
        with pytest.raises(ValidationError):
            BundleManifest(**self._base_manifest(entry_point="tool.txt"))

    def test_dangerous_without_capabilities(self):
        with pytest.raises(ValidationError):
            BundleManifest(
                **self._base_manifest(
                    safety_level="dangerous",
                    capabilities=[],
                )
            )

    def test_dangerous_with_capabilities(self):
        m = BundleManifest(
            **self._base_manifest(
                safety_level="dangerous",
                capabilities=["shell", "network"],
            )
        )
        assert BundleCapability.SHELL in m.capabilities

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            BundleManifest(**self._base_manifest(unknown="x"))

    def test_checksum_validation(self):
        m = BundleManifest(
            **self._base_manifest(checksum_sha256="a" * 64)
        )
        assert m.checksum_sha256 == "a" * 64

    def test_checksum_rejects_short(self):
        with pytest.raises(ValidationError):
            BundleManifest(**self._base_manifest(checksum_sha256="abc"))


# ---------------------------------------------------------------------------
# Nested specs
# ---------------------------------------------------------------------------

class TestNestedSpecs:
    def test_runtime_rejects_bad_version(self):
        with pytest.raises(ValidationError):
            RuntimeSpec(python_min="3")

    def test_runtime_window_ordered(self):
        with pytest.raises(ValidationError):
            BundleManifest(
                schema_version="1.0.0",
                id="wc",
                version="1.0.0",
                name="WC",
                description="x",
                entry_point="tool.py",
                runtime={"python_min": "3.12", "python_max": "3.11"},
            )

    def test_dependencies_accepts_valid(self):
        deps = DependenciesSpec(pip=["pypdf2>=3.0.0", "requests"])
        assert len(deps.pip) == 2

    def test_dependencies_rejects_bad_pip(self):
        with pytest.raises(ValidationError):
            DependenciesSpec(pip=["!!!bad!!!"])


# ---------------------------------------------------------------------------
# Registry records
# ---------------------------------------------------------------------------

class TestRegistryRecords:
    def test_bundle_entry_round_trip(self):
        entry = BundleEntry(
            id="word_count",
            name="Word Count",
            description="desc",
            active_version="1.0.0",
            active_hash="a" * 16,
            installed_versions=[
                BundleVersionRecord(
                    version="1.0.0",
                    hash="a" * 16,
                    installed_at="2026-10-16T15:00:00Z",
                    size_bytes=4096,
                    file_count=3,
                    entry_point="tool.py",
                )
            ],
        )
        assert entry.get_version("1.0.0") is not None
        assert entry.get_version("2.0.0") is None

    def test_registry_index_defaults(self):
        idx = BundleRegistryIndex()
        assert idx.schema_version == "1.0.0"
        assert idx.bundles == {}


# ---------------------------------------------------------------------------
# Install result
# ---------------------------------------------------------------------------

class TestInstallResult:
    def test_install_result_serializes(self):
        result = BundleInstallResult(
            id="wc",
            version="1.0.0",
            hash="a" * 16,
            name="WC",
            description="x",
            size_bytes=100,
            file_count=1,
        )
        payload = result.model_dump()
        assert payload["id"] == "wc"
        assert payload["install_report"]["requirements_installed"] is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
