# backend/integrations/source_control/ports/auth.py
"""
Authentication ports for the Source Control Integration subsystem.

This module defines the contracts for:

    - `AuthPort`           : provider-side authentication (re-exported
                             from `ports.provider`).
    - `CredentialVaultPort`: OS-level secure credential storage.
    - `OAuthFlowPort`      : OAuth authorization-code flow with PKCE.
    - `TokenServicePort`   : token lifecycle (refresh, revoke, expiry).

Design rules:
    - Raw tokens MUST NEVER leave the vault except as an argument to a
      provider API call. They MUST NOT be stored in the domain models,
      the operation journal, the audit log, or the tool results.
    - The vault is the single source of truth for secrets. Every other
      component references secrets by `vault_key`.
    - `OAuthFlowPort` handles only the authorization handshake. It
      returns `AuthSession` and `AuthResult` objects, never tokens.
    - `TokenServicePort` is the ONLY component allowed to read tokens
      from the vault for the purpose of making API calls.

The ports in this module are implemented in `security/` and consumed
by `omweb/services/source_control_facade.py` and the provider
adapters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

from ..domain.enums import ConnectionStatus, ProviderId
from ..domain.models import AuthSession, ProviderAccount
from .provider import AuthPort  # re-exported for convenience

__all__ = [
    # Re-exports
    "AuthPort",
    # Result types
    "AuthResult",
    "OAuthState",
    "TokenInfo",
    # Ports
    "CredentialVaultPort",
    "OAuthFlowPort",
    "TokenServicePort",
    # Helpers
    "make_vault_key",
]


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthResult:
    """
    Outcome of a connection validation or OAuth callback.

    This object intentionally does NOT carry any token. It carries only
    metadata that is safe to persist and to display in the UI.
    """

    provider: ProviderId
    account_id: Optional[str]
    username: Optional[str]
    base_url: Optional[str]
    status: ConnectionStatus
    scopes: Sequence[str] = field(default_factory=tuple)
    expires_at: Optional[datetime] = None
    validated_at: datetime = field(default_factory=_utc_now)
    message: Optional[str] = None

    def is_connected(self) -> bool:
        """Return True if the connection is currently valid."""
        if self.status != ConnectionStatus.CONNECTED:
            return False
        if self.expires_at is None:
            return True
        return _utc_now() < self.expires_at


@dataclass(frozen=True)
class OAuthState:
    """
    Server-side state for a single OAuth authorization attempt.

    The `state` value MUST be cryptographically random and single-use.
    The `code_verifier` MUST NOT be logged. `redirect_uri` is used to
    validate the callback URI exactly.
    """

    state: str
    provider: ProviderId
    code_verifier: Optional[str] = None
    redirect_uri: Optional[str] = None
    base_url: Optional[str] = None
    created_at: datetime = field(default_factory=_utc_now)
    expires_at: Optional[datetime] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        """Return True if the state has expired."""
        if self.expires_at is None:
            return False
        moment = now or _utc_now()
        return moment >= self.expires_at


@dataclass(frozen=True)
class TokenInfo:
    """
    Safe metadata about a stored token.

    This object is returned by `TokenServicePort.get_token_info` and
    MUST NOT carry the raw token value. The raw value is obtained only
    via `TokenServicePort.get_access_token` inside the application
    layer, immediately before an API call.
    """

    provider: ProviderId
    account_id: str
    base_url: str
    expires_at: Optional[datetime] = None
    scopes: Sequence[str] = field(default_factory=tuple)
    has_refresh_token: bool = False
    last_refreshed_at: Optional[datetime] = None

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        """Return True if the token has expired or is about to expire."""
        if self.expires_at is None:
            return False
        moment = now or _utc_now()
        # Treat tokens expiring within 60 seconds as expired to avoid
        # mid-request failures.
        from datetime import timedelta

        return moment >= (self.expires_at - timedelta(seconds=60))


# ---------------------------------------------------------------------------
# Credential vault port
# ---------------------------------------------------------------------------


@runtime_checkable
class CredentialVaultPort(Protocol):
    """
    OS-level secure credential storage.

    Implementations MUST use the operating system's native credential
    store (Windows Credential Manager, macOS Keychain, Linux Secret
    Service). They MUST NOT fall back to plaintext storage silently.

    All methods are synchronous because OS keychain APIs are typically
    synchronous and fast. Callers SHOULD wrap them in
    `asyncio.to_thread` if they need to avoid blocking the event loop
    for large numbers of operations.
    """

    def put(self, key: str, secret: str) -> None:
        """
        Store `secret` under `key`.

        If the key already exists, its value MUST be overwritten.

        Raises:
            ValidationError: If `key` is empty or `secret` is empty.
            ProviderError: If the underlying keychain operation fails.
        """
        ...

    def get(self, key: str) -> Optional[str]:
        """
        Return the secret stored under `key`, or `None` if absent.

        Raises:
            ProviderError: If the keychain is unreachable.
        """
        ...

    def delete(self, key: str) -> None:
        """
        Remove the secret stored under `key`.

        Deleting a non-existent key is a no-op and MUST NOT raise.
        """
        ...

    def exists(self, key: str) -> bool:
        """Return True if a secret is stored under `key`."""
        ...


# ---------------------------------------------------------------------------
# OAuth flow port
# ---------------------------------------------------------------------------


@runtime_checkable
class OAuthFlowPort(Protocol):
    """
    OAuth authorization-code flow with PKCE.

    Implementations MUST:

        - Generate a cryptographically random, single-use `state`.
        - Generate a PKCE `code_verifier` and derive `code_challenge`
          using S256.
        - Bind the loopback callback to `127.0.0.1` or `::1` only.
        - Validate the callback `state`, provider, and redirect URI.
        - Store tokens only in the vault via `CredentialVaultPort`.
        - Never log the authorization code or token response.
    """

    def begin_authorization(
        self,
        provider: ProviderId,
        *,
        base_url: Optional[str] = None,
        scopes: Optional[Sequence[str]] = None,
    ) -> "OAuthStartResult":
        """
        Start an authorization attempt.

        Returns an `OAuthStartResult` containing the authorization URL
        the caller must open in the system browser, and the
        server-side `state` that must be validated on callback.
        """
        ...

    async def complete_authorization(
        self,
        provider: ProviderId,
        *,
        state: str,
        code: str,
        redirect_uri: str,
    ) -> AuthResult:
        """
        Complete an authorization attempt.

        Validates `state`, exchanges `code` for tokens, stores tokens
        in the vault, and returns an `AuthResult`. Never returns the
        raw tokens.

        Raises:
            ValidationError: If `state` is unknown or expired.
            AuthRequiredError: If the token exchange fails.
        """
        ...


@dataclass(frozen=True)
class OAuthStartResult:
    """
    Result of beginning an OAuth authorization.

    `authorization_url` MUST be opened in the system browser by the
    caller. PELDRUN MUST NOT collect provider passwords directly.
    """

    authorization_url: str
    state: str
    provider: ProviderId
    redirect_uri: str
    expires_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Token service port
# ---------------------------------------------------------------------------


@runtime_checkable
class TokenServicePort(Protocol):
    """
    Token lifecycle management.

    Implementations MUST store tokens in the vault and reference them
    by `vault_key`. They MUST NOT persist tokens in any other medium.
    """

    def store_token(
        self,
        provider: ProviderId,
        account_id: str,
        base_url: str,
        *,
        access_token: str,
        refresh_token: Optional[str] = None,
        expires_at: Optional[datetime] = None,
        scopes: Optional[Sequence[str]] = None,
    ) -> None:
        """Persist a token pair in the vault."""
        ...

    def get_access_token(
        self,
        provider: ProviderId,
        account_id: str,
        base_url: str,
    ) -> Optional[str]:
        """
        Return the current access token, or `None` if absent.

        Callers MUST NOT log the returned value.
        """
        ...

    def get_refresh_token(
        self,
        provider: ProviderId,
        account_id: str,
        base_url: str,
    ) -> Optional[str]:
        """Return the current refresh token, or `None` if absent."""
        ...

    def get_token_info(
        self,
        provider: ProviderId,
        account_id: str,
        base_url: str,
    ) -> Optional[TokenInfo]:
        """Return safe metadata about the stored token, or `None`."""
        ...

    async def refresh(
        self,
        provider: ProviderId,
        account_id: str,
        base_url: str,
    ) -> Optional[TokenInfo]:
        """
        Refresh the access token using the stored refresh token.

        Returns the updated `TokenInfo` on success, or `None` if no
        refresh token is available. Raises `AuthExpiredError` if the
        refresh token is rejected by the provider.
        """
        ...

    def revoke(
        self,
        provider: ProviderId,
        account_id: str,
        base_url: str,
    ) -> None:
        """
        Delete the stored token pair from the vault.

        This is a local operation. Provider-side revocation is a
        separate, best-effort call performed by the OAuth flow.
        """
        ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_vault_key(
    provider: ProviderId,
    base_url: str,
    account_id: str,
    *,
    kind: str = "oauth",
) -> str:
    """
    Return a stable, collision-resistant vault key.

    The key format is `peldrun.sc.{kind}.{provider}.{base_url}.{account_id}`.
    Using a namespaced key prevents collisions with other PELDRUN
    secrets and allows per-provider-instance isolation for self-managed
    GitLab or GitHub Enterprise.
    """
    safe_base = (base_url or "default").rstrip("/")
    return f"peldrun.sc.{kind}.{provider.value}.{safe_base}.{account_id}"
