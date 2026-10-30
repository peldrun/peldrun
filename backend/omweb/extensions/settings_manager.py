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

        # Initialize secret storage eagerly: a corrupt key must fail fast
        # at startup, not silently fall back to plaintext writes.
        # Lazy import avoids a circular dependency at module load time.
        from omweb.extensions.secret_storage import get_global_secret_storage

        self._secrets = get_global_secret_storage()

        # One-time migration of any legacy plaintext env blocks.
        # Idempotent: files already carrying env_enc are skipped.
        try:
            migrated = self.migrate_plaintext_files()
            if migrated:
                logger.info(
                    "Migrated %d plaintext settings file(s) to encrypted storage.",
                    migrated,
                )
        except Exception as exc:  # noqa: BLE001
            # Migration failure must not prevent the app from starting,
            # but it must be loud enough to notice in logs.
            logger.warning("Startup plaintext migration failed: %s", exc)

    def _get_server_file_path(self, server_id: str) -> Path:
        """Resolve the atomic storage file path for a given MCP server identifier."""
        safe_id = "".join(c for c in server_id if c.isalnum() or c in ("-", "_"))
        return self.settings_dir / f"{safe_id}.json"

    def _read_server_payload(self, server_id: str) -> Dict[str, Any]:
        """
        Read and normalize the persisted configuration container.

        The on-disk format has two eras:
          * v2 (current): {"settings": {...}, "env_enc": "<base64>", "schema_version": 2}
          * v1 (legacy):  {"settings": {...}, "env": {...}}            ← plaintext

        Decryption is attempted only when env_enc is present. If the
        ciphertext cannot be decrypted (tampered, wrong key), we return
        an empty env rather than silently serving stale plaintext —
        failing closed is the correct behavior for a secret store.
        """
        path = self._get_server_file_path(server_id)
        if not path.exists():
            return {"settings": {}, "env": {}}

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                "Failed to parse settings container for MCP server '%s': %s",
                server_id,
                exc,
            )
            return {"settings": {}, "env": {}}

        if not isinstance(data, dict):
            return {"settings": {}, "env": {}}

        settings = data.get("settings", {})
        if not isinstance(settings, dict):
            settings = {}

        env: Dict[str, Any] = {}
        env_enc = data.get("env_enc")
        if isinstance(env_enc, str) and env_enc:
            # v2 path: decrypt
            try:
                plaintext = self._secrets.decrypt(
                    env_enc, aad=f"global:{server_id}"
                )
                parsed = json.loads(plaintext)
                if isinstance(parsed, dict):
                    env = parsed
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt env for MCP server '%s': %s",
                    server_id,
                    exc,
                )
                # Do NOT fall back to plaintext if env_enc exists but
                # failed — that would mask corruption or a wrong key.
        else:
            # v1 path: legacy plaintext (migrated on next write or by
            # the startup migration routine).
            legacy = data.get("env", {})
            if isinstance(legacy, dict):
                env = legacy

        # ── Global OAuth tokens (for OAuth-only servers) ──────────
        # OAuth servers (Notion, Linear, etc.) do NOT have env_schema
        # because their credentials are tokens, not env vars. The
        # token bundle is stored here so the Store UI can trigger
        # OAuth once and the agent gets tools on the next run
        # without opening a modal.
        oauth: Optional[Dict[str, Any]] = None
        oauth_enc = data.get("oauth_enc")
        if isinstance(oauth_enc, str) and oauth_enc:
            try:
                plaintext = self._secrets.decrypt(
                    oauth_enc, aad=f"global:{server_id}:oauth"
                )
                parsed = json.loads(plaintext)
                if isinstance(parsed, dict):
                    oauth = parsed
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt oauth for MCP server '%s': %s",
                    server_id,
                    exc,
                )

        return {"settings": settings, "env": env, "oauth": oauth}

    def _write_server_payload(self, server_id: str, payload: Dict[str, Any]) -> None:
        """
        Persist the configuration container atomically (write + rename).

        Security invariants enforced here:
          * ``env`` is ALWAYS encrypted before reaching the disk. There
            is no code path that writes plaintext credentials.
          * ``env_enc`` is omitted entirely when ``env`` is empty — no
            point encrypting the empty object, and omitting it keeps
            the v1/v2 read logic unambiguous.
          * The AAD binds the ciphertext to ``global:{server_id}`` so a
            token copied from server A cannot be presented as server B.
        """
        path = self._get_server_file_path(server_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")

        settings = payload.get("settings", {}) or {}
        env = payload.get("env", {}) or {}
        oauth = payload.get("oauth")

        disk_payload: Dict[str, Any] = {
            "settings": settings if isinstance(settings, dict) else {},
            "schema_version": 2,
        }

        if isinstance(env, dict) and env:
            env_json = json.dumps(env, ensure_ascii=False)
            disk_payload["env_enc"] = self._secrets.encrypt(
                env_json, aad=f"global:{server_id}"
            )

        # ── Global OAuth tokens (only when present) ────────────────
        # Encrypted with a distinct AAD ("...:oauth") so a ciphertext
        # swap between env and oauth is rejected — the two cannot be
        # confused at rest.
        if isinstance(oauth, dict) and oauth:
            oauth_json = json.dumps(oauth, ensure_ascii=False)
            disk_payload["oauth_enc"] = self._secrets.encrypt(
                oauth_json, aad=f"global:{server_id}:oauth"
            )

        try:
            tmp_path.write_text(
                json.dumps(disk_payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp_path, path)
        except Exception as exc:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            logger.error(
                "Failed to atomically save configuration for MCP server '%s': %s",
                server_id,
                exc,
            )
            raise

    def migrate_plaintext_files(self) -> int:
        """
        Encrypt every legacy plaintext ``env`` block found under settings/.

        Returns the number of files migrated. Idempotent: files that
        already carry a non-empty ``env_enc`` are skipped, so running
        this on every process start is safe.

        Failure to migrate a single file is logged and skipped — one
        broken JSON does not block the rest of the sweep.
        """
        migrated = 0
        if not self.settings_dir.exists():
            return 0

        for path in self.settings_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue

                # Already migrated?
                env_enc = data.get("env_enc")
                if isinstance(env_enc, str) and env_enc:
                    continue

                legacy_env = data.get("env")
                if not isinstance(legacy_env, dict) or not legacy_env:
                    # Nothing to encrypt (empty or missing env).
                    continue

                server_id = path.stem
                # Route through the normal writer so encryption and
                # atomic-rename semantics are identical to save_env().
                self._write_server_payload(
                    server_id,
                    {
                        "settings": data.get("settings", {}) or {},
                        "env": legacy_env,
                    },
                )
                migrated += 1
                logger.info(
                    "Migrated plaintext env for MCP server '%s'", server_id
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to migrate %s: %s", path, exc)

        return migrated

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

    # ─────────────────────────────────────────────────────────────
    # Global OAuth token storage (for OAuth-only servers)
    # ─────────────────────────────────────────────────────────────

    def save_oauth(
        self,
        server_id: str,
        tokens: Dict[str, Any],
    ) -> None:
        """
        Persist an OAuth token bundle for a server (global scope).

        Used by OAuth-only servers (Notion, Linear, etc.) after the
        Store UI completes the authorization flow. The bundle is
        encrypted with AAD "global:{server_id}:oauth" so it cannot
        be confused with env credentials even if extracted from disk.

        Tokens may be refreshed later; the caller is expected to
        overwrite the whole bundle (refresh token rotation).
        """
        if not isinstance(tokens, dict):
            raise TypeError("tokens must be a dict")
        container = self._read_server_payload(server_id)
        container["oauth"] = tokens
        self._write_server_payload(server_id, container)
        logger.info(
            "Saved global OAuth token bundle for MCP server '%s'.",
            server_id,
        )

    def get_oauth(self, server_id: str) -> Optional[Dict[str, Any]]:
        """
        Return the decrypted global OAuth token bundle, or None.

        Never raises: a corrupt or tampered ciphertext yields None so
        the caller treats it as "no credentials" and re-prompts the
        user rather than serving garbage.
        """
        container = self._read_server_payload(server_id)
        oauth = container.get("oauth")
        if isinstance(oauth, dict) and oauth:
            return oauth
        return None

    def delete_oauth(self, server_id: str) -> bool:
        """
        Remove the global OAuth token bundle for a server.

        Returns True when a bundle was present and removed.
        """
        container = self._read_server_payload(server_id)
        had = isinstance(container.get("oauth"), dict) and bool(
            container.get("oauth")
        )
        if not had:
            return False
        container["oauth"] = {}
        self._write_server_payload(server_id, container)
        logger.info(
            "Removed global OAuth token bundle for MCP server '%s'.",
            server_id,
        )
        return True

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
