# backend/integrations/source_control/providers/github/__init__.py
"""
GitHub provider adapter for the Source Control Integration
subsystem.

This module implements the `SourceControlProvider` protocol for
GitHub.com and GitHub Enterprise. It is the sole entry point for
GitHub-specific behavior: URL construction, authentication,
pagination, error translation, and data mapping all happen here or
in the helper modules under this package.

Design rules:
    - The adapter inherits from `BaseSourceControlProvider`, which
      supplies default implementations for `read_file_content`,
      `get_branch`, and `validate_connection`. The adapter overrides
      those when a provider-specific implementation is materially
      better.
    - All HTTP requests go through the injected `GitHubClient`. The
      adapter does not import `httpx` directly.
    - Every public method accepts an optional `token`. When `None`,
      the adapter resolves the token via the injected
      `TokenService`. The token is NEVER logged, embedded in a URL,
      or returned to the caller.
    - Provider-specific exceptions are translated to the
      `SourceControlError` hierarchy before being raised. Callers
      can rely on the normalized error categories.
    - The adapter is stateless with respect to per-request data. It
      is safe to share across coroutines. The only mutable state is
      a lazily-populated account cache used to feed `TokenService`.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

from ...domain.enums import ChangeRequestState, ProviderId
from ...domain.models import (
    BranchRef,
    ChangeRequestRef,
    CheckRunRef,
    ProviderAccount,
    ProviderCapabilities,
    RepositoryRef,
)
from ...errors import (
    AuthRequiredError,
    ConflictError,
    NetworkError,
    NotFoundError,
    PermissionDeniedError,
    ProviderError,
    RateLimitError,
    UnsupportedCapabilityError,
    ValidationError,
)
from ...ports.provider import BaseSourceControlProvider

__all__ = [
    "GitHubProviderAdapter",
    "GITHUB_DEFAULT_BASE_URL",
    "GITHUB_DEFAULT_API_URL",
]


logger = logging.getLogger(__name__)


#: Public web URL for GitHub.com. Used when constructing URLs for
#: user-facing links.
GITHUB_DEFAULT_BASE_URL: str = "https://github.com"

#: REST API base URL for GitHub.com.
GITHUB_DEFAULT_API_URL: str = "https://api.github.com"

#: Default scopes requested for read-write access. The set is kept
#: deliberately minimal; broader scopes MUST be requested explicitly
#: by the caller.
GITHUB_DEFAULT_SCOPES: tuple[str, ...] = (
    "read:user",
    "user:email",
    "repo",
)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class GitHubProviderAdapter(BaseSourceControlProvider):
    """
    GitHub adapter implementing `SourceControlProvider`.

    The adapter delegates the actual work to helper objects that are
    injected at construction time. When a helper is omitted, the
    adapter lazily builds a default instance using the standard
    GitHub REST API. This lazy construction allows the adapter to be
    instantiated in tests that override individual helpers without
    pulling in the entire dependency graph.

    Attributes:
        provider_id: The stable identifier `"github"`.
    """

    provider_id: str = "github"

    def __init__(
        self,
        *,
        client: Optional[object] = None,
        repositories: Optional[object] = None,
        branches: Optional[object] = None,
        pull_requests: Optional[object] = None,
        checks: Optional[object] = None,
        token_service: Optional[object] = None,
        base_url: str = GITHUB_DEFAULT_BASE_URL,
        api_url: str = GITHUB_DEFAULT_API_URL,
    ) -> None:
        self._client = client
        self._repositories = repositories
        self._branches = branches
        self._pull_requests = pull_requests
        self._checks = checks
        self._token_service = token_service
        self._base_url = base_url.rstrip("/")
        self._api_url = api_url.rstrip("/")

        # Cached account information, used to resolve vault keys when
        # a caller invokes a method without an explicit token and
        # without having previously called `get_authenticated_account`.
        self._account_cache: Optional[ProviderAccount] = None

    # --- Capabilities ---------------------------------------------------

    @property
    def capabilities(self) -> ProviderCapabilities:
        """
        Declare the capabilities supported by this adapter.

        GitHub supports every capability defined by the neutral
        model, except:
            - `self_managed_instance` is only True when the caller
              supplied a non-default `api_url`. The adapter cannot
              know whether the instance is self-managed, so it
              reports `True` whenever a custom URL is provided.
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

    # --- Authentication -------------------------------------------------

    async def get_authenticated_account(
        self, token: Optional[str] = None
    ) -> ProviderAccount:
        """
        Return the account associated with the current credential.

        The result is cached for the lifetime of the adapter so that
        subsequent calls that omit `token` can resolve the vault key
        without an additional API request.

        Raises:
            AuthRequiredError: If no credential is available.
            AuthExpiredError: If the credential has expired.
            NetworkError: On transport failure.
        """
        resolved_token = await self._resolve_token(token)
        try:
            account = await self._client_get_authenticated_account(
                resolved_token
            )
        except Exception as exc:  # noqa: BLE001
            raise _translate_exception(exc) from exc

        # Normalize the base URL to this adapter's configured value.
        account = _ensure_base_url(account, self._base_url)
        self._account_cache = account
        return account

    async def validate_connection(
        self, token: Optional[str] = None
    ) -> bool:
        """
        Verify that the credential is valid.

        Returns True on success, False when the credential is
        rejected. Raises `NetworkError` on transport failures so
        that callers can distinguish "invalid credential" from
        "GitHub unreachable".
        """
        try:
            await self.get_authenticated_account(token=token)
            return True
        except (AuthRequiredError, PermissionDeniedError):
            return False
        except NetworkError:
            raise
        except ProviderError:
            return False

    # --- Repositories ---------------------------------------------------

    async def list_repositories(
        self,
        query: Optional[str] = None,
        page: int = 1,
        page_size: int = 30,
        token: Optional[str] = None,
    ) -> Sequence[RepositoryRef]:
        """
        List repositories the authenticated account can access.

        The listing is filtered client-side when `query` is provided.
        GitHub's `affiliation` parameter is used to include
        repositories owned by the user, their organizations, and
        those they collaborate on.

        Args:
            query: Optional case-insensitive substring filter
                applied to `namespace/name`.
            page: 1-based page number. Defaults to 1.
            page_size: Number of repositories per page. Capped at
                100 by GitHub.
            token: Optional credential override.

        Raises:
            ValidationError: If `page` or `page_size` is out of range.
            AuthRequiredError: If no credential is available.
            RateLimitError: If the GitHub rate limit is exceeded.
        """
        if page < 1:
            raise ValidationError("page must be >= 1.")
        if page_size < 1:
            raise ValidationError("page_size must be >= 1.")
        if page_size > 100:
            page_size = 100

        resolved_token = await self._resolve_token(token)
        try:
            repositories = await self._repositories_list(
                token=resolved_token, page=page, page_size=page_size
            )
        except Exception as exc:  # noqa: BLE001
            raise _translate_exception(exc) from exc

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
        repo: RepositoryRef,
        token: Optional[str] = None,
    ) -> RepositoryRef:
        """
        Fetch fresh metadata for a repository.

        Raises:
            ValidationError: If `repo` is not a `RepositoryRef`.
            NotFoundError: If the repository no longer exists.
        """
        if not isinstance(repo, RepositoryRef):
            raise ValidationError(
                "repo must be a RepositoryRef.",
                details={"type": type(repo).__name__},
            )
        resolved_token = await self._resolve_token(token)
        try:
            return await self._repositories_get(
                repo=repo, token=resolved_token
            )
        except Exception as exc:  # noqa: BLE001
            raise _translate_exception(exc) from exc

    async def list_branches(
        self,
        repo: RepositoryRef,
        token: Optional[str] = None,
    ) -> Sequence[BranchRef]:
        """Return every branch in the repository, up to GitHub's page cap."""
        resolved_token = await self._resolve_token(token)
        try:
            return await self._branches_list(
                repo=repo, token=resolved_token
            )
        except Exception as exc:  # noqa: BLE001
            raise _translate_exception(exc) from exc

    async def get_branch(
        self,
        repo: RepositoryRef,
        name: str,
        token: Optional[str] = None,
    ) -> BranchRef:
        """
        Return metadata for a single branch.

        Overrides the default implementation from
        `BaseSourceControlProvider`, which would list every branch.
        This direct call is cheaper and does not require paging
        through the branch list.
        """
        if not name:
            raise ValidationError("name must not be empty.")
        resolved_token = await self._resolve_token(token)
        try:
            return await self._branches_get(
                repo=repo, name=name, token=resolved_token
            )
        except Exception as exc:  # noqa: BLE001
            raise _translate_exception(exc) from exc

    # --- Files ----------------------------------------------------------

    async def read_file(
        self,
        repo: RepositoryRef,
        file_path: str,
        ref: Optional[str] = None,
        token: Optional[str] = None,
    ) -> str:
        """
        Read a text file at a given ref.

        When `ref` is `None`, the repository's default branch is
        used. The returned string is decoded as UTF-8 with
        replacement for invalid sequences.

        Note: `read_file_content` is provided as an alias by the
        base class, so callers that use the older name continue to
        work.

        Raises:
            ValidationError: If `file_path` is empty.
            NotFoundError: If the file does not exist at the ref.
            UnsupportedCapabilityError: If the file is too large for
                the GitHub contents API (larger than ~1 MiB).
        """
        if not file_path:
            raise ValidationError("file_path must not be empty.")
        resolved_token = await self._resolve_token(token)
        try:
            content = await self._repositories_read_file(
                repo=repo,
                path=file_path,
                ref=ref,
                token=resolved_token,
            )
        except Exception as exc:  # noqa: BLE001
            raise _translate_exception(exc) from exc
        return content

    # --- Change requests -----------------------------------------------

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
        Create a pull request.

        `target_branch` is used as the PR base. `source_branch` MUST
        exist on the remote; the adapter does not create it.

        Raises:
            ValidationError: If any input is empty.
            ConflictError: If a PR already exists for the same
                source/target pair.
        """
        if not source_branch or not target_branch:
            raise ValidationError(
                "source_branch and target_branch are required."
            )
        if not title:
            raise ValidationError("title must not be empty.")

        resolved_token = await self._resolve_token(token)
        try:
            return await self._pull_requests_create(
                repo=repo,
                source_branch=source_branch,
                target_branch=target_branch,
                title=title,
                body=body or "",
                draft=draft,
                token=resolved_token,
            )
        except Exception as exc:  # noqa: BLE001
            raise _translate_exception(exc) from exc

    async def get_change_request(
        self,
        repo: RepositoryRef,
        number_or_iid: str,
        token: Optional[str] = None,
    ) -> ChangeRequestRef:
        """
        Fetch a single pull request by number.
        """
        if not number_or_iid:
            raise ValidationError("number_or_iid must not be empty.")
        resolved_token = await self._resolve_token(token)
        try:
            return await self._pull_requests_get(
                repo=repo,
                number_or_iid=number_or_iid,
                token=resolved_token,
            )
        except Exception as exc:  # noqa: BLE001
            raise _translate_exception(exc) from exc

    # --- Checks ---------------------------------------------------------

    async def list_checks(
        self,
        repo: RepositoryRef,
        ref: str,
        token: Optional[str] = None,
    ) -> Sequence[CheckRunRef]:
        """
        List check runs for a commit SHA or branch name.

        The GitHub `checks` API is queried by SHA. When `ref` is a
        branch name, the adapter resolves it to a SHA first.
        """
        if not ref:
            raise ValidationError("ref must not be empty.")
        resolved_token = await self._resolve_token(token)
        try:
            return await self._checks_list(
                repo=repo, ref=ref, token=resolved_token
            )
        except Exception as exc:  # noqa: BLE001
            raise _translate_exception(exc) from exc

    # --- Helpers --------------------------------------------------------

    async def _resolve_token(self, token: Optional[str]) -> str:
        """
        Return a usable token.

        Resolution order:
            1. The `token` argument, when provided.
            2. The token service, when a cached account is available.
            3. The token service, keyed by the default identity.

        Raises:
            AuthRequiredError: If no token is available.
        """
        if token:
            return token

        if self._token_service is None:
            raise AuthRequiredError(
                "No credential is available and no token service is "
                "configured."
            )

        account = self._account_cache
        if account is None:
            raise AuthRequiredError(
                "No credential is available. Call "
                "`get_authenticated_account` first or provide a token."
            )

        resolved = self._token_service.get_access_token(
            ProviderId.GITHUB, account.account_id, self._base_url
        )
        if not resolved:
            raise AuthRequiredError(
                "No credential is available in the vault for the "
                "current account."
            )
        return resolved

    # --- Delegation: sub-services --------------------------------------

    async def _client_get_authenticated_account(
        self, token: str
    ) -> ProviderAccount:
        """
        Delegate to the injected client.

        The client is imported lazily so that this module can be
        loaded without the client's dependencies being installed.
        """
        client = self._client or _default_client(self._api_url)
        try:
            return await client.get_authenticated_account(token=token)
        except NotImplementedError as exc:
            raise ProviderError(
                "GitHub client does not implement account lookup."
            ) from exc

    async def _repositories_list(
        self, *, token: str, page: int, page_size: int
    ) -> Sequence[RepositoryRef]:
        service = self._repositories or _default_repositories(self._api_url)
        return await service.list_repositories(
            token=token, page=page, page_size=page_size
        )

    async def _repositories_get(
        self, *, repo: RepositoryRef, token: str
    ) -> RepositoryRef:
        service = self._repositories or _default_repositories(self._api_url)
        return await service.get_repository(repo=repo, token=token)

    async def _repositories_read_file(
        self,
        *,
        repo: RepositoryRef,
        path: str,
        ref: Optional[str],
        token: str,
    ) -> str:
        service = self._repositories or _default_repositories(self._api_url)
        return await service.read_file(
            repo=repo, path=path, ref=ref, token=token
        )

    async def _branches_list(
        self, *, repo: RepositoryRef, token: str
    ) -> Sequence[BranchRef]:
        service = self._branches or _default_branches(self._api_url)
        return await service.list_branches(repo=repo, token=token)

    async def _branches_get(
        self, *, repo: RepositoryRef, name: str, token: str
    ) -> BranchRef:
        service = self._branches or _default_branches(self._api_url)
        return await service.get_branch(repo=repo, name=name, token=token)

    async def _pull_requests_create(
        self,
        *,
        repo: RepositoryRef,
        source_branch: str,
        target_branch: str,
        title: str,
        body: str,
        draft: bool,
        token: str,
    ) -> ChangeRequestRef:
        service = self._pull_requests or _default_pull_requests(self._api_url)
        return await service.create_pull_request(
            repo=repo,
            source_branch=source_branch,
            target_branch=target_branch,
            title=title,
            body=body,
            draft=draft,
            token=token,
        )

    async def _pull_requests_get(
        self,
        *,
        repo: RepositoryRef,
        number_or_iid: str,
        token: str,
    ) -> ChangeRequestRef:
        service = self._pull_requests or _default_pull_requests(self._api_url)
        return await service.get_pull_request(
            repo=repo, number_or_iid=number_or_iid, token=token
        )

    async def _checks_list(
        self, *, repo: RepositoryRef, ref: str, token: str
    ) -> Sequence[CheckRunRef]:
        service = self._checks or _default_checks(self._api_url)
        return await service.list_checks(repo=repo, ref=ref, token=token)


# ---------------------------------------------------------------------------
# Lazy default factories
# ---------------------------------------------------------------------------
#
# Each factory imports its target module inside the function body.
# This lets the adapter class be imported even when the corresponding
# sub-module is not yet implemented. When a caller invokes a method
# that requires a missing sub-module, the factory raises a
# `NotImplementedError` with a clear message instead of an
# `ImportError` deep inside the call stack.


def _default_client(api_url: str) -> object:
    """Return the default GitHub HTTP client."""
    try:
        from .client import GitHubClient  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NotImplementedError(
            "The GitHub HTTP client is not implemented yet."
        ) from exc
    return GitHubClient(api_url=api_url)


def _default_repositories(api_url: str) -> object:
    """Return the default GitHub repository service."""
    try:
        from .repositories import GitHubRepositoryService  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NotImplementedError(
            "The GitHub repository service is not implemented yet."
        ) from exc
    return GitHubRepositoryService(api_url=api_url)


def _default_branches(api_url: str) -> object:
    """Return the default GitHub branch service."""
    try:
        from .branches import GitHubBranchService  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NotImplementedError(
            "The GitHub branch service is not implemented yet."
        ) from exc
    return GitHubBranchService(api_url=api_url)


def _default_pull_requests(api_url: str) -> object:
    """Return the default GitHub pull-request service."""
    try:
        from .pull_requests import GitHubPullRequestService  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NotImplementedError(
            "The GitHub pull-request service is not implemented yet."
        ) from exc
    return GitHubPullRequestService(api_url=api_url)


def _default_checks(api_url: str) -> object:
    """Return the default GitHub checks service."""
    try:
        from .checks import GitHubChecksService  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NotImplementedError(
            "The GitHub checks service is not implemented yet."
        ) from exc
    return GitHubChecksService(api_url=api_url)


# ---------------------------------------------------------------------------
# Exception translation
# ---------------------------------------------------------------------------


def _translate_exception(exc: BaseException) -> BaseException:
    """
    Map a provider-specific exception to a `SourceControlError`.

    Already-normalized errors are returned unchanged. `httpx` status
    errors are mapped by HTTP status code. Everything else becomes a
    `ProviderError` with a redacted message.
    """
    # `SourceControlError` subclasses pass through unchanged.
    from ...errors import SourceControlError

    if isinstance(exc, SourceControlError):
        return exc

    # httpx has an optional dependency; import lazily.
    try:
        import httpx  # type: ignore[import-not-found]
    except ImportError:
        httpx = None  # type: ignore[assignment]

    if httpx is not None:
        if isinstance(exc, httpx.HTTPStatusError):
            return _translate_status_error(exc)
        if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
            return NetworkError(
                "GitHub API request failed due to a network error.",
                cause=exc,
            )
        if isinstance(exc, httpx.RequestError):
            return NetworkError(
                "GitHub API request failed.",
                cause=exc,
            )

    if isinstance(exc, TimeoutError):
        return NetworkError("GitHub API request timed out.", cause=exc)

    if isinstance(exc, (ValueError, TypeError)):
        return ValidationError(str(exc), cause=exc)

    return ProviderError(
        "GitHub API returned an unexpected error.",
        details={"reason": type(exc).__name__},
        cause=exc,
    )


def _translate_status_error(exc: BaseException) -> BaseException:
    """
    Translate an `httpx.HTTPStatusError` into a `SourceControlError`.

    The mapping is based on the HTTP status code. Response bodies
    are NOT included in the error message; only the status code and
    a short description are surfaced.
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 401:
        return AuthRequiredError(
            "GitHub rejected the credential.",
            cause=exc,
        )
    if status == 403:
        # 403 is overloaded: it can be a permission issue or a rate
        # limit. Check for the rate-limit headers when available.
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", {}) or {}
        if headers.get("x-ratelimit-remaining") == "0":
            return RateLimitError(
                "GitHub rate limit exceeded.",
                details={
                    "reset": headers.get("x-ratelimit-reset"),
                },
                cause=exc,
            )
        return PermissionDeniedError(
            "GitHub denied the request.",
            cause=exc,
        )
    if status == 404:
        return NotFoundError(
            "GitHub resource not found.",
            cause=exc,
        )
    if status == 409:
        return ConflictError(
            "GitHub reported a conflict.",
            cause=exc,
        )
    if status == 422:
        return ValidationError(
            "GitHub rejected the request as invalid.",
            cause=exc,
        )
    if status == 429:
        return RateLimitError(
            "GitHub rate limit exceeded.",
            cause=exc,
        )
    if status is not None and 500 <= status < 600:
        return ProviderError(
            f"GitHub returned a server error ({status}).",
            cause=exc,
        )
    return ProviderError(
        f"GitHub returned an unexpected status ({status}).",
        cause=exc,
    )


def _ensure_base_url(
    account: ProviderAccount, base_url: str
) -> ProviderAccount:
    """
    Return `account` with `base_url` normalized.

    GitHub's user endpoint returns a `html_url` pointing to the
    user profile, not the instance URL. When the adapter is
    configured for a specific instance, we prefer the configured
    value so that vault keys stay consistent.
    """
    from dataclasses import replace

    if account.base_url == base_url:
        return account
    return replace(account, base_url=base_url)
