"""
Phase 3 unit tests: content-addressed storage layer.

Run:
    cd D:\\AI\\peldrun\\backend
    python -m pytest tests/test_bundle_storage.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pytest

from omweb.tools.bundles.storage import (
    ACTIVE_POINTER,
    BLOB_PREFIX,
    HASH_HEX_LENGTH,
    META_FILENAME,
    BundleStorage,
    compute_bundle_hash,
)


# ---------------------------------------------------------------------------
# Hash
# ---------------------------------------------------------------------------

class TestComputeBundleHash:
    def test_deterministic(self):
        assert compute_bundle_hash(b"hello") == compute_bundle_hash(b"hello")

    def test_different_for_different_input(self):
        assert compute_bundle_hash(b"a") != compute_bundle_hash(b"b")

    def test_correct_length(self):
        assert len(compute_bundle_hash(b"hello")) == HASH_HEX_LENGTH

    def test_hex_encoding(self):
        h = compute_bundle_hash(b"test")
        assert all(c in "0123456789abcdef" for c in h)

    def test_empty_input_ok(self):
        # Empty input still produces a valid hex string
        h = compute_bundle_hash(b"")
        assert len(h) == HASH_HEX_LENGTH


# ---------------------------------------------------------------------------
# Storage — temp dirs
# ---------------------------------------------------------------------------

class TestStorageTempDirs:
    def test_make_temp_dir_unique(self, tmp_path: Path):
        storage = BundleStorage(tmp_path)
        d1 = storage.make_temp_dir()
        d2 = storage.make_temp_dir()
        assert d1 != d2
        assert d1.is_dir() and d2.is_dir()
        # Neither appears in list_blobs
        assert storage.list_blobs() == []

    def test_make_temp_dir_prefix(self, tmp_path: Path):
        storage = BundleStorage(tmp_path)
        d = storage.make_temp_dir()
        assert d.name.startswith(".tmp_")


# ---------------------------------------------------------------------------
# Storage — blobs
# ---------------------------------------------------------------------------

class TestStorageBlobs:
    def test_commit_and_read(self, tmp_path: Path):
        storage = BundleStorage(tmp_path)
        staging = storage.make_temp_dir()
        (staging / "manifest.json").write_text("{}", encoding="utf-8")

        meta = {"bundle_id": "test", "version": "1.0.0"}
        blob_path = storage.commit_blob(staging, "abcdef1234567890", meta)

        assert blob_path.is_dir()
        assert (blob_path / "manifest.json").is_file()
        assert (blob_path / META_FILENAME).is_file()
        # Staging must have been moved, not copied
        assert not staging.exists()

    def test_read_meta(self, tmp_path: Path):
        storage = BundleStorage(tmp_path)
        staging = storage.make_temp_dir()
        (staging / "manifest.json").write_text("{}", encoding="utf-8")

        meta = {"bundle_id": "x", "version": "2.0.0", "extra": 42}
        storage.commit_blob(staging, "1111222233334444", meta)

        loaded = storage.read_blob_meta("1111222233334444")
        assert loaded is not None
        assert loaded["bundle_id"] == "x"
        assert loaded["extra"] == 42

    def test_commit_idempotent(self, tmp_path: Path):
        """Committing the same hash twice does not overwrite."""
        storage = BundleStorage(tmp_path)

        staging1 = storage.make_temp_dir()
        (staging1 / "manifest.json").write_text('{"v": 1}', encoding="utf-8")
        storage.commit_blob(staging1, "aaaa111122223333", {"v": 1})

        staging2 = storage.make_temp_dir()
        (staging2 / "manifest.json").write_text('{"v": 2}', encoding="utf-8")
        blob2 = storage.commit_blob(staging2, "aaaa111122223333", {"v": 2})

        # Second call: staging dropped, original blob intact
        assert not staging2.exists()
        content = (blob2 / "manifest.json").read_text(encoding="utf-8")
        assert content == '{"v": 1}'
        loaded = storage.read_blob_meta("aaaa111122223333")
        assert loaded["v"] == 1

    def test_blob_exists(self, tmp_path: Path):
        storage = BundleStorage(tmp_path)
        assert storage.blob_exists("0" * 16) is False

        staging = storage.make_temp_dir()
        (staging / "manifest.json").write_text("{}", encoding="utf-8")
        storage.commit_blob(staging, "0" * 16, {})
        assert storage.blob_exists("0" * 16) is True

    def test_list_blobs(self, tmp_path: Path):
        storage = BundleStorage(tmp_path)
        for h in ("a" * 16, "b" * 16, "c" * 16):
            staging = storage.make_temp_dir()
            (staging / "manifest.json").write_text("{}", encoding="utf-8")
            storage.commit_blob(staging, h, {})

        blobs = storage.list_blobs()
        assert set(blobs) == {"a" * 16, "b" * 16, "c" * 16}

    def test_delete_blob(self, tmp_path: Path):
        storage = BundleStorage(tmp_path)
        staging = storage.make_temp_dir()
        (staging / "manifest.json").write_text("{}", encoding="utf-8")
        storage.commit_blob(staging, "a" * 16, {})

        assert storage.delete_blob("a" * 16) is True
        assert storage.delete_blob("a" * 16) is False  # already gone

    def test_list_orphaned_blobs(self, tmp_path: Path):
        storage = BundleStorage(tmp_path)
        for h in ("a" * 16, "b" * 16, "c" * 16):
            staging = storage.make_temp_dir()
            (staging / "manifest.json").write_text("{}", encoding="utf-8")
            storage.commit_blob(staging, h, {})

        active = {"a" * 16, "c" * 16}
        orphans = storage.list_orphaned_blobs(active)
        assert orphans == ["b" * 16]


# ---------------------------------------------------------------------------
# Storage — install pointers
# ---------------------------------------------------------------------------

class TestStorageInstallPointers:
    def test_set_and_get_active_hash(self, tmp_path: Path):
        storage = BundleStorage(tmp_path)
        storage.set_active_hash("word_count", "a" * 16)
        assert storage.get_active_hash("word_count") == "a" * 16

    def test_get_missing_returns_none(self, tmp_path: Path):
        storage = BundleStorage(tmp_path)
        assert storage.get_active_hash("nope") is None

    def test_pointer_swap(self, tmp_path: Path):
        storage = BundleStorage(tmp_path)
        storage.set_active_hash("wc", "a" * 16)
        storage.set_active_hash("wc", "b" * 16)
        assert storage.get_active_hash("wc") == "b" * 16

    def test_list_installs(self, tmp_path: Path):
        storage = BundleStorage(tmp_path)
        storage.set_active_hash("x", "a" * 16)
        storage.set_active_hash("y", "b" * 16)
        assert set(storage.list_installs()) == {"x", "y"}

    def test_delete_install(self, tmp_path: Path):
        storage = BundleStorage(tmp_path)
        storage.set_active_hash("x", "a" * 16)
        assert storage.delete_install("x") is True
        assert storage.get_active_hash("x") is None


# ---------------------------------------------------------------------------
# Storage — cleanup
# ---------------------------------------------------------------------------

class TestStorageCleanup:
    def test_cleanup_orphan_temp_dirs(self, tmp_path: Path):
        import time

        storage = BundleStorage(tmp_path)

        # Create two temp dirs and backdate one
        old_tmp = storage.make_temp_dir()
        fresh_tmp = storage.make_temp_dir()

        # Backdate the "old" one by 2 hours
        old_time = time.time() - 7200
        import os
        os.utime(str(old_tmp), (old_time, old_time))

        removed = storage.cleanup_orphan_temp_dirs(older_than_seconds=3600)
        assert removed == 1
        assert not old_tmp.exists()
        assert fresh_tmp.exists()

    def test_cleanup_respects_recency(self, tmp_path: Path):
        storage = BundleStorage(tmp_path)
        tmp_dir = storage.make_temp_dir()
        # Too fresh to be cleaned
        removed = storage.cleanup_orphan_temp_dirs(older_than_seconds=3600)
        assert removed == 0
        assert tmp_dir.exists()
