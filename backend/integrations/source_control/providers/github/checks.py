# backend/integrations/source_control/providers/github/checks.py
"""
GitHub checks service for the Source Control Integration subsystem.

This service implements the checks-specific subset of the
`SourceControlProvider` protocol for GitHub. It is used by
`GitHubProviderAdapter` and is not intended to be called directly by
the application layer.

The service knows about two GitHub REST endpoints:

    - `GET /repos/{owner}/{repo}/commits/{ref}/check-runs`
      List check runs for a commit.
    - `GET /repos/{owner}/{repo}/commits/{ref}`
      Resolve a branch name to a commit SHA when the caller supplies
      a branch instead of a SHA.

All HTTP is delegated to `GitHubClient`. Error mapping, rate-limit
handling, and retries are the client's responsibility.

Design rules:
    - The service never returns an empty tuple to mean "all checks
      passed". An empty tuple means "no checks were reported". The
      application layer is responsible for distinguishing the two.
    - The service resolves branch names to SHAs only when the input
      does not look like a SHA. This avoids an unnecessary API call
      on the common path.
    - The service caps the number of check runs it returns in a
      single call at `MAX_CHECK_RUNS`. Repositories with a
      pathological number of check runs require explicit paging by
      the caller.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import quote

from ...domain.enums import (
    CheckConclusion,
    CheckStatus,
    ProviderId,
)
from ...domain.models import CheckRunRef, RepositoryRef
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
    "GitHubChecksService",
    "MAX_CHECK_RUNS",
]


logger = logging.getLogger(__name__)


#: Maximum number of check runs collected by `list_checks` in one
#: call. Repositories with more check runs require explicit paging
#: by the caller.
MAX_CHECK_RUNS: int = 1_000

#: Default page size when paging through the check-runs endpoint.
_DEFAULT_PAGE_SIZE: int = 100

#: Length of a SHA-1 hash. GitHub currently uses SHA-1 for commit
#: identifiers, but the code also accepts SHA-256 for future-proofing.
_SHA1_LEN: int = 40
_SHA256_LEN: int = 64

#: Pattern matching a full-length hexadecimal SHA.
_HEX_SHA = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class GitHubChecksService:
    """
    Check-run operations against the GitHub REST API.

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

    # --- List check runs -----------------------------------------------

    async def list_checks(
        self,
        *,
        repo: RepositoryRef,
        ref: str,
        token: str,
        filter_latest: bool = True,
        max_results: int = MAX_CHECK_RUNS,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ) -> Sequence[CheckRunRef]:
        """
        Return check runs for a commit.

        Args:
            repo: The repository reference.
            ref: Branch name, tag, or commit SHA. Branch names are
                resolved to SHAs first.
            token: Access token. Required.
            filter_latest: When True (default), GitHub returns only
                the latest check run per name. When False, every
                historical run is returned.
            max_results: Cap on the number of results.
            page_size: Page size used internally. Capped at 100.

        Raises:
            ValidationError: If inputs are malformed.
            NotFoundError: If the ref does not exist.
            PermissionDeniedError: If the token cannot read checks.
        """
        if not isinstance(repo, RepositoryRef):
            raise ValidationError(
                "repo must be a RepositoryRef.",
                details={"type": type(repo).__name__},
            )
        if not ref or not isinstance(ref, str):
            raise ValidationError("ref must be a non-empty string.")
        if not token:
            raise ValidationError("token must not be empty.")
        if max_results <= 0:
            raise ValidationError("max_results must be positive.")
        if page_size <= 0:
            raise ValidationError("page_size must be positive.")

        capped_page_size = min(page_size, MAX_PAGE_SIZE)

        # Resolve the ref to a SHA when necessary. GitHub's
        # check-runs endpoint accepts a ref directly, but resolving
        # to a SHA first lets us provide a clearer error message
        # when the ref is missing.
        sha = await self._resolve_ref_to_sha(
            repo=repo, ref=ref, token=token
        )

        repo_path = self._build_repo_path(repo)
        encoded_sha = quote(sha, safe="")
        endpoint = f"{repo_path}/commits/{encoded_sha}/check-runs"

        check_runs: List[CheckRunRef] = []
        page = 1
        max_pages = (
            max_results + capped_page_size - 1
        ) // capped_page_size

        while page <= max_pages:
            params: Dict[str, Any] = {
                "per_page": capped_page_size,
                "page": page,
            }
            if filter_latest:
                params["filter"] = "latest"

            payload = await self._client.get_json(
                endpoint,
                token=token,
                params=params,
            )

            if not isinstance(payload, Mapping):
                raise ProviderError(
                    "GitHub returned an unexpected payload for the "
                    "check-runs endpoint.",
                    details={"ref": ref},
                )

            runs_raw = payload.get("check_runs")
            if not isinstance(runs_raw, list):
                # Some payloads may have `check_runs: null` when
                # there are no runs at all. Treat that as empty.
                if runs_raw is None:
                    break
                raise ProviderError(
                    "GitHub returned a malformed check-runs payload.",
                    details={"ref": ref},
                )

            if not runs_raw:
                break

            for item in runs_raw:
                if not isinstance(item, Mapping):
                    continue
                try:
                    run = self._map_check_run(item)
                except ValidationError:
                    logger.debug(
                        "Skipping malformed check-run entry on page %d.",
                        page,
                    )
                    continue
                check_runs.append(run)
                if len(check_runs) >= max_results:
                    break

            if len(check_runs) >= max_results:
                break
            if len(runs_raw) < capped_page_size:
                break
            page += 1

        return tuple(check_runs)

    # --- Internal: ref resolution --------------------------------------

    async def _resolve_ref_to_sha(
        self,
        *,
        repo: RepositoryRef,
        ref: str,
        token: str,
    ) -> str:
        """
        Return the commit SHA for `ref`.

        When `ref` already looks like a full SHA, it is returned
        unchanged. Otherwise, the commit endpoint is queried to
        resolve the branch or tag name.

        GitHub's check-runs endpoint accepts short SHAs and branch
        names, but resolving explicitly gives us a clear error when
        the ref does not exist and avoids ambiguity with tag names
        that look like short SHAs.
        """
        if _looks_like_sha(ref):
            return ref

        repo_path = self._build_repo_path(repo)
        encoded_ref = quote(ref, safe="")
        payload = await self._client.get_json(
            f"{repo_path}/commits/{encoded_ref}",
            token=token,
        )
        if not isinstance(payload, Mapping):
            raise ProviderError(
                "GitHub returned an unexpected payload when resolving "
                "the ref.",
                details={"ref": ref},
            )
        sha = payload.get("sha")
        if not isinstance(sha, str) or not sha:
            raise ProviderError(
                "GitHub did not return a commit SHA for the ref.",
                details={"ref": ref},
            )
        return sha

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

    # --- Internal: payload mapping -------------------------------------

    @staticmethod
    def _map_check_run(payload: Mapping[str, Any]) -> CheckRunRef:
        """
        Map a GitHub check-run payload to `CheckRunRef`.

        Raises `ValidationError` when a required field is missing.
        """
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            raise ValidationError(
                "Check-run payload is missing a valid `name`."
            )

        status_raw = str(payload.get("status") or "")
        conclusion_raw = payload.get("conclusion")
        conclusion_text = (
            str(conclusion_raw) if conclusion_raw is not None else ""
        )

        return CheckRunRef(
            name=name,
            status=_map_status(status_raw),
            conclusion=_map_conclusion(conclusion_text),
            details_url=_opt_str(payload.get("details_url")),
            started_at=_parse_iso(payload.get("started_at")),
            completed_at=_parse_iso(payload.get("completed_at")),
        )

    # --- Presentation ---------------------------------------------------

    def __repr__(self) -> str:  # pragma: no cover - trivial formatting
        return f"<GitHubChecksService api_url={self._api_url!r}>"


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------


#: GitHub status values (case-sensitive).
_STATUS_MAP: Dict[str, CheckStatus] = {
    "queued": CheckStatus.QUEUED,
    "in_progress": CheckStatus.IN_PROGRESS,
    "completed": CheckStatus.COMPLETED,
    "waiting": CheckStatus.QUEUED,
    "requested": CheckStatus.QUEUED,
    "pending": CheckStatus.QUEUED,
}


def _map_status(value: str) -> CheckStatus:
    """
    Map a GitHub check-run status to `CheckStatus`.

    Unknown values map to `CheckStatus.UNKNOWN` rather than raising,
    so that new statuses introduced by GitHub do not break the
    caller.
    """
    if not value:
        return CheckStatus.UNKNOWN
    return _STATUS_MAP.get(value.lower(), CheckStatus.UNKNOWN)


#: GitHub conclusion values (case-sensitive).
_CONCLUSION_MAP: Dict[str, CheckConclusion] = {
    "success": CheckConclusion.SUCCESS,
    "failure": CheckConclusion.FAILURE,
    "neutral": CheckConclusion.NEUTRAL,
    "cancelled": CheckConclusion.CANCELLED,
    "skipped": CheckConclusion.SKIPPED,
    "timed_out": CheckConclusion.TIMED_OUT,
    "action_required": CheckConclusion.ACTION_REQUIRED,
    "stale": CheckConclusion.STALE,
}


def _map_conclusion(value: str) -> CheckConclusion:
    """
    Map a GitHub check-run conclusion to `CheckConclusion`.

    An empty value (check still running, or no conclusion yet) maps
    to `CheckConclusion.UNKNOWN`.
    """
    if not value:
        return CheckConclusion.UNKNOWN
    return _CONCLUSION_MAP.get(value.lower(), CheckConclusion.UNKNOWN)


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def _looks_like_sha(value: str) -> bool:
    """
    Return True when `value` looks like a full-length Git SHA.

    Only full-length SHAs are accepted. Short SHAs and branch names
    are resolved through the commits endpoint.
    """
    if not value:
        return False
    length = len(value)
    if length not in (_SHA1_LEN, _SHA256_LEN):
        return False
    return _HEX_SHA.match(value) is not None


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


def _opt_str(value: Any) -> Optional[str]:
    """Return `value` as a stripped string, or `None` when empty."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None
