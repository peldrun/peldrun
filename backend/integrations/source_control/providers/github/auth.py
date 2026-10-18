from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence
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
from ...security.redaction import redact_text, redact_url
from .client import GitHubApiConfig, GitHubClient
from .manifest import (
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
        # NOTE: ``GitHubClient`` accepts only a ``GitHubApiConfig`` and
        # an optional pre-built ``httpx.AsyncClient``. The previous
        # implementation passed ``manifest=`` and ``timeout_seconds=``
        # directly to ``GitHubClient``, which raised ``TypeError``
        # because neither keyword exists. The configuration is now
        # assembled from the manifest and the caller-supplied timeout
        # so the two stay in sync.
        self.client = client or GitHubClient(
            config=GitHubApiConfig(
                api_url=self.manifest.api_base_url,
                timeout_seconds=timeout_seconds,
                user_agent=self.manifest.user_agent,
                api_version=self.manifest.api_version,
            )
        )
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

        The OAuth token endpoint (``https://github.com/login/oauth/access_token``)
        lives on a different host than the REST API (``api.github.com``)
        and uses its own error envelope (``{"error": ..., "error_description": ...}``).
        It is therefore called through a dedicated ``httpx.AsyncClient``
        rather than through ``GitHubClient``, whose retry and error
        mapping are tuned for the REST API. The short-lived client is
        closed by the ``async with`` block.

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
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds
            ) as http_client:
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
        except httpx.NetworkError as exc:
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
        """Introspects token by querying the /user endpoint.

        Returns a tuple of ``(account, granted_scopes)``.

        The account is resolved from the JSON body of ``GET /user``.
        The ``granted_scopes`` tuple is currently ALWAYS EMPTY: GitHub
        exposes granted OAuth scopes only through the ``X-OAuth-Scopes``
        response header, and the ``GitHubClient`` abstraction exposes
        only the parsed JSON body. An empty tuple therefore means
        "granted scopes are unknown", NOT "the token grants no scopes".

        ``base_url`` is set to ``self.manifest.web_base_url`` — the
        human-facing web URL of the provider (e.g. ``https://github.com``),
        matching ``mapper.parse_account`` and ``ProviderAccount``'s
        intended semantics. This is distinct from
        ``GitHubClient.api_url``, which is the REST API endpoint and is
        used internally for HTTP requests only.

        Raises:
            AuthRequiredError: If the token is empty.
            NetworkError: On transport failure.
            SourceControlError: If the profile cannot be parsed.
        """
        clean_token = token.strip()
        if not clean_token:
            raise AuthRequiredError("A non-empty access token is required for validation.")

        # ``GitHubClient.get_json`` returns the parsed JSON body
        # directly; there is no ``Response`` object and no ``.data``
        # attribute. The previous implementation used
        # ``self.client.get(...)`` and read ``res.data``/``res.headers``,
        # neither of which exist on GitHubClient and both of which
        # would have raised AttributeError on the first call.
        payload = await self.client.get_json("/user", token=clean_token)
        if not isinstance(payload, dict) or "id" not in payload:
            raise SourceControlError("Failed to parse authenticated GitHub user profile.")

        account_id = str(payload["id"])
        username = str(payload.get("login") or f"user_{account_id}")

        # Scopes are reported by GitHub only via the ``X-OAuth-Scopes``
        # response header. GitHubClient does not surface response
        # headers, so we cannot read them here. Empty tuple = "unknown".
        granted_scopes: tuple[str, ...] = ()

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
        """Verifies whether the given token possesses all required GitHub OAuth scopes.

        IMPORTANT: Because ``validate_token`` cannot surface granted
        scopes (see its docstring), this method's result is only
        meaningful when ``required_scopes`` is empty. For any
        non-empty required set it returns ``False`` — callers MUST
        treat that as "cannot verify", not as "definitely missing".
        This limitation is inherited from ``GitHubClient``; extending
        that class to expose response headers would lift it.

        The default ``required_scopes`` remains
        ``manifest.default_scopes`` so the "cannot verify" behavior is
        explicit rather than silently permissive.
        """
        target_scopes = set(required_scopes or self.manifest.default_scopes)
        if not target_scopes:
            return True
        _, granted_tuple = await self.validate_token(token)
        granted_set = set(granted_tuple)
        return target_scopes.issubset(granted_set)
