from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from ..domain.enums import ProviderId
from ..domain.models import CheckResult, CheckSuite, RepositoryRef


@runtime_checkable
class ChecksPort(Protocol):
    """Abstract port defining contracts for inspecting remote CI check runs and commit statuses."""

    @property
    def provider_id(self) -> ProviderId:
        """Returns the unique identifier for this provider."""
        ...

    async def list_checks(
        self,
        repo: RepositoryRef,
        commit_sha: str,
    ) -> Sequence[CheckResult]:
        """Retrieves individual CI check runs and commit status results for a specific SHA."""
        ...

    async def get_check_suite(
        self,
        repo: RepositoryRef,
        commit_sha: str,
    ) -> CheckSuite:
        """Retrieves the aggregated execution status and suite for all checks run against a commit SHA."""
        ...
