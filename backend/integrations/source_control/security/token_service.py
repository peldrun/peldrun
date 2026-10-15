from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from typing import Any, Optional

from ..domain.enums import ProviderId
from ..errors import AuthenticationExpiredError, AuthenticationRequiredError
from ..ports.auth import AuthProviderPort, AuthorizationResult
from .vault import CredentialVault, get_credential_vault


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
    def _key_prefix(provider_id: ProviderId, account_id: str) -> str:
        clean_account = account_id.strip()
        return f"{provider_id.value}:{clean_account}"

    def _access_token_key(self, provider_id: ProviderId, account_id: str) -> str:
        return f"{self._key_prefix(provider_id, account_id)}:access"

    def _refresh_token_key(self, provider_id: ProviderId, account_id: str) -> str:
        return f"{self._key_prefix(provider_id, account_id)}:refresh"

    def _metadata_key(self, provider_id: ProviderId, account_id: str) -> str:
        return f"{self._key_prefix(provider_id, account_id)}:meta"

    def save_tokens(
        self,
        provider_id: ProviderId,
        account_id: str,
        auth_result: AuthorizationResult,
    ) -> None:
        """Securely stores access and refresh tokens along with expiry metadata in the credential vault."""
        with self._lock:
            # 1. Store primary access token
            self._vault.put(
                self._access_token_key(provider_id, account_id),
                auth_result.access_token,
            )

            # 2. Store optional refresh token
            if auth_result.refresh_token:
                self._vault.put(
                    self._refresh_token_key(provider_id, account_id),
                    auth_result.refresh_token,
                )

            # 3. Store serializable metadata (expires_at, token_type, granted_scopes)
            meta: dict[str, Any] = {
                "token_type": auth_result.token_type,
                "expires_at": (
                    auth_result.expires_at.isoformat()
                    if auth_result.expires_at
                    else None
                ),
                "granted_scopes": list(auth_result.granted_scopes),
                "updated_at": datetime.utcnow().isoformat(),
            }
            self._vault.put(
                self._metadata_key(provider_id, account_id),
                json.dumps(meta),
            )

    def get_token_metadata(
        self, provider_id: ProviderId, account_id: str
    ) -> Optional[dict[str, Any]]:
        """Retrieves non-secret token metadata (expiry, scopes) from the vault."""
        raw_meta = self._vault.get(self._metadata_key(provider_id, account_id))
        if not raw_meta:
            return None
        try:
            return json.loads(raw_meta)
        except Exception:
            return None

    def get_refresh_token(
        self, provider_id: ProviderId, account_id: str
    ) -> Optional[str]:
        """Retrieves the stored refresh token for the account, if present."""
        return self._vault.get(self._refresh_token_key(provider_id, account_id))

    def is_token_expired(
        self, provider_id: ProviderId, account_id: str
    ) -> bool:
        """Determines whether the token is expired or approaching expiry within the configured clock skew window."""
        meta = self.get_token_metadata(provider_id, account_id)
        if not meta or not meta.get("expires_at"):
            # Tokens without explicit expiration (e.g. personal tokens) are treated as active
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
        account_id: str,
        auth_port: Optional[AuthProviderPort] = None,
    ) -> str:
        """Retrieves a valid access token.

        If the token is expired and an auth provider port is provided with a stored refresh token,
        it automatically attempts proactive refresh before returning the credential.
        """
        access_key = self._access_token_key(provider_id, account_id)
        access_token = self._vault.get(access_key)

        if not access_token:
            raise AuthenticationRequiredError(
                f"No credentials found for provider '{provider_id.value}' and account '{account_id}'.",
                provider=provider_id.value,
            )

        if not self.is_token_expired(provider_id, account_id):
            return access_token

        # Token is expired or expiring soon; attempt proactive refresh
        refresh_token = self.get_refresh_token(provider_id, account_id)
        if refresh_token and auth_port:
            try:
                refreshed_result = await auth_port.refresh_access_token(refresh_token)
                self.save_tokens(provider_id, account_id, refreshed_result)
                return refreshed_result.access_token
            except Exception as exc:
                raise AuthenticationExpiredError(
                    f"Session expired and automatic token refresh failed for provider '{provider_id.value}': {exc}",
                    provider=provider_id.value,
                ) from exc

        # Cannot refresh automatically
        raise AuthenticationExpiredError(
            f"Access token for provider '{provider_id.value}' has expired. Re-authentication required.",
            provider=provider_id.value,
        )

    async def delete_tokens(
        self,
        provider_id: ProviderId,
        account_id: str,
        auth_port: Optional[AuthProviderPort] = None,
    ) -> bool:
        """Revokes token access remotely where supported and cleans all vault secrets for the account."""
        access_key = self._access_token_key(provider_id, account_id)
        refresh_key = self._refresh_token_key(provider_id, account_id)
        meta_key = self._metadata_key(provider_id, account_id)

        # 1. Attempt remote revocation if provider port and token are available
        if auth_port:
            token = self._vault.get(access_key) or self._vault.get(refresh_key)
            if token:
                try:
                    await auth_port.revoke_token(token)
                except Exception:
                    pass

        # 2. Delete credentials from local vault
        with self._lock:
            del_access = self._vault.delete(access_key)
            self._vault.delete(refresh_key)
            self._vault.delete(meta_key)

        return del_access

    def has_token(self, provider_id: ProviderId, account_id: str) -> bool:
        """Checks whether credentials exist in the vault for the specified provider and account."""
        return self._vault.get(self._access_token_key(provider_id, account_id)) is not None


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
