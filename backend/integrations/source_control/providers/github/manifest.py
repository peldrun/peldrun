from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

from ...capabilities import ProviderCapabilities
from ...domain.enums import ProviderId

DEFAULT_GITHUB_API_URL: str = "https://api.github.com"
DEFAULT_GITHUB_WEB_URL: str = "https://github.com"
GITHUB_API_VERSION: str = "2022-11-28"
GITHUB_USER_AGENT: str = "PELDRUN-SourceControl/1.0"

GITHUB_OAUTH_AUTHORIZE_URL: str = "https://github.com/login/oauth/authorize"
GITHUB_OAUTH_TOKEN_URL: str = "https://github.com/login/oauth/access_token"
GITHUB_OAUTH_DEVICE_CODE_URL: str = "https://github.com/login/device/code"

# Default OAuth scopes required for repo discovery, workspace sync, and pull requests
DEFAULT_GITHUB_SCOPES: tuple[str, ...] = (
    "repo",
    "read:user",
    "user:email",
)

# Authoritative technical capabilities for GitHub Cloud integration
GITHUB_CAPABILITIES = ProviderCapabilities(
    list_repositories=True,
    read_files=True,
    create_branch=True,
    push_branch=True,
    create_change_request=True,
    list_checks=True,
    comments=True,
    merge_change_request=False,  # Disabled by default policy in release v1
    self_managed_instance=False,
)


@dataclass(frozen=True)
class GitHubManifest:
    """Immutable metadata and capability descriptor for the official GitHub provider adapter."""

    provider_id: ProviderId = ProviderId.GITHUB
    display_name: str = "GitHub"
    api_base_url: str = DEFAULT_GITHUB_API_URL
    web_base_url: str = DEFAULT_GITHUB_WEB_URL
    api_version: str = GITHUB_API_VERSION
    user_agent: str = GITHUB_USER_AGENT
    capabilities: ProviderCapabilities = GITHUB_CAPABILITIES
    default_scopes: tuple[str, ...] = DEFAULT_GITHUB_SCOPES

    def build_default_headers(self, token: str | None = None) -> dict[str, str]:
        """Constructs standard HTTP headers for GitHub REST API requests."""
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": self.api_version,
            "User-Agent": self.user_agent,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def get_authorize_url(
        self,
        client_id: str,
        redirect_uri: str,
        state: str,
        scopes: tuple[str, ...] | None = None,
    ) -> str:
        """Constructs the standard browser authorization URL for initiating GitHub OAuth."""
        effective_scopes = " ".join(scopes or self.default_scopes)
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": effective_scopes,
            "state": state,
            "allow_signup": "true",
        }
        return f"{GITHUB_OAUTH_AUTHORIZE_URL}?{urlencode(params)}"
