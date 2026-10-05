"""
Sovereign Tool Registry for PELDRUN Web Platform.
Handles discovery, runtime upload validation, hot-reloading, collision protection,
and executable instantiation for custom and built-in tools.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from omweb.tools.base import SovereignBaseTool

RESERVED_BUILTIN_IDS: Set[str] = {
    "bash",
    "python_execute",
    "str_replace_editor",
    "web_search",
    "browser_use",
    "ask_human",
    "terminate",
    "file_saver",
}


def sanitize_tool_identifier(raw_name: str) -> str:
    """Sanitizes tool names into valid function-calling identifiers matching ^[a-zA-Z0-9_-]{1,64}$."""
    clean = re.sub(r"[^a-zA-Z0-9_-]", "_", raw_name.strip())
    clean = re.sub(r"_+", "_", clean).strip("_")
    if not clean:
        clean = "custom_tool"
    return clean[:64]


class ToolRegistry:
    """
    Manages metadata discovery, collision safety, and dynamic loading of tools.
    """

    def __init__(self) -> None:
        backend_dir = Path(__file__).resolve().parent.parent.parent
        self.builtins_dir = Path(__file__).resolve().parent / "builtins"
        self.custom_tools_dir = backend_dir / "storage" / "store" / "tools" / "custom"
        self.custom_tools_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = backend_dir / "storage" / "store" / "tools" / "tools_state.json"
        self._builtin_tools: Dict[str, Dict[str, Any]] = self._init_builtin_tools()
        self._loaded_custom_instances: Dict[str, SovereignBaseTool] = {}

    def _init_builtin_tools(self) -> Dict[str, Dict[str, Any]]:
        """Loads built-in tool definitions from json manifests in builtins folder."""
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
                    print(f"[TOOL REGISTRY WARNING] Failed to parse builtin manifest {json_file.name}: {e}")

        if not tools:
            fallback_ids = [
                "python_execute", "bash", "str_replace_editor",
                "web_search", "browser_use", "ask_human", "file_saver"
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
                    "parameters": {"type": "object", "properties": {}, "required": []}
                }
        return tools

    def _load_states(self) -> Dict[str, bool]:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_states(self, states: Dict[str, bool]) -> None:
        try:
            self.state_file.write_text(json.dumps(states, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            print(f"[TOOL REGISTRY ERROR] Could not save tool states: {e}")

    def load_custom_tool_class(self, file_path: Path) -> Optional[SovereignBaseTool]:
        """Dynamically imports a user-uploaded python tool and instantiates its SovereignBaseTool class."""
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
                    instance = attr()
                    return instance

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

    def list_tools(self) -> List[Dict[str, Any]]:
        """Discovers all built-in tools and custom uploaded tools with collision prevention."""
        states = self._load_states()
        results: Dict[str, Dict[str, Any]] = {}

        # 1. Built-in tools
        for tid, tmeta in self._builtin_tools.items():
            copied = dict(tmeta)
            copied["is_enabled"] = states.get(tid, copied.get("is_enabled", True))
            copied["status"] = "active" if copied["is_enabled"] else "disabled"
            results[tid] = copied

        # 2. Custom tools from filesystem
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
                        "file_path": str(p.resolve())
                    }

        return list(results.values())

    def get_custom_tool_instance(self, tool_id: str) -> Optional[SovereignBaseTool]:
        """Returns the instantiated custom tool object if registered."""
        if tool_id in self._loaded_custom_instances:
            return self._loaded_custom_instances[tool_id]

        p = self.custom_tools_dir / f"{tool_id}.py"
        if not p.exists() and tool_id.startswith("custom_"):
            p = self.custom_tools_dir / f"{tool_id[7:]}.py"

        if p.exists():
            inst = self.load_custom_tool_class(p)
            if inst:
                self._loaded_custom_instances[tool_id] = inst
                return inst
        return None

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
        """
        Saves user-uploaded tool file with collision protection.
        Prevents overwriting reserved system tool identifiers.
        """
        raw_name = Path(filename).stem
        ext = Path(filename).suffix.lower()

        # Enforce Collision Guard
        if raw_name in RESERVED_BUILTIN_IDS:
            safe_name = f"custom_{raw_name}"
        else:
            safe_name = sanitize_tool_identifier(raw_name)

        final_filename = f"{safe_name}{ext}"
        target_path = self.custom_tools_dir / final_filename

        # If a duplicate exists with same name, increment suffix cleanly
        counter = 1
        while target_path.exists():
            existing_content = target_path.read_bytes()
            if existing_content == content:
                # Same content update
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
                "is_builtin": False
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