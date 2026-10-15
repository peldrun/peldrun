from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from ...capabilities import ProviderCapabilities
from ...domain.enums import ProviderId
from ...domain.models import (
    BranchRef,
    ChangeRequestRef,
    CommitRef,
    DiffSummary,
    FileDiff,
    ProviderAccount,
    RepositoryRef,
)
from ...ports.provider import SourceControlProvider
from .auth import GitHubAuthService, GitHubTokenResponse
from .branches import GitHubBranchService
from .checks import CheckRunRef, CheckSummaryRef, GitHubChecksService
from .client import GitHubClient, GitHubResponse
from .errors import GitHubAPIError, map_github_http_error
from .manifest import GITHUB_CAPABILITIES, GitHubManifest
from .mapper import GitHubDataMapper
from .pull_requests import GitHubPullRequestService
from .repositories import GitHubRepositoryService


class GitHubProviderAdapter:
    """Official GitHub Cloud provider adapter conforming to SourceControlProvider interface."""

    def __init__(
        self,
        manifest: Optional[GitHubManifest] = None,
        client: Optional[GitHubClient] = None,
    ) -> None:
        self.manifest = manifest or GitHubManifest()
        self.client = client or GitHubClient(manifest=self.manifest)
        self.auth = GitHubAuthService(client=self.client, manifest=self.manifest)
        self.repositories = GitHubRepositoryService(client=self.client)
        self.branches = GitHubBranchService(client=self.client)
        self.pull_requests = GitHubPullRequestService(client=self.client)
        self.checks = GitHubChecksService(client=self.client)
        self.mapper = GitHubDataMapper()

    @property
    def provider_id(self) -> ProviderId:
        """Returns the stable provider identifier."""
        return ProviderId.GITHUB

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Returns authoritative technical capabilities of the GitHub integration."""
        return self.manifest.capabilities

    async def get_authenticated_account(self, token: Optional[str] = None) -> ProviderAccount:
        """Introspects user token and returns verified ProviderAccount."""
        effective_token = token or self.client.token
        if not effective_token:
            raise ValueError("Authentication token is required to inspect GitHub account.")
        return await self.auth.get_authenticated_account(effective_token)

    async def list_repositories(
        self,
        query: Optional[str] = None,
        limit: int = 100,
        token: Optional[str] = None,
    ) -> Sequence[RepositoryRef]:
        """Lists user-accessible repositories with optional query filtering."""
        return await self.repositories.list_repositories(query=query, limit=limit, token=token)

    async def get_repository(
        self,
        repository_id: str,
        token: Optional[str] = None,
    ) -> RepositoryRef:
        """Retrieves a single repository by numeric ID or slug."""
        return await self.repositories.get_repository(repository_id, token=token)

    async def read_file(
        self,
        repo: RepositoryRef,
        file_path: str,
        ref: Optional[str] = None,
        token: Optional[str] = None,
    ) -> bytes:
        """Reads file contents at a given ref/commit via remote GitHub Contents API."""
        return await self.repositories.read_file(repo=repo, file_path=file_path, ref=ref, token=token)

    async def list_branches(
        self,
        repo: RepositoryRef,
        limit: int = 100,
        token: Optional[str] = None,
    ) -> Sequence[BranchRef]:
        """Lists remote branches for the specified repository."""
        return await self.branches.list_branches(repo=repo, limit=limit, token=token)

    async def get_branch(
        self,
        repo: RepositoryRef,
        branch_name: str,
        token: Optional[str] = None,
    ) -> BranchRef:
        """Retrieves details of a specific remote branch."""
        return await self.branches.get_branch(repo=repo, branch_name=branch_name, token=token)

    async def create_branch(
        self,
        repo: RepositoryRef,
        branch_name: str,
        from_sha: str,
        token: Optional[str] = None,
    ) -> BranchRef:
        """Creates a new branch on GitHub pointing to the given commit SHA."""
        return await self.branches.create_branch(
            repo=repo, branch_name=branch_name, from_sha=from_sha, token=token
        )

    async def list_change_requests(
        self,
        repo: RepositoryRef,
        source_branch: Optional[str] = None,
        target_branch: Optional[str] = None,
        state: Optional[str] = "open",
        limit: int = 100,
        token: Optional[str] = None,
    ) -> Sequence[ChangeRequestRef]:
        """Lists pull requests for the repository with optional branch/state filtering."""
        return await self.pull_requests.list_change_requests(
            repo=repo,
            source_branch=source_branch,
            target_branch=target_branch,
            state=state,
            limit=limit,
            token=token,
        )

    async def get_change_request(
        self,
        repo: RepositoryRef,
        number_or_iid: str | int,
        token: Optional[str] = None,
    ) -> ChangeRequestRef:
        """Retrieves details of a specific pull request."""
        return await self.pull_requests.get_change_request(
            repo=repo, number_or_iid=number_or_iid, token=token
        )

    async def create_change_request(
        self,
        repo: RepositoryRef,
        source_branch: str,
        target_branch: str,
        title: str,
        body: str = "",
        draft: bool = False,
        token: Optional[str] = None,
    ) -> ChangeRequestRef:
        """Creates a new pull request on GitHub."""
        return await self.pull_requests.create_change_request(
            repo=repo,
            source_branch=source_branch,
            target_branch=target_branch,
            title=title,
            body=body,
            draft=draft,
            token=token,
        )

    async def get_check_summary(
        self,
        repo: RepositoryRef,
        ref: str,
        token: Optional[str] = None,
    ) -> CheckSummaryRef:
        """Aggregates all Check Runs and Commit Statuses into an authoritative CheckSummaryRef."""
        return await self.checks.get_check_summary(repo=repo, ref=ref, token=token)

    async def close(self) -> None:
        """Closes the underlying HTTP transport session."""
        await self.client.close()


__all__ = [
    # Top-Level Official Adapter
    "GitHubProviderAdapter",
    # Metadata & Manifest
    "GitHubManifest",
    "GITHUB_CAPABILITIES",
    # Transport Client & Response
    "GitHubClient",
    "GitHubResponse",
    # Domain Services
    "GitHubAuthService",
    "GitHubTokenResponse",
    "GitHubRepositoryService",
    "GitHubBranchService",
    "GitHubPullRequestService",
    "GitHubChecksService",
    "CheckRunRef",
    "CheckSummaryRef",
    # Data Mapper & Errors
    "GitHubDataMapper",
    "GitHubAPIError",
    "map_github_http_error",
]
