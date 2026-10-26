# backend/omweb/extensions/settings_manager.py
"""
Centralized Model Context Protocol (MCP) Server Settings Manager.

Responsibilities:
  * Maintain persistent user configuration and environment variables for MCP servers.
  * Validate user inputs against server-defined settings_schema and env_schema.
  * Merge schema default properties with user-specified values.
  * Guarantee zero corruption via atomic writes (.tmp + os.replace).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from jsonschema import validate, ValidationError
    _JSONSCHEMA_AVAILABLE = True
except ImportError:
    _JSONSCHEMA_AVAILABLE = False

logger = logging.getLogger("omweb.extensions.settings_manager")


class McpSettingsManager:
    """Manages persistent settings and environment configurations for MCP servers."""

    def __init__(self, backend_storage_dir: Optional[Path] = None) -> None:
        if backend_storage_dir is not None:
            self.storage_dir = backend_storage_dir
        else:
            backend_dir = Path(__file__).resolve().parent.parent.parent
            self.storage_dir = backend_dir / "storage"

        self.settings_dir = self.storage_dir / "store" / "extensions" / "settings"
        self.settings_dir.mkdir(parents=True, exist_ok=True)

    def _get_server_file_path(self, server_id: str) -> Path:
        """Resolve the atomic storage file path for a given MCP server identifier."""
        safe_id = "".join(c for c in server_id if c.isalnum() or c in ("-", "_"))
        return self.settings_dir / f"{safe_id}.json"

    def _read_server_payload(self, server_id: str) -> Dict[str, Any]:
        """Read the raw persisted configuration container for the server."""
        path = self._get_server_file_path(server_id)
        if not path.exists():
            return {"settings": {}, "env": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {
                    "settings": data.get("settings", {}) if isinstance(data.get("settings"), dict) else {},
                    "env": data.get("env", {}) if isinstance(data.get("env"), dict) else {},
                }
        except Exception as exc:
            logger.warning("Failed to parse settings container for MCP server '%s': %s", server_id, exc)
        return {"settings": {}, "env": {}}

    def _write_server_payload(self, server_id: str, payload: Dict[str, Any]) -> None:
        """Write configuration container to disk atomically using os.replace."""
        path = self._get_server_file_path(server_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")

        try:
            tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp_path, path)
        except Exception as exc:
            if tmp_path.exists():
                tmp_path.unlink()
            logger.error("Failed to atomically save configuration for MCP server '%s': %s", server_id, exc)
            raise

    def _extract_defaults(self, schema: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Extract default values declared within a JSON schema properties block."""
        if not schema or not isinstance(schema, dict):
            return {}
        defaults: Dict[str, Any] = {}
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for prop_key, prop_spec in properties.items():
                if isinstance(prop_spec, dict) and "default" in prop_spec:
                    defaults[prop_key] = prop_spec["default"]
        return defaults

    def get_settings(
        self,
        server_id: str,
        schema: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Return the effective user settings for an MCP server merged with schema defaults.
        """
        defaults = self._extract_defaults(schema)
        container = self._read_server_payload(server_id)
        user_values = container.get("settings", {})
        return {**defaults, **user_values}

    def save_settings(
        self,
        server_id: str,
        values: Dict[str, Any],
        schema: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Validate and atomically persist user settings for an MCP server.
        """
        if schema and _JSONSCHEMA_AVAILABLE:
            try:
                validate(instance=values, schema=schema)
            except ValidationError as exc:
                logger.warning("Settings validation rejected for MCP server '%s': %s", server_id, exc)
                raise ValueError(f"Invalid settings: {exc.message}") from exc

        container = self._read_server_payload(server_id)
        container["settings"] = values
        self._write_server_payload(server_id, container)
        logger.info("Successfully updated settings for MCP server '%s'", server_id)
        return values

    def get_env(
        self,
        server_id: str,
        schema: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Return the effective environment variables for an MCP server merged with schema defaults.
        """
        defaults = self._extract_defaults(schema)
        container = self._read_server_payload(server_id)
        user_env = container.get("env", {})
        return {**defaults, **user_env}

    def save_env(
        self,
        server_id: str,
        values: Dict[str, Any],
        schema: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Validate and atomically persist environment variables for an MCP server.
        """
        if schema and _JSONSCHEMA_AVAILABLE:
            try:
                validate(instance=values, schema=schema)
            except ValidationError as exc:
                logger.warning("Environment validation rejected for MCP server '%s': %s", server_id, exc)
                raise ValueError(f"Invalid environment variables: {exc.message}") from exc

        container = self._read_server_payload(server_id)
        container["env"] = values
        self._write_server_payload(server_id, container)
        logger.info("Successfully updated environment variables for MCP server '%s'", server_id)
        return values

    def get_effective_runtime_config(
        self,
        server_id: str,
        manifest: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Construct a comprehensive runtime execution context including settings and env.
        """
        settings_schema = manifest.get("settings_schema") if manifest else None
        env_schema = manifest.get("env_schema") if manifest else None

        return {
            "settings": self.get_settings(server_id, settings_schema),
            "env": self.get_env(server_id, env_schema),
        }


mcp_settings_manager = McpSettingsManager()

__all__ = ["McpSettingsManager", "mcp_settings_manager"]
