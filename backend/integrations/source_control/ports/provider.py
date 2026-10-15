from __future__ import annotations

from typing import Optional, Protocol, Sequence, runtime_checkable

from ..capabilities import ProviderCapabilities
from ..domain.enums import ProviderId
from ..domain.models import (
    BranchRef,
    ChangeRequestRef,
    CheckResult,
    ProviderAccount,
    RepositoryRef,
)


@runtime_checkable
class SourceControlProvider(Protocol):
    """Abstract port defining the contract for source control provider adapters.

    Every provider implementation (GitHub, GitLab, Bitbucket) must conform to this
    protocol to be registered in the central provider registry.
    """

    @property
    def provider_id(self) -> ProviderId:
        """Returns the unique identifier for this provider."""
        ...

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Returns the declared capability descriptor and feature limits."""
        ...

    async def get_authenticated_account(self) -> ProviderAccount:
        """Retrieves metadata of the currently authenticated account."""
        ...

    async def validate_connection(self) -> bool:
        """Verifies if the stored credentials remain valid and functional."""
        ...

    async def list_repositories(
        self,
        query: Optional[str] = None,
        page: int = 1,
        page_size: int = 30,
    ) -> Sequence[RepositoryRef]:
        """Lists accessible repositories with optional name filtering and pagination."""
        ...

    async def get_repository(self, repository_id: str) -> RepositoryRef:
        """Fetches normalized metadata for a specific repository by its stable ID."""
        ...

    async def list_branches(
        self, repo: RepositoryRef
    ) -> Sequence[BranchRef]:
        """Lists all branches available on the remote repository."""
        ...

    async def get_branch(
        self, repo: RepositoryRef, branch_name: str
    ) -> BranchRef:
        """Fetches metadata for a single branch, including current head commit SHA."""
        ...

    async def read_file_content(
        self,
        repo: RepositoryRef,
        file_path: str,
        ref: Optional[str] = None,
    ) -> str:
        """Reads textual content of a remote repository file at a specific branch or commit SHA."""
        ...

    async def create_branch(
        self,
        repo: RepositoryRef,
        branch_name: str,
        from_sha: str,
    ) -> BranchRef:
        """Creates a new branch on the remote repository pointing to the specified commit SHA."""
        ...

    async def create_change_request(
        self,
        repo: RepositoryRef,
        source_branch: str,
        target_branch: str,
        title: str,
        body: str,
        draft: bool = True,
    ) -> ChangeRequestRef:
        """Creates a new pull request or merge request against the target branch."""
        ...

    async def get_change_request(
        self,
        repo: RepositoryRef,
        number_or_iid: str,
    ) -> ChangeRequestRef:
        """Fetches status and details of an existing change request."""
        ...

    async def list_checks(
        self,
        repo: RepositoryRef,
        commit_sha: str,
    ) -> Sequence[CheckResult]:
        """Retrieves remote CI check runs and commit status results for a specific SHA."""
        ...
