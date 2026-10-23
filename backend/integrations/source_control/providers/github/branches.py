# backend/integrations/source_control/providers/github/branches.py
"""
GitHub branch service for the Source Control Integration subsystem.

This service implements the branch-specific subset of the
`SourceControlProvider` protocol for GitHub. It is used by
`GitHubProviderAdapter` and is not intended to be called directly by
the application layer.

The service knows about two GitHub REST endpoints:

    - `GET /repos/{owner}/{repo}/branches`
      List branches, paginated.
    - `GET /repos/{owner}/{repo}/branches/{branch}`
      Fetch a single branch.

All HTTP is delegated to `GitHubClient`. Error mapping, rate-limit
handling, and retries are the client's responsibility. This service
only handles GitHub-specific data shaping.

Design rules:
    - The service is stateless with respect to per-request data. It
      is safe to share across coroutines.
    - The service never logs tokens, branch content, or user data.
    - Branch names are always URL-encoded before being used in a
      path, because Git branch names may contain slashes and other
      characters that GitHub requires to be encoded.
    - The service caps the number of branches it will return in a
      single listing at `MAX_BRANCHES`. Repositories with more
      branches than this require explicit paging by the caller.
"""

from __future__ import annotations

import logging
from typing import Any, List, Mapping, Optional, Sequence
from urllib.parse import quote

from ...domain.enums import ProviderId
from ...domain.models import BranchRef, RepositoryRef
from ...errors import (
    NotFoundError,
    ProviderError,
    ValidationError,
)
from .client import (
    DEFAULT_API_URL,
    GitHubApiConfig,
    GitHubClient,
    MAX_PAGE_SIZE,
)

__all__ = [
    "GitHubBranchService",
    "MAX_BRANCHES",
]


logger = logging.getLogger(__name__)


#: Maximum number of branches collected by `list_branches` in one
#: call. Repositories with more branches require explicit paging by
#: the caller.
MAX_BRANCHES: int = 1_000

#: Default page size when paging through the branches endpoint.
_DEFAULT_PAGE_SIZE: int = 100


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class GitHubBranchService:
    """
    Branch operations against the GitHub REST API.

    The service holds an injected `GitHubClient`. When the client is
    not provided, a default client is built using the supplied
    `api_url`.
    """

    def __init__(
        self,
        *,
        client: Optional[GitHubClient] = None,
        api_url: str = DEFAULT_API_URL,
    ) -> None:
        self._client = client or GitHubClient(
            config=GitHubApiConfig(api_url=api_url)
        )
        self._api_url = self._client.config.api_url

    @property
    def client(self) -> GitHubClient:
        """Return the underlying GitHub HTTP client."""
        return self._client

    @property
    def api_url(self) -> str:
        """Return the API base URL."""
        return self._api_url

    # --- List branches -------------------------------------------------

    async def list_branches(
        self,
        *,
        repo: RepositoryRef,
        token: str,
        default_branch: Optional[str] = None,
        max_branches: int = MAX_BRANCHES,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ) -> Sequence[BranchRef]:
        """
        Return every branch in the repository, up to `max_branches`.

        The GitHub branches endpoint is paginated. This method pages
        through the results until either the repository's branches
        are exhausted or `max_branches` is reached.

        Args:
            repo: The repository reference.
            token: Access token. Required.
            default_branch: Optional name of the default branch.
                When supplied, `BranchRef.is_default` is set
                accordingly. When omitted, no branch is marked as
                the default.
            max_branches: Cap on the number of branches returned.
                Callers that need a full enumeration on very large
                repositories SHOULD set this explicitly.
            page_size: Page size used internally. Capped at 100.

        Raises:
            ValidationError: If `repo` is not a `RepositoryRef`, or
                if `page_size` is out of range.
            NotFoundError: If the repository does not exist.
            PermissionDeniedError: If the token cannot list
                branches.
        """
        if not isinstance(repo, RepositoryRef):
            raise ValidationError(
                "repo must be a RepositoryRef.",
                details={"type": type(repo).__name__},
            )
        if not token:
            raise ValidationError("token must not be empty.")
        if max_branches <= 0:
            raise ValidationError("max_branches must be positive.")
        if page_size <= 0:
            raise ValidationError("page_size must be positive.")

        capped_page_size = min(page_size, MAX_PAGE_SIZE)
        repo_path = self._build_repo_path(repo)

        branches: List[BranchRef] = []
        page = 1

        # Bound the number of pages we are willing to fetch. This
        # protects against a pathological repository with an
        # unbounded number of branches.
        max_pages = (max_branches + capped_page_size - 1) // capped_page_size

        while page <= max_pages:
            params = {
                "per_page": capped_page_size,
                "page": page,
            }
            payload = await self._client.get_json(
                f"{repo_path}/branches",
                token=token,
                params=params,
            )

            if not isinstance(payload, list):
                raise ProviderError(
                    "GitHub returned an unexpected payload for the "
                    "branches endpoint.",
                    details={"page": page},
                )

            if not payload:
                break

            for item in payload:
                if not isinstance(item, Mapping):
                    continue
                try:
                    branch = self._map_branch(
                        item, default_branch=default_branch
                    )
                except ValidationError:
                    logger.debug(
                        "Skipping malformed branch entry on page %d.",
                        page,
                    )
                    continue
                branches.append(branch)
                if len(branches) >= max_branches:
                    break

            if len(branches) >= max_branches:
                break
            if len(payload) < capped_page_size:
                # Last page was partial: no more branches.
                break
            page += 1

        return tuple(branches)

    # --- Single branch -------------------------------------------------

    async def get_branch(
        self,
        *,
        repo: RepositoryRef,
        name: str,
        token: str,
        default_branch: Optional[str] = None,
    ) -> BranchRef:
        """
        Return metadata for a single branch.

        Branch names containing slashes (for example
        `feature/new-thing`) are URL-encoded as a single path
        segment. GitHub accepts `%2F` in this position.

        Args:
            repo: The repository reference.
            name: Branch name.
            token: Access token. Required.
            default_branch: Optional default-branch name for
                `is_default` classification.

        Raises:
            ValidationError: If `name` is empty.
            NotFoundError: If the branch does not exist.
        """
        if not isinstance(repo, RepositoryRef):
            raise ValidationError(
                "repo must be a RepositoryRef.",
                details={"type": type(repo).__name__},
            )
        if not name or not isinstance(name, str):
            raise ValidationError("name must be a non-empty string.")
        if not token:
            raise ValidationError("token must not be empty.")

        repo_path = self._build_repo_path(repo)
        encoded_name = quote(name, safe="")
        payload = await self._client.get_json(
            f"{repo_path}/branches/{encoded_name}",
            token=token,
        )

        if not isinstance(payload, Mapping):
            raise ProviderError(
                "GitHub returned an unexpected payload for the branch "
                "endpoint.",
                details={"branch": name},
            )
        return self._map_branch(payload, default_branch=default_branch)

    # --- Branch protection ---------------------------------------------

    async def get_branch_protection(
        self,
        *,
        repo: RepositoryRef,
        branch_name: str,
        token: str,
    ) -> Optional[Mapping[str, Any]]:
        """
        Return the GitHub branch protection configuration for a branch.

        Calls ``GET /repos/{owner}/{repo}/branches/{branch}/protection``.

        Returns:
            The protection configuration as a mapping when the branch
            IS protected. Returns ``None`` when the branch is NOT
            protected (GitHub responds with 404 for unprotected
            branches — this is a legitimate state, not an error).

        Raises:
            ValidationError: If inputs are malformed.
            PermissionDeniedError: If the token cannot read protection
                settings (usually requires admin scope).
            ProviderError: For other unexpected errors.
        """
        if not isinstance(repo, RepositoryRef):
            raise ValidationError(
                "repo must be a RepositoryRef.",
                details={"type": type(repo).__name__},
            )
        if not branch_name or not isinstance(branch_name, str):
            raise ValidationError(
                "branch_name must be a non-empty string."
            )
        if not token:
            raise ValidationError("token must not be empty.")

        repo_path = self._build_repo_path(repo)
        encoded_name = quote(branch_name, safe="")

        try:
            payload = await self._client.get_json(
                f"{repo_path}/branches/{encoded_name}/protection",
                token=token,
            )
        except NotFoundError:
            # 404 = no protection configured. This is the "not
            # protected" state, not an error. Returning None lets
            # the caller distinguish "unprotected" from "check
            # failed" (which raises).
            return None

        if not isinstance(payload, Mapping):
            raise ProviderError(
                "GitHub returned an unexpected payload for branch "
                "protection.",
                details={"branch": branch_name},
            )
        return payload

    async def is_branch_protected_remotely(
        self,
        *,
        repo: RepositoryRef,
        branch_name: str,
        token: str,
    ) -> Optional[bool]:
        """
        Return whether a branch is protected on GitHub.

        This is a convenience wrapper around
        ``get_branch_protection`` that returns a tri-state answer
        instead of a payload:

            - ``True``  : branch IS protected on GitHub.
            - ``False`` : branch is NOT protected on GitHub (404).
            - ``None``  : the check could not be performed (network
                          error, insufficient permissions, unknown
                          branch). The caller MUST treat this as
                          "unknown, assume protected" — never as
                          "not protected".

        The tri-state is deliberate: a silent ``False`` on failure
        would let a push to a genuinely protected branch proceed
        whenever the GitHub API is momentarily unavailable, which
        defeats the entire safety guarantee.
        """
        try:
            protection = await self.get_branch_protection(
                repo=repo, branch_name=branch_name, token=token
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not determine remote protection for '%s': %s",
                branch_name,
                type(exc).__name__,
            )
            return None
        return protection is not None

    # --- Internal: URL construction ------------------------------------

    @staticmethod
    def _build_repo_path(repo: RepositoryRef) -> str:
        """
        Return the `/repos/{owner}/{name}` path for a repository.

        Kept private to this module to avoid a cross-module coupling
        with `repositories.py`. The construction is trivial enough
        that duplication is acceptable.
        """
        owner = quote(repo.namespace, safe="")
        name = quote(repo.name, safe="")
        if not owner or not name:
            raise ValidationError(
                "Repository namespace and name must not be empty.",
                details={
                    "namespace": repo.namespace,
                    "name": repo.name,
                },
            )
        return f"/repos/{owner}/{name}"

    # --- Internal: payload mapping -------------------------------------

    @staticmethod
    def _map_branch(
        payload: Mapping[str, Any],
        *,
        default_branch: Optional[str],
    ) -> BranchRef:
        """
        Map a GitHub branch payload to `BranchRef`.

        GitHub returns a `commit` object with a `sha`. The commit SHA
        is required; branches without one are rejected.

        The `protected` field is set to `True` when the payload
        reports protection. Older API versions hid protection behind
        a preview header, but the current stable version exposes the
        boolean field unconditionally; we read it defensively.
        """
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            raise ValidationError(
                "Branch payload is missing a valid `name`."
            )

        commit = payload.get("commit")
        head_sha: Optional[str] = None
        if isinstance(commit, Mapping):
            raw_sha = commit.get("sha")
            if isinstance(raw_sha, str) and raw_sha:
                head_sha = raw_sha
        if head_sha is None:
            raise ValidationError(
                "Branch payload is missing `commit.sha`.",
                details={"branch": name},
            )

        is_default = bool(
            default_branch and name == default_branch
        )
        is_protected = bool(payload.get("protected", False))

        return BranchRef(
            name=name,
            head_sha=head_sha,
            is_default=is_default,
            is_protected=is_protected,
        )

    # --- Presentation ---------------------------------------------------

    def __repr__(self) -> str:  # pragma: no cover - trivial formatting
        return f"<GitHubBranchService api_url={self._api_url!r}>"
