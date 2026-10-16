"""
Phase 4 unit tests: Python loader for bundle blobs.

Run:
    cd D:\\AI\\peldrun\\backend
    python -m pytest tests/test_bundle_loader.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pytest

from omweb.tools.base import SovereignBaseTool
from omweb.tools.bundles import (
    BundleLoadError,
    BundleManager,
    load_tool_from_bundle,
)
from tests.fixtures import bundle_factory as bf


# ---------------------------------------------------------------------------
# Fixture: installed bundle
# ---------------------------------------------------------------------------

@pytest.fixture
def installed_manager(tmp_path: Path) -> BundleManager:
    root = tmp_path / "store_tools"
    manager = BundleManager(root)
    payload = bf.make_valid_bundle()
    manager.install_bundle(payload, "test.zip")
    return manager


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestLoadHappyPath:
    def test_load_by_class_name(self, installed_manager: BundleManager):
        entry = installed_manager.get_bundle("test_tool")
        blob = installed_manager.storage.blob_dir(entry.active_hash)

        instance = load_tool_from_bundle(
            blob_dir=blob,
            entry_point="tool.py",
            class_name="TestTool",
            bundle_id="test_tool",
        )
        assert isinstance(instance, SovereignBaseTool)
        assert instance.name == "test_tool"

    def test_load_auto_class(self, installed_manager: BundleManager):
        entry = installed_manager.get_bundle("test_tool")
        blob = installed_manager.storage.blob_dir(entry.active_hash)

        instance = load_tool_from_bundle(
            blob_dir=blob,
            entry_point="tool.py",
        )
        assert isinstance(instance, SovereignBaseTool)

    def test_workspace_root_bound(self, tmp_path: Path):
        """
        Verify the loader binds the workspace root when the tool declares
        support for one. The factory's default `TestTool` does not declare
        workspace support, so we build a custom bundle here with a tool that
        does — mirroring the contract of real workspace-scoped tools.
        """
        import json as _json

        # Build a bundle whose tool declares a `workspace_root` attribute.
        aware_source = (
            "from omweb.tools.base import SovereignBaseTool\n"
            "\n"
            "class AwareTool(SovereignBaseTool):\n"
            "    name: str = 'aware_tool'\n"
            "    description: str = 'Workspace-aware tool.'\n"
            "    category: str = 'custom'\n"
            "    workspace_root: str = ''\n"
            "\n"
            "    def set_workspace_root(self, workspace_root: str) -> None:\n"
            "        self.workspace_root = workspace_root\n"
            "\n"
            "    async def execute(self, **kwargs):\n"
            "        return {'output': self.workspace_root, 'is_error': False, 'exit_code': 0}\n"
        )
        manifest = bf.make_manifest(
            id="aware_tool",
            name="Aware",
            entry_point="tool.py",
        )
        payload = bf.build_zip([
            ("manifest.json", _json.dumps(manifest).encode("utf-8")),
            ("tool.py", aware_source.encode("utf-8")),
        ])

        manager = BundleManager(tmp_path / "store_aware")
        manager.install_bundle(payload, "aware.zip")
        entry = manager.get_bundle("aware_tool")
        blob = manager.storage.blob_dir(entry.active_hash)

        workspace = tmp_path / "some_workspace"
        workspace.mkdir()

        instance = load_tool_from_bundle(
            blob_dir=blob,
            entry_point="tool.py",
            class_name="AwareTool",
            workspace_root=str(workspace),
            bundle_id="aware_tool",
        )

        # The workspace root must now be bound
        assert getattr(instance, "workspace_root", "") == str(workspace)

    def test_load_with_helpers(self, tmp_path: Path):
        manager = BundleManager(tmp_path / "store")
        payload = bf.build_zip([
            ("manifest.json", __import__("json").dumps(bf.make_manifest(
                id="with_helpers", name="Helpers"
            )).encode("utf-8")),
            ("tool.py", bf.make_tool_source().encode("utf-8")),
            ("helpers/__init__.py", b""),
            ("helpers/util.py", b"HELPER_CONSTANT = 42\n"),
        ])
        manager.install_bundle(payload, "helpers.zip")
        entry = manager.get_bundle("with_helpers")
        blob = manager.storage.blob_dir(entry.active_hash)

        # Should load without error even with helpers on sys.path
        instance = load_tool_from_bundle(
            blob_dir=blob,
            entry_point="tool.py",
            bundle_id="with_helpers",
        )
        assert isinstance(instance, SovereignBaseTool)


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------

class TestLoadFailures:
    def test_missing_blob_dir(self, tmp_path: Path):
        with pytest.raises(BundleLoadError):
            load_tool_from_bundle(
                blob_dir=tmp_path / "nonexistent",
                entry_point="tool.py",
            )

    def test_missing_entry_point(self, tmp_path: Path):
        blob = tmp_path / "blob"
        blob.mkdir()
        with pytest.raises(BundleLoadError):
            load_tool_from_bundle(blob_dir=blob, entry_point="tool.py")

    def test_entry_point_escapes_blob(self, tmp_path: Path):
        blob = tmp_path / "blob"
        blob.mkdir()
        (blob / "tool.py").write_text("x = 1", encoding="utf-8")

        with pytest.raises(BundleLoadError):
            load_tool_from_bundle(
                blob_dir=blob,
                entry_point="../tool.py",
            )

    def test_missing_explicit_class_name(self, tmp_path: Path):
        blob = tmp_path / "blob"
        blob.mkdir()
        (blob / "tool.py").write_text(bf.make_tool_source("RealTool"), encoding="utf-8")

        with pytest.raises(BundleLoadError):
            load_tool_from_bundle(
                blob_dir=blob,
                entry_point="tool.py",
                class_name="MissingTool",
            )

    def test_no_subclass_in_module(self, tmp_path: Path):
        blob = tmp_path / "blob"
        blob.mkdir()
        (blob / "tool.py").write_text("x = 1\n", encoding="utf-8")

        with pytest.raises(BundleLoadError):
            load_tool_from_bundle(blob_dir=blob, entry_point="tool.py")

    def test_import_error_raises_bundle_load_error(self, tmp_path: Path):
        blob = tmp_path / "blob"
        blob.mkdir()
        (blob / "tool.py").write_text(
            "import this_module_does_not_exist_at_all\n", encoding="utf-8"
        )

        with pytest.raises(BundleLoadError):
            load_tool_from_bundle(blob_dir=blob, entry_point="tool.py")


# ---------------------------------------------------------------------------
# sys.path hygiene
# ---------------------------------------------------------------------------

class TestSysPathHygiene:
    def test_sys_path_restored_after_load(self, installed_manager: BundleManager):
        entry = installed_manager.get_bundle("test_tool")
        blob = installed_manager.storage.blob_dir(entry.active_hash)

        before = list(sys.path)
        load_tool_from_bundle(
            blob_dir=blob,
            entry_point="tool.py",
            class_name="TestTool",
        )
        after = list(sys.path)

        # The blob path must NOT remain on sys.path
        assert str(blob) not in after
        assert len(after) == len(before) or str(blob) not in after

    def test_sys_path_restored_after_failed_load(self, tmp_path: Path):
        blob = tmp_path / "blob"
        blob.mkdir()
        (blob / "tool.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")

        before = list(sys.path)
        with pytest.raises(BundleLoadError):
            load_tool_from_bundle(blob_dir=blob, entry_point="tool.py")
        after = list(sys.path)

        assert str(blob) not in after
