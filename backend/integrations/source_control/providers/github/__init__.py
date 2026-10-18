# backend/integrations/source_control/providers/github/__init__.py
"""
GitHub provider adapter for the Source Control Integration subsystem.

This module defines `GitHubProviderAdapter`, the concrete class that
implements the `SourceControlProvider` protocol for GitHub.com and
GitHub Enterprise. It wires together the GitHub-specific sub-services
(client, repositories, branches, pull requests, checks, auth) behind
a single facade that the application layer can use uniformly.

Design rules:
    - All sub-services are lazily constructed on first use. This
      keeps the adapter importable even when a particular sub-service
      module is missing or partially implemented.
    - Every public method accepts an optional `token`. When `None`
      is passed, the adapter raises `AuthRequiredError` immediately
      rather than silently performing an unauthenticated call; the
      caller (application service) is responsible for resolving the
      token from the vault.
    - The adapter never raises raw provider exceptions. All errors
      surfaced to the caller are `SourceControlError` subclasses.
    - The adapter is stateless with respect to per-request data. It
      is safe to share across coroutines.

Repository identity:
    Methods that operate on a repository accept either a
    `RepositoryRef` (when the caller already holds a resolved
    identity) or a plain repository id string (when the caller only
    has the numeric id from the HTTP layer). The string form is
    resolved through GitHub's `GET /repositories/{id}` endpoint,
    which accepts the numeric id directly. This dual signature
    avoids forcing every caller to keep a `RepositoryRef` cache.

Declared-but-unimplemented capabilities:
    `ProviderCapabilities.delete_branch` and
    `ProviderCapabilities.merge_change_request` are declared `True`
    for GitHub, but no corresponding adapter methods exist yet. The
    policy engine blocks these operations by default
    (`test_default_policy_blocks_delete_remote_branch` and
    `test_default_policy_blocks_merge_change_request`). Adding the
    methods here and relaxing the policy is tracked as future work;
    until then, the capability flags reflect "GitHub supports this"
    rather than "this adapter exposes it".
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from ...domain.enums import ProviderId
from ...domain.models import (
    BranchRef,
    ChangeRequestRef,
    CheckRunRef,
    ProviderAccount,
    ProviderCapabilities,
    RepositoryRef,
)
from ...errors import AuthRequiredError, UnsupportedCapabilityError
from ...ports.provider import BaseSourceControlProvider

__all__ = [
    "GitHubProviderAdapter",
    "GITHUB_DEFAULT_BASE_URL",
    "GITHUB_DEFAULT_API_URL",
]


logger = logging.getLogger(__name__)


#: Public web URL for GitHub.com.
GITHUB_DEFAULT_BASE_URL: str = "https://github.com"

#: REST API base URL for GitHub.com.
GITHUB_DEFAULT_API_URL: str = "https://api.github.com"


class GitHubProviderAdapter(BaseSourceControlProvider):
    """
    GitHub adapter implementing `SourceControlProvider`.

    Sub-services are constructed lazily on first use. When a caller
    injects its own sub-service (for tests), the injected instance is
    used instead and the lazy construction is skipped.
    """

    provider_id: str = "github"

    def __init__(
        self,
        *,
        client: Optional[Any] = None,
        repositories: Optional[Any] = None,
        branches: Optional[Any] = None,
        pull_requests: Optional[Any] = None,
        checks: Optional[Any] = None,
        auth: Optional[Any] = None,
        token_service: Optional[Any] = None,
        base_url: str = GITHUB_DEFAULT_BASE_URL,
        api_url: str = GITHUB_DEFAULT_API_URL,
    ) -> None:
        self._client = client
        self._repositories = repositories
        self._branches = branches
        self._pull_requests = pull_requests
        self._checks = checks
        self._auth = auth
        self._token_service = token_service
        self._base_url = base_url.rstrip("/")
        self._api_url = api_url.rstrip("/")

    # --- Capabilities ---------------------------------------------------

    @property
    def capabilities(self) -> ProviderCapabilities:
        """
        Return the capabilities declared by this GitHub adapter.

        GitHub supports every capability defined by the neutral model
        except self-managed-instance-only capabilities, which depend
        on the API URL the adapter was configured with.
        """
        return ProviderCapabilities(
            list_repositories=True,
            read_files=True,
            create_branch=True,
            push_branch=True,
            create_change_request=True,
            list_checks=True,
            comments=True,
            merge_change_request=True,
            delete_branch=True,
            self_managed_instance=self._api_url != GITHUB_DEFAULT_API_URL,
        )

    @property
    def base_url(self) -> str:
        """Return the public web URL for this GitHub instance."""
        return self._base_url

    @property
    def api_url(self) -> str:
        """Return the REST API URL for this GitHub instance."""
        return self._api_url

    # --- Auth -----------------------------------------------------------

    async def get_authenticated_account(
        self, token: Optional[str] = None
    ) -> ProviderAccount:
        """
        Return the account associated with `token`.

        The `token` argument is REQUIRED: the adapter does not resolve
        credentials itself. The application service is responsible for
        fetching the token from the vault before calling this method.
        """
        if not token:
            raise AuthRequiredError(
                "A non-empty token is required to fetch the GitHub "
                "account."
            )
        service = self._get_auth_service()
        # The auth service exposes `get_authenticated_account(token)`.
        return await service.get_authenticated_account(token)

    # --- Repositories ---------------------------------------------------

    async def list_repositories(
        self,
        query: Optional[str] = None,
        page: int = 1,
        page_size: int = 30,
        token: Optional[str] = None,
    ) -> Sequence[RepositoryRef]:
        """
        List repositories accessible to the authenticated account.

        The GitHub REST API does not support a case-insensitive
        substring filter directly, so the adapter applies `query`
        client-side after fetching a page. This matches the behavior
        of the other provider adapters.
        """
        if not token:
            raise AuthRequiredError(
                "A non-empty token is required to list repositories."
            )
        service = self._get_repository_service()
        repositories = await service.list_repositories(
            token=token, page=page, page_size=page_size
        )
        if not query:
            return tuple(repositories)
        needle = query.strip().lower()
        return tuple(
            repo
            for repo in repositories
            if needle in f"{repo.namespace}/{repo.name}".lower()
        )

    async def get_repository(
        self,
        repo: RepositoryRef | str,
        token: Optional[str] = None,
    ) -> RepositoryRef:
        """
        Fetch repository metadata.

        Accepts either a `RepositoryRef` (already-resolved identity)
        or a plain repository id string. The string form is resolved
        via the GitHub `GET /repositories/{id}` endpoint, which
        accepts the stable numeric ID that the `repositories` service
        stores in `RepositoryRef.repository_id`. This dual signature
        keeps the adapter usable from both the application service
        (which has only a string id from the HTTP layer) and from
        lower-level callers that already hold a `RepositoryRef`.
        """
        if not token:
            raise AuthRequiredError(
                "A non-empty token is required to fetch repository "
                "details."
            )
        if isinstance(repo, RepositoryRef):
            service = self._get_repository_service()
            return await service.get_repository(repo=repo, token=token)
        if isinstance(repo, str):
            return await self.get_repository_by_id(repo, token=token)
        raise ValidationError(
            "repo must be a RepositoryRef or a non-empty repository "
            "id string.",
            details={"type": type(repo).__name__},
        )

    async def get_repository_by_id(
        self,
        repository_id: str,
        token: Optional[str] = None,
    ) -> RepositoryRef:
        """
        Fetch repository metadata by its stable numeric ID.

        GitHub exposes a dedicated endpoint (`GET /repositories/{id}`)
        that accepts the numeric repository identifier directly. When
        the underlying `GitHubRepositoryService` later grows a
        matching method, it is used; otherwise the adapter calls the
        endpoint itself and reuses the service's private payload
        mapper, keeping the parsing logic centralized in one place.
        """
        if not repository_id or not isinstance(repository_id, str):
            raise ValidationError(
                "repository_id must be a non-empty string."
            )
        if not token:
            raise AuthRequiredError(
                "A non-empty token is required to fetch repository "
                "details."
            )
        service = self._get_repository_service()

        # Prefer the service method when it exists (forward-
        # compatible with a future `GitHubRepositoryService.
        # get_repository_by_id`).
        explicit = getattr(service, "get_repository_by_id", None)
        if callable(explicit):
            return await explicit(repository_id, token=token)

        client = self._get_client()
        encoded = quote(repository_id, safe="")
        payload = await client.get_json(
            f"/repositories/{encoded}", token=token
        )
        if not isinstance(payload, Mapping):
            raise ProviderError(
                "GitHub returned an unexpected payload for the "
                "repository lookup.",
                details={"repository_id": repository_id},
            )
        mapper = getattr(service, "_map_repository", None)
        if not callable(mapper):
            raise UnsupportedCapabilityError(
                "Repository mapping is unavailable in the current "
                "service.",
                details={"provider": self.provider_id},
            )
        return mapper(payload)

    async def list_branches(
        self,
        repo: RepositoryRef,
        token: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Sequence[BranchRef]:
        """
        Return branches in the repository, up to `limit`.

        When `limit` is provided, it is passed to the underlying
        service as `max_branches` so that the caller does not pay for
        branches that will be discarded, and the result is
        additionally sliced to guarantee the exact cap even if the
        service returns more than requested.
        """
        if not token:
            raise AuthRequiredError(
                "A non-empty token is required to list branches."
            )
        if limit is not None and limit <= 0:
            raise ValidationError("limit must be a positive integer.")

        service = self._get_branch_service()
        kwargs: dict[str, Any] = {
            "repo": repo,
            "token": token,
            "default_branch": repo.default_branch,
        }
        if limit is not None:
            kwargs["max_branches"] = limit

        branches = await service.list_branches(**kwargs)
        if limit is not None:
            return tuple(branches[:limit])
        return tuple(branches)

    async def create_branch(
        self,
        repo: RepositoryRef,
        branch_name: str,
        from_sha: str,
        token: Optional[str] = None,
    ) -> BranchRef:
        """
        Create a new branch on the remote repository.

        The GitHub API represents branches as Git refs under
        `refs/heads/<name>`; the corresponding write endpoint is
        `POST /repos/{owner}/{repo}/git/refs`. When the underlying
        `GitHubBranchService` later grows a matching method, it is
        used; otherwise the adapter calls the endpoint itself.

        The returned `BranchRef` is constructed optimistically: the
        branch is brand new, so it is not the default branch and any
        protection is only enforceable once GitHub has applied its
        rules. Callers that need authoritative protection state
        SHOULD call `list_branches` or `get_branch` after creation.
        """
        if not token:
            raise AuthRequiredError(
                "A non-empty token is required to create a branch."
            )
        if not branch_name or not isinstance(branch_name, str):
            raise ValidationError(
                "branch_name must be a non-empty string."
            )
        if not from_sha or not isinstance(from_sha, str):
            raise ValidationError(
                "from_sha must be a non-empty string."
            )

        service = self._get_branch_service()

        # Prefer the service method when it exists (forward-
        # compatible with a future `GitHubBranchService.create_branch`).
        explicit = getattr(service, "create_branch", None)
        if callable(explicit):
            return await explicit(
                repo=repo,
                branch_name=branch_name,
                from_sha=from_sha,
                token=token,
            )

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

        client = self._get_client()
        endpoint = f"/repos/{owner}/{name}/git/refs"
        body = {
            "ref": f"refs/heads/{branch_name}",
            "sha": from_sha,
        }
        response = await client.post_json(
            endpoint, token=token, json_body=body
        )
        if not isinstance(response, Mapping):
            raise ProviderError(
                "GitHub returned an unexpected payload when creating "
                "the branch.",
                details={"branch_name": branch_name},
            )

        # The response carries the resolved SHA in `object.sha`. Fall
        # back to `from_sha` when the field is missing.
        head_sha = from_sha
        obj = response.get("object")
        if isinstance(obj, Mapping):
            candidate = obj.get("sha")
            if isinstance(candidate, str) and candidate:
                head_sha = candidate

        return BranchRef(
            name=branch_name,
            head_sha=head_sha,
            is_default=False,
            is_protected=False,
        )

    async def read_file(
        self,
        repo: RepositoryRef,
        file_path: str,
        ref: Optional[str] = None,
        token: Optional[str] = None,
    ) -> str:
        """Read a text file at a given ref."""
        if not token:
            raise AuthRequiredError(
                "A non-empty token is required to read a file."
            )
        service = self._get_repository_service()
        return await service.read_file(
            repo=repo, path=file_path, ref=ref, token=token
        )

    # --- Change requests ------------------------------------------------

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
        """Create a pull request."""
        if not token:
            raise AuthRequiredError(
                "A non-empty token is required to create a change "
                "request."
            )
        service = self._get_pull_request_service()
        return await service.create_pull_request(
            repo=repo,
            source_branch=source_branch,
            target_branch=target_branch,
            title=title,
            body=body,
            draft=draft,
            token=token,
        )

    # --- Checks ---------------------------------------------------------

    async def list_checks(
        self,
        repo: RepositoryRef,
        ref: str,
        token: Optional[str] = None,
    ) -> Sequence[CheckRunRef]:
        """Return CI check runs for a commit."""
        if not token:
            raise AuthRequiredError(
                "A non-empty token is required to list checks."
            )
        service = self._get_checks_service()
        return await service.list_checks(repo=repo, ref=ref, token=token)

    # --- Lazy sub-service factories ------------------------------------

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from .client import GitHubClient, GitHubApiConfig
        except ImportError as exc:  # noqa: BLE001
            raise UnsupportedCapabilityError(
                "The GitHub HTTP client is not available.",
                details={"provider": self.provider_id},
                cause=exc,
            ) from exc
        self._client = GitHubClient(
            config=GitHubApiConfig(api_url=self._api_url)
        )
        return self._client

    def _get_repository_service(self) -> Any:
        if self._repositories is not None:
            return self._repositories
        try:
            from .repositories import GitHubRepositoryService
        except ImportError as exc:  # noqa: BLE001
            raise UnsupportedCapabilityError(
                "The GitHub repository service is not available.",
                details={"provider": self.provider_id},
                cause=exc,
            ) from exc
        self._repositories = GitHubRepositoryService(
            client=self._get_client()
        )
        return self._repositories

    def _get_branch_service(self) -> Any:
        if self._branches is not None:
            return self._branches
        try:
            from .branches import GitHubBranchService
        except ImportError as exc:  # noqa: BLE001
            raise UnsupportedCapabilityError(
                "The GitHub branch service is not available.",
                details={"provider": self.provider_id},
                cause=exc,
            ) from exc
        self._branches = GitHubBranchService(client=self._get_client())
        return self._branches

    def _get_pull_request_service(self) -> Any:
        if self._pull_requests is not None:
            return self._pull_requests
        try:
            from .pull_requests import GitHubPullRequestService
        except ImportError as exc:  # noqa: BLE001
            raise UnsupportedCapabilityError(
                "The GitHub pull-request service is not available.",
                details={"provider": self.provider_id},
                cause=exc,
            ) from exc
        self._pull_requests = GitHubPullRequestService(
            client=self._get_client()
        )
        return self._pull_requests

    def _get_checks_service(self) -> Any:
        if self._checks is not None:
            return self._checks
        try:
            from .checks import GitHubChecksService
        except ImportError as exc:  # noqa: BLE001
            raise UnsupportedCapabilityError(
                "The GitHub checks service is not available.",
                details={"provider": self.provider_id},
                cause=exc,
            ) from exc
        self._checks = GitHubChecksService(client=self._get_client())
        return self._checks

    def _get_auth_service(self) -> Any:
        if self._auth is not None:
            return self._auth
        try:
            from .auth import GitHubAuthService
        except ImportError as exc:  # noqa: BLE001
            raise UnsupportedCapabilityError(
                "The GitHub auth service is not available.",
                details={"provider": self.provider_id},
                cause=exc,
            ) from exc
        self._auth = GitHubAuthService(client=self._get_client())
        return self._auth

    def __repr__(self) -> str:  # pragma: no cover - trivial formatting
        return (
            f"<GitHubProviderAdapter api_url={self._api_url!r} "
            f"base_url={self._base_url!r}>"
        )
