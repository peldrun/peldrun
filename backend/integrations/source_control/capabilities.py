from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .domain.enums import ProviderId


@dataclass(frozen=True)
class ProviderCapabilities:
    """Declared technical capabilities and feature matrix for a source control provider."""

    list_repositories: bool = True
    read_files: bool = True
    create_branch: bool = True
    push_branch: bool = True
    create_change_request: bool = True
    list_checks: bool = False
    comments: bool = False
    merge_change_request: bool = False
    self_managed_instance: bool = False
    supports_draft_prs: bool = True
    supports_commit_status: bool = True
    supports_fine_grained_permissions: bool = True
    default_page_size: int = 30
    max_page_size: int = 100


GITHUB_CAPABILITIES = ProviderCapabilities(
    list_repositories=True,
    read_files=True,
    create_branch=True,
    push_branch=True,
    create_change_request=True,
    list_checks=True,
    comments=True,
    merge_change_request=False,  # Disabled by default; requires explicit elevated policy
    self_managed_instance=False,
    supports_draft_prs=True,
    supports_commit_status=True,
    supports_fine_grained_permissions=True,
    default_page_size=30,
    max_page_size=100,
)

GITLAB_CAPABILITIES = ProviderCapabilities(
    list_repositories=True,
    read_files=True,
    create_branch=True,
    push_branch=True,
    create_change_request=True,
    list_checks=True,
    comments=True,
    merge_change_request=False,
    self_managed_instance=True,  # GitLab natively supports self-managed instance base URLs
    supports_draft_prs=True,
    supports_commit_status=True,
    supports_fine_grained_permissions=False,
    default_page_size=20,
    max_page_size=100,
)

BITBUCKET_CAPABILITIES = ProviderCapabilities(
    list_repositories=True,
    read_files=True,
    create_branch=True,
    push_branch=True,
    create_change_request=True,
    list_checks=True,
    comments=True,
    merge_change_request=False,
    self_managed_instance=False,
    supports_draft_prs=False,  # Bitbucket Cloud standard PR API lacks native draft state
    supports_commit_status=True,
    supports_fine_grained_permissions=False,
    default_page_size=25,
    max_page_size=100,
)

DEFAULT_PROVIDER_CAPABILITIES: Mapping[ProviderId, ProviderCapabilities] = {
    ProviderId.GITHUB: GITHUB_CAPABILITIES,
    ProviderId.GITLAB: GITLAB_CAPABILITIES,
    ProviderId.BITBUCKET: BITBUCKET_CAPABILITIES,
}


def get_default_capabilities(provider_id: ProviderId) -> ProviderCapabilities:
    """Retrieves the default capability descriptor for the specified provider."""
    try:
        return DEFAULT_PROVIDER_CAPABILITIES[provider_id]
    except KeyError as exc:
        raise ValueError(
            f"No capability descriptor registered for provider '{provider_id.value}'"
        ) from exc


def supports_capability(
    capabilities: ProviderCapabilities, capability_name: str
) -> bool:
    """Checks whether a specific named capability is supported."""
    return bool(getattr(capabilities, capability_name, False))
