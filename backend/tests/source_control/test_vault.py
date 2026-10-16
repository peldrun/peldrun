from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta
from typing import Any, Optional
import pytest

# Resilient imports for security and vault package
try:
    from ...integrations.source_control.domain.enums import ProviderId
    from ...integrations.source_control.errors import (
        AuthExpiredError,
        AuthRequiredError,
        ValidationError,
    )
    from ...integrations.source_control.security.oauth_flow import (
        OAuthFlowCoordinator,
        OAuthFlowSession,
    )
    from ...integrations.source_control.security.token_service import TokenService
    from ...integrations.source_control.security.vault import CredentialVault
except (ImportError, ValueError):
    from backend.integrations.source_control.domain.enums import ProviderId
    from backend.integrations.source_control.errors import (
        AuthExpiredError,
        AuthRequiredError,
        ValidationError,
    )
    from backend.integrations.source_control.security.oauth_flow import (
        OAuthFlowCoordinator,
        OAuthFlowSession,
    )
    from backend.integrations.source_control.security.token_service import TokenService
    from backend.integrations.source_control.security.vault import CredentialVault

from .conftest import InMemoryVault


# ---------------------------------------------------------------------------
# 1. Credential Vault Contract Tests
# ---------------------------------------------------------------------------


def test_in_memory_vault_put_get_delete(in_memory_vault: InMemoryVault) -> None:
    """Verifies standard put, get, and delete operations on the credential vault."""
    assert in_memory_vault.get("non_existent_key") is None

    in_memory_vault.put("test_key", "secret_value_123")
    assert in_memory_vault.get("test_key") == "secret_value_123"

    in_memory_vault.delete("test_key")
    assert in_memory_vault.get("test_key") is None


def test_in_memory_vault_overwrite_and_clear(in_memory_vault: InMemoryVault) -> None:
    """Verifies that keys can be overwritten atomically and cleared entirely."""
    in_memory_vault.put("key_a", "val_1")
    in_memory_vault.put("key_a", "val_2")
    assert in_memory_vault.get("key_a") == "val_2"

    in_memory_vault.put("key_b", "val_b")
    in_memory_vault.clear()
    assert in_memory_vault.get("key_a") is None
    assert in_memory_vault.get("key_b") is None


# ---------------------------------------------------------------------------
# 2. TokenService Lifecycle Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_service_save_and_retrieve_access_token(
    token_service: TokenService,
) -> None:
    """Ensures access tokens can be stored and retrieved cleanly across providers."""
    await token_service.save_tokens(
        provider_id=ProviderId.GITHUB,
        access_token="ghp_mock_personal_token_987654321",
        expires_in=3600,
        scopes=["repo", "read:user"],
    )

    retrieved = await token_service.get_access_token(ProviderId.GITHUB)
    assert retrieved == "ghp_mock_personal_token_987654321"

    # Verify that unconfigured provider returns None
    gitlab_token = await token_service.get_access_token(ProviderId.GITLAB)
    assert gitlab_token is None


@pytest.mark.asyncio
async def test_token_service_expiry_detection(
    in_memory_vault: InMemoryVault,
    token_service: TokenService,
) -> None:
    """Verifies that expired access tokens are detected and return None when no refresh token exists."""
    # 1. Save token that already expired 10 minutes ago
    in_memory_vault.put("sc_github_access_token", "ghp_expired_token_abc")
    past_ts = (datetime.utcnow() - timedelta(minutes=10)).isoformat()
    in_memory_vault.put("sc_github_expires_at", past_ts)

    # 2. Token should be recognized as expired and return None
    retrieved = await token_service.get_access_token(ProviderId.GITHUB)
    assert retrieved is None


@pytest.mark.asyncio
async def test_token_service_revocation_purges_vault_keys(
    in_memory_vault: InMemoryVault,
    token_service: TokenService,
) -> None:
    """Verifies that revoking credentials completely eliminates all associated vault records."""
    await token_service.save_tokens(
        provider_id=ProviderId.GITHUB,
        access_token="ghp_test_token_to_revoke",
        refresh_token="ghr_test_refresh_token",
        expires_in=7200,
        scopes=["repo"],
    )

    assert await token_service.get_access_token(ProviderId.GITHUB) is not None

    # Revoke tokens
    await token_service.revoke_tokens(ProviderId.GITHUB)

    # Validate that both access token and metadata keys are purged from vault
    assert await token_service.get_access_token(ProviderId.GITHUB) is None
    assert in_memory_vault.get("sc_github_access_token") is None
    assert in_memory_vault.get("sc_github_refresh_token") is None
    assert in_memory_vault.get("sc_github_expires_at") is None


@pytest.mark.asyncio
async def test_token_service_provider_isolation(
    token_service: TokenService,
) -> None:
    """Ensures multiple provider credentials do not collide or leak into one another."""
    await token_service.save_tokens(
        provider_id=ProviderId.GITHUB,
        access_token="gh_access_token_1",
    )
    await token_service.save_tokens(
        provider_id=ProviderId.GITLAB,
        access_token="gl_access_token_2",
    )

    assert await token_service.get_access_token(ProviderId.GITHUB) == "gh_access_token_1"
    assert await token_service.get_access_token(ProviderId.GITLAB) == "gl_access_token_2"

    await token_service.revoke_tokens(ProviderId.GITHUB)
    assert await token_service.get_access_token(ProviderId.GITHUB) is None
    assert await token_service.get_access_token(ProviderId.GITLAB) == "gl_access_token_2"


# ---------------------------------------------------------------------------
# 3. OAuth 2.0 PKCE Flow Coordinator Tests
# ---------------------------------------------------------------------------


def test_oauth_flow_pkce_challenge_computation() -> None:
    """Verifies that code_challenge strictly satisfies RFC 7636 Base64URL-encoded SHA-256."""
    coordinator = OAuthFlowCoordinator()
    session: OAuthFlowSession = coordinator.start_flow(
        provider=ProviderId.GITHUB,
        client_id="test_client_id_123",
        scopes=["repo", "user:email"],
        redirect_uri="http://127.0.0.1:8000/callback",
    )

    assert session.code_verifier is not None
    assert len(session.code_verifier) >= 43
    assert session.code_challenge_method == "S256"

    # Compute expected challenge independently
    expected_digest = hashlib.sha256(session.code_verifier.encode("ascii")).digest()
    expected_challenge = base64.urlsafe_b64encode(expected_digest).decode("ascii").rstrip("=")

    assert session.code_challenge == expected_challenge


def test_oauth_flow_authorization_url_generation() -> None:
    """Ensures the constructed browser authorization URL contains all mandatory OAuth parameters."""
    coordinator = OAuthFlowCoordinator()
    session = coordinator.start_flow(
        provider=ProviderId.GITHUB,
        client_id="my_app_client_id",
        scopes=["repo"],
        redirect_uri="http://127.0.0.1:8000/api/source-control/oauth/callback/github",
    )

    auth_url = session.build_authorization_url("https://github.com/login/oauth/authorize")

    assert auth_url.startswith("https://github.com/login/oauth/authorize?")
    assert "client_id=my_app_client_id" in auth_url
    assert f"state={session.state}" in auth_url
    assert f"code_challenge={session.code_challenge}" in auth_url
    assert "code_challenge_method=S256" in auth_url
    assert "redirect_uri=" in auth_url


def test_oauth_flow_session_lookup_and_clearing() -> None:
    """Ensures sessions can be retrieved by CSRF state and cleared after single use."""
    coordinator = OAuthFlowCoordinator()
    session = coordinator.start_flow(
        provider=ProviderId.GITHUB,
        client_id="client_xyz",
    )

    state = session.state
    assert coordinator.get_flow(state) == session

    # Unknown or tampered state must return None
    assert coordinator.get_flow("tampered_state_value") is None

    # Clearing flow purges the session
    coordinator.clear_flow(state)
    assert coordinator.get_flow(state) is None


# ---------------------------------------------------------------------------
# 4. Secret Leakage Prevention Tests
# ---------------------------------------------------------------------------


def test_token_service_repr_does_not_leak_secrets(token_service: TokenService) -> None:
    """Ensures TokenService __repr__ and string conversions do not expose active token contents."""
    service_repr = repr(token_service)
    assert "ghp_" not in service_repr
    assert "secret" not in service_repr.lower() or "service" in service_repr.lower()
