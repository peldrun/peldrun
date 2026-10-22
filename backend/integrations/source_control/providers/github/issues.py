# backend/integrations/source_control/providers/github/issues.py
"""
GitHub issue service for the Source Control Integration subsystem.

This service implements the issue-related subset of the pull
request / issue workflow for GitHub. It is used by the source
control facade and is not intended to be called directly by the
application layer.

Endpoints used:

    - ``GET   /repos/{owner}/{repo}/issues``
      List issues (server-side state/labels filters).
    - ``GET   /repos/{owner}/{repo}/issues/{number}``
      Fetch a single issue.
    - ``POST  /repos/{owner}/{repo}/issues``
      Create an issue.
    - ``PATCH /repos/{owner}/{repo}/issues/{number}``
      Update state (close / reopen).
    - ``GET   /repos/{owner}/{repo}/issues/{number}/comments``
      List issue comments.
    - ``POST  /repos/{owner}/{repo}/issues/{number}/comments``
      Create an issue comment.

QUIRK: PULL REQUESTS ARE ISSUES.

    GitHub's ``/issues`` endpoints return BOTH issues and pull
    requests, distinguished by the ``pull_request`` field in the
    payload. This service filters PRs out on every listing unless
    the caller explicitly opts in via ``include_pull_requests``.
    Without the filter, a user asking for "open issues" would see
    every open PR as well.

Design rules:
    - All HTTP goes through the shared `GitHubClient`, which owns
      retries, rate-limit handling, and error mapping.
    - Issue and comment dataclasses are defined in this module.
      Promotin them to ``domain/models.py`` is a larger refactor
      that is out of scope for this phase.
    - The service never logs tokens, issue bodies, or comment
      bodies in full. Bodies are redacted before being returned.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import quote

from ...domain.models import RepositoryRef
from ...errors import (
    ProviderError,
    ValidationError,
)
from ...security.redaction import redact_text
from .client import (
    DEFAULT_API_URL,
    GitHubApiConfig,
    GitHubClient,
    MAX_PAGE_SIZE,
)

__all__ = [
    "GitHubIssueService",
    "IssueRef",
    "IssueCommentRef",
    "MAX_ISSUES",
    "MAX_ISSUE_COMMENTS",
]


logger = logging.getLogger(__name__)


#: Maximum number of issues returned in a single listing.
MAX_ISSUES: int = 500

#: Maximum number of comments returned per listing.
MAX_ISSUE_COMMENTS: int = 1_000

#: Default page size used when paging through listing endpoints.
_DEFAULT_PAGE_SIZE: int = 100

#: Valid issue states accepted by GitHub's create/update endpoints.
_VALID_STATES: frozenset[str] = frozenset({"open", "closed"})

#: Valid closure reasons accepted by GitHub.
_VALID_STATE_REASONS: frozenset[str] = frozenset(
    {"completed", "not_planned", "reopened"}
)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IssueRef:
    """A single issue on a repository.

    ``is_pull_request`` is True when GitHub's underlying resource is
    a pull request. Callers that render a combined timeline can use
    it to route PR-backed entries to the PR detail view instead of
    the issue detail view.
    """

    issue_id: str
    number: str
    title: str
    body: Optional[str]
    state: str
    author_login: Optional[str] = None
    labels: Sequence[str] = field(default_factory=tuple)
    comments_count: int = 0
    is_pull_request: bool = False
    state_reason: Optional[str] = None
    web_url: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None


@dataclass(frozen=True)
class IssueCommentRef:
    """A single comment on an issue (or general comment on a PR)."""

    comment_id: str
    body: str
    author_login: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class GitHubIssueService:
    """Issue operations against the GitHub REST API."""

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

    # --- List ----------------------------------------------------------

    async def list_issues(
        self,
        *,
        repo: RepositoryRef,
        token: str,
        state: str = "open",
        labels: Optional[Sequence[str]] = None,
        page: int = 1,
        page_size: int = _DEFAULT_PAGE_SIZE,
        max_results: int = MAX_ISSUES,
        include_pull_requests: bool = False,
    ) -> Sequence[IssueRef]:
        """List issues for a repository.

        Pull requests are filtered out by default. Pass
        ``include_pull_requests=True`` to receive them alongside
        real issues (GitHub returns both from the same endpoint).

        Args:
            repo: The repository reference.
            token: Access token. Required.
            state: ``"open"``, ``"closed"``, or ``"all"``.
                GitHub's API understands these three values
                directly; no client-side remapping is needed for
                issues (unlike PRs, which merge closed PRs into
                "closed" server-side).
            labels: Optional label filter. Empty/None means no
                filter.
            page: 1-based page number.
            page_size: Page size, capped at 100.
            max_results: Cap on the number of results returned.
            include_pull_requests: When True, PRs are included in
                the response (each carries ``is_pull_request=True``).

        Raises:
            ValidationError: If inputs are malformed.
        """
        self._validate_repo_and_token(repo=repo, token=token)
        clean_state = (state or "open").strip().lower()
        if clean_state not in ("open", "closed", "all"):
            raise ValidationError(
                "state must be 'open', 'closed', or 'all'.",
                details={"state": state},
            )
        if max_results <= 0:
            raise ValidationError("max_results must be positive.")

        repo_path = self._build_repo_path(repo)
        params: Dict[str, Any] = {
            "state": clean_state,
            "per_page": min(page_size, MAX_PAGE_SIZE),
            "page": page,
            "sort": "updated",
            "direction": "desc",
        }
        if labels:
            params["labels"] = ",".join(labels)

        payload = await self._client.get_json(
            f"{repo_path}/issues",
            token=token,
            params=params,
        )
        if not isinstance(payload, list):
            raise ProviderError(
                "GitHub returned an unexpected payload for the "
                "issues endpoint.",
                details={"state": clean_state},
            )

        results: List[IssueRef] = []
        for item in payload:
            if not isinstance(item, Mapping):
                continue
            try:
                issue = self._map_issue(item)
            except ValidationError:
                logger.debug(
                    "Skipping malformed issue entry in listing."
                )
                continue
            if issue.is_pull_request and not include_pull_requests:
                continue
            results.append(issue)
            if len(results) >= max_results:
                break
        return tuple(results)

    # --- Get -----------------------------------------------------------

    async def get_issue(
        self,
        *,
        repo: RepositoryRef,
        number: int,
        token: str,
    ) -> IssueRef:
        """Fetch a single issue by number."""
        self._validate_repo_and_token(repo=repo, token=token)
        if not isinstance(number, int) or number <= 0:
            raise ValidationError(
                "number must be a positive integer.",
                details={"number": number},
            )
        repo_path = self._build_repo_path(repo)
        payload = await self._client.get_json(
            f"{repo_path}/issues/{number}",
            token=token,
        )
        if not isinstance(payload, Mapping):
            raise ProviderError(
                "GitHub returned an unexpected payload for the "
                "issue endpoint.",
                details={"number": number},
            )
        return self._map_issue(payload)

    # --- Create --------------------------------------------------------

    async def create_issue(
        self,
        *,
        repo: RepositoryRef,
        title: str,
        body: str,
        labels: Sequence[str],
        assignees: Sequence[str],
        token: str,
    ) -> IssueRef:
        """Create a new issue on the remote."""
        self._validate_repo_and_token(repo=repo, token=token)
        if not title or not title.strip():
            raise ValidationError("title must not be empty.")

        payload: Dict[str, Any] = {
            "title": title,
            "body": body or "",
        }
        if labels:
            payload["labels"] = list(labels)
        if assignees:
            payload["assignees"] = list(assignees)

        repo_path = self._build_repo_path(repo)
        response = await self._client.post_json(
            f"{repo_path}/issues",
            token=token,
            json_body=payload,
        )
        if not isinstance(response, Mapping):
            raise ProviderError(
                "GitHub returned an unexpected payload for the "
                "issue creation endpoint.",
            )
        return self._map_issue(response)

    # --- Update state --------------------------------------------------

    async def update_issue_state(
        self,
        *,
        repo: RepositoryRef,
        number: int,
        state: str,
        state_reason: Optional[str] = None,
        token: str,
    ) -> IssueRef:
        """Update an issue's state (close or reopen)."""
        self._validate_repo_and_token(repo=repo, token=token)
        if not isinstance(number, int) or number <= 0:
            raise ValidationError(
                "number must be a positive integer.",
                details={"number": number},
            )
        clean_state = (state or "").strip().lower()
        if clean_state not in _VALID_STATES:
            raise ValidationError(
                "state must be 'open' or 'closed'.",
                details={"state": state},
            )

        payload: Dict[str, Any] = {"state": clean_state}
        if state_reason:
            clean_reason = state_reason.strip().lower()
            if clean_reason not in _VALID_STATE_REASONS:
                raise ValidationError(
                    "state_reason must be 'completed', 'not_planned', "
                    "or 'reopened'.",
                    details={"state_reason": state_reason},
                )
            payload["state_reason"] = clean_reason

        repo_path = self._build_repo_path(repo)
        response = await self._client.patch_json(
            f"{repo_path}/issues/{number}",
            token=token,
            json_body=payload,
        )
        if not isinstance(response, Mapping):
            raise ProviderError(
                "GitHub returned an unexpected payload for the "
                "issue update endpoint.",
                details={"number": number},
            )
        return self._map_issue(response)

    # --- List comments -------------------------------------------------

    async def list_comments(
        self,
        *,
        repo: RepositoryRef,
        number: int,
        token: str,
        max_results: int = MAX_ISSUE_COMMENTS,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ) -> Sequence[IssueCommentRef]:
        """List comments on an issue."""
        self._validate_repo_and_token(repo=repo, token=token)
        if not isinstance(number, int) or number <= 0:
            raise ValidationError(
                "number must be a positive integer.",
                details={"number": number},
            )
        if max_results <= 0:
            return ()

        repo_path = self._build_repo_path(repo)
        capped = min(page_size, MAX_PAGE_SIZE)
        max_pages = (max_results + capped - 1) // capped
        results: List[IssueCommentRef] = []
        page = 1
        while page <= max_pages:
            payload = await self._client.get_json(
                f"{repo_path}/issues/{number}/comments",
                token=token,
                params={"per_page": capped, "page": page},
            )
            if not isinstance(payload, list):
                raise ProviderError(
                    "GitHub returned an unexpected payload for the "
                    "issue-comments endpoint.",
                    details={"number": number},
                )
            if not payload:
                break
            for item in payload:
                if not isinstance(item, Mapping):
                    continue
                try:
                    results.append(self._map_comment(item))
                except ValidationError:
                    logger.debug(
                        "Skipping malformed issue comment entry."
                    )
                    continue
                if len(results) >= max_results:
                    break
            if len(results) >= max_results or len(payload) < capped:
                break
            page += 1
        return tuple(results)

    # --- Create comment ------------------------------------------------

    async def create_comment(
        self,
        *,
        repo: RepositoryRef,
        number: int,
        body: str,
        token: str,
    ) -> IssueCommentRef:
        """Create a new comment on an issue."""
        self._validate_repo_and_token(repo=repo, token=token)
        if not isinstance(number, int) or number <= 0:
            raise ValidationError(
                "number must be a positive integer.",
                details={"number": number},
            )
        if not body or not body.strip():
            raise ValidationError("body must not be empty.")

        repo_path = self._build_repo_path(repo)
        response = await self._client.post_json(
            f"{repo_path}/issues/{number}/comments",
            token=token,
            json_body={"body": body},
        )
        if not isinstance(response, Mapping):
            raise ProviderError(
                "GitHub returned an unexpected payload for the "
                "issue-comment endpoint.",
                details={"number": number},
            )
        return self._map_comment(response)

    # --- Internal: validation -----------------------------------------

    @staticmethod
    def _validate_repo_and_token(
        *,
        repo: RepositoryRef,
        token: str,
    ) -> None:
        if not isinstance(repo, RepositoryRef):
            raise ValidationError(
                "repo must be a RepositoryRef.",
                details={"type": type(repo).__name__},
            )
        if not token:
            raise ValidationError("token must not be empty.")

    @staticmethod
    def _build_repo_path(repo: RepositoryRef) -> str:
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

    # --- Internal: mapping --------------------------------------------

    @staticmethod
    def _map_issue(payload: Mapping[str, Any]) -> IssueRef:
        issue_id = payload.get("id")
        if issue_id is None:
            raise ValidationError("Issue payload is missing `id`.")
        number = payload.get("number")
        if number is None:
            raise ValidationError("Issue payload is missing `number`.")
        title = payload.get("title")
        if not isinstance(title, str):
            raise ValidationError("Issue payload is missing `title`.")

        raw_body = payload.get("body")
        clean_body: Optional[str] = None
        if isinstance(raw_body, str) and raw_body:
            clean_body = redact_text(raw_body)

        state = str(payload.get("state") or "open").lower()
        state_reason_raw = payload.get("state_reason")
        state_reason: Optional[str] = None
        if isinstance(state_reason_raw, str) and state_reason_raw:
            state_reason = state_reason_raw.lower()

        user = payload.get("user")
        author: Optional[str] = None
        if isinstance(user, Mapping):
            login = user.get("login")
            if isinstance(login, str) and login:
                author = login

        # Labels come back as an array of objects with `name`.
        labels_raw = payload.get("labels")
        labels: List[str] = []
        if isinstance(labels_raw, list):
            for label in labels_raw:
                if isinstance(label, Mapping):
                    name = label.get("name")
                    if isinstance(name, str) and name:
                        labels.append(name)
                elif isinstance(label, str):
                    labels.append(label)

        web_url = payload.get("html_url")
        safe_url: Optional[str] = None
        if isinstance(web_url, str) and web_url:
            safe_url = web_url

        # `pull_request` is present (as a non-null object) when the
        # underlying resource is a PR.
        is_pr = payload.get("pull_request") is not None

        comments_count_raw = payload.get("comments")
        comments_count = 0
        if isinstance(comments_count_raw, int):
            comments_count = comments_count_raw

        return IssueRef(
            issue_id=str(issue_id),
            number=str(number),
            title=title,
            body=clean_body,
            state=state,
            author_login=author,
            labels=tuple(labels),
            comments_count=comments_count,
            is_pull_request=is_pr,
            state_reason=state_reason,
            web_url=safe_url,
            created_at=_parse_iso(payload.get("created_at")),
            updated_at=_parse_iso(payload.get("updated_at")),
            closed_at=_parse_iso(payload.get("closed_at")),
        )

    @staticmethod
    def _map_comment(payload: Mapping[str, Any]) -> IssueCommentRef:
        comment_id = payload.get("id")
        if comment_id is None:
            raise ValidationError(
                "Issue comment payload is missing `id`."
            )
        body = payload.get("body")
        if not isinstance(body, str):
            raise ValidationError(
                "Issue comment payload is missing `body`."
            )

        user = payload.get("user")
        author: Optional[str] = None
        if isinstance(user, Mapping):
            login = user.get("login")
            if isinstance(login, str) and login:
                author = login

        return IssueCommentRef(
            comment_id=str(comment_id),
            body=redact_text(body),
            author_login=author,
            created_at=_parse_iso(payload.get("created_at")),
            updated_at=_parse_iso(payload.get("updated_at")),
        )

    # --- Presentation --------------------------------------------------

    def __repr__(self) -> str:  # pragma: no cover - trivial formatting
        return f"<GitHubIssueService api_url={self._api_url!r}>"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_iso(value: Any) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp from a GitHub payload."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
