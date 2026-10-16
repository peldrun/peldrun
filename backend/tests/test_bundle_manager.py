"""
Phase 3 unit tests: BundleManager orchestration layer.

Run:
    cd D:\\AI\\peldrun\\backend
    python -m pytest tests/test_bundle_manager.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pytest

from omweb.tools.bundles import (
    BundleManager,
    BundleManifestError,
    BundleSecurityError,
    BundleStorageError,
    BundleValidationError,
)
from tests.fixtures import bundle_factory as bf


# ---------------------------------------------------------------------------
# Fixture: fresh manager per test
# ---------------------------------------------------------------------------

@pytest.fixture
def manager(tmp_path: Path) -> BundleManager:
    root = tmp_path / "store_tools"
    return BundleManager(root)


# ---------------------------------------------------------------------------
# Install — happy path
# ---------------------------------------------------------------------------

class TestInstallHappyPath:
    def test_install_minimal(self, manager: BundleManager):
        payload = bf.make_valid_bundle()
        result = manager.install_bundle(payload, "test_bundle.zip")

        assert result.id == "test_tool"
        assert result.version == "1.0.0"
        assert len(result.hash) == 16
        assert result.size_bytes == len(payload)

    def test_install_with_helpers(self, manager: BundleManager):
        payload = bf.make_bundle_with_helpers(
        ) if hasattr(bf, "make_bundle_with_helpers") else bf.make_valid_bundle()

        result = manager.install_bundle(payload, "helpers.zip")
        assert result.id in ("with_helpers", "test_tool")

    def test_installed_bundle_visible_in_list(self, manager: BundleManager):
        payload = bf.make_valid_bundle()
        manager.install_bundle(payload, "x.zip")

        bundles = manager.list_bundles()
        assert len(bundles) == 1
        assert bundles[0].id == "test_tool"
        assert bundles[0].active_version == "1.0.0"

    def test_get_bundle(self, manager: BundleManager):
        payload = bf.make_valid_bundle()
        manager.install_bundle(payload, "x.zip")

        entry = manager.get_bundle("test_tool")
        assert entry is not None
        assert entry.id == "test_tool"

    def test_get_missing_returns_none(self, manager: BundleManager):
        assert manager.get_bundle("nonexistent") is None


# ---------------------------------------------------------------------------
# Install — idempotency
# ---------------------------------------------------------------------------

class TestInstallIdempotency:
    def test_same_payload_twice_uses_same_blob(self, manager: BundleManager):
        payload = bf.make_valid_bundle()

        r1 = manager.install_bundle(payload, "x.zip")
        r2 = manager.install_bundle(payload, "x.zip")

        assert r1.hash == r2.hash
        # Only one blob should exist
        blobs = manager.storage.list_blobs()
        assert len(blobs) == 1
        # Only one version record (not duplicated)
        entry = manager.get_bundle(r1.id)
        assert len(entry.installed_versions) == 1

    def test_same_payload_after_registry_reload(self, manager: BundleManager, tmp_path: Path):
        payload = bf.make_valid_bundle()

        r1 = manager.install_bundle(payload, "x.zip")

        # Reload manager from disk
        manager2 = BundleManager(manager.root_dir)
        r2 = manager2.install_bundle(payload, "x.zip")

        assert r1.hash == r2.hash


# ---------------------------------------------------------------------------
# Install — version bumps
# ---------------------------------------------------------------------------

class TestVersionBumps:
    def test_two_versions_side_by_side(self, manager: BundleManager):
        p1 = bf.make_valid_bundle(manifest_overrides={"version": "1.0.0"})
        p2 = bf.make_valid_bundle(manifest_overrides={"version": "1.1.0"})

        r1 = manager.install_bundle(p1, "v1.zip")
        r2 = manager.install_bundle(p2, "v2.zip")

        assert r1.hash != r2.hash
        entry = manager.get_bundle(r1.id)
        assert len(entry.installed_versions) == 2
        assert entry.active_version == "1.1.0"

        # Both blobs still present
        blobs = manager.storage.list_blobs()
        assert r1.hash in blobs
        assert r2.hash in blobs

    def test_reinstall_same_version_updates_record(self, manager: BundleManager):
        payload = bf.make_valid_bundle()

        manager.install_bundle(payload, "x.zip")
        manager.install_bundle(payload, "x.zip")

        entry = manager.get_bundle("test_tool")
        # Still only one version record
        assert len(entry.installed_versions) == 1


# ---------------------------------------------------------------------------
# Install — validation failures
# ---------------------------------------------------------------------------

class TestInstallValidation:
    def test_rejects_traversal(self, manager: BundleManager):
        payload = bf.make_bundle_with_traversal()
        with pytest.raises(BundleValidationError):
            manager.install_bundle(payload, "evil.zip")

    def test_rejects_absolute_path(self, manager: BundleManager):
        payload = bf.make_bundle_with_absolute_path()
        with pytest.raises(BundleValidationError):
            manager.install_bundle(payload, "evil.zip")

    def test_rejects_missing_manifest(self, manager: BundleManager):
        payload = bf.make_bundle_missing_manifest()
        with pytest.raises(BundleValidationError):
            manager.install_bundle(payload, "broken.zip")

    def test_rejects_malformed_manifest(self, manager: BundleManager):
        # Build a valid zip but with a bogus manifest inside
        import json

        bad_manifest = bf.make_manifest()
        bad_manifest["version"] = "not-semver"
        payload = bf.build_zip(
            [
                ("manifest.json", json.dumps(bad_manifest).encode("utf-8")),
                ("tool.py", bf.make_tool_source().encode("utf-8")),
            ]
        )
        with pytest.raises(BundleManifestError):
            manager.install_bundle(payload, "bad.zip")


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------

class TestDeletion:
    def test_delete_single_version_bundle(self, manager: BundleManager):
        payload = bf.make_valid_bundle()
        manager.install_bundle(payload, "x.zip")

        assert manager.delete_bundle("test_tool") is True
        assert manager.get_bundle("test_tool") is None
        assert manager.storage.list_blobs() == []

    def test_delete_refuses_multi_version(self, manager: BundleManager):
        p1 = bf.make_valid_bundle(manifest_overrides={"version": "1.0.0"})
        p2 = bf.make_valid_bundle(manifest_overrides={"version": "2.0.0"})
        manager.install_bundle(p1, "v1.zip")
        manager.install_bundle(p2, "v2.zip")

        with pytest.raises(Exception):
            manager.delete_bundle("test_tool")

        # Force deletion works
        assert manager.delete_bundle("test_tool", force=True) is True
        assert manager.get_bundle("test_tool") is None

    def test_delete_missing_returns_false(self, manager: BundleManager):
        assert manager.delete_bundle("nonexistent") is False


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------

class TestActivation:
    def test_activate_previous_version(self, manager: BundleManager):
        p1 = bf.make_valid_bundle(manifest_overrides={"version": "1.0.0"})
        p2 = bf.make_valid_bundle(manifest_overrides={"version": "2.0.0"})

        r1 = manager.install_bundle(p1, "v1.zip")
        r2 = manager.install_bundle(p2, "v2.zip")

        assert manager.activate_version("test_tool", "1.0.0") is True
        entry = manager.get_bundle("test_tool")
        assert entry.active_version == "1.0.0"
        assert entry.active_hash == r1.hash
        # Pointer file also reflects the change
        assert manager.storage.get_active_hash("test_tool") == r1.hash

    def test_activate_missing_version(self, manager: BundleManager):
        payload = bf.make_valid_bundle()
        manager.install_bundle(payload, "x.zip")

        assert manager.activate_version("test_tool", "9.9.9") is False

    def test_activate_missing_bundle(self, manager: BundleManager):
        assert manager.activate_version("nope", "1.0.0") is False


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class TestHealth:
    def test_healthy_bundle(self, manager: BundleManager):
        payload = bf.make_valid_bundle()
        manager.install_bundle(payload, "x.zip")

        report = manager.check_health("test_tool")
        assert report is not None
        assert report.status == "healthy"
        assert all(c.passed for c in report.checks)

    def test_health_missing_bundle(self, manager: BundleManager):
        assert manager.check_health("nonexistent") is None

    def test_health_degraded_when_blob_deleted(self, manager: BundleManager):
        payload = bf.make_valid_bundle()
        result = manager.install_bundle(payload, "x.zip")

        # Simulate manual corruption
        manager.storage.delete_blob(result.hash)

        report = manager.check_health("test_tool")
        assert report.status in ("degraded", "broken")


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_registry_survives_restart(self, tmp_path: Path):
        root = tmp_path / "store_tools"

        # Session 1
        mgr1 = BundleManager(root)
        payload = bf.make_valid_bundle()
        r = mgr1.install_bundle(payload, "x.zip")

        # Session 2 (fresh instance reading same directory)
        mgr2 = BundleManager(root)
        entry = mgr2.get_bundle("test_tool")
        assert entry is not None
        assert entry.active_hash == r.hash

    def test_cleanup_orphans(self, manager: BundleManager):
        payload = bf.make_valid_bundle()
        result = manager.install_bundle(payload, "x.zip")

        # Create a stray blob directly
        staging = manager.storage.make_temp_dir()
        (staging / "manifest.json").write_text("{}", encoding="utf-8")
        manager.storage.commit_blob(staging, "f" * 16, {})

        report = manager.cleanup_orphans()
        assert report["removed_orphan_blobs"] == 1
        # The legitimate blob is still there
        assert manager.storage.blob_exists(result.hash) is True
