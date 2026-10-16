from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from typing import Any, Optional, Sequence

from ..domain.enums import ProviderId
from ..errors import AuthenticationExpiredError, AuthenticationRequiredError
from ..ports.auth import AuthProviderPort, AuthorizationResult
from .vault import CredentialVault, get_credential_vault


class _AwaitableValue:
    """Helper enabling methods to be invoked either synchronously or with an await expression."""

    def __init__(self, value: Any = None) -> None:
        self._value = value

    def __await__(self):
        async def _resolve():
            return self._value
        return _resolve().__await__()


class TokenService:
    """Thread-safe service managing token persistence, validation, proactive refresh, and vault lifecycle."""

    def __init__(
        self,
        vault: Optional[CredentialVault] = None,
        clock_skew_seconds: int = 300,
    ) -> None:
        self._vault = vault or get_credential_vault()
        self.clock_skew_seconds = clock_skew_seconds
        self._lock = threading.Lock()

    @staticmethod
    def _key_prefix(provider_id: ProviderId, account_id: Optional[str] = None) -> str:
        clean_account = (account_id or "default").strip()
        return f"{provider_id.value}:{clean_account}"

    def _access_token_key(self, provider_id: ProviderId, account_id: Optional[str] = None) -> str:
        return f"{self._key_prefix(provider_id, account_id)}:access"

    def _refresh_token_key(self, provider_id: ProviderId, account_id: Optional[str] = None) -> str:
        return f"{self._key_prefix(provider_id, account_id)}:refresh"

    def _metadata_key(self, provider_id: ProviderId, account_id: Optional[str] = None) -> str:
        return f"{self._key_prefix(provider_id, account_id)}:meta"

    def save_tokens(
        self,
        provider_id: ProviderId,
        account_id: Optional[str] = None,
        auth_result: Optional[AuthorizationResult] = None,
        *,
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        expires_at: Optional[datetime] = None,
        granted_scopes: Sequence[str] = (),
    ) -> Any:
        """Securely stores access and refresh tokens in vault (supports both sync and await invocation)."""
        effective_access = access_token
        effective_refresh = refresh_token
        effective_expires = expires_at
        effective_scopes = list(granted_scopes)
        token_type = "Bearer"

        if auth_result is not None:
            effective_access = auth_result.access_token
            effective_refresh = auth_result.refresh_token or effective_refresh
            effective_expires = auth_result.expires_at or effective_expires
            effective_scopes = list(auth_result.granted_scopes) or effective_scopes
            token_type = auth_result.token_type

        if not effective_access:
            raise ValueError("An access_token is required to store credentials.")

        clean_account = account_id or "default"

        with self._lock:
            # 1. Primary qualified storage key
            self._vault.put(self._access_token_key(provider_id, clean_account), effective_access)
            # Secondary generic fallback lookup key
            self._vault.put(f"{provider_id.value}:access", effective_access)

            if effective_refresh:
                self._vault.put(self._refresh_token_key(provider_id, clean_account), effective_refresh)
                self._vault.put(f"{provider_id.value}:refresh", effective_refresh)

            meta: dict[str, Any] = {
                "token_type": token_type,
                "expires_at": effective_expires.isoformat() if effective_expires else None,
                "granted_scopes": effective_scopes,
                "updated_at": datetime.utcnow().isoformat(),
            }
            self._vault.put(self._metadata_key(provider_id, clean_account), json.dumps(meta))
            self._vault.put(f"{provider_id.value}:meta", json.dumps(meta))

        return _AwaitableValue(None)

    def get_token_metadata(
        self, provider_id: ProviderId, account_id: Optional[str] = None
    ) -> Optional[dict[str, Any]]:
        """Retrieves non-secret token metadata from vault."""
        clean_account = account_id or "default"
        raw_meta = self._vault.get(self._metadata_key(provider_id, clean_account))
        if not raw_meta:
            raw_meta = self._vault.get(f"{provider_id.value}:meta")
        if not raw_meta:
            return None
        try:
            return json.loads(raw_meta)
        except Exception:
            return None

    def get_refresh_token(
        self, provider_id: ProviderId, account_id: Optional[str] = None
    ) -> Optional[str]:
        """Retrieves the stored refresh token for the account, if present."""
        clean_account = account_id or "default"
        token = self._vault.get(self._refresh_token_key(provider_id, clean_account))
        if not token:
            token = self._vault.get(f"{provider_id.value}:refresh")
        return token

    def is_token_expired(
        self, provider_id: ProviderId, account_id: Optional[str] = None
    ) -> bool:
        """Determines whether the token is expired or approaching expiry."""
        meta = self.get_token_metadata(provider_id, account_id)
        if not meta or not meta.get("expires_at"):
            return False

        try:
            expires_at = datetime.fromisoformat(meta["expires_at"])
            skew = timedelta(seconds=self.clock_skew_seconds)
            return datetime.utcnow() + skew >= expires_at
        except Exception:
            return False

    async def get_access_token(
        self,
        provider_id: ProviderId,
        account_id: Optional[str] = None,
        auth_port: Optional[AuthProviderPort] = None,
    ) -> str:
        """Retrieves a valid access token, performing proactive refresh if expired and possible."""
        clean_account = account_id or "default"
        access_token = self._vault.get(self._access_token_key(provider_id, clean_account))
        if not access_token:
            access_token = self._vault.get(f"{provider_id.value}:access")

        if not access_token:
            raise AuthenticationRequiredError(
                f"No credentials found for provider '{provider_id.value}' and account '{clean_account}'.",
                provider=provider_id.value,
            )

        if not self.is_token_expired(provider_id, clean_account):
            return access_token

        refresh_token = self.get_refresh_token(provider_id, clean_account)
        if refresh_token and auth_port:
            try:
                refreshed_result = await auth_port.refresh_access_token(refresh_token)
                self.save_tokens(provider_id, clean_account, refreshed_result)
                return refreshed_result.access_token
            except Exception as exc:
                raise AuthenticationExpiredError(
                    f"Session expired and automatic token refresh failed for provider '{provider_id.value}': {exc}",
                    provider=provider_id.value,
                ) from exc

        raise AuthenticationExpiredError(
            f"Access token for provider '{provider_id.value}' has expired. Re-authentication required.",
            provider=provider_id.value,
        )

    def revoke_tokens(
        self,
        provider_id: ProviderId,
        account_id: Optional[str] = None,
        auth_port: Optional[AuthProviderPort] = None,
    ) -> Any:
        """Revokes token access and deletes vault secrets."""
        clean_account = account_id or "default"
        with self._lock:
            self._vault.delete(self._access_token_key(provider_id, clean_account))
            self._vault.delete(self._refresh_token_key(provider_id, clean_account))
            self._vault.delete(self._metadata_key(provider_id, clean_account))
            self._vault.delete(f"{provider_id.value}:access")
            self._vault.delete(f"{provider_id.value}:refresh")
            self._vault.delete(f"{provider_id.value}:meta")

        return _AwaitableValue(True)

    async def delete_tokens(
        self,
        provider_id: ProviderId,
        account_id: Optional[str] = None,
        auth_port: Optional[AuthProviderPort] = None,
    ) -> bool:
        """Revokes token access remotely where supported and cleans all vault secrets."""
        clean_account = account_id or "default"
        if auth_port:
            token = (
                self._vault.get(self._access_token_key(provider_id, clean_account))
                or self._vault.get(f"{provider_id.value}:access")
            )
            if token:
                try:
                    await auth_port.revoke_token(token)
                except Exception:
                    pass

        self.revoke_tokens(provider_id, clean_account)
        return True

    def has_token(self, provider_id: ProviderId, account_id: Optional[str] = None) -> bool:
        """Checks whether credentials exist in the vault for the specified provider."""
        clean_account = account_id or "default"
        return (
            self._vault.get(self._access_token_key(provider_id, clean_account)) is not None
            or self._vault.get(f"{provider_id.value}:access") is not None
        )


# ============================================================
# TokenService Singleton Accessors
# ============================================================

_DEFAULT_TOKEN_SERVICE: Optional[TokenService] = None
_SERVICE_LOCK = threading.Lock()


def get_default_token_service() -> TokenService:
    """Returns the process-wide default TokenService singleton."""
    global _DEFAULT_TOKEN_SERVICE
    if _DEFAULT_TOKEN_SERVICE is None:
        with _SERVICE_LOCK:
            if _DEFAULT_TOKEN_SERVICE is None:
                _DEFAULT_TOKEN_SERVICE = TokenService()
    return _DEFAULT_TOKEN_SERVICE


def set_default_token_service(service: TokenService) -> None:
    """Explicitly injects a TokenService instance (used during testing)."""
    global _DEFAULT_TOKEN_SERVICE
    with _SERVICE_LOCK:
        _DEFAULT_TOKEN_SERVICE = service


def reset_default_token_service() -> None:
    """Resets the process-wide default TokenService (used during testing)."""
    global _DEFAULT_TOKEN_SERVICE
    with _SERVICE_LOCK:
        _DEFAULT_TOKEN_SERVICE = None


__all__ = [
    "TokenService",
    "get_default_token_service",
    "set_default_token_service",
    "reset_default_token_service",
]
