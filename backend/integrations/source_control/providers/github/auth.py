from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlencode

import httpx

from ...domain.enums import ProviderId
from ...domain.models import ProviderAccount
from ...errors import (
    AuthExpiredError,
    AuthRequiredError,
    NetworkError,
    SourceControlError,
    ValidationError,
)
from ...security.redaction import redact_data, redact_text, redact_url
from .client import GitHubClient
from .errors import map_github_http_error
from .manifest import (
    DEFAULT_GITHUB_API_URL,
    DEFAULT_GITHUB_SCOPES,
    GITHUB_OAUTH_AUTHORIZE_URL,
    GITHUB_OAUTH_TOKEN_URL,
    GitHubManifest,
)


@dataclass(frozen=True)
class GitHubTokenResponse:
    """Immutable payload returned by GitHub OAuth token exchange endpoint."""

    access_token: str
    token_type: str = "bearer"
    scope: str = ""
    expires_in: Optional[int] = None
    refresh_token: Optional[str] = None
    refresh_token_expires_in: Optional[int] = None

    @property
    def granted_scopes(self) -> tuple[str, ...]:
        """Returns normalized tuple of granted OAuth scope identifiers."""
        if not self.scope:
            return ()
        return tuple(s.strip() for s in self.scope.split(",") if s.strip())


class GitHubAuthService:
    """Service coordinating GitHub OAuth 2.0 PKCE authentication flow and token introspection."""

    def __init__(
        self,
        client: Optional[GitHubClient] = None,
        manifest: Optional[GitHubManifest] = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.manifest = manifest or GitHubManifest()
        self.client = client or GitHubClient(manifest=self.manifest, timeout_seconds=timeout_seconds)
        self.timeout_seconds = timeout_seconds

    def build_authorization_url(
        self,
        client_id: str,
        redirect_uri: str,
        state: str,
        scopes: Optional[Sequence[str]] = None,
        *,
        code_challenge: Optional[str] = None,
        code_challenge_method: str = "S256",
    ) -> str:
        """Constructs browser authorization URL for initiating GitHub OAuth PKCE flow."""
        clean_client_id = client_id.strip()
        if not clean_client_id:
            raise ValidationError("Client ID cannot be empty.")

        effective_scopes = " ".join(scopes or self.manifest.default_scopes)
        params: dict[str, str] = {
            "client_id": clean_client_id,
            "redirect_uri": redirect_uri.strip(),
            "scope": effective_scopes,
            "state": state.strip(),
            "allow_signup": "true",
        }

        if code_challenge:
            params["code_challenge"] = code_challenge.strip()
            params["code_challenge_method"] = code_challenge_method.strip()

        return f"{GITHUB_OAUTH_AUTHORIZE_URL}?{urlencode(params)}"

    async def exchange_code_for_token(
        self,
        client_id: str,
        code: str,
        redirect_uri: str,
        *,
        client_secret: Optional[str] = None,
        code_verifier: Optional[str] = None,
    ) -> GitHubTokenResponse:
        """Exchanges an authorization code for an OAuth access token.

        Raises:
            ValidationError: If authorization code or configuration parameters are invalid.
            AuthExpiredError: If authorization code is expired or bad verification code is returned.
            NetworkError: If transport timeout occurs during token exchange.
            SourceControlError: If token exchange returns an unexpected provider error.
        """
        clean_client_id = client_id.strip()
        clean_code = code.strip()
        if not clean_client_id or not clean_code:
            raise ValidationError("Both client_id and authorization code are required for token exchange.")

        payload: dict[str, str] = {
            "client_id": clean_client_id,
            "code": clean_code,
            "redirect_uri": redirect_uri.strip(),
        }

        if client_secret:
            payload["client_secret"] = client_secret.strip()
        if code_verifier:
            payload["code_verifier"] = code_verifier.strip()

        headers = {
            "Accept": "application/json",
            "User-Agent": self.manifest.user_agent,
        }

        safe_token_url = redact_url(GITHUB_OAUTH_TOKEN_URL)
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_seconds)) as http_client:
                response = await http_client.post(
                    GITHUB_OAUTH_TOKEN_URL,
                    json=payload,
                    headers=headers,
                )
        except httpx.TimeoutException as exc:
            raise NetworkError(
                f"Timeout exchanging GitHub authorization code: {safe_token_url}",
                details={"timeout_seconds": str(self.timeout_seconds)},
            ) from exc
        except (httpx.NetworkError, httpx.ConnectError) as exc:
            raise NetworkError(
                f"Network failure communicating with GitHub token endpoint: {exc}"
            ) from exc

        try:
            data = response.json()
        except Exception as exc:
            raise SourceControlError(
                f"Malformed non-JSON response from GitHub token endpoint (HTTP {response.status_code})."
            ) from exc

        if not isinstance(data, dict):
            raise SourceControlError("Unexpected response format from GitHub OAuth token endpoint.")

        # Check for OAuth error envelope: {"error": "...", "error_description": "..."}
        if "error" in data:
            error_code = str(data.get("error", ""))
            error_desc = redact_text(str(data.get("error_description", "OAuth exchange failed.")))
            diagnostic_details = {"oauth_error": error_code, "error_description": error_desc}

            if error_code in {"bad_verification_code", "expired_token"}:
                raise AuthExpiredError(
                    f"GitHub authorization code is invalid or expired: {error_desc}",
                    details=diagnostic_details,
                )
            if error_code in {"redirect_uri_mismatch", "invalid_client", "application_suspended"}:
                raise ValidationError(
                    f"GitHub OAuth validation failure ({error_code}): {error_desc}",
                    details=diagnostic_details,
                )

            raise SourceControlError(
                f"GitHub token exchange rejected with error '{error_code}': {error_desc}",
                details=diagnostic_details,
            )

        access_token = data.get("access_token")
        if not access_token or not isinstance(access_token, str):
            raise SourceControlError("GitHub token response did not contain an access_token.")

        token_type = str(data.get("token_type", "bearer")).lower()
        scope = str(data.get("scope", ""))
        expires_in = int(data["expires_in"]) if "expires_in" in data and str(data["expires_in"]).isdigit() else None
        refresh_token = str(data["refresh_token"]) if "refresh_token" in data else None
        refresh_expires_in = (
            int(data["refresh_token_expires_in"])
            if "refresh_token_expires_in" in data and str(data["refresh_token_expires_in"]).isdigit()
            else None
        )

        return GitHubTokenResponse(
            access_token=access_token,
            token_type=token_type,
            scope=scope,
            expires_in=expires_in,
            refresh_token=refresh_token,
            refresh_token_expires_in=refresh_expires_in,
        )

    async def validate_token(self, token: str) -> tuple[ProviderAccount, tuple[str, ...]]:
        """Introspects token by querying /user endpoint and returns authenticated account and granted scopes.

        Raises:
            AuthExpiredError: If token is revoked or expired.
            AuthRequiredError: If token is missing or unauthorized.
            NetworkError: On network or transport timeout.
        """
        clean_token = token.strip()
        if not clean_token:
            raise AuthRequiredError("A non-empty access token is required for validation.")

        # Request user profile to confirm identity and read granted scopes header
        res = await self.client.get("/user", token=clean_token)
        if not isinstance(res.data, dict) or "id" not in res.data:
            raise SourceControlError("Failed to parse authenticated GitHub user profile.")

        account_id = str(res.data["id"])
        username = str(res.data.get("login") or f"user_{account_id}")

        # Extract granted scopes from case-insensitive header
        raw_scopes = ""
        for h_key, h_val in res.headers.items():
            if h_key.lower() == "x-oauth-scopes":
                raw_scopes = h_val
                break

        granted_scopes: tuple[str, ...] = tuple(
            s.strip() for s in raw_scopes.split(",") if s.strip()
        )

        account = ProviderAccount(
            provider=ProviderId.GITHUB,
            account_id=account_id,
            username=username,
            base_url=self.manifest.web_base_url,
            connected=True,
        )

        return account, granted_scopes

    async def get_authenticated_account(self, token: str) -> ProviderAccount:
        """Retrieves verified ProviderAccount for the given token."""
        account, _ = await self.validate_token(token)
        return account

    async def check_scopes(
        self, token: str, required_scopes: Optional[Sequence[str]] = None
    ) -> bool:
        """Verifies whether the given token possesses all required GitHub OAuth scopes."""
        target_scopes = set(required_scopes or self.manifest.default_scopes)
        _, granted_tuple = await self.validate_token(token)
        granted_set = set(granted_tuple)
        return target_scopes.issubset(granted_set)
