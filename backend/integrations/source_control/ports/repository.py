from __future__ import annotations

from typing import Optional, Protocol, Sequence, runtime_checkable

from ..domain.enums import ProviderId
from ..domain.models import BranchRef, RepositoryRef


@runtime_checkable
class RepositoryPort(Protocol):
    """Abstract port defining repository inspection, branch queries, and remote file reading."""

    @property
    def provider_id(self) -> ProviderId:
        """Returns the unique identifier for this provider."""
        ...

    async def list_repositories(
        self,
        query: Optional[str] = None,
        page: int = 1,
        page_size: int = 30,
    ) -> Sequence[RepositoryRef]:
        """Lists accessible repositories with optional name filtering and pagination."""
        ...

    async def get_repository(
        self,
        repository_id: str,
    ) -> RepositoryRef:
        """Fetches normalized metadata for a single repository by its stable ID."""
        ...

    async def list_branches(
        self,
        repo: RepositoryRef,
    ) -> Sequence[BranchRef]:
        """Lists all branches available on the remote repository."""
        ...

    async def get_branch(
        self,
        repo: RepositoryRef,
        branch_name: str,
    ) -> BranchRef:
        """Fetches metadata for a single branch, including current head commit SHA."""
        ...

    async def create_branch(
        self,
        repo: RepositoryRef,
        branch_name: str,
        from_sha: str,
    ) -> BranchRef:
        """Creates a new branch on the remote repository pointing to the specified commit SHA."""
        ...

    async def read_file_content(
        self,
        repo: RepositoryRef,
        file_path: str,
        ref: Optional[str] = None,
    ) -> str:
        """Reads textual content of a remote repository file at a specific branch or commit SHA."""
        ...

    async def file_exists(
        self,
        repo: RepositoryRef,
        file_path: str,
        ref: Optional[str] = None,
    ) -> bool:
        """Checks if a specific file exists on the remote repository at the specified reference."""
        ...
