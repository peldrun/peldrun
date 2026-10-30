# backend/omweb/extensions/oauth_provider_store.py
"""
Persistent storage for OAuth provider client credentials.

Why a dedicated store (not per-server env):
    Google's three Workspace MCP servers (Gmail, Drive, Calendar)
    share a single OAuth 2.0 client. Saving the client_id/secret
    once at the provider level means the user configures it once,
    not three times. The same applies to GitHub (one client covers
    every GitHub-based MCP server the user installs).

Storage:
    backend/storage/store/oauth_providers/{provider_key}.json
    where provider_key is a sanitized hostname (e.g. "googleapis.com").

Security:
    * client_id and client_secret are encrypted with AES-256-GCM
      using SecretStorage, exactly like env credentials.
    * Distinct AAD per field binds the ciphertext to its purpose:
      a client_secret cannot be replayed as a client_id.
    * File permissions inherit from parent (0600 on POSIX).
    * Reads fail-closed: a decryption error yields None, so the
      caller falls back to env vars rather than serving garbage.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from omweb.config import STORAGE_ROOT
from omweb.extensions.secret_storage import (
    SecretStorage,
    get_global_secret_storage,
)

logger = logging.getLogger("omweb.extensions.oauth_provider_store")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sanitize_key(provider_key: str) -> str:
    """
    Normalize a provider key into a safe filename segment.

    Accepts things like "googleapis.com", "github.com",
    "accounts.google.com" and produces "googleapis.com",
    "github.com", "accounts.google.com" — dots and hyphens are
    allowed; everything else is stripped.
    """
    raw = (provider_key or "").strip().lower()
    safe = "".join(
        c for c in raw if c.isalnum() or c in ("-", ".", "_")
    )
    if not safe or safe != raw:
        raise ValueError(
            f"Invalid provider key: {provider_key!r}. "
            f"Expected a lowercase hostname-like string."
        )
    return safe


def _atomic_write_json(target: Path, data: Any) -> None:
    """Atomic write with fsync — mirrors ProjectManager discipline."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)


class OAuthProviderStore:
    """
    Store of pre-registered OAuth client credentials.

    One entry per provider hostname. Shared by every MCP server
    whose issuer matches that hostname.
    """

    def __init__(self, secrets: Optional[SecretStorage] = None) -> None:
        self._secrets = secrets or get_global_secret_storage()
        self._root = Path(STORAGE_ROOT) / "store" / "oauth_providers"
        self._root.mkdir(parents=True, exist_ok=True)

    # ─────────────────────────────────────────────────────────────
    # Paths
    # ─────────────────────────────────────────────────────────────

    def _path(self, provider_key: str) -> Path:
        return self._root / f"{_sanitize_key(provider_key)}.json"

    # ─────────────────────────────────────────────────────────────
    # Read
    # ─────────────────────────────────────────────────────────────

    def get(self, provider_key: str) -> Optional[Dict[str, str]]:
        """
        Return the decrypted credentials for a provider, or None.

        Returns ``{"client_id": "...", "client_secret": "..."}``
        on success. If either field fails to decrypt (tampered,
        wrong key, missing), returns None — the caller must fall
        back to env vars or raise a clear error.
        """
        try:
            path = self._path(provider_key)
        except ValueError as exc:
            logger.warning("get() rejected invalid provider_key: %s", exc)
            return None

        if not path.exists():
            return None

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to parse OAuth provider file for '%s': %s",
                provider_key,
                exc,
            )
            return None

        if not isinstance(data, dict):
            return None

        cid_enc = data.get("client_id_enc")
        sec_enc = data.get("client_secret_enc")
        if not isinstance(cid_enc, str) or not isinstance(sec_enc, str):
            return None

        safe_key = _sanitize_key(provider_key)
        try:
            client_id = self._secrets.decrypt(
                cid_enc, aad=f"oauth_client:{safe_key}:client_id"
            )
            client_secret = self._secrets.decrypt(
                sec_enc, aad=f"oauth_client:{safe_key}:client_secret"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to decrypt OAuth provider credentials for "
                "'%s': %s",
                provider_key,
                exc,
            )
            return None

        return {
            "client_id": client_id,
            "client_secret": client_secret,
        }

    def is_configured(self, provider_key: str) -> bool:
        """True when both client_id and client_secret are stored."""
        return self.get(provider_key) is not None

    def list_configured(self) -> List[str]:
        """Return the provider keys that have stored credentials."""
        out: List[str] = []
        if not self._root.exists():
            return out
        for p in sorted(self._root.glob("*.json")):
            key = p.stem
            if self.is_configured(key):
                out.append(key)
        return out

    # ─────────────────────────────────────────────────────────────
    # Write
    # ─────────────────────────────────────────────────────────────

    def save(
        self,
        provider_key: str,
        client_id: str,
        client_secret: str,
    ) -> None:
        """
        Encrypt and persist client credentials for a provider.

        Idempotent: overwrites any existing entry. ``created_at``
        is preserved across updates.
        """
        safe_key = _sanitize_key(provider_key)
        if not client_id or not client_secret:
            raise ValueError(
                "Both client_id and client_secret are required."
            )

        cid_enc = self._secrets.encrypt(
            client_id, aad=f"oauth_client:{safe_key}:client_id"
        )
        sec_enc = self._secrets.encrypt(
            client_secret, aad=f"oauth_client:{safe_key}:client_secret"
        )

        path = self._path(provider_key)
        now = _utcnow_iso()

        created_at = now
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(existing, dict) and existing.get("created_at"):
                    created_at = existing["created_at"]
            except Exception:  # noqa: BLE001
                pass

        payload = {
            "version": 1,
            "provider_key": safe_key,
            "client_id_enc": cid_enc,
            "client_secret_enc": sec_enc,
            "created_at": created_at,
            "updated_at": now,
        }
        _atomic_write_json(path, payload)
        logger.info(
            "Saved OAuth provider credentials for '%s'.", safe_key
        )

    def delete(self, provider_key: str) -> bool:
        """Remove the stored credentials. Returns True if a file existed."""
        try:
            path = self._path(provider_key)
        except ValueError:
            return False
        if not path.exists():
            return False
        try:
            path.unlink()
            logger.info(
                "Deleted OAuth provider credentials for '%s'.",
                provider_key,
            )
            return True
        except OSError as exc:
            logger.warning(
                "Failed to delete OAuth provider file for '%s': %s",
                provider_key,
                exc,
            )
            return False


# ─────────────────────────────────────────────────────────────────
# Process-wide singleton
# ─────────────────────────────────────────────────────────────────

_singleton: Optional[OAuthProviderStore] = None


def get_oauth_provider_store() -> OAuthProviderStore:
    """Return the process-wide OAuthProviderStore instance."""
    global _singleton
    if _singleton is None:
        _singleton = OAuthProviderStore()
    return _singleton


__all__ = ["OAuthProviderStore", "get_oauth_provider_store"]
