# backend/omweb/extensions/registry.py
"""
Centralized Extension and MCP Server Registry for PELDRUN.

Responsibilities:
  * Discover and manage built-in and user-installed extension manifests.
  * Bridge MCP server configurations with McpSettingsManager.
  * Provide effective server specifications merged with validated settings and env.
  * Guarantee zero corruption on manifest mutation via atomic writes.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from omweb.extensions.settings_manager import mcp_settings_manager

logger = logging.getLogger("omweb.extensions.registry")


class ExtensionRegistry:
    """Registry maintaining extension declarations and dedicated MCP server lifecycles."""

    def __init__(self) -> None:
        self.builtin_dir = Path(__file__).resolve().parent / "builtins"
        self.builtin_dir.mkdir(parents=True, exist_ok=True)

        backend_dir = Path(__file__).resolve().parent.parent.parent
        self.custom_dir = backend_dir / "storage" / "store" / "extensions"
        self.custom_dir.mkdir(parents=True, exist_ok=True)

        legacy_file = backend_dir / "storage" / "extensions.json"
        if legacy_file.exists():
            self._migrate_legacy_extensions(legacy_file)

    def _migrate_legacy_extensions(self, legacy_file: Path) -> None:
        """Migrate legacy monolithic extensions.json into per-server JSON manifests."""
        try:
            data = json.loads(legacy_file.read_text(encoding="utf-8"))
            items = data if isinstance(data, list) else data.get("extensions", [])
            for item in items:
                ext_id = item.get("id")
                if ext_id and not (self.custom_dir / f"{ext_id}.json").exists():
                    target = self.custom_dir / f"{ext_id}.json"
                    self._atomic_write_json(target, item)
        except Exception as exc:
            logger.warning("Failed to migrate legacy extensions file: %s", exc)

    def _atomic_write_json(self, target_path: Path, payload: Dict[str, Any]) -> None:
        """Write JSON payload to disk atomically via temporary file replacement."""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp_path, target_path)
        except Exception as exc:
            if tmp_path.exists():
                tmp_path.unlink()
            logger.error("Atomic write failed for '%s': %s", target_path, exc)
            raise

    def _load_ext_from_file(self, file_path: Path, is_builtin: bool) -> Optional[Dict[str, Any]]:
        """Load and sanitize extension manifest from disk."""
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            data.setdefault("id", file_path.stem)
            data["is_builtin"] = is_builtin
            return data
        except Exception as exc:
            logger.debug("Could not parse extension manifest from '%s': %s", file_path, exc)
            return None

    def list_extensions(self) -> List[Dict[str, Any]]:
        """Return all registered extensions and tool servers (builtins + custom)."""
        results: Dict[str, Dict[str, Any]] = {}
        if self.builtin_dir.exists():
            for p in sorted(self.builtin_dir.glob("*.json")):
                ext = self._load_ext_from_file(p, is_builtin=True)
                if ext and "id" in ext:
                    results[ext["id"]] = ext

        if self.custom_dir.exists():
            for p in sorted(self.custom_dir.glob("*.json")):
                ext = self._load_ext_from_file(p, is_builtin=False)
                if ext and "id" in ext:
                    results[ext["id"]] = ext

        return list(results.values())

    def list_mcp_servers(self) -> List[Dict[str, Any]]:
        """List all extensions that adhere to the MCP protocol specification."""
        return [
            e for e in self.list_extensions()
            if e.get("type") in ("mcp_server", "mcp")
        ]

    def get_extension(self, ext_id: str) -> Optional[Dict[str, Any]]:
        """Fetch raw extension manifest by identifier."""
        custom_file = self.custom_dir / f"{ext_id}.json"
        if custom_file.exists():
            return self._load_ext_from_file(custom_file, is_builtin=False)

        builtin_file = self.builtin_dir / f"{ext_id}.json"
        if builtin_file.exists():
            return self._load_ext_from_file(builtin_file, is_builtin=True)

        return None

    def get_effective_server(self, server_id: str) -> Optional[Dict[str, Any]]:
        """
        Return the complete manifest enriched with persisted settings and env values.
        """
        ext = self.get_extension(server_id)
        if not ext:
            return None

        runtime_config = mcp_settings_manager.get_effective_runtime_config(server_id, ext)
        effective = dict(ext)
        effective["effective_settings"] = runtime_config.get("settings", {})
        effective["effective_env"] = runtime_config.get("env", {})
        return effective

    def toggle_status(self, ext_id: str) -> Dict[str, Any]:
        """Toggle an extension or server status between active and ready."""
        ext = self.get_extension(ext_id)
        if not ext:
            return {"error": f"Extension '{ext_id}' not found"}

        new_status = "ready" if ext.get("status") == "active" else "active"
        ext["status"] = new_status
        ext["is_builtin"] = False

        target_file = self.custom_dir / f"{ext_id}.json"
        self._atomic_write_json(target_file, ext)
        return ext

    def get_active_mcp_servers(self) -> List[Dict[str, Any]]:
        """Retrieve all currently activated MCP protocol servers."""
        return [
            e for e in self.list_mcp_servers()
            if e.get("status") == "active"
        ]

    def save_extension(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Persist or update an extension manifest on disk atomically."""
        ext_id = data.get("id")
        if not ext_id:
            raise ValueError("Extension id is required")

        target_file = self.custom_dir / f"{ext_id}.json"
        self._atomic_write_json(target_file, data)
        return data

    def delete_extension(self, ext_id: str) -> bool:
        """Remove a custom extension manifest from disk."""
        custom_file = self.custom_dir / f"{ext_id}.json"
        if custom_file.exists():
            custom_file.unlink()
            return True
        return False

    # ─────────────────────────────────────────────────────────────
    # MCP Settings & Environment Helpers
    # ─────────────────────────────────────────────────────────────

    def get_server_settings(self, server_id: str) -> Dict[str, Any]:
        """Fetch effective user settings for a server."""
        ext = self.get_extension(server_id)
        schema = ext.get("settings_schema") if ext else None
        return mcp_settings_manager.get_settings(server_id, schema)

    def save_server_settings(self, server_id: str, values: Dict[str, Any]) -> Dict[str, Any]:
        """Validate and persist user settings for a server."""
        ext = self.get_extension(server_id)
        if not ext:
            raise ValueError(f"MCP server '{server_id}' not found.")
        schema = ext.get("settings_schema")
        return mcp_settings_manager.save_settings(server_id, values, schema)

    def get_server_env(self, server_id: str) -> Dict[str, Any]:
        """Fetch effective environment variables for a server."""
        ext = self.get_extension(server_id)
        schema = ext.get("env_schema") if ext else None
        return mcp_settings_manager.get_env(server_id, schema)

    def save_server_env(self, server_id: str, values: Dict[str, Any]) -> Dict[str, Any]:
        """Validate and persist environment variables for a server."""
        ext = self.get_extension(server_id)
        if not ext:
            raise ValueError(f"MCP server '{server_id}' not found.")
        schema = ext.get("env_schema")
        return mcp_settings_manager.save_env(server_id, values, schema)


extension_registry = ExtensionRegistry()

__all__ = ["ExtensionRegistry", "extension_registry"]
