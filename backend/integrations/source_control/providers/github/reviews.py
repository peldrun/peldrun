# backend/integrations/source_control/providers/github/reviews.py
"""
GitHub pull-request review and comment service.

This service implements the review- and comment-related subset of
the pull request workflow for GitHub. It is used by the source
control facade and is not intended to be called directly by the
application layer.

Endpoints used:

    - ``POST /repos/{owner}/{repo}/pulls/{number}/reviews``
      Submit a review (APPROVE / REQUEST_CHANGES / COMMENT).
    - ``GET  /repos/{owner}/{repo}/pulls/{number}/reviews``
      List submitted reviews.
    - ``POST /repos/{owner}/{repo}/pulls/{number}/comments``
      Create a single inline comment on a file/line.
    - ``POST /repos/{owner}/{repo}/issues/{number}/comments``
      Create a general (conversation) comment. Note the endpoint
      path: general PR comments live on the ``issues`` namespace,
      not on ``pulls``. This is a GitHub API quirk and is intentional.
    - ``GET  /repos/{owner}/{repo}/pulls/{number}/comments``
      List inline review comments.
    - ``GET  /repos/{owner}/{repo}/issues/{number}/comments``
      List general comments.

Design rules:
    - All HTTP goes through the shared `GitHubClient`, which owns
      retries, rate-limit handling, and error mapping.
    - Review and comment dataclasses are defined in this module
      rather than in ``domain/models.py``. They are provider-layer
      value objects used only by the facade; promoting them to the
      domain is a larger refactor that is out of scope for this
      phase.
    - General comments are keyed by ``number`` (the PR number), but
      sent through the ``issues`` endpoint. This is correct for
      GitHub and is not a bug.
    - The service never logs tokens, review bodies, or comment
      bodies in full. Bodies are redacted before being returned so
      that accidental secrets never reach the audit log or UI.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
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
    "GitHubReviewsService",
    "ReviewRef",
    "ReviewCommentRef",
    "IssueCommentRef",
    "MAX_REVIEWS",
    "MAX_COMMENTS",
]


logger = logging.getLogger(__name__)


#: Maximum number of reviews returned in a single listing.
MAX_REVIEWS: int = 500

#: Maximum number of comments returned per listing (inline or
#: general).
MAX_COMMENTS: int = 1_000

#: Default page size used when paging through listing endpoints.
_DEFAULT_PAGE_SIZE: int = 100

#: Review events accepted by GitHub's submit-review endpoint.
_VALID_REVIEW_EVENTS: frozenset[str] = frozenset(
    {"APPROVE", "REQUEST_CHANGES", "COMMENT"}
)

#: Diff sides accepted by GitHub's inline-comment endpoint.
_VALID_SIDES: frozenset[str] = frozenset({"LEFT", "RIGHT"})


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewRef:
    """A single submitted review on a pull request.

    Attributes:
        review_id:    GitHub's numeric review ID as a string.
        state:        One of ``approved``, ``changes_requested``,
                      ``commented``, ``dismissed``, ``pending``, or
                      ``unknown``.
        body:         Review body, redacted.
        author_login: Reviewer login, when available.
        submitted_at: Submission timestamp, when available.
    """

    review_id: str
    state: str
    body: Optional[str] = None
    author_login: Optional[str] = None
    submitted_at: Optional[datetime] = None


@dataclass(frozen=True)
class ReviewCommentRef:
    """A single inline review comment on a file/line.

    Attributes:
        comment_id:      GitHub's numeric comment ID as a string.
        body:            Comment body, redacted.
        path:            File path the comment applies to.
        line:            1-based line number.
        side:            ``LEFT`` or ``RIGHT``.
        author_login:    Author login, when available.
        created_at:      Creation timestamp.
        updated_at:      Last-update timestamp.
        in_reply_to_id:  Parent comment ID when this is a reply.
    """

    comment_id: str
    body: str
    path: str
    line: int
    side: str
    author_login: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    in_reply_to_id: Optional[str] = None


@dataclass(frozen=True)
class IssueCommentRef:
    """A general (conversation) comment on a pull request.

    Attributes:
        comment_id:   GitHub's numeric comment ID as a string.
        body:         Comment body, redacted.
        author_login: Author login, when available.
        created_at:   Creation timestamp.
        updated_at:   Last-update timestamp.
    """

    comment_id: str
    body: str
    author_login: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class GitHubReviewsService:
    """Review and comment operations on GitHub pull requests."""

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

    # --- Submit review -------------------------------------------------

    async def submit_review(
        self,
        *,
        repo: RepositoryRef,
        number: int,
        event: str,
        body: Optional[str] = None,
        comments: Sequence[Mapping[str, Any]] = (),
        commit_id: Optional[str] = None,
        token: str,
    ) -> ReviewRef:
        """Submit a review on a pull request.

        Args:
            repo: The repository reference.
            number: PR number (positive integer).
            event: One of ``"APPROVE"``, ``"REQUEST_CHANGES"``, or
                ``"COMMENT"``. Case-insensitive.
            body: Optional review body. Required when ``event`` is
                ``REQUEST_CHANGES`` or ``COMMENT``.
            comments: Optional inline comments. Each entry must have
                at least ``path``, ``line``, and ``body`` keys;
                ``side`` defaults to ``"RIGHT"`` when omitted.
            commit_id: Optional SHA the review applies to. Defaults
                to the PR head when omitted.
            token: Access token. Required.

        Raises:
            ValidationError: If inputs are malformed.
            NotFoundError: If the PR does not exist.
            PermissionDeniedError: If the token cannot submit a
                review.
        """
        self._validate_pr_inputs(repo=repo, number=number, token=token)

        clean_event = (event or "").strip().upper()
        if clean_event not in _VALID_REVIEW_EVENTS:
            raise ValidationError(
                f"Invalid review event '{event}'. "
                f"Must be one of: {sorted(_VALID_REVIEW_EVENTS)}",
                details={"event": event},
            )

        # APPROVE can be submitted without a body; the other events
        # require one because they convey actionable feedback.
        if clean_event in ("REQUEST_CHANGES", "COMMENT") and not body:
            raise ValidationError(
                f"event='{clean_event}' requires a non-empty body.",
                details={"event": clean_event},
            )

        payload: Dict[str, Any] = {"event": clean_event}
        if body is not None:
            payload["body"] = body
        if commit_id:
            payload["commit_id"] = commit_id
        if comments:
            payload["comments"] = [
                {
                    "path": str(c["path"]),
                    "line": int(c["line"]),
                    "side": str(c.get("side", "RIGHT")).upper(),
                    "body": str(c["body"]),
                }
                for c in comments
            ]

        repo_path = self._build_repo_path(repo)
        response = await self._client.post_json(
            f"{repo_path}/pulls/{number}/reviews",
            token=token,
            json_body=payload,
        )
        if not isinstance(response, Mapping):
            raise ProviderError(
                "GitHub returned an unexpected payload for the "
                "review submission endpoint.",
                details={"number": number},
            )
        return self._map_review(response)

    # --- List reviews --------------------------------------------------

    async def list_reviews(
        self,
        *,
        repo: RepositoryRef,
        number: int,
        token: str,
        max_results: int = MAX_REVIEWS,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ) -> Sequence[ReviewRef]:
        """List submitted reviews on a pull request."""
        self._validate_pr_inputs(repo=repo, number=number, token=token)
        repo_path = self._build_repo_path(repo)
        results = await self._list_paged(
            f"{repo_path}/pulls/{number}/reviews",
            token=token,
            max_results=max_results,
            page_size=page_size,
            mapper=self._map_review,
        )
        return tuple(results)

    # --- Create inline comment -----------------------------------------

    async def create_review_comment(
        self,
        *,
        repo: RepositoryRef,
        number: int,
        body: str,
        path: str,
        line: int,
        side: str = "RIGHT",
        commit_id: Optional[str] = None,
        token: str,
    ) -> ReviewCommentRef:
        """Create a single inline comment on a PR.

        When ``commit_id`` is not provided, the PR's current head
        SHA is resolved automatically via an extra ``GET`` request.
        Supplying it explicitly avoids that extra round-trip.
        """
        self._validate_pr_inputs(repo=repo, number=number, token=token)
        if not body or not body.strip():
            raise ValidationError("body must not be empty.")
        if not path:
            raise ValidationError("path must not be empty.")
        if line <= 0:
            raise ValidationError("line must be positive.")
        clean_side = (side or "").strip().upper()
        if clean_side not in _VALID_SIDES:
            raise ValidationError(
                f"Invalid side '{side}'. Must be LEFT or RIGHT.",
                details={"side": side},
            )

        repo_path = self._build_repo_path(repo)

        effective_commit = commit_id
        if not effective_commit:
            effective_commit = await self._resolve_head_sha(
                repo_path=repo_path, number=number, token=token
            )

        payload = {
            "body": body,
            "commit_id": effective_commit,
            "path": path,
            "line": int(line),
            "side": clean_side,
        }
        response = await self._client.post_json(
            f"{repo_path}/pulls/{number}/comments",
            token=token,
            json_body=payload,
        )
        if not isinstance(response, Mapping):
            raise ProviderError(
                "GitHub returned an unexpected payload for the "
                "inline-comment endpoint.",
                details={"number": number},
            )
        return self._map_review_comment(response)

    # --- Create general comment ----------------------------------------

    async def create_issue_comment(
        self,
        *,
        repo: RepositoryRef,
        number: int,
        body: str,
        token: str,
    ) -> IssueCommentRef:
        """Create a general (conversation) comment on a PR.

        Uses the ``issues`` endpoint, which is where GitHub stores
        general PR comments. This is intentional and not a bug.
        """
        self._validate_pr_inputs(repo=repo, number=number, token=token)
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
                "general-comment endpoint.",
                details={"number": number},
            )
        return self._map_issue_comment(response)

    # --- List comments -------------------------------------------------

    async def list_comments(
        self,
        *,
        repo: RepositoryRef,
        number: int,
        token: str,
        max_results: int = MAX_COMMENTS,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ) -> tuple[tuple[IssueCommentRef, ...], tuple[ReviewCommentRef, ...]]:
        """List both general and inline comments on a PR.

        Returns a 2-tuple ``(general_comments, review_comments)``.
        The split mirrors the two distinct GitHub endpoints; the
        caller combines them into a single chronological view if
        the UI needs one.
        """
        self._validate_pr_inputs(repo=repo, number=number, token=token)
        repo_path = self._build_repo_path(repo)

        general = await self._list_paged(
            f"{repo_path}/issues/{number}/comments",
            token=token,
            max_results=max_results,
            page_size=page_size,
            mapper=self._map_issue_comment,
        )
        inline = await self._list_paged(
            f"{repo_path}/pulls/{number}/comments",
            token=token,
            max_results=max_results,
            page_size=page_size,
            mapper=self._map_review_comment,
        )
        return tuple(general), tuple(inline)

    # --- Internal: paging ----------------------------------------------

    async def _list_paged(
        self,
        endpoint: str,
        *,
        token: str,
        max_results: int,
        page_size: int,
        mapper: Any,
    ) -> List[Any]:
        """Generic page-through-listing helper.

        The loop is bounded by `max_results` and by a computed
        `max_pages`, so a pathological repository cannot force an
        unbounded number of requests. Malformed entries are skipped
        with a debug log rather than aborting the whole listing.
        """
        if max_results <= 0:
            return []
        capped = min(page_size, MAX_PAGE_SIZE)
        max_pages = (max_results + capped - 1) // capped
        results: List[Any] = []
        page = 1
        while page <= max_pages:
            payload = await self._client.get_json(
                endpoint,
                token=token,
                params={"per_page": capped, "page": page},
            )
            if not isinstance(payload, list):
                raise ProviderError(
                    f"GitHub returned an unexpected payload for {endpoint}.",
                )
            if not payload:
                break
            for item in payload:
                if not isinstance(item, Mapping):
                    continue
                try:
                    results.append(mapper(item))
                except ValidationError:
                    logger.debug(
                        "Skipping malformed entry from %s.", endpoint
                    )
                    continue
                if len(results) >= max_results:
                    break
            if len(results) >= max_results or len(payload) < capped:
                break
            page += 1
        return results

    async def _resolve_head_sha(
        self,
        *,
        repo_path: str,
        number: int,
        token: str,
    ) -> str:
        """Resolve the PR head commit SHA via a dedicated GET.

        Used by `create_review_comment` when the caller does not
        supply a `commit_id`. GitHub accepts a review comment
        without `commit_id` in some versions, but requiring it
        explicitly gives us consistent behaviour across versions and
        produces a clear error when the PR has no commits.
        """
        payload = await self._client.get_json(
            f"{repo_path}/pulls/{number}", token=token
        )
        if not isinstance(payload, Mapping):
            raise ProviderError(
                "GitHub returned an unexpected payload when resolving "
                "the pull request head.",
                details={"number": number},
            )
        head = payload.get("head")
        if isinstance(head, Mapping):
            sha = head.get("sha")
            if isinstance(sha, str) and sha:
                return sha
        raise ProviderError(
            "Could not resolve the pull request head commit SHA.",
            details={"number": number},
        )

    # --- Internal: validation ------------------------------------------

    @staticmethod
    def _validate_pr_inputs(
        *,
        repo: RepositoryRef,
        number: int,
        token: str,
    ) -> None:
        if not isinstance(repo, RepositoryRef):
            raise ValidationError(
                "repo must be a RepositoryRef.",
                details={"type": type(repo).__name__},
            )
        if not isinstance(number, int) or number <= 0:
            raise ValidationError(
                "number must be a positive integer.",
                details={"number": number},
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

    # --- Internal: mapping ---------------------------------------------

    @staticmethod
    def _map_review(payload: Mapping[str, Any]) -> ReviewRef:
        review_id = payload.get("id")
        if review_id is None:
            raise ValidationError(
                "Review payload is missing `id`."
            )
        state = str(payload.get("state") or "unknown").lower()

        raw_body = payload.get("body")
        clean_body: Optional[str] = None
        if isinstance(raw_body, str) and raw_body:
            clean_body = redact_text(raw_body)

        user = payload.get("user")
        author: Optional[str] = None
        if isinstance(user, Mapping):
            login = user.get("login")
            if isinstance(login, str) and login:
                author = login

        return ReviewRef(
            review_id=str(review_id),
            state=state,
            body=clean_body,
            author_login=author,
            submitted_at=_parse_iso(payload.get("submitted_at")),
        )

    @staticmethod
    def _map_review_comment(payload: Mapping[str, Any]) -> ReviewCommentRef:
        comment_id = payload.get("id")
        if comment_id is None:
            raise ValidationError(
                "Review comment payload is missing `id`."
            )
        body = payload.get("body")
        if not isinstance(body, str):
            raise ValidationError(
                "Review comment payload is missing `body`."
            )
        path = payload.get("path")
        if not isinstance(path, str) or not path:
            raise ValidationError(
                "Review comment payload is missing `path`."
            )
        # `line` may be null for outdated comments; fall back to
        # `original_line` so the UI can still place the comment on
        # the diff it was created against.
        raw_line = payload.get("line") or payload.get("original_line")
        if raw_line is None:
            raise ValidationError(
                "Review comment payload is missing `line`."
            )
        try:
            line_int = int(raw_line)
        except (TypeError, ValueError):
            raise ValidationError(
                "Review comment payload has an invalid `line`."
            )

        side_raw = (
            payload.get("side")
            or payload.get("original_side")
            or "RIGHT"
        )
        side = str(side_raw).upper()
        if side not in _VALID_SIDES:
            side = "RIGHT"

        user = payload.get("user")
        author: Optional[str] = None
        if isinstance(user, Mapping):
            login = user.get("login")
            if isinstance(login, str) and login:
                author = login

        reply_to = payload.get("in_reply_to_id")
        reply_to_str = (
            str(reply_to) if reply_to is not None else None
        )

        return ReviewCommentRef(
            comment_id=str(comment_id),
            body=redact_text(body),
            path=path,
            line=line_int,
            side=side,
            author_login=author,
            created_at=_parse_iso(payload.get("created_at")),
            updated_at=_parse_iso(payload.get("updated_at")),
            in_reply_to_id=reply_to_str,
        )

    @staticmethod
    def _map_issue_comment(payload: Mapping[str, Any]) -> IssueCommentRef:
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
        return f"<GitHubReviewsService api_url={self._api_url!r}>"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_iso(value: Any) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp from a GitHub payload.

    GitHub returns timestamps like ``2025-01-01T12:34:56Z``. The
    trailing ``Z`` is replaced with ``+00:00`` before parsing so
    that `datetime.fromisoformat` accepts the string on every
    supported Python version. Invalid input returns ``None`` rather
    than raising, so a single malformed timestamp cannot break a
    listing.
    """
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
