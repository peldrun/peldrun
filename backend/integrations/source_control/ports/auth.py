from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Protocol, Sequence, runtime_checkable

from ..domain.enums import ProviderId
from ..domain.models import ProviderAccount


@dataclass(frozen=True)
class AuthorizationSession:
    """Ephemeral state tracking an active in-flight OAuth authorization session."""

    session_id: str
    provider_id: ProviderId
    state: str
    code_verifier: Optional[str]
    redirect_uri: str
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class AuthorizationResult:
    """Result of successfully completing an OAuth authorization code exchange."""

    account: ProviderAccount
    access_token: str
    refresh_token: Optional[str] = None
    token_type: str = "Bearer"
    expires_at: Optional[datetime] = None
    granted_scopes: tuple[str, ...] = ()


@runtime_checkable
class AuthProviderPort(Protocol):
    """Abstract port defining authentication and token lifecycle operations."""

    @property
    def provider_id(self) -> ProviderId:
        """Returns the unique identifier for this provider."""
        ...

    async def build_authorization_url(
        self,
        redirect_uri: str,
        state: str,
        code_challenge: Optional[str] = None,
        scopes: Optional[Sequence[str]] = None,
    ) -> str:
        """Constructs the provider OAuth authorization URL with required query parameters."""
        ...

    async def exchange_code_for_token(
        self,
        code: str,
        redirect_uri: str,
        code_verifier: Optional[str] = None,
    ) -> AuthorizationResult:
        """Exchanges an authorization code for access and refresh credentials."""
        ...

    async def refresh_access_token(
        self,
        refresh_token: str,
    ) -> AuthorizationResult:
        """Refreshes an expired access token using the stored refresh token."""
        ...

    async def validate_token(
        self,
        access_token: str,
    ) -> bool:
        """Verifies if the provided access token is currently valid and active."""
        ...

    async def revoke_token(
        self,
        token: str,
    ) -> bool:
        """Revokes token access on the remote provider platform."""
        ...
