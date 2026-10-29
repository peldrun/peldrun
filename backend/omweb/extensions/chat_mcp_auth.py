# backend/omweb/extensions/chat_mcp_auth.py
"""
Per-chat MCP credential store.

Design invariants:
  * mcp_auth.json lives NEXT TO session.json — inside the canonical chat
    directory resolved by ProjectManager. There is no parallel storage
    tree that could drift or outlive a delete_chat() call.
  * All secrets are AES-256-GCM encrypted with AAD = "chat:{cid}:{sid}".
    A token copied from chat A cannot be replayed in chat B or against
    a different server.
  * The file is written atomically with fsync (mirrors ProjectManager)
    so a power loss mid-write cannot corrupt the credential store.
  * "ephemeral" mode is IN-MEMORY ONLY — it never touches the disk and
    is dropped when the process restarts. This is the correct semantics
    for "this one time only" credentials.
  * chat_id and job_id are interchangeable inputs; both are normalised
    through ProjectManager.get_chat() before any file I/O.

On-disk format (all secrets encrypted):
    {
      "version": 1,
      "entries": {
        "<server_id>": {
          "mode": "custom" | "use_global",
          "env_enc": "<base64>",       # only when mode == "custom"
          "oauth_enc": "<base64>",     # optional, reserved for OAuth tokens
          "created_at": "<iso8601>",
          "updated_at": "<iso8601>"
        }
      }
    }

Note: "ephemeral" entries are NOT written here. They live only in
ChatMcpAuthStore._ephemeral (in-memory dict).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from omweb.extensions.secret_storage import (
    SecretStorage,
    get_global_secret_storage,
)

logger = logging.getLogger("omweb.extensions.chat_mcp_auth")

# Modes are a closed set. Anything else is treated as "no decision yet"
# and will trigger the pre-flight auth check in the engine.
MODE_CUSTOM = "custom"
MODE_USE_GLOBAL = "use_global"
MODE_EPHEMERAL = "ephemeral"
_VALID_MODES = frozenset({MODE_CUSTOM, MODE_USE_GLOBAL, MODE_EPHEMERAL})


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(target_path: Path, data: Any) -> None:
    """
    Atomic JSON write with fsync — same discipline as ProjectManager.
    A partially written mcp_auth.json must never be observable by a
    concurrent reader, and it must survive an abrupt power loss.
    """
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_suffix(target_path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, target_path)


class ChatMcpAuthStore:
    """Per-chat credential store for MCP servers."""

    FILENAME = "mcp_auth.json"
    SCHEMA_VERSION = 1

    def __init__(self, secrets: Optional[SecretStorage] = None) -> None:
        self._secrets = secrets or get_global_secret_storage()

        # Per-chat asyncio.Lock to serialize read-modify-write cycles.
        # Chat-level granularity — two chats can save concurrently
        # without contending.
        self._locks: Dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

        # Ephemeral (one-shot) credentials — never persisted.
        # Shape: {chat_id: {server_id: {"env": {...}, "oauth": {...}}}}
        self._ephemeral: Dict[str, Dict[str, Dict[str, Any]]] = {}
        # Guard for the ephemeral dict (async-safe reads / writes).
        self._ephemeral_guard = asyncio.Lock()

    # ─────────────────────────────────────────────────────────────
    # Chat directory resolution
    # ─────────────────────────────────────────────────────────────

    def _resolve_chat_dir(self, chat_id: str) -> Path:
        """
        Resolve the canonical chat directory via ProjectManager.

        This deliberately mirrors ProjectManager.get_chat_files_dir()
        semantics: there is ONE canonical location per chat, and no
        fallback between the standalone-chat tree and the per-project
        tree. If the chat is not yet indexed, we fall back to the
        standalone tree — ProjectManager.save_chat_session() will
        create the same directory when the job first saves.

        Lazy import avoids a circular dependency: extensions/ is
        imported very early during bootstrap, before the project
        manager is available.
        """
        from omweb.project_manager import project_manager  # lazy import

        meta = project_manager.get_chat(chat_id)
        project_id = "default_project"
        if isinstance(meta, dict):
            project_id = meta.get("project_id") or "default_project"
        return project_manager.get_chat_dir(chat_id, project_id)

    def _auth_file(self, chat_id: str) -> Path:
        return self._resolve_chat_dir(chat_id) / self.FILENAME

    # ─────────────────────────────────────────────────────────────
    # Locking
    # ─────────────────────────────────────────────────────────────

    async def _get_lock(self, chat_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            if chat_id not in self._locks:
                self._locks[chat_id] = asyncio.Lock()
            return self._locks[chat_id]

    # ─────────────────────────────────────────────────────────────
    # On-disk container helpers
    # ─────────────────────────────────────────────────────────────

    def _read_container(self, chat_id: str) -> Dict[str, Any]:
        path = self._auth_file(chat_id)
        if not path.exists():
            return {"version": self.SCHEMA_VERSION, "entries": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {"version": self.SCHEMA_VERSION, "entries": {}}
            entries = data.get("entries")
            if not isinstance(entries, dict):
                entries = {}
            return {
                "version": data.get("version", self.SCHEMA_VERSION),
                "entries": entries,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to read chat auth file for '%s': %s", chat_id, exc)
            return {"version": self.SCHEMA_VERSION, "entries": {}}

    def _write_container(self, chat_id: str, container: Dict[str, Any]) -> None:
        _atomic_write_json(self._auth_file(chat_id), container)

    # ─────────────────────────────────────────────────────────────
    # Public read API
    # ─────────────────────────────────────────────────────────────

    def get_mode(self, chat_id: str, server_id: str) -> Optional[str]:
        """
        Return the persisted mode for a (chat, server) pair, or None if
        the user has not yet made a decision. Ephemeral mode is checked
        in memory only.
        """
        entry = self._read_container(chat_id)["entries"].get(server_id)
        if isinstance(entry, dict):
            mode = entry.get("mode")
            if mode in (MODE_CUSTOM, MODE_USE_GLOBAL):
                return mode

        # Ephemeral lives only in memory; check it last so a
        # persisted decision always wins over a transient one.
        if self._peek_ephemeral(chat_id, server_id) is not None:
            return MODE_EPHEMERAL

        return None

    def get_env(self, chat_id: str, server_id: str) -> Optional[Dict[str, str]]:
        """
        Return decrypted env for a "custom" entry, or None when the entry
        is absent / not custom / cannot be decrypted.

        Never returns plaintext from a corrupted ciphertext — a failed
        decrypt yields None, which the engine interprets as "no
        credentials" and re-prompts the user.
        """
        entry = self._read_container(chat_id)["entries"].get(server_id)
        if not isinstance(entry, dict) or entry.get("mode") != MODE_CUSTOM:
            return None
        encrypted = entry.get("env_enc")
        if not isinstance(encrypted, str) or not encrypted:
            return None
        try:
            plaintext = self._secrets.decrypt(
                encrypted, aad=f"chat:{chat_id}:{server_id}"
            )
            parsed = json.loads(plaintext)
            if not isinstance(parsed, dict):
                return None
            return {str(k): str(v) for k, v in parsed.items()}
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Decrypt failed for chat '%s' server '%s': %s",
                chat_id,
                server_id,
                exc,
            )
            return None

    def get_oauth_tokens(self, chat_id: str, server_id: str) -> Optional[Dict[str, Any]]:
        """Return decrypted OAuth token bundle for a custom entry, or None."""
        entry = self._read_container(chat_id)["entries"].get(server_id)
        if not isinstance(entry, dict) or entry.get("mode") != MODE_CUSTOM:
            return None
        encrypted = entry.get("oauth_enc")
        if not isinstance(encrypted, str) or not encrypted:
            return None
        try:
            plaintext = self._secrets.decrypt(
                encrypted, aad=f"chat:{chat_id}:{server_id}:oauth"
            )
            parsed = json.loads(plaintext)
            return parsed if isinstance(parsed, dict) else None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "OAuth decrypt failed for chat '%s' server '%s': %s",
                chat_id,
                server_id,
                exc,
            )
            return None

    def get_ephemeral(self, chat_id: str, server_id: str) -> Optional[Dict[str, str]]:
        """Return in-memory ephemeral env, if any."""
        snap = self._peek_ephemeral(chat_id, server_id)
        if not snap:
            return None
        env = snap.get("env")
        return dict(env) if isinstance(env, dict) else None

    def get_ephemeral_oauth(self, chat_id: str, server_id: str) -> Optional[Dict[str, Any]]:
        """Return in-memory ephemeral OAuth tokens, if any."""
        snap = self._peek_ephemeral(chat_id, server_id)
        if not snap:
            return None
        oauth = snap.get("oauth")
        return dict(oauth) if isinstance(oauth, dict) else None

    def list_entries(self, chat_id: str) -> List[Dict[str, Any]]:
        """
        List the persisted entries for a chat. Ephemeral entries are
        not listed here — they are transient by definition.
        """
        out: List[Dict[str, Any]] = []
        for sid, e in self._read_container(chat_id)["entries"].items():
            if not isinstance(e, dict):
                continue
            out.append({
                "server_id": sid,
                "mode": e.get("mode"),
                "has_env": bool(e.get("env_enc")),
                "has_oauth": bool(e.get("oauth_enc")),
                "created_at": e.get("created_at"),
                "updated_at": e.get("updated_at"),
            })
        return out

    # ─────────────────────────────────────────────────────────────
    # Public write API
    # ─────────────────────────────────────────────────────────────

    async def save_custom(
        self,
        chat_id: str,
        server_id: str,
        env: Dict[str, str],
        oauth_tokens: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Persist a custom env (and optional OAuth token bundle) for a
        (chat, server) pair. Both are encrypted with distinct AADs so
        that a ciphertext swap between the two fields is rejected.
        """
        if not isinstance(env, dict):
            raise TypeError("env must be a dict")
        normalised_env = {str(k): str(v) for k, v in env.items() if v is not None}

        lock = await self._get_lock(chat_id)
        async with lock:
            container = self._read_container(chat_id)
            now = _utcnow_iso()

            env_enc = self._secrets.encrypt(
                json.dumps(normalised_env, ensure_ascii=False),
                aad=f"chat:{chat_id}:{server_id}",
            )

            entry: Dict[str, Any] = {
                "mode": MODE_CUSTOM,
                "env_enc": env_enc,
                "created_at": now,
                "updated_at": now,
            }

            # Preserve an existing created_at across updates.
            previous = container["entries"].get(server_id)
            if isinstance(previous, dict) and previous.get("created_at"):
                entry["created_at"] = previous["created_at"]

            if oauth_tokens:
                entry["oauth_enc"] = self._secrets.encrypt(
                    json.dumps(oauth_tokens, ensure_ascii=False),
                    aad=f"chat:{chat_id}:{server_id}:oauth",
                )

            container["entries"][server_id] = entry
            self._write_container(chat_id, container)

            # A successful save supersedes any in-memory ephemeral value.
            await self._clear_ephemeral(chat_id, server_id)

    async def save_oauth_tokens(
        self,
        chat_id: str,
        server_id: str,
        access_token: str,
        refresh_token: Optional[str] = None,
        expires_in: Optional[int] = None,
        token_type: str = "Bearer",
        resource: Optional[str] = None,
    ) -> None:
        """
        Persist an OAuth token bundle. If a custom env already exists
        for this (chat, server) we keep it; otherwise we create a
        custom entry whose env is the empty dict.
        """
        now = _utcnow_iso()
        oauth_bundle: Dict[str, Any] = {
            "access_token": access_token,
            "token_type": token_type,
        }
        if refresh_token:
            oauth_bundle["refresh_token"] = refresh_token
        if expires_in is not None:
            oauth_bundle["expires_in"] = int(expires_in)
            # Absolute expiry makes refresh logic trivial and avoids
            # depending on a monotonic clock across restarts.
            oauth_bundle["expires_at"] = int(
                datetime.now(timezone.utc).timestamp() + int(expires_in)
            )
        if resource:
            oauth_bundle["resource"] = resource

        lock = await self._get_lock(chat_id)
        async with lock:
            container = self._read_container(chat_id)
            entry = container["entries"].get(server_id)
            if not isinstance(entry, dict) or entry.get("mode") != MODE_CUSTOM:
                entry = {
                    "mode": MODE_CUSTOM,
                    "created_at": now,
                    "updated_at": now,
                }
                # Only encrypt an empty env if one is not already present;
                # an OAuth-only entry has no env to persist.
                if "env_enc" not in entry:
                    entry["env_enc"] = self._secrets.encrypt(
                        json.dumps({}, ensure_ascii=False),
                        aad=f"chat:{chat_id}:{server_id}",
                    )

            entry["mode"] = MODE_CUSTOM
            entry["oauth_enc"] = self._secrets.encrypt(
                json.dumps(oauth_bundle, ensure_ascii=False),
                aad=f"chat:{chat_id}:{server_id}:oauth",
            )
            entry["updated_at"] = now
            container["entries"][server_id] = entry
            self._write_container(chat_id, container)

            await self._clear_ephemeral(chat_id, server_id)

    async def save_use_global(self, chat_id: str, server_id: str) -> None:
        """Record the user's choice to fall back to global settings."""
        lock = await self._get_lock(chat_id)
        async with lock:
            container = self._read_container(chat_id)
            now = _utcnow_iso()
            entry: Dict[str, Any] = {
                "mode": MODE_USE_GLOBAL,
                "created_at": now,
                "updated_at": now,
            }
            previous = container["entries"].get(server_id)
            if isinstance(previous, dict) and previous.get("created_at"):
                entry["created_at"] = previous["created_at"]

            container["entries"][server_id] = entry
            self._write_container(chat_id, container)

            await self._clear_ephemeral(chat_id, server_id)

    async def save_ephemeral(
        self,
        chat_id: str,
        server_id: str,
        env: Optional[Dict[str, str]] = None,
        oauth_tokens: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Store one-shot credentials in memory only.

        "Ephemeral" here means literally: not persisted, not recoverable
        after process restart, and cleared once a persisted save
        supersedes it. That matches the user's mental model of
        "just this once".
        """
        if env is None and oauth_tokens is None:
            raise ValueError("save_ephemeral requires env or oauth_tokens")

        normalised_env = None
        if env is not None:
            if not isinstance(env, dict):
                raise TypeError("env must be a dict")
            normalised_env = {
                str(k): str(v) for k, v in env.items() if v is not None
            }

        async with self._ephemeral_guard:
            bucket = self._ephemeral.setdefault(chat_id, {})
            bucket[server_id] = {
                "env": normalised_env or {},
                "oauth": dict(oauth_tokens) if oauth_tokens else {},
            }

    async def delete(self, chat_id: str, server_id: str) -> bool:
        """
        Remove a persisted entry and any in-memory ephemeral counterpart.

        Returns True if something was actually removed.
        """
        removed = await self._clear_ephemeral(chat_id, server_id)

        lock = await self._get_lock(chat_id)
        async with lock:
            container = self._read_container(chat_id)
            if server_id in container["entries"]:
                del container["entries"][server_id]
                self._write_container(chat_id, container)
                removed = True
        return removed

    async def clear_chat(self, chat_id: str) -> None:
        """
        Drop every ephemeral credential and delete the on-disk file for
        a chat. Used by delete_chat hooks if ever needed; today
        ProjectManager.delete_chat() removes the whole directory tree,
        which is already sufficient.
        """
        async with self._ephemeral_guard:
            self._ephemeral.pop(chat_id, None)

        path = self._auth_file(chat_id)
        if path.exists():
            try:
                path.unlink()
            except OSError as exc:
                logger.warning(
                    "Failed to remove chat auth file '%s': %s", path, exc
                )

    # ─────────────────────────────────────────────────────────────
    # Ephemeral helpers (private)
    # ─────────────────────────────────────────────────────────────

    def _peek_ephemeral(
        self, chat_id: str, server_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Synchronous peek used by read paths.

        Mutations to ``_ephemeral`` always happen under the async guard,
        but Python dict lookups are atomic under the GIL — so this is
        safe without an await. Kept synchronous so read paths (get_mode,
        get_env, ...) stay sync, matching the rest of the store.
        """
        bucket = self._ephemeral.get(chat_id)
        if not bucket:
            return None
        return bucket.get(server_id)

    async def _clear_ephemeral(self, chat_id: str, server_id: str) -> bool:
        async with self._ephemeral_guard:
            bucket = self._ephemeral.get(chat_id)
            if not bucket or server_id not in bucket:
                return False
            del bucket[server_id]
            if not bucket:
                self._ephemeral.pop(chat_id, None)
            return True


# ─────────────────────────────────────────────────────────────────
# Process-wide singleton
# ─────────────────────────────────────────────────────────────────

_singleton: Optional[ChatMcpAuthStore] = None


def get_chat_mcp_auth_store() -> ChatMcpAuthStore:
    """Return the process-wide ChatMcpAuthStore instance."""
    global _singleton
    if _singleton is None:
        _singleton = ChatMcpAuthStore()
    return _singleton


__all__ = [
    "ChatMcpAuthStore",
    "get_chat_mcp_auth_store",
    "MODE_CUSTOM",
    "MODE_USE_GLOBAL",
    "MODE_EPHEMERAL",
]
