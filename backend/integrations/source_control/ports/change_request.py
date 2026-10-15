from __future__ import annotations

from typing import Optional, Protocol, Sequence, runtime_checkable

from ..domain.enums import ChangeRequestState, ProviderId
from ..domain.models import ChangeRequestRef, RepositoryRef


@runtime_checkable
class ChangeRequestPort(Protocol):
    """Abstract port defining change request lifecycle (Pull Requests / Merge Requests)."""

    @property
    def provider_id(self) -> ProviderId:
        """Returns the unique identifier for this provider."""
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

    async def list_change_requests(
        self,
        repo: RepositoryRef,
        state: Optional[ChangeRequestState] = None,
        source_branch: Optional[str] = None,
        target_branch: Optional[str] = None,
        page: int = 1,
        page_size: int = 30,
    ) -> Sequence[ChangeRequestRef]:
        """Lists change requests with optional filtering by lifecycle state and branches."""
        ...

    async def update_change_request(
        self,
        repo: RepositoryRef,
        number_or_iid: str,
        title: Optional[str] = None,
        body: Optional[str] = None,
    ) -> ChangeRequestRef:
        """Updates metadata (title and description) of an existing change request."""
        ...

    async def close_change_request(
        self,
        repo: RepositoryRef,
        number_or_iid: str,
    ) -> ChangeRequestRef:
        """Closes an active change request without merging."""
        ...

    async def get_change_request_diff(
        self,
        repo: RepositoryRef,
        number_or_iid: str,
    ) -> str:
        """Retrieves raw unified patch diff representation for the change request."""
        ...
