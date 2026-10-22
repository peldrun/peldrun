# backend/omweb/tools/settings_manager.py
"""
Centralized Tool Settings Manager.

Responsibilities:
  * Resolve storage paths for built-in tools and bundles.
  * Validate user-provided settings against a JSON Schema.
  * Merge defaults with user values.
  * Perform atomic writes to prevent data corruption.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from jsonschema import validate, ValidationError

logger = logging.getLogger("omweb.tools.settings_manager")


class ToolSettingsManager:
    """Manages per-tool user settings with schema validation and atomic writes."""

    def __init__(self, backend_storage_dir: Path) -> None:
        self.storage_dir = backend_storage_dir
        # Path for built-in tool settings (kept separate from tool bundles).
        self.builtin_settings_dir = (
            self.storage_dir / "store" / "tools" / "builtin_settings"
        )
        self.builtin_settings_dir.mkdir(parents=True, exist_ok=True)

    def _get_settings_path(self, tool_id: str, is_bundle: bool) -> Path:
        """Determine the storage path based on the tool type (bundle vs built-in)."""
        if is_bundle:
            # Bundles store their settings in _installs/<id>/settings.json
            return (
                self.storage_dir
                / "store"
                / "tools"
                / "_installs"
                / tool_id
                / "settings.json"
            )
        else:
            # Built-in tools store settings in a dedicated directory.
            return self.builtin_settings_dir / f"{tool_id}.json"

    def get_settings(
        self,
        tool_id: str,
        schema: Optional[Dict[str, Any]],
        is_bundle: bool = False,
    ) -> Dict[str, Any]:
        """
        Return current settings merged with schema defaults.

        If no schema is provided, returns an empty dict (tool is not configurable).
        """
        if not schema:
            return {}

        # 1. Extract default values from the schema.
        defaults: Dict[str, Any] = {}
        for prop, spec in schema.get("properties", {}).items():
            if "default" in spec:
                defaults[prop] = spec["default"]

        # 2. Read user-provided values from disk (if any).
        path = self._get_settings_path(tool_id, is_bundle)
        user_values: Dict[str, Any] = {}
        if path.exists():
            try:
                user_values = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning(
                    "Failed to read settings for %s: %s", tool_id, exc
                )

        # 3. Merge (user values override defaults).
        merged = {**defaults, **user_values}
        return merged

    def save_settings(
        self,
        tool_id: str,
        values: Dict[str, Any],
        schema: Optional[Dict[str, Any]],
        is_bundle: bool = False,
    ) -> Dict[str, Any]:
        """
        Validate values against the schema and persist them atomically.

        Raises:
            ValueError: If the tool has no schema or validation fails.
        """
        if not schema:
            raise ValueError(f"Tool '{tool_id}' has no settings schema.")

        # 1. Validate using jsonschema.
        try:
            validate(instance=values, schema=schema)
        except ValidationError as exc:
            logger.warning(
                "Settings validation failed for %s: %s", tool_id, exc
            )
            raise ValueError(f"Invalid settings: {exc.message}") from exc

        # 2. Atomic write via os.replace().
        path = self._get_settings_path(tool_id, is_bundle)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")

        try:
            tmp_path.write_text(
                json.dumps(values, indent=2), encoding="utf-8"
            )
            os.replace(tmp_path, path)  # Atomic operation.
        except Exception as exc:
            if tmp_path.exists():
                tmp_path.unlink()
            logger.error(
                "Failed to save settings for %s: %s", tool_id, exc
            )
            raise

        logger.info("Saved settings for tool %s", tool_id)
        return values
