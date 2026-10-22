# backend/integrations/source_control/providers/github/pull_requests.py
"""
GitHub pull request service for the Source Control Integration
subsystem.

This service implements the pull-request-specific subset of the
`SourceControlProvider` protocol for GitHub. It is used by
`GitHubProviderAdapter` and is not intended to be called directly by
the application layer.

The service knows about three GitHub REST endpoints:

    - `POST /repos/{owner}/{repo}/pulls`         (create a PR)
    - `GET  /repos/{owner}/{repo}/pulls/{number}` (fetch a PR)
    - `GET  /repos/{owner}/{repo}/pulls`          (list PRs)

All HTTP is delegated to `GitHubClient`. Error mapping, rate-limit
handling, and retries are the client's responsibility.

Design rules:
    - The service never creates a PR in a non-draft state without an
      explicit `draft=False` argument. PELDRUN creates draft PRs by
      default, matching its policy for reviewed changes.
    - Title and body length are validated before the request is
      sent, so that GitHub's 422 errors are avoided for the most
      common invalid inputs.
    - The service translates GitHub's "already exists" 422 response
      into a `ConflictError`, which the application layer can
      present as a user-friendly message.
    - The service is stateless with respect to per-request data and
      is safe to share across coroutines.

MERGED-STATE DETECTION:
    GitHub's single-PR endpoint (`GET /pulls/{number}`) exposes both
    ``merged: bool`` and ``merged_at: string | null``, but the list
    endpoint (`GET /pulls`) omits ``merged`` and only carries
    ``merged_at``. The state mapper therefore treats a PR as merged
    when EITHER signal is true. Relying on ``merged`` alone would
    report every merged PR as CLOSED whenever it was read from a
    listing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import quote

from ...domain.enums import ChangeRequestState, ProviderId
from ...domain.models import ChangeRequestRef, RepositoryRef
from ...errors import (
    ConflictError,
    NotFoundError,
    ProviderError,
    RemoteStateChangedError,
    ValidationError,
)
from ...security.redaction import redact_text, redact_url
from .client import (
    DEFAULT_API_URL,
    GitHubApiConfig,
    GitHubClient,
    MAX_PAGE_SIZE,
)

__all__ = [
    "GitHubPullRequestService",
    "MergeResult",
    "MAX_PR_TITLE_LENGTH",
    "MAX_PR_BODY_LENGTH",
    "MAX_PULL_REQUESTS",
]


logger = logging.getLogger(__name__)


#: GitHub's documented maximum length for a pull request title.
MAX_PR_TITLE_LENGTH: int = 256

#: GitHub's documented maximum length for a pull request body.
MAX_PR_BODY_LENGTH: int = 65_536

#: Default page size for pull request listing.
_DEFAULT_PAGE_SIZE: int = 30

#: Cap on the number of pull requests returned in one call.
MAX_PULL_REQUESTS: int = 1_000

#: Merge methods accepted by GitHub's PUT /merge endpoint.
#:
#: `fast_forward` is deliberately NOT in this set: the GitHub REST
#: API exposes no direct fast-forward merge method. Callers who want
#: a fast-forward should rebase first (a separate operation).
_ALLOWED_MERGE_METHODS: frozenset[str] = frozenset(
    {"merge", "squash", "rebase"}
)


# ---------------------------------------------------------------------------
# Merge result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MergeResult:
    """Result of a merge attempt against the GitHub API.

    Attributes:
        merged:  True when GitHub reported the merge as applied.
        sha:     SHA of the merge commit, when merged.
        message: Human-readable status message from GitHub.
    """

    merged: bool
    sha: Optional[str] = None
    message: str = ""


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class GitHubPullRequestService:
    """
    Pull request operations against the GitHub REST API.

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

    # --- Create --------------------------------------------------------

    async def create_pull_request(
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
        """
        Create a pull request.

        Args:
            repo: The repository reference.
            source_branch: The branch containing the changes. MUST
                already exist on the remote.
            target_branch: The branch the PR is opened against.
            title: Non-empty, at most `MAX_PR_TITLE_LENGTH` chars.
            body: Optional description. At most
                `MAX_PR_BODY_LENGTH` chars.
            draft: When True, create the PR as a draft.
            token: Access token. Required.

        Raises:
            ValidationError: If any input is malformed.
            ConflictError: If a PR already exists for the same
                source/target pair.
            PermissionDeniedError: If the token cannot create PRs.
            NotFoundError: If the repository or source branch is not
                found.
        """
        self._validate_create_inputs(
            repo=repo,
            source_branch=source_branch,
            target_branch=target_branch,
            title=title,
            body=body,
        )
        if not token:
            raise ValidationError("token must not be empty.")

        # GitHub expects `head` as `owner:branch` for cross-repo PRs,
        # but `branch` alone is accepted for same-repo PRs. We always
        # use the owner-qualified form to be explicit and to support
        # future cross-repo scenarios.
        head = f"{repo.namespace}:{source_branch}"
        payload = {
            "title": title,
            "body": body or "",
            "head": head,
            "base": target_branch,
            "draft": bool(draft),
            "maintainer_can_modify": True,
        }

        repo_path = self._build_repo_path(repo)
        try:
            response = await self._client.post_json(
                f"{repo_path}/pulls",
                token=token,
                json_body=payload,
            )
        except ValidationError as exc:
            if self._is_already_exists_error(exc):
                raise ConflictError(
                    "A pull request already exists for the given "
                    "source and target branches.",
                    details={
                        "source_branch": source_branch,
                        "target_branch": target_branch,
                        "reason": "already_exists",
                    },
                ) from exc
            raise

        if not isinstance(response, Mapping):
            raise ProviderError(
                "GitHub returned an unexpected payload when creating "
                "the pull request.",
            )
        return self._map_pull_request(response)

    # --- Read ----------------------------------------------------------

    async def get_pull_request(
        self,
        *,
        repo: RepositoryRef,
        number_or_iid: str,
        token: str,
    ) -> ChangeRequestRef:
        """
        Fetch a single pull request by number.

        Args:
            repo: The repository reference.
            number_or_iid: PR number as a string. GitHub uses
                integers; the value is parsed and re-serialized.
            token: Access token. Required.

        Raises:
            ValidationError: If `number_or_iid` is not a valid
                integer string.
            NotFoundError: If the PR does not exist.
        """
        if not isinstance(repo, RepositoryRef):
            raise ValidationError(
                "repo must be a RepositoryRef.",
                details={"type": type(repo).__name__},
            )
        if not number_or_iid:
            raise ValidationError("number_or_iid must not be empty.")
        if not token:
            raise ValidationError("token must not be empty.")

        try:
            number = int(str(number_or_iid).strip())
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "number_or_iid must be an integer string.",
                details={"value": number_or_iid},
            ) from exc
        if number <= 0:
            raise ValidationError("PR number must be positive.")

        repo_path = self._build_repo_path(repo)
        payload = await self._client.get_json(
            f"{repo_path}/pulls/{number}",
            token=token,
        )
        if not isinstance(payload, Mapping):
            raise ProviderError(
                "GitHub returned an unexpected payload for the pull "
                "request endpoint.",
                details={"number": number},
            )
        return self._map_pull_request(payload)

    # --- List ----------------------------------------------------------

    async def list_pull_requests(
        self,
        *,
        repo: RepositoryRef,
        token: str,
        state: Optional[ChangeRequestState] = None,
        source_branch: Optional[str] = None,
        target_branch: Optional[str] = None,
        page: int = 1,
        page_size: int = _DEFAULT_PAGE_SIZE,
        max_results: int = MAX_PULL_REQUESTS,
    ) -> Sequence[ChangeRequestRef]:
        """
        List pull requests for a repository.

        Only the state, source, and target filters are applied.
        Additional filters supported by GitHub (author, label,
        milestone) are intentionally not exposed here; callers that
        need them SHOULD use the GitHub API directly.

        Args:
            repo: The repository reference.
            token: Access token. Required.
            state: Optional state filter. GitHub's server-side API
                only understands `open`, `closed`, and `all`, and
                `closed` returns both merged and not-merged PRs. A
                client-side filter narrows the results to the exact
                state the caller asked for.
            source_branch: Optional filter on the head branch.
            target_branch: Optional filter on the base branch.
            page: 1-based page number.
            page_size: Page size, capped at 100.
            max_results: Cap on the number of results returned.

        Raises:
            ValidationError: If paging parameters are out of range.
        """
        if not isinstance(repo, RepositoryRef):
            raise ValidationError(
                "repo must be a RepositoryRef.",
                details={"type": type(repo).__name__},
            )
        if not token:
            raise ValidationError("token must not be empty.")
        if page < 1:
            raise ValidationError("page must be >= 1.")
        if page_size < 1:
            raise ValidationError("page_size must be >= 1.")
        if max_results <= 0:
            raise ValidationError("max_results must be positive.")

        capped_size = min(page_size, MAX_PAGE_SIZE)
        repo_path = self._build_repo_path(repo)

        params: dict = {
            "per_page": capped_size,
            "page": page,
            "sort": "updated",
            "direction": "desc",
        }
        if state is not None:
            params["state"] = self._map_state_for_query(state)
        if source_branch:
            params["head"] = f"{repo.namespace}:{source_branch}"
        if target_branch:
            params["base"] = target_branch

        payload = await self._client.get_json(
            f"{repo_path}/pulls",
            token=token,
            params=params,
        )
        if not isinstance(payload, list):
            raise ProviderError(
                "GitHub returned an unexpected payload for the pull "
                "request listing."
            )

        results: List[ChangeRequestRef] = []
        for item in payload:
            if not isinstance(item, Mapping):
                continue
            try:
                pr = self._map_pull_request(item)
            except ValidationError:
                logger.debug(
                    "Skipping malformed pull request entry in "
                    "listing."
                )
                continue

            # GitHub's server-side `state` filter is coarse: it only
            # understands `open`, `closed`, and `all`, and `closed`
            # returns BOTH merged and closed-not-merged PRs. Apply a
            # client-side filter so that the caller actually gets
            # the state they asked for:
            #   - OPEN   -> OPEN + DRAFT (drafts are, by definition,
            #               still open)
            #   - DRAFT  -> DRAFT only
            #   - CLOSED -> CLOSED only (MERGED excluded)
            #   - MERGED -> MERGED only
            if state is not None and not _state_filter_matches(
                pr.state, state
            ):
                continue
            results.append(pr)
            if len(results) >= max_results:
                break

        return tuple(results)

    # --- Merge --------------------------------------------------------

    async def merge_pull_request(
        self,
        *,
        repo: RepositoryRef,
        number: int,
        merge_method: str = "merge",
        commit_title: Optional[str] = None,
        commit_message: Optional[str] = None,
        delete_source_branch: bool = False,
        expected_head_sha: Optional[str] = None,
        token: str,
    ) -> MergeResult:
        """Merge an open pull request.

        Verification model:
            When ``expected_head_sha`` is provided, it is sent to
            GitHub as the ``sha`` parameter of the merge request.
            GitHub rejects the call with a 409 Conflict if the PR's
            head has moved since the SHA was captured, which prevents
            a merge of commits that were never reviewed. The check
            is enforced server-side, so no pre-fetch is needed.

        Source-branch deletion:
            When ``delete_source_branch`` is True, the PR is fetched
            first to resolve the source branch, and that branch is
            deleted after a successful merge. The branch deletion is
            best-effort: a 404 (branch already gone) is swallowed
            and logged, because the merge itself already succeeded
            and there is nothing to roll back.

        Args:
            repo: The repository reference.
            number: PR number (positive integer).
            merge_method: One of ``"merge"``, ``"squash"``, or
                ``"rebase"``.
            commit_title: Optional custom merge-commit title.
            commit_message: Optional custom merge-commit body.
            delete_source_branch: Delete the source branch after a
                successful merge.
            expected_head_sha: Optional SHA that the PR head must
                currently match.
            token: Access token. Required.

        Returns:
            A `MergeResult` describing the outcome.

        Raises:
            ValidationError: If inputs are malformed (including an
                unsupported merge method).
            NotFoundError: If the PR or repository does not exist.
            ConflictError: If the PR is not mergeable (already
                merged, base conflicts, or branch-protection rules
                forbid the merge).
            RemoteStateChangedError: If the PR head moved since the
                caller captured ``expected_head_sha``.
            PermissionDeniedError: If the token cannot merge PRs.
        """
        # --- Validation ---
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
        clean_method = (merge_method or "").strip().lower()
        if clean_method not in _ALLOWED_MERGE_METHODS:
            raise ValidationError(
                f"Invalid merge_method '{merge_method}'. "
                f"Must be one of: {sorted(_ALLOWED_MERGE_METHODS)}",
                details={"merge_method": merge_method},
            )

        # --- Resolve source branch if we will need to delete it ---
        source_branch: Optional[str] = None
        if delete_source_branch:
            pr_ref = await self.get_pull_request(
                repo=repo, number_or_iid=str(number), token=token
            )
            source_branch = pr_ref.source_branch

        # --- Build the merge payload ---
        payload: Dict[str, Any] = {"merge_method": clean_method}
        if commit_title:
            payload["commit_title"] = commit_title
        if commit_message:
            payload["commit_message"] = commit_message
        if expected_head_sha:
            payload["sha"] = expected_head_sha

        # --- Execute the merge ---
        repo_path = self._build_repo_path(repo)
        try:
            response = await self._client.request(
                "PUT",
                f"{repo_path}/pulls/{number}/merge",
                token=token,
                json_body=payload,
            )
        except ValidationError as exc:
            # `GitHubClient` maps 405 (not mergeable) to a generic
            # ValidationError whose details carry the raw status.
            # Translate that into a domain ConflictError so the UI
            # can present it as "cannot merge" rather than
            # "invalid input".
            status = (exc.details or {}).get("status")
            if status == 405:
                raise ConflictError(
                    "Pull request is not mergeable. It may already "
                    "be merged, the base branch may have diverged, "
                    "or branch protection rules may forbid the "
                    "merge.",
                    details={
                        "number": number,
                        "status": 405,
                    },
                ) from exc
            raise
        except ConflictError as exc:
            # A 409 from GitHub's merge endpoint specifically means
            # the PR head changed since the last check. Surface it
            # as RemoteStateChangedError so the UI can prompt the
            # user to re-fetch and re-review.
            raise RemoteStateChangedError(
                "The pull request head was modified before the merge "
                "could be applied. Re-fetch the pull request and try "
                "again.",
                details={"number": number},
            ) from exc

        if not isinstance(response, Mapping):
            raise ProviderError(
                "GitHub returned an unexpected payload for the merge "
                "endpoint.",
                details={"number": number},
            )

        merged = bool(response.get("merged", False))
        merge_sha = response.get("sha")
        message = str(response.get("message") or "")

        # --- Optional: delete the source branch ---
        if merged and delete_source_branch and source_branch:
            encoded_branch = quote(source_branch, safe="/")
            try:
                await self._client.delete_json(
                    f"{repo_path}/git/refs/heads/{encoded_branch}",
                    token=token,
                )
            except NotFoundError:
                # Branch already deleted; the merge is still valid.
                logger.info(
                    "Source branch '%s' was already deleted after merge.",
                    source_branch,
                )
            except Exception as exc:  # noqa: BLE001
                # Non-fatal: the merge succeeded. Log and continue
                # so the caller sees a successful merge result.
                logger.warning(
                    "Failed to delete source branch '%s' after merge: %s",
                    source_branch,
                    type(exc).__name__,
                )

        return MergeResult(
            merged=merged,
            sha=merge_sha if isinstance(merge_sha, str) else None,
            message=message,
        )

    # --- Internal: validation ------------------------------------------

    @staticmethod
    def _validate_create_inputs(
        *,
        repo: RepositoryRef,
        source_branch: str,
        target_branch: str,
        title: str,
        body: str,
    ) -> None:
        """
        Validate inputs for `create_pull_request` before any HTTP
        call is made.
        """
        if not isinstance(repo, RepositoryRef):
            raise ValidationError(
                "repo must be a RepositoryRef.",
                details={"type": type(repo).__name__},
            )
        if not source_branch or not isinstance(source_branch, str):
            raise ValidationError(
                "source_branch must be a non-empty string."
            )
        if not target_branch or not isinstance(target_branch, str):
            raise ValidationError(
                "target_branch must be a non-empty string."
            )
        if source_branch == target_branch:
            raise ValidationError(
                "source_branch and target_branch must differ.",
                details={
                    "source_branch": source_branch,
                    "target_branch": target_branch,
                },
            )
        if not title or not isinstance(title, str) or not title.strip():
            raise ValidationError("title must be a non-empty string.")
        if len(title) > MAX_PR_TITLE_LENGTH:
            raise ValidationError(
                "title exceeds the maximum length.",
                details={
                    "length": len(title),
                    "max_length": MAX_PR_TITLE_LENGTH,
                },
            )
        if body is None:
            body = ""
        if not isinstance(body, str):
            raise ValidationError("body must be a string.")
        if len(body) > MAX_PR_BODY_LENGTH:
            raise ValidationError(
                "body exceeds the maximum length.",
                details={
                    "length": len(body),
                    "max_length": MAX_PR_BODY_LENGTH,
                },
            )

    @staticmethod
    def _is_already_exists_error(exc: ValidationError) -> bool:
        """
        Return True when a 422 response indicates that a PR already
        exists for the given source/target pair.

        GitHub's message is: "A pull request already exists for
        owner:branch." The check is case-insensitive and does not
        depend on the exact owner or branch names.
        """
        details = exc.details or {}
        message = str(details.get("provider_message") or "").lower()
        if not message:
            message = str(exc).lower()
        return "already exists" in message

    # --- Internal: URL construction ------------------------------------

    @staticmethod
    def _build_repo_path(repo: RepositoryRef) -> str:
        """Return the `/repos/{owner}/{name}` path."""
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

    # --- Internal: state mapping ---------------------------------------

    @staticmethod
    def _map_state_for_query(state: ChangeRequestState) -> str:
        """
        Map a neutral state to a GitHub query value.

        GitHub accepts only `open`, `closed`, and `all` for the
        `state` query parameter. MERGED PRs are closed on GitHub, so
        they are filtered client-side after the fetch. DRAFT PRs are
        open on GitHub, so they are also requested via `open`.
        """
        if state == ChangeRequestState.OPEN:
            return "open"
        if state == ChangeRequestState.CLOSED:
            return "closed"
        if state == ChangeRequestState.MERGED:
            return "closed"
        if state == ChangeRequestState.DRAFT:
            return "open"
        return "all"

    @staticmethod
    def _map_pull_request_state(
        payload: Mapping[str, Any],
    ) -> ChangeRequestState:
        """
        Map the GitHub PR state to a `ChangeRequestState`.

        GitHub's PR payload exposes two merged-related fields with
        subtly different shapes depending on the endpoint:

            - The single-PR endpoint (`GET /pulls/{number}`) returns
              both ``merged: bool`` and ``merged_at: string | null``.
            - The list endpoint (`GET /pulls`) returns only
              ``merged_at: string | null``; the boolean ``merged``
              field is NOT present.

        To classify PRs correctly from BOTH payload shapes, this
        method treats a PR as merged when EITHER signal is true.
        Relying solely on ``merged`` (as the original implementation
        did) caused every merged PR to be reported as CLOSED when
        read through ``list_pull_requests``.

        Draft PRs are classified as DRAFT regardless of their
        ``state`` value, because GitHub marks them ``state: open``
        with an auxiliary ``draft: true`` flag.
        """
        state_raw = str(payload.get("state") or "").lower()
        merged_flag = bool(payload.get("merged", False))
        merged_at_present = payload.get("merged_at") is not None
        is_merged = merged_flag or merged_at_present
        draft = bool(payload.get("draft", False))

        if state_raw == "closed":
            return (
                ChangeRequestState.MERGED
                if is_merged
                else ChangeRequestState.CLOSED
            )
        if state_raw == "open":
            return (
                ChangeRequestState.DRAFT
                if draft
                else ChangeRequestState.OPEN
            )
        return ChangeRequestState.UNKNOWN

    # --- Internal: payload mapping -------------------------------------

    @staticmethod
    def _map_pull_request(
        payload: Mapping[str, Any],
    ) -> ChangeRequestRef:
        """
        Map a GitHub PR payload to `ChangeRequestRef`.

        The title and web URL are sanitized with the same helpers
        used by ``GitHubDataMapper.parse_change_request`` so that
        credentials accidentally embedded in a PR title or URL are
        never surfaced to the UI or the audit log.

        Raises `ValidationError` when a required field is missing.
        """
        number = payload.get("number")
        if number is None:
            raise ValidationError(
                "Pull request payload is missing `number`."
            )

        web_url = payload.get("html_url")
        if not isinstance(web_url, str) or not web_url:
            raise ValidationError(
                "Pull request payload is missing `html_url`."
            )
        # Defensive: apply redaction to any URL surfaced to callers.
        # URLs from GitHub should never contain credentials, but
        # redaction is cheap and matches the sibling mapper.
        safe_web_url = redact_url(web_url)

        head = payload.get("head")
        if not isinstance(head, Mapping):
            raise ValidationError(
                "Pull request payload is missing `head`."
            )
        source_branch = head.get("ref")
        if not isinstance(source_branch, str) or not source_branch:
            raise ValidationError(
                "Pull request payload is missing `head.ref`."
            )

        base = payload.get("base")
        if not isinstance(base, Mapping):
            raise ValidationError(
                "Pull request payload is missing `base`."
            )
        target_branch = base.get("ref")
        if not isinstance(target_branch, str) or not target_branch:
            raise ValidationError(
                "Pull request payload is missing `base.ref`."
            )

        # The repository the PR belongs to. GitHub includes it in
        # the `base.repo` object of the PR payload.
        base_repo = base.get("repo")
        repository_id = ""
        if isinstance(base_repo, Mapping):
            repo_id_raw = base_repo.get("id")
            if repo_id_raw is not None:
                repository_id = str(repo_id_raw)

        state = GitHubPullRequestService._map_pull_request_state(payload)

        # Defense in depth: redact the title so that a user who
        # accidentally includes a token in a PR title does not cause
        # that token to land in the operation journal or audit log.
        safe_title = redact_text(str(payload.get("title") or ""))

        return ChangeRequestRef(
            provider=ProviderId.GITHUB,
            repository_id=repository_id,
            number_or_iid=str(number),
            web_url=safe_web_url,
            state=state,
            source_branch=source_branch,
            target_branch=target_branch,
            title=safe_title,
            is_draft=bool(payload.get("draft", False)),
            created_at=_parse_iso(payload.get("created_at")),
            updated_at=_parse_iso(payload.get("updated_at")),
            merged_at=_parse_iso(payload.get("merged_at")),
        )

    # --- Presentation ---------------------------------------------------

    def __repr__(self) -> str:  # pragma: no cover - trivial formatting
        return f"<GitHubPullRequestService api_url={self._api_url!r}>"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _state_filter_matches(
    actual: ChangeRequestState,
    requested: ChangeRequestState,
) -> bool:
    """
    Return True when ``actual`` satisfies the caller's ``requested``
    state filter.

    GitHub's REST API only supports ``open``, ``closed``, and ``all``
    server-side, and ``closed`` returns merged and not-merged PRs
    alike. This helper narrows the results to the exact state the
    caller asked for:

        - OPEN   : OPEN and DRAFT (a draft is, by definition, open)
        - DRAFT  : DRAFT only
        - CLOSED : CLOSED only (MERGED is excluded)
        - MERGED : MERGED only

    Any other ``requested`` value (e.g. UNKNOWN) matches everything.
    """
    if requested == ChangeRequestState.OPEN:
        return actual in (
            ChangeRequestState.OPEN,
            ChangeRequestState.DRAFT,
        )
    if requested == ChangeRequestState.DRAFT:
        return actual == ChangeRequestState.DRAFT
    if requested == ChangeRequestState.CLOSED:
        return actual == ChangeRequestState.CLOSED
    if requested == ChangeRequestState.MERGED:
        return actual == ChangeRequestState.MERGED
    return True


def _parse_iso(value: Any) -> Optional[datetime]:
    """
    Parse an ISO 8601 timestamp from a GitHub payload.

    GitHub returns timestamps like `2025-01-01T12:34:56Z`. The
    trailing `Z` is replaced with `+00:00` before parsing so that
    `datetime.fromisoformat` accepts the string on every supported
    Python version.
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
