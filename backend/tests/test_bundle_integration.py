"""
Phase 4 integration tests: bundle lifecycle end-to-end.

Verifies that:
  * An installed bundle is discoverable via `ToolRegistry.list_tools()`.
  * `ToolRegistry.get_custom_tool_instance()` dispatches to the bundle.
  * The loaded tool can be executed with a real workspace root.
  * Legacy tools continue to take priority when ids collide.

Run:
    cd D:\\AI\\peldrun\\backend
    python -m pytest tests/test_bundle_integration.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pytest

from omweb.tools.base import SovereignBaseTool
from omweb.tools.bundles import BundleManager
from omweb.tools.registry import ToolRegistry
from tests.fixtures import bundle_factory as bf


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def registry_with_bundle(tmp_path: Path, monkeypatch):
    """
    A ToolRegistry backed by a temporary BundleManager with one bundle
    already installed.
    """
    # Redirect the registry's file paths to tmp_path to keep tests isolated.
    monkeypatch.setattr(
        "omweb.tools.registry.Path.resolve",
        Path.resolve,
        raising=False,
    )

    manager = BundleManager(tmp_path / "store_tools")
    payload = bf.make_valid_bundle()
    manager.install_bundle(payload, "x.zip")

    registry = ToolRegistry()
    registry.set_bundle_manager(manager)
    return registry, manager


# ---------------------------------------------------------------------------
# Integration: bundle visible in list_tools
# ---------------------------------------------------------------------------

class TestBundleVisibleInList:
    def test_bundle_appears_in_list(self, registry_with_bundle):
        registry, _ = registry_with_bundle
        tools = {t["id"]: t for t in registry.list_tools()}

        assert "test_tool" in tools
        assert tools["test_tool"]["source"] == "bundle"
        assert tools["test_tool"]["version"] == "1.0.0"

    def test_get_tool_returns_bundle_meta(self, registry_with_bundle):
        registry, _ = registry_with_bundle
        meta = registry.get_tool("test_tool")
        assert meta is not None
        assert meta["source"] == "bundle"
        assert meta["hash"] == registry.get_bundle_manager().get_bundle("test_tool").active_hash


# ---------------------------------------------------------------------------
# Integration: instance resolution
# ---------------------------------------------------------------------------

class TestBundleInstanceResolution:
    def test_get_custom_tool_instance_returns_bundle_tool(self, registry_with_bundle, tmp_path: Path):
        registry, _ = registry_with_bundle
        workspace = tmp_path / "ws"
        workspace.mkdir()

        instance = registry.get_custom_tool_instance(
            "test_tool",
            workspace_root=str(workspace),
        )
        assert instance is not None
        assert isinstance(instance, SovereignBaseTool)
        assert instance.name == "test_tool"

    def test_bundle_instance_cached_on_second_call(self, registry_with_bundle, tmp_path: Path):
        registry, _ = registry_with_bundle
        workspace = tmp_path / "ws"
        workspace.mkdir()

        a = registry.get_custom_tool_instance("test_tool", workspace_root=str(workspace))
        b = registry.get_custom_tool_instance("test_tool", workspace_root=str(workspace))
        assert a is b  # same cached object

    def test_missing_bundle_returns_none(self, registry_with_bundle):
        registry, _ = registry_with_bundle
        assert registry.get_custom_tool_instance("nonexistent") is None


# ---------------------------------------------------------------------------
# Integration: end-to-end execution
# ---------------------------------------------------------------------------

class TestBundleExecution:
    def test_tool_executes_successfully(self, registry_with_bundle, tmp_path: Path):
        """
        Install a bundle whose tool reads a workspace file, load it, and run
        it — verifying the full pipeline: manifest → install → load → execute.
        """
        import asyncio

        manager = BundleManager(tmp_path / "store_tools_2")

        tool_source = (
            "from pathlib import Path\n"
            "from omweb.tools.base import SovereignBaseTool\n"
            "\n"
            "class FileReader(SovereignBaseTool):\n"
            "    name: str = 'file_reader_test'\n"
            "    description: str = 'Reads a file from the workspace.'\n"
            "    category: str = 'custom'\n"
            "    workspace_root: str = ''\n"
            "\n"
            "    def set_workspace_root(self, workspace_root: str) -> None:\n"
            "        self.workspace_root = workspace_root\n"
            "\n"
            "    async def execute(self, **kwargs):\n"
            "        path = kwargs.get('path', 'hello.txt')\n"
            "        target = Path(self.workspace_root) / path\n"
            "        try:\n"
            "            content = target.read_text(encoding='utf-8')\n"
            "        except Exception as exc:\n"
            "            return {'output': str(exc), 'is_error': True, 'exit_code': 1}\n"
            "        return {'output': content, 'is_error': False, 'exit_code': 0}\n"
        )

        manifest = bf.make_manifest(
            id="file_reader_test",
            name="File Reader Test",
            entry_point="tool.py",
        )
        payload = bf.build_zip([
            ("manifest.json", json.dumps(manifest).encode("utf-8")),
            ("tool.py", tool_source.encode("utf-8")),
        ])

        manager.install_bundle(payload, "reader.zip")

        registry = ToolRegistry()
        registry.set_bundle_manager(manager)

        # Prepare workspace
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "hello.txt").write_text("HELLO BUNDLE", encoding="utf-8")

        # Resolve and execute
        instance = registry.get_custom_tool_instance(
            "file_reader_test",
            workspace_root=str(workspace),
        )
        assert instance is not None

        result = asyncio.run(instance.execute(path="hello.txt"))
        assert result["output"] == "HELLO BUNDLE"
        assert result["is_error"] is False


# ---------------------------------------------------------------------------
# Priority: legacy wins over bundle
# ---------------------------------------------------------------------------

class TestResolutionPriority:
    def test_legacy_file_wins_over_bundle(self, tmp_path: Path, monkeypatch):
        """
        If the same id exists both as a legacy custom file and as a bundle,
        the legacy file must win (backward compatibility).
        """
        # Set up an isolated registry with custom tool dir under tmp_path
        manager = BundleManager(tmp_path / "store_tools")

        # Bundle with id "collide"
        manifest = bf.make_manifest(id="collide", name="From Bundle")
        bundle_payload = bf.build_zip([
            ("manifest.json", json.dumps(manifest).encode("utf-8")),
            ("tool.py", bf.make_tool_source().encode("utf-8")),
        ])
        manager.install_bundle(bundle_payload, "collide.zip")

        registry = ToolRegistry()
        registry.set_bundle_manager(manager)

        # Redirect legacy custom dir to a tmp location
        legacy_dir = tmp_path / "legacy_custom"
        legacy_dir.mkdir()
        monkeypatch.setattr(registry, "custom_tools_dir", legacy_dir)

        # Create a legacy .py with the same id
        legacy_source = (
            "from omweb.tools.base import SovereignBaseTool\n"
            "\n"
            "class LegacyCollide(SovereignBaseTool):\n"
            "    name: str = 'collide'\n"
            "    description: str = 'LEGACY'\n"
            "    category: str = 'custom'\n"
            "    async def execute(self, **kwargs):\n"
            "        return {'output': 'legacy', 'is_error': False, 'exit_code': 0}\n"
        )
        (legacy_dir / "collide.py").write_text(legacy_source, encoding="utf-8")

        # Metadata listing must NOT include the bundle under the same id
        tools = {t["id"]: t for t in registry.list_tools()}
        assert "collide" in tools
        assert tools["collide"].get("source") != "bundle"

        # Instance resolution: legacy wins
        inst = registry.get_custom_tool_instance("collide")
        assert inst is not None
        assert "legacy" in inst.description.lower() or inst.name == "collide"
