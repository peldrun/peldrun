"""
Sovereign Tool Registry for PELDRUN Web Platform.

Handles discovery, runtime upload validation, hot-reloading, collision
protection, and executable instantiation for custom and built-in tools.

Phase 4 extension:
  * Optional `BundleManager` integration. When a manager is attached via
    `set_bundle_manager()`, the registry transparently dispatches tool
    resolution to installed bundles. Legacy single-file tools continue to
    work unchanged.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, TYPE_CHECKING

from omweb.tools.base import SovereignBaseTool

if TYPE_CHECKING:  # pragma: no cover — avoid hard import cycle
    from omweb.tools.bundles.manager import BundleManager

logger = logging.getLogger("omweb.tools.registry")


RESERVED_BUILTIN_IDS: Set[str] = {
    "bash",
    "python_execute",
    "str_replace_editor",
    "web_search",
    "http_fetch",
    "ask_human",
    "terminate",
    "file_saver",
    "file_rename",
}


def sanitize_tool_identifier(raw_name: str) -> str:
    """Sanitize a tool name into a valid function-calling identifier."""
    clean = re.sub(r"[^a-zA-Z0-9_-]", "_", raw_name.strip())
    clean = re.sub(r"_+", "_", clean).strip("_")
    if not clean:
        clean = "custom_tool"
    return clean[:64]


class ToolRegistry:
    """
    Manages metadata discovery, collision safety, and dynamic loading of tools.

    Resolution order for `get_custom_tool_instance(tool_id)`:
      1. In-memory cache of previously loaded instances.
      2. `custom_tools_dir` — legacy single-file `.py` tools.
      3. `builtins_dir` — platform Python tools shipped with the app.
      4. BundleManager (if attached) — installed bundles.

    Bundles take lowest priority so that a legacy install can still
    override a bundle of the same id, matching historical behavior.
    """

    def __init__(self) -> None:
        backend_dir = Path(__file__).resolve().parent.parent.parent
        self.builtins_dir = Path(__file__).resolve().parent / "builtins"
        self.custom_tools_dir = backend_dir / "storage" / "store" / "tools" / "custom"
        self.custom_tools_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = backend_dir / "storage" / "store" / "tools" / "tools_state.json"
        self._builtin_tools: Dict[str, Dict[str, Any]] = self._init_builtin_tools()
        self._loaded_custom_instances: Dict[str, SovereignBaseTool] = {}
        self._bundle_manager: Optional["BundleManager"] = None

    # ── Bundle manager wiring ──────────────────────────────────────────────

    def set_bundle_manager(self, manager: "BundleManager") -> None:
        """Attach a BundleManager instance. Called during application startup."""
        self._bundle_manager = manager
        logger.info("BundleManager attached to ToolRegistry.")

    def get_bundle_manager(self) -> Optional["BundleManager"]:
        return self._bundle_manager

    # ── Builtin discovery ──────────────────────────────────────────────────

    def _init_builtin_tools(self) -> Dict[str, Dict[str, Any]]:
        """Load built-in tool definitions from JSON manifests in builtins/."""
        tools: Dict[str, Dict[str, Any]] = {}
        if self.builtins_dir.exists():
            for json_file in self.builtins_dir.glob("*.json"):
                try:
                    data = json.loads(json_file.read_text(encoding="utf-8"))
                    tid = data.get("id") or json_file.stem
                    data["id"] = tid
                    data["is_builtin"] = True
                    data.setdefault("is_enabled", True)
                    tools[tid] = data
                except Exception as e:
                    print(
                        f"[TOOL REGISTRY WARNING] Failed to parse builtin "
                        f"manifest {json_file.name}: {e}"
                    )

        if not tools:
            fallback_ids = [
                "python_execute", "bash", "str_replace_editor",
                "web_search", "http_fetch", "ask_human", "file_saver",
            ]
            for fid in fallback_ids:
                tools[fid] = {
                    "id": fid,
                    "name": fid.replace("_", " ").title(),
                    "description": f"Native builtin tool '{fid}'.",
                    "category": "system",
                    "safety_level": "safe",
                    "is_builtin": True,
                    "is_enabled": True,
                    "parameters": {"type": "object", "properties": {}, "required": []},
                }
        return tools

    # ── State I/O ──────────────────────────────────────────────────────────

    def _load_states(self) -> Dict[str, bool]:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_states(self, states: Dict[str, bool]) -> None:
        try:
            self.state_file.write_text(
                json.dumps(states, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[TOOL REGISTRY ERROR] Could not save tool states: {e}")

    # ── Legacy custom tool loading ─────────────────────────────────────────

    def load_custom_tool_class(self, file_path: Path) -> Optional[SovereignBaseTool]:
        """Import a single-file Python tool and instantiate its class."""
        try:
            module_name = f"custom_tool_{file_path.stem}"
            spec = importlib.util.spec_from_file_location(module_name, str(file_path))
            if not spec or not spec.loader:
                return None

            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)

            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (
                    inspect.isclass(attr)
                    and issubclass(attr, SovereignBaseTool)
                    and attr is not SovereignBaseTool
                ):
                    return attr()

            for attr_name in ("execute", "run", "handler"):
                fn = getattr(module, attr_name, None)
                if fn and callable(fn):
                    class DynamicTool(SovereignBaseTool):
                        name: str = file_path.stem
                        description: str = f"Dynamically loaded script '{file_path.name}'"
                        parameters: Dict[str, Any] = {"type": "object", "properties": {}}

                        async def execute(self, **kwargs: Any) -> Any:
                            if inspect.iscoroutinefunction(fn):
                                return await fn(**kwargs)
                            return fn(**kwargs)

                    return DynamicTool()

        except Exception as ex:
            print(f"[TOOL REGISTRY ERROR] Could not load custom tool from {file_path.name}: {ex}")
        return None

    # ── Metadata listing ───────────────────────────────────────────────────

    def list_tools(self) -> List[Dict[str, Any]]:
        """
        Discover built-in, legacy custom, and bundle-installed tools.

        Bundle tools are appended after built-in and legacy custom tools so
        that existing behavior is fully preserved when bundles are empty.
        """
        states = self._load_states()
        results: Dict[str, Dict[str, Any]] = {}

        # 1. Built-ins
        for tid, tmeta in self._builtin_tools.items():
            copied = dict(tmeta)
            copied["is_enabled"] = states.get(tid, copied.get("is_enabled", True))
            copied["status"] = "active" if copied["is_enabled"] else "disabled"
            results[tid] = copied

        # 2. Legacy custom tools
        if self.custom_tools_dir.exists():
            for p in self.custom_tools_dir.glob("*.*"):
                raw_id = p.stem
                if raw_id in RESERVED_BUILTIN_IDS:
                    safe_id = f"custom_{raw_id}"
                else:
                    safe_id = sanitize_tool_identifier(raw_id)

                if p.suffix.lower() == ".json":
                    try:
                        data = json.loads(p.read_text(encoding="utf-8"))
                        data["id"] = safe_id
                        data["is_builtin"] = False
                        data["is_enabled"] = states.get(safe_id, data.get("is_enabled", True))
                        data["status"] = "active" if data["is_enabled"] else "disabled"
                        data.setdefault("category", "custom")
                        data.setdefault("safety_level", "custom")
                        results[safe_id] = data
                    except Exception:
                        continue
                elif p.suffix.lower() == ".py":
                    if safe_id not in self._loaded_custom_instances:
                        inst = self.load_custom_tool_class(p)
                        if inst:
                            self._loaded_custom_instances[safe_id] = inst

                    inst = self._loaded_custom_instances.get(safe_id)
                    t_desc = inst.description if inst else f"Custom script '{p.name}'"
                    t_params = inst.parameters if inst else {"type": "object", "properties": {}}

                    results[safe_id] = {
                        "id": safe_id,
                        "name": safe_id.replace("_", " ").title(),
                        "description": t_desc,
                        "category": "custom",
                        "safety_level": "custom",
                        "is_builtin": False,
                        "is_enabled": states.get(safe_id, True),
                        "status": "active" if states.get(safe_id, True) else "disabled",
                        "parameters": t_params,
                        "file_path": str(p.resolve()),
                        "source": "custom_file",
                    }

        # 3. Bundle-installed tools
        if self._bundle_manager is not None:
            for entry in self._bundle_manager.list_bundles():
                # Do not shadow a legacy install with the same id
                if entry.id in results:
                    continue
                results[entry.id] = {
                    "id": entry.id,
                    "name": entry.name,
                    "description": entry.description,
                    "category": "bundle",
                    "safety_level": entry.safety_level,
                    "is_builtin": False,
                    "is_enabled": entry.status == "active",
                    "status": entry.status,
                    "version": entry.active_version,
                    "hash": entry.active_hash,
                    "capabilities": list(entry.capabilities),
                    "tags": list(entry.tags),
                    "parameters": {"type": "object", "properties": {}, "required": []},
                    "source": "bundle",
                }

        return list(results.values())

    # ── Metadata lookup ────────────────────────────────────────────────────

    def get_tool(self, tool_id: str) -> Optional[Dict[str, Any]]:
        """
        Return metadata for a registered tool (built-in, legacy, or bundle).
        """
        for t in self.list_tools():
            if isinstance(t, dict) and t.get("id") == tool_id:
                return t
        return None

    # ── Instance resolution ────────────────────────────────────────────────

    def get_custom_tool_instance(
        self,
        tool_id: str,
        workspace_root: Optional[Any] = None,
    ) -> Optional[SovereignBaseTool]:
        """
        Resolve an instantiated tool by id.

        Search order:
          1. In-memory cache (with workspace rebinding).
          2. Legacy `custom_tools_dir` files.
          3. Platform `builtins_dir` files.
          4. BundleManager (installed bundles).

        For bundles, the loader is called with the workspace root so the
        instantiated tool has the correct working directory bound.
        """
        # 1. Cached instance
        if tool_id in self._loaded_custom_instances:
            cached = self._loaded_custom_instances[tool_id]
            if workspace_root is not None:
                self._apply_workspace_root(cached, workspace_root)
            return cached

        # 2 & 3. Legacy file-based lookup
        candidate_paths: List[Path] = [self.custom_tools_dir / f"{tool_id}.py"]
        if tool_id.startswith("custom_"):
            candidate_paths.append(self.custom_tools_dir / f"{tool_id[7:]}.py")
        candidate_paths.append(self.builtins_dir / f"{tool_id}.py")

        for p in candidate_paths:
            if p.exists():
                inst = self.load_custom_tool_class(p)
                if inst:
                    if workspace_root is not None:
                        self._apply_workspace_root(inst, workspace_root)
                    self._loaded_custom_instances[tool_id] = inst
                    return inst

        # 4. Bundle lookup
        if self._bundle_manager is not None:
            entry = self._bundle_manager.get_bundle(tool_id)
            if entry is not None:
                return self._load_from_bundle(entry, workspace_root)

        return None

    def _load_from_bundle(
        self,
        entry: Any,
        workspace_root: Optional[Any],
    ) -> Optional[SovereignBaseTool]:
        """
        Load a tool instance from an installed bundle.

        Any failure is caught and logged; None is returned so the caller can
        continue its fallback chain.
        """
        try:
            from omweb.tools.bundles.loader import load_tool_from_bundle
        except Exception as exc:
            logger.warning("Bundle loader import failed: %s", exc)
            return None

        storage = self._bundle_manager.storage
        blob_dir = storage.blob_dir(entry.active_hash)
        if not blob_dir.is_dir():
            logger.warning(
                "Blob directory missing for bundle %s@%s",
                entry.id,
                entry.active_version,
            )
            return None

        meta = storage.read_blob_meta(entry.active_hash) or {}
        entry_point = meta.get("entry_point") or self._manifest_entry_point(blob_dir)
        if not entry_point:
            logger.warning("No entry_point for bundle %s", entry.id)
            return None

        class_name = meta.get("class_name") or self._manifest_class_name(blob_dir)

        try:
            instance = load_tool_from_bundle(
                blob_dir=blob_dir,
                entry_point=str(entry_point),
                class_name=class_name if isinstance(class_name, str) else None,
                workspace_root=str(workspace_root) if workspace_root else None,
                bundle_id=entry.id,
            )
        except Exception as exc:
            logger.warning(
                "Bundle load failed for %s@%s: %s",
                entry.id,
                entry.active_version,
                exc,
            )
            return None

        self._loaded_custom_instances[tool_id_original := entry.id] = instance
        return instance

    @staticmethod
    def _manifest_entry_point(blob_dir: Path) -> Optional[str]:
        manifest = blob_dir / "manifest.json"
        if not manifest.is_file():
            return None
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            value = data.get("entry_point")
            return value if isinstance(value, str) else None
        except Exception:
            return None

    @staticmethod
    def _manifest_class_name(blob_dir: Path) -> Optional[str]:
        manifest = blob_dir / "manifest.json"
        if not manifest.is_file():
            return None
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            value = data.get("class_name")
            return value if isinstance(value, str) else None
        except Exception:
            return None

    # ── Workspace binding helper ───────────────────────────────────────────

    @staticmethod
    def _apply_workspace_root(instance: SovereignBaseTool, workspace_root: Any) -> None:
        """Best-effort propagation of the workspace root onto a tool instance."""
        root_str = str(workspace_root)
        if hasattr(instance, "set_workspace_root") and callable(instance.set_workspace_root):
            try:
                instance.set_workspace_root(root_str)
                return
            except Exception:
                pass
        if hasattr(instance, "workspace_root"):
            try:
                setattr(instance, "workspace_root", root_str)
            except Exception:
                pass

    # ── Toggle / delete ────────────────────────────────────────────────────

    def toggle_tool_status(self, tool_id: str) -> Dict[str, Any]:
        tools = {t["id"]: t for t in self.list_tools()}
        if tool_id not in tools:
            return {"error": f"Tool '{tool_id}' not found"}

        states = self._load_states()
        current_state = states.get(tool_id, tools[tool_id].get("is_enabled", True))
        new_state = not current_state
        states[tool_id] = new_state
        self._save_states(states)

        tools[tool_id]["is_enabled"] = new_state
        tools[tool_id]["status"] = "active" if new_state else "disabled"
        return tools[tool_id]

    def save_custom_tool_file(self, filename: str, content: bytes) -> Dict[str, Any]:
        """Persist a single-file custom tool (legacy path)."""
        raw_name = Path(filename).stem
        ext = Path(filename).suffix.lower()

        if raw_name in RESERVED_BUILTIN_IDS:
            safe_name = f"custom_{raw_name}"
        else:
            safe_name = sanitize_tool_identifier(raw_name)

        final_filename = f"{safe_name}{ext}"
        target_path = self.custom_tools_dir / final_filename

        counter = 1
        while target_path.exists():
            existing_content = target_path.read_bytes()
            if existing_content == content:
                break
            counter += 1
            final_filename = f"{safe_name}_{counter}{ext}"
            target_path = self.custom_tools_dir / final_filename

        target_path.write_bytes(content)
        tool_id = target_path.stem

        if ext == ".py":
            inst = self.load_custom_tool_class(target_path)
            if inst:
                self._loaded_custom_instances[tool_id] = inst
            return {
                "id": tool_id,
                "name": tool_id.replace("_", " ").title(),
                "description": f"Custom Python tool registered as '{final_filename}'.",
                "category": "custom",
                "status": "active",
                "is_builtin": False,
            }
        elif ext == ".json":
            try:
                data = json.loads(content.decode("utf-8"))
                data["id"] = tool_id
                data.setdefault("name", tool_id)
                data["is_builtin"] = False
                data["status"] = "active"
                return data
            except Exception as e:
                return {"id": tool_id, "error": f"Invalid JSON manifest: {e}"}

        return {"id": tool_id, "status": "active"}

    def delete_custom_tool(self, tool_id: str) -> bool:
        self._loaded_custom_instances.pop(tool_id, None)
        deleted = False
        for p in self.custom_tools_dir.glob(f"{tool_id}.*"):
            try:
                p.unlink()
                deleted = True
            except Exception:
                pass
        return deleted


tool_registry = ToolRegistry()
