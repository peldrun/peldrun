# backend/integrations/source_control/ports/provider.py
"""
Provider ports (interfaces) for the Source Control Integration
subsystem.

This module defines the contracts that every provider adapter MUST
implement. It follows the Interface Segregation Principle by splitting
the contract into four narrow ports:

    - `AuthPort`            : account identity and connection validation
    - `RepositoryPort`      : repository discovery and file/branch reads
    - `ChangeRequestPort`   : pull/merge request lifecycle
    - `ChecksPort`          : CI check status

`SourceControlProvider` is a composed protocol that includes all four.
It is used by the registry for `isinstance` checks and by the
application service for dependency injection.

The `BaseSourceControlProvider` abstract base class provides default
implementations for backward-compatible aliases (for example
`read_file_content` maps to `read_file`). Adapters SHOULD inherit from
`BaseSourceControlProvider` to avoid implementing aliases manually.

Design rules:
    - All methods that perform I/O MUST be `async`.
    - Tokens are NEVER accepted as required arguments. Every method
      accepts an optional `token` for callers that hold one; when
      `None`, the adapter resolves the token from the vault.
    - Methods MUST NOT perform local filesystem I/O. Local operations
      belong to the `git` package.
    - Methods MUST NOT make approval or policy decisions. Those belong
      to `security` and `operations`.
    - Providers MUST translate provider-specific errors into
      `SourceControlError` subclasses before raising.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional, Protocol, Sequence, runtime_checkable

from ..domain.enums import ChangeRequestState  # noqa: F401 (re-exported)
from ..domain.models import (
    BranchRef,
    ChangeRequestRef,
    CheckRunRef,
    ProviderAccount,
    ProviderCapabilities,
    RepositoryRef,
)

__all__ = [
    # Ports
    "AuthPort",
    "RepositoryPort",
    "ChangeRequestPort",
    "ChecksPort",
    "SourceControlProvider",
    # Base class
    "BaseSourceControlProvider",
]


# ---------------------------------------------------------------------------
# Auth port
# ---------------------------------------------------------------------------


@runtime_checkable
class AuthPort(Protocol):
    """
    Authentication and account-identity operations.

    Implementations MUST be safe to call concurrently. They MUST NOT
    cache tokens in a way that outlives the adapter's token service.
    """

    @property
    def provider_id(self) -> str:
        """Stable provider identifier (e.g. `"github"`)."""
        ...

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Declared capabilities of this adapter instance."""
        ...

    async def get_authenticated_account(
        self, token: Optional[str] = None
    ) -> ProviderAccount:
        """
        Return the account associated with the current credential.

        Raises:
            AuthRequiredError: If no credential is available.
            AuthExpiredError: If the credential has expired.
            NetworkError: On transport failure.
        """
        ...

    async def validate_connection(
        self, token: Optional[str] = None
    ) -> bool:
        """
        Verify that the current credential is valid and the provider
        API is reachable.

        Returns `True` on success and `False` if the credential is
        invalid. Raises `NetworkError` on transport failure. Never
        raises on an invalid credential; the caller decides how to
        react.
        """
        ...


# ---------------------------------------------------------------------------
# Repository port
# ---------------------------------------------------------------------------


@runtime_checkable
class RepositoryPort(Protocol):
    """
    Repository discovery and read operations.
    """

    async def list_repositories(
        self,
        query: Optional[str] = None,
        page: int = 1,
        page_size: int = 30,
        token: Optional[str] = None,
    ) -> Sequence[RepositoryRef]:
        """
        List repositories the authenticated account can access.

        Args:
            query: Optional case-insensitive substring filter.
            page: 1-based page number. Adapters translate to their
                provider's pagination scheme.
            page_size: Maximum number of results to return.
            token: Optional credential override. When `None`, the
                adapter resolves the token from the vault.

        Raises:
            AuthRequiredError: If no credential is available.
            RateLimitError: If the provider rate limit was exceeded.
            NetworkError: On transport failure.
        """
        ...

    async def get_repository(
        self,
        repo: RepositoryRef,
        token: Optional[str] = None,
    ) -> RepositoryRef:
        """
        Fetch fresh metadata for a repository.

        The `repository_id` on `repo` is authoritative; the adapter
        MUST resolve by ID, not by name.
        """
        ...

    async def list_branches(
        self,
        repo: RepositoryRef,
        token: Optional[str] = None,
    ) -> Sequence[BranchRef]:
        """Return all branches visible to the authenticated account."""
        ...

    async def get_branch(
        self,
        repo: RepositoryRef,
        name: str,
        token: Optional[str] = None,
    ) -> BranchRef:
        """
        Fetch metadata for a single branch.

        Raises:
            NotFoundError: If the branch does not exist.
        """
        ...

    async def read_file(
        self,
        repo: RepositoryRef,
        file_path: str,
        ref: Optional[str] = None,
        token: Optional[str] = None,
    ) -> str:
        """
        Read a text file at a given ref (branch, tag, or commit SHA).

        When `ref` is `None`, the repository's default branch is used.
        The returned string is decoded as UTF-8 with replacement for
        invalid sequences.

        Raises:
            NotFoundError: If the file does not exist at the ref.
            UnsupportedCapabilityError: If the provider cannot read
                file content via its API.
        """
        ...


# ---------------------------------------------------------------------------
# Change request port
# ---------------------------------------------------------------------------


@runtime_checkable
class ChangeRequestPort(Protocol):
    """
    Pull request / merge request lifecycle.
    """

    async def create_change_request(
        self,
        repo: RepositoryRef,
        source_branch: str,
        target_branch: str,
        title: str,
        body: str,
        draft: bool = True,
        token: Optional[str] = None,
    ) -> ChangeRequestRef:
        """
        Create a new change request.

        The adapter MUST NOT create the request if `source_branch` does
        not exist on the remote. The request is created in draft mode
        by default.

        Raises:
            ConflictError: If a change request already exists for the
                same source/target pair.
            ValidationError: If the inputs are invalid.
        """
        ...

    async def get_change_request(
        self,
        repo: RepositoryRef,
        number_or_iid: str,
        token: Optional[str] = None,
    ) -> ChangeRequestRef:
        """Fetch a single change request by number or IID."""
        ...


# ---------------------------------------------------------------------------
# Checks port
# ---------------------------------------------------------------------------


@runtime_checkable
class ChecksPort(Protocol):
    """
    CI check status for a commit or branch.
    """

    async def list_checks(
        self,
        repo: RepositoryRef,
        ref: str,
        token: Optional[str] = None,
    ) -> Sequence[CheckRunRef]:
        """
        List check runs for a commit SHA or branch name.

        Providers without a checks API MUST raise
        `UnsupportedCapabilityError`. They MUST NOT return an empty
        list, because that would be indistinguishable from "all checks
        passed".
        """
        ...


# ---------------------------------------------------------------------------
# Composed protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class SourceControlProvider(
    AuthPort, RepositoryPort, ChangeRequestPort, ChecksPort, Protocol
):
    """
    Composed provider protocol.

    Every provider adapter MUST satisfy this protocol. The registry
    performs an `isinstance` check against this protocol before
    registering an adapter.
    """


# ---------------------------------------------------------------------------
# Base class with default implementations
# ---------------------------------------------------------------------------


class BaseSourceControlProvider(ABC):
    """
    Abstract base class for provider adapters.

    Inheriting from this class provides:

        - Default implementations of backward-compatible aliases such
          as `read_file_content` (delegates to `read_file`).
        - A consistent `__repr__` for logs.
        - A `require_token` helper for adapters that need a token but
          are called without one.

    Adapters SHOULD inherit from this class. Inheriting is not
    mandatory; any object that satisfies `SourceControlProvider` will
    pass the registry check.
    """

    #: Overridden by subclasses with the canonical provider ID.
    provider_id: str = "unknown"

    @property
    @abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        """Return the declared capabilities of this adapter."""
        raise NotImplementedError

    # --- Auth -----------------------------------------------------------

    @abstractmethod
    async def get_authenticated_account(
        self, token: Optional[str] = None
    ) -> ProviderAccount:
        raise NotImplementedError

    async def validate_connection(
        self, token: Optional[str] = None
    ) -> bool:
        """
        Default implementation: attempt to fetch the account and
        report success or failure based on the outcome.
        """
        try:
            await self.get_authenticated_account(token=token)
            return True
        except Exception:
            return False

    # --- Repository -----------------------------------------------------

    @abstractmethod
    async def list_repositories(
        self,
        query: Optional[str] = None,
        page: int = 1,
        page_size: int = 30,
        token: Optional[str] = None,
    ) -> Sequence[RepositoryRef]:
        raise NotImplementedError

    @abstractmethod
    async def get_repository(
        self,
        repo: RepositoryRef,
        token: Optional[str] = None,
    ) -> RepositoryRef:
        raise NotImplementedError

    @abstractmethod
    async def list_branches(
        self,
        repo: RepositoryRef,
        token: Optional[str] = None,
    ) -> Sequence[BranchRef]:
        raise NotImplementedError

    async def get_branch(
        self,
        repo: RepositoryRef,
        name: str,
        token: Optional[str] = None,
    ) -> BranchRef:
        """
        Default implementation: list branches and filter by name.

        Adapters SHOULD override this with a direct API call when the
        provider supports it.
        """
        branches = await self.list_branches(repo, token=token)
        for branch in branches:
            if branch.name == name:
                return branch
        from ..errors import NotFoundError

        raise NotFoundError(
            f"Branch '{name}' not found.",
            details={"branch": name, "repository_id": repo.repository_id},
        )

    @abstractmethod
    async def read_file(
        self,
        repo: RepositoryRef,
        file_path: str,
        ref: Optional[str] = None,
        token: Optional[str] = None,
    ) -> str:
        raise NotImplementedError

    # --- Backward-compatible alias --------------------------------------

    async def read_file_content(
        self,
        repo: RepositoryRef,
        file_path: str,
        ref: Optional[str] = None,
        token: Optional[str] = None,
    ) -> str:
        """
        Backward-compatible alias for `read_file`.

        New code SHOULD call `read_file` directly. This alias exists so
        that older callers and the test suite keep working.
        """
        return await self.read_file(
            repo, file_path, ref=ref, token=token
        )

    # --- Change requests ------------------------------------------------

    @abstractmethod
    async def create_change_request(
        self,
        repo: RepositoryRef,
        source_branch: str,
        target_branch: str,
        title: str,
        body: str,
        draft: bool = True,
        token: Optional[str] = None,
    ) -> ChangeRequestRef:
        raise NotImplementedError

    async def get_change_request(
        self,
        repo: RepositoryRef,
        number_or_iid: str,
        token: Optional[str] = None,
    ) -> ChangeRequestRef:
        """
        Default implementation raises `UnsupportedCapabilityError`.

        Adapters that support reading a single change request MUST
        override this method.
        """
        from ..errors import UnsupportedCapabilityError

        raise UnsupportedCapabilityError(
            "Reading a single change request is not supported by this "
            "provider.",
            details={
                "provider": self.provider_id,
                "operation": "get_change_request",
            },
        )

    # --- Checks ---------------------------------------------------------

    async def list_checks(
        self,
        repo: RepositoryRef,
        ref: str,
        token: Optional[str] = None,
    ) -> Sequence[CheckRunRef]:
        """
        Default implementation returns an empty tuple.

        Providers without a checks API SHOULD leave this default in
        place. The capability declaration (`capabilities.list_checks =
        False`) is what prevents the caller from invoking it.
        """
        return ()

    # --- Helpers --------------------------------------------------------

    def require_token(self, token: Optional[str]) -> str:
        """
        Return `token` if present, otherwise raise `AuthRequiredError`.

        Adapters SHOULD call this at the start of any operation that
        cannot proceed without a credential.
        """
        if token:
            return token
        from ..errors import AuthRequiredError

        raise AuthRequiredError(
            f"No credential available for provider '{self.provider_id}'."
        )

    def __repr__(self) -> str:  # pragma: no cover - trivial formatting
        return f"<{type(self).__name__} provider_id={self.provider_id!r}>"
