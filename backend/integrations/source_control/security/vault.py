from __future__ import annotations

import ctypes
import os
import sys
import threading
from typing import Optional, Protocol, runtime_checkable

from ..domain.enums import SourceControlErrorCode
from ..errors import SourceControlError


class VaultError(SourceControlError):
    """Raised when an operation against the credential vault fails."""

    def __init__(
        self,
        message: str,
        *,
        details: Optional[dict[str, str]] = None,
    ) -> None:
        super().__init__(
            message=message,
            code=SourceControlErrorCode.PROVIDER_ERROR,
            retryable=False,
            details=details,
        )


@runtime_checkable
class CredentialVault(Protocol):
    """Abstract port for secure local credential and token storage."""

    def put(self, key: str, secret: str) -> None:
        """Stores a sensitive credential under the specified key."""
        ...

    def get(self, key: str) -> Optional[str]:
        """Retrieves a secret by key. Returns None if key does not exist."""
        ...

    def delete(self, key: str) -> bool:
        """Deletes a secret by key. Returns True if deleted, False if not found."""
        ...


class InMemoryCredentialVault:
    """Thread-safe in-memory credential vault intended for isolated test environments."""

    def __init__(self) -> None:
        self._secrets: dict[str, str] = {}
        self._lock = threading.Lock()

    def put(self, key: str, secret: str) -> None:
        if not key:
            raise VaultError("Vault key cannot be empty.")
        with self._lock:
            self._secrets[key] = secret

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            return self._secrets.get(key)

    def delete(self, key: str) -> bool:
        with self._lock:
            return self._secrets.pop(key, None) is not None

    def clear(self) -> None:
        """Removes all stored credentials (test teardown helper)."""
        with self._lock:
            self._secrets.clear()


class KeyringCredentialVault:
    """Credential vault utilizing the system keyring (Windows Credential Manager, Keychain, etc.)."""

    def __init__(self, service_name: str = "peldrun_source_control") -> None:
        self.service_name = service_name
        try:
            import keyring
            self._keyring = keyring
        except ImportError as exc:
            raise VaultError(
                "The 'keyring' package is not installed. Install keyring or configure native vault."
            ) from exc

    def put(self, key: str, secret: str) -> None:
        try:
            self._keyring.set_password(self.service_name, key, secret)
        except Exception as exc:
            raise VaultError(f"Failed to write credential to system keyring: {exc}") from exc

    def get(self, key: str) -> Optional[str]:
        try:
            return self._keyring.get_password(self.service_name, key)
        except Exception as exc:
            raise VaultError(f"Failed to read credential from system keyring: {exc}") from exc

    def delete(self, key: str) -> bool:
        try:
            self._keyring.delete_password(self.service_name, key)
            return True
        except Exception:
            return False


class WindowsNativeCredentialVault:
    """Native Windows Credential Manager integration via advapi32.dll without external dependencies."""

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2

    def __init__(self, prefix: str = "peldrun:sc:") -> None:
        if sys.platform != "win32":
            raise VaultError("WindowsNativeCredentialVault is only supported on Windows platforms.")
        self.prefix = prefix
        self._lock = threading.Lock()

    def _qualify_target(self, key: str) -> str:
        return f"{self.prefix}{key}"

    def put(self, key: str, secret: str) -> None:
        target_name = self._qualify_target(key)
        secret_bytes = secret.encode("utf-16-le")

        class CREDENTIALW(ctypes.Structure):
            _fields_ = [
                ("Flags", ctypes.c_ulong),
                ("Type", ctypes.c_ulong),
                ("TargetName", ctypes.c_wchar_p),
                ("Comment", ctypes.c_wchar_p),
                ("LastWritten", ctypes.c_ulonglong),
                ("CredentialBlobSize", ctypes.c_ulong),
                ("CredentialBlob", ctypes.c_void_p),
                ("Persist", ctypes.c_ulong),
                ("AttributeCount", ctypes.c_ulong),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", ctypes.c_wchar_p),
                ("UserName", ctypes.c_wchar_p),
            ]

        blob_buffer = ctypes.create_string_buffer(secret_bytes, len(secret_bytes))
        cred = CREDENTIALW()
        cred.Flags = 0
        cred.Type = self.CRED_TYPE_GENERIC
        cred.TargetName = target_name
        cred.Comment = "PELDRUN Source Control Credential"
        cred.CredentialBlobSize = len(secret_bytes)
        cred.CredentialBlob = ctypes.cast(blob_buffer, ctypes.c_void_p)
        cred.Persist = self.CRED_PERSIST_LOCAL_MACHINE
        cred.AttributeCount = 0
        cred.Attributes = None
        cred.TargetAlias = None
        cred.UserName = "PELDRUN"

        with self._lock:
            ret = ctypes.windll.advapi32.CredWriteW(ctypes.byref(cred), 0)
            if not ret:
                err = ctypes.GetLastError()
                raise VaultError(f"CredWriteW failed with Windows error code: {err}")

    def get(self, key: str) -> Optional[str]:
        target_name = self._qualify_target(key)

        class CREDENTIALW(ctypes.Structure):
            _fields_ = [
                ("Flags", ctypes.c_ulong),
                ("Type", ctypes.c_ulong),
                ("TargetName", ctypes.c_wchar_p),
                ("Comment", ctypes.c_wchar_p),
                ("LastWritten", ctypes.c_ulonglong),
                ("CredentialBlobSize", ctypes.c_ulong),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
                ("Persist", ctypes.c_ulong),
                ("AttributeCount", ctypes.c_ulong),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", ctypes.c_wchar_p),
                ("UserName", ctypes.c_wchar_p),
            ]

        pcred = ctypes.POINTER(CREDENTIALW)()
        with self._lock:
            ret = ctypes.windll.advapi32.CredReadW(
                target_name, self.CRED_TYPE_GENERIC, 0, ctypes.byref(pcred)
            )
            if not ret:
                return None
            try:
                blob_size = pcred.contents.CredentialBlobSize
                raw_bytes = ctypes.string_at(pcred.contents.CredentialBlob, blob_size)
                return raw_bytes.decode("utf-16-le")
            finally:
                ctypes.windll.advapi32.CredFree(pcred)

    def delete(self, key: str) -> bool:
        target_name = self._qualify_target(key)
        with self._lock:
            ret = ctypes.windll.advapi32.CredDeleteW(target_name, self.CRED_TYPE_GENERIC, 0)
            return bool(ret)


# ============================================================
# Vault Singleton and Factory Accessors
# ============================================================

_ACTIVE_VAULT: Optional[CredentialVault] = None
_VAULT_LOCK = threading.Lock()


def get_credential_vault() -> CredentialVault:
    """Returns the process-wide active CredentialVault implementation."""
    global _ACTIVE_VAULT
    if _ACTIVE_VAULT is not None:
        return _ACTIVE_VAULT

    with _VAULT_LOCK:
        if _ACTIVE_VAULT is not None:
            return _ACTIVE_VAULT

        # Check for explicit testing override via environment variable
        if os.environ.get("PELDRUN_TEST_VAULT") == "1":
            _ACTIVE_VAULT = InMemoryCredentialVault()
            return _ACTIVE_VAULT

        # Prefer standard keyring if available
        try:
            _ACTIVE_VAULT = KeyringCredentialVault()
            return _ACTIVE_VAULT
        except Exception:
            pass

        # Fallback to native Windows Credential Manager if on Windows
        if sys.platform == "win32":
            try:
                _ACTIVE_VAULT = WindowsNativeCredentialVault()
                return _ACTIVE_VAULT
            except Exception:
                pass

        # If running in development/headless environment without OS vault, log a warning
        # and provide InMemory vault with explicit notice.
        _ACTIVE_VAULT = InMemoryCredentialVault()
        return _ACTIVE_VAULT


def set_credential_vault(vault: CredentialVault) -> None:
    """Explicitly injects a credential vault (used during tests or custom setup)."""
    global _ACTIVE_VAULT
    with _VAULT_LOCK:
        _ACTIVE_VAULT = vault
