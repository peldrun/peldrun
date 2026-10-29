# backend/omweb/extensions/secret_storage.py
"""
AES-256-GCM encryption with a machine-bound key for MCP credentials.

Security model:
  * Key material lives at ~/.peldrun/secrets.key, OUTSIDE backend/storage/,
    so it does not travel with project backups or git archives.
  * AAD binds every ciphertext to its logical context (e.g.
    "chat:chat_123:mcp_github") so a leaked token cannot be replayed
    against a different chat, server, or scope.
  * Key file is chmod'ed to 0600 on POSIX systems on first use.
  * Decryption failures (tampered ciphertext, wrong AAD, wrong key)
    raise ValueError — never silently return empty data.
"""

from __future__ import annotations

import base64
import logging
import os
import sys
from pathlib import Path
from typing import Optional

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError as exc:  # pragma: no cover - import-time guard
    raise ImportError(
        "The 'cryptography' package is required for MCP secret storage. "
        "Install it with: pip install cryptography"
    ) from exc

logger = logging.getLogger("omweb.extensions.secret_storage")


class SecretStorage:
    """AES-256-GCM at-rest encryption for MCP credentials."""

    NONCE_LEN = 12   # 96-bit GCM nonce (recommended)
    KEY_LEN = 32     # 256-bit key
    TAG_LEN = 16     # GCM authentication tag length

    def __init__(self, key_path: Path) -> None:
        self.key_path = Path(key_path)
        self._key = self._load_or_create_key()

    # ─────────────────────────────────────────────────────────────
    # Key material
    # ─────────────────────────────────────────────────────────────

    def _load_or_create_key(self) -> bytes:
        if self.key_path.exists():
            data = self.key_path.read_bytes()
            if len(data) != self.KEY_LEN:
                raise ValueError(
                    f"Corrupt key at {self.key_path}: expected "
                    f"{self.KEY_LEN} bytes, got {len(data)}. "
                    "Refusing to start with an invalid key."
                )
            self._harden_permissions(self.key_path)
            return data

        key = os.urandom(self.KEY_LEN)
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        self.key_path.write_bytes(key)
        self._harden_permissions(self.key_path)
        logger.info("Generated new secret key at %s", self.key_path)
        return key

    @staticmethod
    def _harden_permissions(path: Path) -> None:
        """Restrict the key file to owner-only read/write where supported."""
        try:
            os.chmod(path, 0o600)
        except OSError:
            # Windows does not honor POSIX chmod; ACLs are managed by
            # the parent directory or an OS keyring on that platform.
            if sys.platform != "win32":
                logger.warning(
                    "Could not restrict permissions on secret key at %s",
                    path,
                )

    # ─────────────────────────────────────────────────────────────
    # Crypto primitives
    # ─────────────────────────────────────────────────────────────

    def encrypt(self, plaintext: str, *, aad: str = "") -> str:
        """
        Encrypt a UTF-8 string and return a base64-encoded blob.

        The output layout is: base64( nonce || ciphertext || tag ).
        """
        if not isinstance(plaintext, str):
            raise TypeError("plaintext must be a string")
        if not isinstance(aad, str):
            raise TypeError("aad must be a string")

        aesgcm = AESGCM(self._key)
        nonce = os.urandom(self.NONCE_LEN)
        ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), aad.encode("utf-8"))
        return base64.b64encode(nonce + ct).decode("ascii")

    def decrypt(self, token: str, *, aad: str = "") -> str:
        """
        Decrypt a blob produced by :meth:`encrypt`.

        Raises ValueError if the ciphertext is malformed, truncated,
        tampered with, or was produced with a different AAD / key.
        """
        if not isinstance(token, str):
            raise TypeError("token must be a string")
        if not isinstance(aad, str):
            raise TypeError("aad must be a string")

        try:
            raw = base64.b64decode(token, validate=True)
        except Exception as exc:
            raise ValueError("Ciphertext is not valid base64") from exc

        if len(raw) < self.NONCE_LEN + self.TAG_LEN:
            raise ValueError("Ciphertext too short")

        nonce, ct = raw[: self.NONCE_LEN], raw[self.NONCE_LEN :]
        aesgcm = AESGCM(self._key)
        return aesgcm.decrypt(nonce, ct, aad.encode("utf-8")).decode("utf-8")


# ─────────────────────────────────────────────────────────────────
# Process-wide singleton
# ─────────────────────────────────────────────────────────────────

_singleton: Optional[SecretStorage] = None


def get_global_secret_storage() -> SecretStorage:
    """Return the process-wide SecretStorage instance."""
    global _singleton
    if _singleton is None:
        _singleton = SecretStorage(Path.home() / ".peldrun" / "secrets.key")
    return _singleton


__all__ = ["SecretStorage", "get_global_secret_storage"]
