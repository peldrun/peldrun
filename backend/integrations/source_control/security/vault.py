# backend/integrations/source_control/security/vault.py
"""
Credential vault implementations for the Source Control Integration
subsystem.

The vault is the ONLY component permitted to persist raw secrets
(access tokens, refresh tokens, PATs, private keys). Every other
component references secrets by `vault_key` and obtains the raw value
only through the vault API.

Three implementations are provided:

    - `InMemoryVault`  : for tests and development only.
    - `KeyringVault`   : OS-native credential storage (Windows
                         Credential Manager, macOS Keychain, Linux
                         Secret Service). This is the production
                         implementation.
    - `FallbackVault`  : wraps an ordered list of vaults and uses the
                         first one that reports itself available.

Design rules:
    - A vault failure MUST NOT fall back to plaintext storage. If no
      secure backend is available in production, the subsystem MUST
      refuse to operate.
    - Secrets MUST be redacted in any log or error message produced by
      the vault.
    - The default vault singleton is selected by environment. Tests
      and development use `InMemoryVault`; production requires a real
      OS keychain.
    - The vault never exposes the full secret in its `__repr__` or in
      error details.

Naming:
    - `get_default_vault` / `set_default_vault` are the canonical
      singleton accessors.
    - `get_credential_vault` / `set_credential_vault` are
      backward-compatibility aliases used by the package `__init__`
      and by callers that prefer the more explicit name. Both pairs
      operate on the same process-wide singleton.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, Iterable, Iterator, Mapping, Optional, Sequence

from ..errors import ProviderError, ValidationError
from ..ports.auth import CredentialVaultPort, make_vault_key

__all__ = [
    "CredentialVault",
    "VaultError",
    "VaultHealth",
    "InMemoryVault",
    "InMemoryCredentialVault",
    "KeyringVault",
    "KeyringCredentialVault",
    "WindowsNativeCredentialVault",
    "FallbackVault",
    "get_default_vault",
    "set_default_vault",
    "reset_default_vault",
    "get_credential_vault",
    "set_credential_vault",
]


logger = logging.getLogger(__name__)


#: Every key managed by this subsystem MUST start with this prefix.
#: The prefix prevents collisions with other applications that share
#: the same OS keychain.
VAULT_KEY_PREFIX = "peldrun.sc."


# ---------------------------------------------------------------------------
# Exceptions and diagnostics
# ---------------------------------------------------------------------------


class VaultError(ProviderError):
    """
    Raised when the underlying credential store fails.

    The message MUST be pre-redacted. The `details` mapping MUST NOT
    contain the secret value.
    """

    default_code = None  # ProviderError default is used
    default_retryable = False
    default_http_status = 500


@dataclass(frozen=True)
class VaultHealth:
    """
    Diagnostic snapshot of a vault.

    Safe to expose in a status endpoint; contains no secret material.
    """

    backend: str
    available: bool
    key_count: Optional[int] = None
    message: Optional[str] = None
    metadata: Mapping[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class CredentialVault(ABC):
    """
    Abstract credential vault.

    Subclasses implement the four primitive operations (`put`, `get`,
    `delete`, `exists`). The base class provides convenience methods
    and enforces key-prefix policy.
    """

    #: Human-readable backend name used in diagnostics.
    backend_name: str = "unknown"

    # --- Primitive operations ------------------------------------------

    @abstractmethod
    def _put(self, key: str, secret: str) -> None:
        """Backend-specific storage. MUST be called with a validated key."""
        raise NotImplementedError

    @abstractmethod
    def _get(self, key: str) -> Optional[str]:
        """Backend-specific retrieval. MUST return None for absent keys."""
        raise NotImplementedError

    @abstractmethod
    def _delete(self, key: str) -> None:
        """Backend-specific deletion. MUST be a no-op for absent keys."""
        raise NotImplementedError

    @abstractmethod
    def _exists(self, key: str) -> bool:
        """Backend-specific existence check."""
        raise NotImplementedError

    # --- Public API (matches CredentialVaultPort) ----------------------

    def put(self, key: str, secret: str) -> None:
        """
        Store `secret` under `key`, overwriting any previous value.

        Raises:
            ValidationError: If `key` or `secret` is empty, or if the
                key does not use the required prefix.
            VaultError: If the backend rejects the operation.
        """
        validated_key = self._validate_key(key)
        if not secret:
            raise ValidationError("Secret value must not be empty.")
        try:
            self._put(validated_key, secret)
        except VaultError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise VaultError(
                f"Vault '{self.backend_name}' failed to store a secret.",
                details={
                    "backend": self.backend_name,
                    "key": validated_key,
                    "reason": type(exc).__name__,
                },
                cause=exc,
            ) from exc

    def get(self, key: str) -> Optional[str]:
        """
        Return the secret stored under `key`, or `None` if absent.

        Raises:
            ValidationError: If `key` does not use the required prefix.
            VaultError: If the backend rejects the lookup.
        """
        validated_key = self._validate_key(key)
        try:
            return self._get(validated_key)
        except VaultError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise VaultError(
                f"Vault '{self.backend_name}' failed to read a secret.",
                details={
                    "backend": self.backend_name,
                    "key": validated_key,
                    "reason": type(exc).__name__,
                },
                cause=exc,
            ) from exc

    def delete(self, key: str) -> None:
        """
        Remove the secret stored under `key`.

        Deleting a non-existent key is a no-op. Raises `ValidationError`
        for an invalid key and `VaultError` for a backend failure.
        """
        validated_key = self._validate_key(key)
        try:
            self._delete(validated_key)
        except VaultError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise VaultError(
                f"Vault '{self.backend_name}' failed to delete a secret.",
                details={
                    "backend": self.backend_name,
                    "key": validated_key,
                    "reason": type(exc).__name__,
                },
                cause=exc,
            ) from exc

    def exists(self, key: str) -> bool:
        """
        Return True if a secret is stored under `key`.

        Raises `ValidationError` for an invalid key.
        """
        validated_key = self._validate_key(key)
        try:
            return bool(self._exists(validated_key))
        except VaultError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise VaultError(
                f"Vault '{self.backend_name}' failed to check a key.",
                details={
                    "backend": self.backend_name,
                    "key": validated_key,
                    "reason": type(exc).__name__,
                },
                cause=exc,
            ) from exc

    # --- Convenience ----------------------------------------------------

    def get_or_raise(self, key: str) -> str:
        """
        Return the secret, raising `VaultError` if absent.

        Use this when a missing secret is a programming error rather
        than a legitimate state.
        """
        value = self.get(key)
        if value is None:
            raise VaultError(
                f"No secret stored for key '{key}'.",
                details={"backend": self.backend_name, "key": key},
            )
        return value

    def put_many(self, items: Mapping[str, str]) -> None:
        """Store multiple secrets atomically (best-effort)."""
        for key, value in items.items():
            self.put(key, value)

    def delete_many(self, keys: Iterable[str]) -> None:
        """Delete multiple secrets (best-effort)."""
        for key in keys:
            self.delete(key)

    def is_available(self) -> bool:
        """
        Return True if the backend is operational.

        Default implementation returns `True`. Subclasses that depend
        on an external library or OS service override this method.
        """
        return True

    def health(self) -> VaultHealth:
        """
        Return a diagnostic snapshot of the vault.

        The default implementation reports availability only. Subclasses
        may add key counts or backend metadata.
        """
        return VaultHealth(
            backend=self.backend_name,
            available=self.is_available(),
        )

    # --- Validation helpers --------------------------------------------

    @staticmethod
    def _validate_key(key: str) -> str:
        """
        Validate and normalize a vault key.

        Keys are trimmed and MUST start with `peldrun.sc.`. The prefix
        prevents collisions with unrelated applications that may use
        the same OS keychain.
        """
        if not key or not isinstance(key, str):
            raise ValidationError("Vault key must be a non-empty string.")
        trimmed = key.strip()
        if not trimmed:
            raise ValidationError("Vault key must not be blank.")
        if not trimmed.startswith(VAULT_KEY_PREFIX):
            raise ValidationError(
                f"Vault key must start with '{VAULT_KEY_PREFIX}'.",
                details={"key": trimmed},
            )
        return trimmed

    # --- Presentation ---------------------------------------------------

    def __repr__(self) -> str:  # pragma: no cover - trivial formatting
        return f"<{type(self).__name__} backend={self.backend_name!r}>"


# ---------------------------------------------------------------------------
# In-memory implementation (tests and development)
# ---------------------------------------------------------------------------


class InMemoryVault(CredentialVault):
    """
    In-memory vault for tests and development.

    The contents are lost when the process exits. This implementation
    MUST NOT be used in production because it does not persist secrets
    across restarts and does not protect them from other processes.
    """

    backend_name = "memory"

    def __init__(self, initial: Optional[Mapping[str, str]] = None) -> None:
        self._store: Dict[str, str] = {}
        self._lock = threading.RLock()
        if initial:
            for key, value in initial.items():
                self.put(key, value)

    def _put(self, key: str, secret: str) -> None:
        with self._lock:
            self._store[key] = secret

    def _get(self, key: str) -> Optional[str]:
        with self._lock:
            return self._store.get(key)

    def _delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def _exists(self, key: str) -> bool:
        with self._lock:
            return key in self._store

    def clear(self) -> None:
        """Remove every secret. Intended for tests."""
        with self._lock:
            self._store.clear()

    def keys(self) -> Sequence[str]:
        """Return a snapshot of the stored keys."""
        with self._lock:
            return tuple(sorted(self._store.keys()))

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)

    def __iter__(self) -> Iterator[str]:
        return iter(self.keys())

    def health(self) -> VaultHealth:
        with self._lock:
            return VaultHealth(
                backend=self.backend_name,
                available=True,
                key_count=len(self._store),
            )


# ---------------------------------------------------------------------------
# Keyring implementation (production)
# ---------------------------------------------------------------------------


class KeyringVault(CredentialVault):
    """
    OS-native credential vault backed by the `keyring` library.

    Supports Windows Credential Manager, macOS Keychain, and Linux
    Secret Service (via `libsecret` or `kwallet`). The `keyring`
    package is imported lazily so that PELDRUN can start even when the
    library is not installed; in that case `is_available()` returns
    False.

    Storage layout
    --------------
    Each secret is stored as a ``(service, username)`` pair:

        service  = ``peldrun.sc.{kind}`` where ``{kind}`` is the
                   token kind segment of the vault key (e.g.
                   ``oauth``, ``pat``).
        username = ``{provider}.{base_url}.{account_id}`` derived
                   from the remainder of the vault key.

    The service groups secrets by token kind, while the username
    uniquely identifies the account (including the provider and the
    instance base URL). Two entries under different ``kind`` values
    never collide because they use different service names; two
    entries under the same ``kind`` never collide because their
    usernames differ in the ``provider``, ``base_url``, or
    ``account_id`` segments.
    """

    backend_name = "keyring"

    def __init__(self, service_prefix: str = "peldrun.sc") -> None:
        self._service_prefix = service_prefix
        self._keyring = self._import_keyring()

    # --- Backend availability -------------------------------------------

    @staticmethod
    def _import_keyring():
        """Attempt to import `keyring`; return None if unavailable."""
        try:
            import keyring  # type: ignore[import-not-found]
        except Exception:  # noqa: BLE001
            logger.warning(
                "keyring library is not installed; secure vault is "
                "unavailable."
            )
            return None
        return keyring

    def is_available(self) -> bool:
        """Return True if `keyring` is installed and has a backend."""
        if self._keyring is None:
            return False
        try:
            backend = self._keyring.get_keyring()
        except Exception:  # noqa: BLE001
            return False
        # `fail.Keyring` is returned when no real backend exists.
        backend_name = type(backend).__name__.lower()
        if "fail" in backend_name:
            return False
        return True

    # --- Backend operations ---------------------------------------------

    def _split_key(self, key: str) -> tuple[str, str]:
        """
        Split a validated key into `(service, username)`.

        The key format produced by `make_vault_key` is
        ``peldrun.sc.{kind}.{provider}.{base_url}.{account_id}``.
        This method returns:

            service  = ``peldrun.sc.{kind}``  (first three segments)
            username = ``{provider}.{base_url}.{account_id}``
                       (everything after the third segment)

        If the key does not have at least three dot-separated
        segments after the prefix, the whole key becomes the
        username under the default service prefix. This is a
        defensive fallback for keys produced by future ``kind``
        variants; it is not expected in normal operation.
        """
        parts = key.split(".")
        if len(parts) >= 4:
            service = ".".join(parts[:3])
            username = ".".join(parts[3:])
        else:
            service = self._service_prefix
            username = key
        return service, username

    def _put(self, key: str, secret: str) -> None:
        if self._keyring is None:
            raise VaultError(
                "Secure vault is unavailable: keyring library missing.",
                details={"backend": self.backend_name},
            )
        service, username = self._split_key(key)
        self._keyring.set_password(service, username, secret)

    def _get(self, key: str) -> Optional[str]:
        if self._keyring is None:
            raise VaultError(
                "Secure vault is unavailable: keyring library missing.",
                details={"backend": self.backend_name},
            )
        service, username = self._split_key(key)
        return self._keyring.get_password(service, username)

    def _delete(self, key: str) -> None:
        """
        Delete the secret, tolerating only the "entry does not exist"
        case as a no-op.

        The keyring library raises a backend-specific exception (for
        example ``keyring.errors.PasswordDeleteError``) when the
        entry is missing, but real backend failures — network errors,
        permission denials, or a broken keychain service — raise
        different exception types and MUST propagate so the caller
        can detect that a credential was NOT actually removed.

        The previous implementation caught every ``Exception`` and
        silently ignored it, which meant a failed revocation left the
        token in the vault while the caller believed the operation
        had succeeded. The new implementation checks existence first
        and only skips ``delete_password`` when the entry is
        positively confirmed absent.
        """
        if self._keyring is None:
            raise VaultError(
                "Secure vault is unavailable: keyring library missing.",
                details={"backend": self.backend_name},
            )
        service, username = self._split_key(key)
        # ``get_password`` returns ``None`` for missing entries and
        # raises for real backend failures; both are the signals we
        # need to distinguish "nothing to delete" from "deletion
        # might actually fail".
        existing = self._keyring.get_password(service, username)
        if existing is None:
            return
        self._keyring.delete_password(service, username)

    def _exists(self, key: str) -> bool:
        return self._get(key) is not None

    def health(self) -> VaultHealth:
        try:
            backend = self._keyring.get_keyring() if self._keyring else None
        except Exception:  # noqa: BLE001
            backend = None
        backend_name = type(backend).__name__ if backend else None
        return VaultHealth(
            backend=self.backend_name,
            available=self.is_available(),
            metadata={"keyring_backend": backend_name},
        )


# ---------------------------------------------------------------------------
# Encrypted File Vault (Cross-Platform Fallback)
# ---------------------------------------------------------------------------

class EncryptedFileVault(CredentialVault):
    """
    Cross-platform fallback vault that encrypts secrets locally.
    Used when the OS keyring is unavailable, ensuring data is never
    stored in plaintext on Linux or headless environments.
    """
    backend_name = "encrypted-file"

    def __init__(self, storage_path: str = "data/source_control/secrets.enc") -> None:
        from pathlib import Path
        from cryptography.fernet import Fernet

        self._path = Path(storage_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

        key_path = self._path.parent / ".vault_key"
        if key_path.exists():
            self._key = key_path.read_bytes()
        else:
            self._key = Fernet.generate_key()
            key_path.write_bytes(self._key)
            try:
                import os
                os.chmod(key_path, 0o600) # Secure permissions
            except Exception:
                pass

        self._fernet = Fernet(self._key)
        self._data = self._load()

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            decrypted = self._fernet.decrypt(self._path.read_bytes())
            import json
            return json.loads(decrypted.decode("utf-8"))
        except Exception:
            return {}

    def _save(self) -> None:
        import json
        encrypted = self._fernet.encrypt(json.dumps(self._data).encode("utf-8"))
        self._path.write_bytes(encrypted)

    def _put(self, key: str, secret: str) -> None:
        self._data[key] = secret
        self._save()

    def _get(self, key: str) -> Optional[str]:
        return self._data.get(key)

    def _delete(self, key: str) -> None:
        if key in self._data:
            del self._data[key]
            self._save()

    def _exists(self, key: str) -> bool:
        return key in self._data

    def keys(self) -> Sequence[str]:
        """Return a snapshot of the stored keys."""
        return tuple(sorted(self._data.keys()))

    def is_available(self) -> bool:
        return True # Always available if cryptography is installed


# ---------------------------------------------------------------------------
# Fallback vault
# ---------------------------------------------------------------------------


class FallbackVault(CredentialVault):
    """
    Vault that wraps an ordered list of vaults.

    The first vault that reports itself available is used for writes.
    Reads consult every available vault in order, so a secret written
    by an earlier preferred backend remains readable after the
    preference changes. If none of the wrapped vaults is available,
    every operation raises `VaultError`. This class NEVER falls back
    to an insecure backend.
    """

    backend_name = "fallback"

    def __init__(self, vaults: Sequence[CredentialVault]) -> None:
        if not vaults:
            raise ValidationError(
                "FallbackVault requires at least one wrapped vault."
            )
        for index, vault in enumerate(vaults):
            if not isinstance(vault, CredentialVault):
                raise ValidationError(
                    "FallbackVault requires every wrapped object to be "
                    "a CredentialVault instance.",
                    details={
                        "index": index,
                        "type": type(vault).__name__,
                    },
                )
        self._vaults = tuple(vaults)

    def _active(self) -> CredentialVault:
        for vault in self._vaults:
            if vault.is_available():
                return vault
        raise VaultError(
            "No secure vault backend is available.",
            details={"backends": [v.backend_name for v in self._vaults]},
        )

    def _put(self, key: str, secret: str) -> None:
        self._active().put(key, secret)

    def _get(self, key: str) -> Optional[str]:
        for vault in self._vaults:
            if not vault.is_available():
                continue
            value = vault.get(key)
            if value is not None:
                return value
        return None

    def _delete(self, key: str) -> None:
        """
        Best-effort deletion across every available wrapped vault.

        A failure in one vault MUST NOT prevent the remaining vaults
        from being cleaned up. The first captured exception is
        re-raised after every vault has been visited so that callers
        can observe that at least one backend failed to delete the
        secret.
        """
        first_error: Optional[BaseException] = None
        visited = 0
        for vault in self._vaults:
            if not vault.is_available():
                continue
            visited += 1
            try:
                vault.delete(key)
            except Exception as exc:  # noqa: BLE001
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error
        if visited == 0:
            raise VaultError(
                "No secure vault backend is available.",
                details={
                    "backends": [v.backend_name for v in self._vaults],
                },
            )

    def _exists(self, key: str) -> bool:
        return self._get(key) is not None

    def keys(self) -> Sequence[str]:
        """Return a merged snapshot of keys from all available vaults."""
        all_keys = set()
        for vault in self._vaults:
            if vault.is_available() and hasattr(vault, "keys"):
                try:
                    all_keys.update(vault.keys())
                except Exception:
                    pass
        return tuple(sorted(all_keys))

    def is_available(self) -> bool:
        return any(v.is_available() for v in self._vaults)

    def health(self) -> VaultHealth:
        return VaultHealth(
            backend=self.backend_name,
            available=self.is_available(),
            metadata={
                "wrapped": [v.health().backend for v in self._vaults],
            },
        )


# ---------------------------------------------------------------------------
# Default vault singleton
# ---------------------------------------------------------------------------


_DEFAULT_VAULT: Optional[CredentialVault] = None
_DEFAULT_LOCK = threading.RLock()


#: Environment names accepted by `get_default_vault`.
_ENV_PRODUCTION = "production"
_ENV_DEVELOPMENT = "development"
_ENV_TEST = "test"


def _build_default_vault(environment: str) -> CredentialVault:
    """
    Construct the default vault for the given environment.

    Rules:
        - `production`: use `KeyringVault`. If unavailable, raise.
        - `development`: use a `FallbackVault` that prefers
          `KeyringVault` and degrades to `InMemoryVault`. A warning is
          logged when the fallback is used.
        - `test`: use `InMemoryVault`.
    """
    env = (environment or _ENV_DEVELOPMENT).strip().lower()

    if env == _ENV_TEST:
        return InMemoryVault()

    if env == _ENV_PRODUCTION:
        keyring_vault = KeyringVault()
        if not keyring_vault.is_available():
            raise VaultError(
                "A secure credential vault is required in production "
                "but no OS keychain backend is available.",
                details={"environment": env},
            )
        return keyring_vault

    # Development: prefer keyring, fallback to encrypted file, then memory.
    keyring_vault = KeyringVault()

    # Resolve absolute path to backend/data/source_control/secrets.enc
    from pathlib import Path
    backend_root = Path(__file__).resolve().parent.parent.parent.parent
    storage_path = backend_root / "data" / "source_control" / "secrets.enc"

    encrypted_vault = EncryptedFileVault(storage_path=str(storage_path))
    fallback = FallbackVault([keyring_vault, encrypted_vault, InMemoryVault()])

    if not keyring_vault.is_available():
        logger.warning(
            "No OS keychain available; falling back to EncryptedFileVault. "
            "Secrets will persist securely across restarts."
        )
    return fallback


def get_default_vault(
    environment: Optional[str] = None,
) -> CredentialVault:
    """
    Return the process-wide default vault, creating it on first use.

    The `environment` argument is honored only on the first call. It
    defaults to the `PELDRUN_ENV` environment variable, or
    `"development"` when unset.
    """
    global _DEFAULT_VAULT
    with _DEFAULT_LOCK:
        if _DEFAULT_VAULT is None:
            resolved_env = (
                environment
                or os.environ.get("PELDRUN_ENV")
                or _ENV_DEVELOPMENT
            )
            _DEFAULT_VAULT = _build_default_vault(resolved_env)
        return _DEFAULT_VAULT


def set_default_vault(vault: CredentialVault) -> None:
    """
    Replace the process-wide default vault.

    Intended for tests and for embedding scenarios that supply their
    own vault implementation.
    """
    if not isinstance(vault, CredentialVault):
        raise TypeError(
            "set_default_vault expects a CredentialVault instance."
        )
    global _DEFAULT_VAULT
    with _DEFAULT_LOCK:
        _DEFAULT_VAULT = vault


def reset_default_vault(
    environment: Optional[str] = None,
) -> CredentialVault:
    """
    Discard the current default vault and create a fresh one.

    Used by tests to guarantee isolation. Returns the new vault for
    convenience.
    """
    global _DEFAULT_VAULT
    with _DEFAULT_LOCK:
        _DEFAULT_VAULT = _build_default_vault(
            environment or _ENV_TEST
        )
        return _DEFAULT_VAULT


# ---------------------------------------------------------------------------
# Backward-compatibility aliases
# ---------------------------------------------------------------------------
#
# The package `__init__` and some callers prefer the name
# `get_credential_vault` / `set_credential_vault`, which is more
# explicit about what the vault stores. Both pairs operate on the
# SAME process-wide singleton: calling `get_credential_vault()` after
# `set_default_vault(v)` returns `v`.

get_credential_vault = get_default_vault
set_credential_vault = set_default_vault


# ---------------------------------------------------------------------------
# Backward-compatibility aliases for the vault implementations
# ---------------------------------------------------------------------------
#
# The class names below were used by earlier callers and remain part
# of the public API. They refer to the SAME class as their canonical
# counterpart, so `isinstance` and `type()` checks are interchangeable.

#: Alias for `InMemoryVault`.
#:
#: The longer name emphasizes that the vault stores credentials.
InMemoryCredentialVault = InMemoryVault

#: Alias for `KeyringVault`.
KeyringCredentialVault = KeyringVault


class WindowsNativeCredentialVault(KeyringVault):
    """
    Windows-native credential vault.

    On Windows, the `keyring` library delegates to the Windows
    Credential Manager, which is the OS-native secure store. This
    class exists as a named specialization so that callers can
    explicitly request the Windows backend without depending on the
    `keyring` library's runtime backend selection.

    On non-Windows platforms, ``is_available()`` returns ``False``
    because the class contract is specifically to provide a
    Windows-native store. Callers that need a cross-platform vault
    SHOULD use ``KeyringVault`` directly.
    """

    backend_name = "windows-native"

    def is_available(self) -> bool:
        """Return True only when running on Windows with a real backend."""
        if sys.platform != "win32":
            return False
        return super().is_available()
