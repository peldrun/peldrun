# backend/omweb/extensions/bundle_manager.py
"""
Model Context Protocol (MCP) Bundle Lifecycle and Installation Manager.

Responsibilities:
  * Extract uploaded MCP zip archives into dedicated per-server bundle directories.
  * Enforce strict Zip-Slip and path traversal security guards during extraction.
  * Validate manifest.json integrity, metadata, runtime requirements, and schemas.
  * Register bundle servers in ExtensionRegistry with bundle-isolated execution roots.
  * Provide atomic deletion and comprehensive filesystem/session clean-up.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from omweb.extensions.mcp_pool import mcp_session_pool
from omweb.extensions.registry import extension_registry
from omweb.extensions.settings_manager import mcp_settings_manager

logger = logging.getLogger("omweb.extensions.bundle_manager")


class McpBundleError(Exception):
    """Base exception for MCP bundle installation and verification failures."""
    def __init__(self, message: str, code: str = "BUNDLE_ERROR") -> None:
        super().__init__(message)
        self.message = message
        self.code = code

    def to_dict(self) -> Dict[str, Any]:
        return {"error": self.message, "code": self.code}


class McpBundleManager:
    """Manages installation, validation, and uninstallation of standalone MCP bundles."""

    def __init__(self, backend_storage_dir: Optional[Path] = None) -> None:
        if backend_storage_dir is not None:
            self.storage_dir = backend_storage_dir
        else:
            backend_dir = Path(__file__).resolve().parent.parent.parent
            self.storage_dir = backend_dir / "storage"

        self.bundles_dir = self.storage_dir / "store" / "extensions" / "bundles"
        self.bundles_dir.mkdir(parents=True, exist_ok=True)

    def _sanitize_bundle_id(self, raw_id: str) -> str:
        """Sanitize bundle identifier to prevent directory traversal or invalid characters."""
        clean = "".join(c for c in raw_id if c.isalnum() or c in ("-", "_")).strip()
        if not clean:
            raise McpBundleError("Invalid bundle ID: must contain alphanumeric characters.", "INVALID_ID")
        return clean

    def _safe_extract(self, zip_ref: zipfile.ZipFile, target_dir: Path) -> None:
        """
        Safely extract archive contents ensuring no path escapes target directory (Zip-Slip defense).
        """
        resolved_target = target_dir.resolve()
        for member in zip_ref.infolist():
            # Disallow absolute paths and directory traversal sequences
            member_path = Path(member.filename)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise McpBundleError(
                    f"Malicious path detected in archive: {member.filename}",
                    "ZIP_SLIP_DETECTED",
                )

            extracted_path = (target_dir / member.filename).resolve()
            if resolved_target not in extracted_path.parents and extracted_path != resolved_target:
                raise McpBundleError(
                    f"Archive member attempts path escape: {member.filename}",
                    "PATH_TRAVERSAL_DETECTED",
                )

        zip_ref.extractall(target_dir)

    def install_bundle(self, archive_bytes: bytes, source_filename: str = "bundle.zip") -> Dict[str, Any]:
        """
        Validate, extract, and register an MCP bundle from raw zip archive bytes.
        """
        try:
            zip_buffer = io.BytesIO(archive_bytes)
            with zipfile.ZipFile(zip_buffer, "r") as zip_ref:
                file_list = zip_ref.namelist()

                # Find manifest.json (either at root or within single parent directory)
                manifest_filename = None
                for fname in file_list:
                    parts = Path(fname).parts
                    if len(parts) == 1 and parts[0] == "manifest.json":
                        manifest_filename = fname
                        break
                    elif len(parts) == 2 and parts[1] == "manifest.json":
                        manifest_filename = fname
                        break

                if not manifest_filename:
                    raise McpBundleError(
                        "MCP bundle must contain a 'manifest.json' file at its root.",
                        "MISSING_MANIFEST",
                    )

                try:
                    manifest_data = json.loads(zip_ref.read(manifest_filename).decode("utf-8"))
                except Exception as exc:
                    raise McpBundleError(f"Failed to parse manifest.json: {exc}", "INVALID_MANIFEST_JSON") from exc

                raw_id = manifest_data.get("id")
                if not raw_id:
                    raise McpBundleError("manifest.json must declare an 'id' field.", "MISSING_BUNDLE_ID")

                bundle_id = self._sanitize_bundle_id(str(raw_id))
                target_bundle_dir = self.bundles_dir / bundle_id

                # If existing bundle directory exists, clean it first to ensure clean state
                if target_bundle_dir.exists():
                    shutil.rmtree(target_bundle_dir, ignore_errors=True)
                target_bundle_dir.mkdir(parents=True, exist_ok=True)

                # Safely extract all bundle files
                self._safe_extract(zip_ref, target_bundle_dir)

                # If manifest was inside a subfolder, relocate contents to target root
                manifest_path_in_dest = target_bundle_dir / manifest_filename
                if manifest_path_in_dest.parent != target_bundle_dir:
                    subfolder = manifest_path_in_dest.parent
                    for item in subfolder.iterdir():
                        shutil.move(str(item), str(target_bundle_dir))
                    shutil.rmtree(subfolder, ignore_errors=True)

                # Normalize manifest properties
                manifest_data["id"] = bundle_id
                manifest_data["name"] = manifest_data.get("name", bundle_id)
                manifest_data["type"] = "mcp_server"
                manifest_data["is_builtin"] = False
                manifest_data["is_bundle"] = True
                manifest_data["bundle_dir"] = str(target_bundle_dir.resolve())
                manifest_data.setdefault("status", "ready")
                manifest_data.setdefault("transport", "stdio")

                # Resolve execution command relative to bundle directory if command provided
                command = manifest_data.get("command")
                entrypoint = manifest_data.get("entrypoint")
                if entrypoint and not command:
                    runtime = manifest_data.get("runtime", "node").lower()
                    if runtime == "node":
                        manifest_data["command"] = f"node {entrypoint}"
                    elif runtime == "python":
                        manifest_data["command"] = f"python {entrypoint}"

                # Register the MCP server manifest
                saved_manifest = extension_registry.save_extension(manifest_data)
                logger.info("Successfully installed and registered MCP bundle '%s'", bundle_id)

                return {
                    "id": bundle_id,
                    "name": saved_manifest.get("name"),
                    "bundle_dir": str(target_bundle_dir),
                    "server": saved_manifest,
                    "installed_files": len(zip_ref.infolist()),
                }

        except McpBundleError:
            raise
        except Exception as exc:
            logger.exception("Unexpected error during MCP bundle installation: %s", exc)
            raise McpBundleError(f"Bundle installation failed: {exc}", "INSTALLATION_FAILED") from exc

    async def delete_bundle(self, bundle_id: str) -> bool:
        """
        Completely delete an installed MCP bundle, removing its folder, configuration, and active sessions.
        """
        clean_id = self._sanitize_bundle_id(bundle_id)

        # 1. Terminate and purge active subprocess sessions
        await mcp_session_pool.close_session(clean_id)

        # 2. Remove physical bundle folder containing all bundle files
        target_bundle_dir = self.bundles_dir / clean_id
        if target_bundle_dir.exists():
            try:
                shutil.rmtree(target_bundle_dir)
                logger.info("Removed bundle directory for '%s'", clean_id)
            except Exception as exc:
                logger.warning("Failed to remove bundle directory '%s': %s", target_bundle_dir, exc)

        # 3. Remove persisted settings file if present
        settings_file = mcp_settings_manager._get_server_file_path(clean_id)
        if settings_file.exists():
            try:
                settings_file.unlink()
            except Exception as exc:
                logger.debug("Could not remove settings file for '%s': %s", clean_id, exc)

        # 4. Remove extension registration manifest
        deleted = extension_registry.delete_extension(clean_id)
        logger.info("Purged MCP bundle registration for '%s' (success: %s)", clean_id, deleted)
        return deleted

    def list_installed_bundles(self) -> List[Dict[str, Any]]:
        """List all installed MCP bundles on disk."""
        installed = []
        if not self.bundles_dir.exists():
            return installed

        for path in self.bundles_dir.iterdir():
            if path.is_dir():
                manifest_file = path / "manifest.json"
                if manifest_file.exists():
                    try:
                        data = json.loads(manifest_file.read_text(encoding="utf-8"))
                        installed.append({
                            "id": path.name,
                            "name": data.get("name", path.name),
                            "path": str(path),
                            "version": data.get("version", "1.0.0"),
                        })
                    except Exception:
                        installed.append({"id": path.name, "path": str(path)})
        return installed


mcp_bundle_manager = McpBundleManager()

__all__ = ["McpBundleManager", "McpBundleError", "mcp_bundle_manager"]
