# backend/integrations/source_control/providers/github/releases.py
"""
GitHub release service for the Source Control Integration subsystem.

A GitHub Release is a GitHub concept that combines:

    - A tag (created automatically if it does not exist).
    - A title and release notes (Markdown).
    - Optional binary assets (NOT managed by this service; asset
      upload requires a separate upload flow with the uploads.
      github.com host, which is a larger feature).
    - Draft / prerelease flags.

This service manages the release lifecycle through the GitHub
REST API. It is intentionally distinct from ``git/tag.py``:

    - ``tag.py`` handles LOCAL Git tags.
    - ``releases.py`` handles REMOTE GitHub releases (which
      create or reference remote tags).

Both are legitimate and complementary. A user who creates a tag
via the Tag Manager will not see a release automatically; a user
who creates a release via the Release Manager will see both a
remote tag and a release. This matches GitHub's own UI model.

Endpoints used:

    - ``GET    /repos/{owner}/{repo}/releases``
      List releases (default: excludes drafts).
    - ``POST   /repos/{owner}/{repo}/releases``
      Create a release. If ``tag_name`` does not exist, GitHub
      creates the tag from ``target_commitish``.
    - ``GET    /repos/{owner}/{repo}/releases/{id}``
      Fetch a single release.
    - ``DELETE /repos/{owner}/{repo}/releases/{id}``
      Delete a release. Does NOT delete the tag.

Design rules:
    - Bodies are redacted before being returned to the caller.
    - The service never logs tokens, release bodies, or asset URLs
      in full.
    - The service never uploads assets. That is documented as an
      explicit non-goal for this phase.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, List, Mapping, Optional, Sequence
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
    "GitHubReleaseService",
    "ReleaseRef",
    "MAX_RELEASES",
]


logger = logging.getLogger(__name__)


#: Maximum number of releases returned in a single listing.
MAX_RELEASES: int = 200

#: Default page size for release listings.
_DEFAULT_PAGE_SIZE: int = 30


# ---------------------------------------------------------------------------
# Value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReleaseRef:
    """A single GitHub release.

    The ``release_id`` is the GitHub numeric ID as a string. It is
    distinct from the ``tag_name``: several releases can share a
    tag (in theory), and the tag can be renamed independently of
    the release. The UI navigates by ``release_id``.
    """

    release_id: str
    tag_name: str
    name: str
    body: Optional[str]
    html_url: str
    is_draft: bool
    is_prerelease: bool
    author_login: Optional[str] = None
    target_commitish: Optional[str] = None
    created_at: Optional[datetime] = None
    published_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class GitHubReleaseService:
    """Release operations against the GitHub REST API."""

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

    async def list_releases(
        self,
        *,
        repo: RepositoryRef,
        token: str,
        include_drafts: bool = False,
        page: int = 1,
        page_size: int = _DEFAULT_PAGE_SIZE,
        max_results: int = MAX_RELEASES,
    ) -> Sequence[ReleaseRef]:
        """List releases for a repository.

        By default, drafts are excluded (GitHub's default). Pass
        ``include_drafts=True`` to include them. Prereleases are
        always included; the UI distinguishes them via the
        ``is_prerelease`` flag.
        """
        self._validate_repo_and_token(repo=repo, token=token)
        if page < 1:
            raise ValidationError("page must be >= 1.")
        if page_size < 1:
            raise ValidationError("page_size must be >= 1.")
        if max_results <= 0:
            raise ValidationError("max_results must be positive.")

        repo_path = self._build_repo_path(repo)
        capped = min(page_size, MAX_PAGE_SIZE)
        max_pages = (max_results + capped - 1) // capped
        results: List[ReleaseRef] = []
        current_page = page
        pages_fetched = 0

        while pages_fetched < max_pages:
            payload = await self._client.get_json(
                f"{repo_path}/releases",
                token=token,
                params={"per_page": capped, "page": current_page},
            )
            if not isinstance(payload, list):
                raise ProviderError(
                    "GitHub returned an unexpected payload for the "
                    "releases endpoint.",
                )
            if not payload:
                break
            for item in payload:
                if not isinstance(item, Mapping):
                    continue
                try:
                    release = self._map_release(item)
                except ValidationError:
                    logger.debug(
                        "Skipping malformed release entry in listing."
                    )
                    continue
                if release.is_draft and not include_drafts:
                    continue
                results.append(release)
                if len(results) >= max_results:
                    break
            if len(results) >= max_results or len(payload) < capped:
                break
            current_page += 1
            pages_fetched += 1
        return tuple(results)

    # --- Get -----------------------------------------------------------

    async def get_release(
        self,
        *,
        repo: RepositoryRef,
        release_id: int,
        token: str,
    ) -> ReleaseRef:
        """Fetch a single release by its numeric GitHub ID."""
        self._validate_repo_and_token(repo=repo, token=token)
        if not isinstance(release_id, int) or release_id <= 0:
            raise ValidationError(
                "release_id must be a positive integer.",
                details={"release_id": release_id},
            )
        repo_path = self._build_repo_path(repo)
        payload = await self._client.get_json(
            f"{repo_path}/releases/{release_id}",
            token=token,
        )
        if not isinstance(payload, Mapping):
            raise ProviderError(
                "GitHub returned an unexpected payload for the "
                "release endpoint.",
                details={"release_id": release_id},
            )
        return self._map_release(payload)

    # --- Create --------------------------------------------------------

    async def create_release(
        self,
        *,
        repo: RepositoryRef,
        tag_name: str,
        name: str,
        body: str,
        target_commitish: str,
        draft: bool,
        prerelease: bool,
        token: str,
    ) -> ReleaseRef:
        """Create a GitHub release.

        If ``tag_name`` does not exist on the remote, GitHub creates
        it from ``target_commitish`` (a branch name or a commit
        SHA). If it already exists, GitHub uses the existing tag
        and IGNORES ``target_commitish``; this matches GitHub's own
        behavior, and the caller should not expect the tag to be
        moved.

        Args:
            tag_name: The tag the release targets. Required.
            name: The release title. May be empty (GitHub falls
                back to ``tag_name`` as the display title).
            body: Release notes (Markdown). May be empty.
            target_commitish: Branch or SHA the tag is created
                from, when the tag does not already exist.
                Ignored if the tag exists.
            draft: When True, the release is created as a draft
                and is invisible to the public until published.
            prerelease: When True, the release is marked as a
                prerelease.
            token: Access token. Required.
        """
        self._validate_repo_and_token(repo=repo, token=token)
        if not tag_name or not tag_name.strip():
            raise ValidationError("tag_name must not be empty.")
        if not target_commitish or not target_commitish.strip():
            raise ValidationError(
                "target_commitish must not be empty."
            )

        payload: dict = {
            "tag_name": tag_name,
            "name": name or tag_name,
            "body": body or "",
            "target_commitish": target_commitish,
            "draft": bool(draft),
            "prerelease": bool(prerelease),
        }

        repo_path = self._build_repo_path(repo)
        response = await self._client.post_json(
            f"{repo_path}/releases",
            token=token,
            json_body=payload,
        )
        if not isinstance(response, Mapping):
            raise ProviderError(
                "GitHub returned an unexpected payload for the "
                "release creation endpoint.",
            )
        return self._map_release(response)

    # --- Delete --------------------------------------------------------

    async def delete_release(
        self,
        *,
        repo: RepositoryRef,
        release_id: int,
        token: str,
    ) -> None:
        """Delete a GitHub release.

        This deletes the RELEASE only. The underlying tag is NOT
        deleted; it remains in the repository. If the caller wants
        to delete the tag as well, that is a separate operation
        (``delete_tag`` on the tag service, or ``git tag -d``
        locally).
        """
        self._validate_repo_and_token(repo=repo, token=token)
        if not isinstance(release_id, int) or release_id <= 0:
            raise ValidationError(
                "release_id must be a positive integer.",
                details={"release_id": release_id},
            )
        repo_path = self._build_repo_path(repo)
        await self._client.delete_json(
            f"{repo_path}/releases/{release_id}",
            token=token,
        )

    # --- Internal: validation and URL construction ---------------------

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

    # --- Internal: mapping ---------------------------------------------

    @staticmethod
    def _map_release(payload: Mapping[str, Any]) -> ReleaseRef:
        release_id = payload.get("id")
        if release_id is None:
            raise ValidationError(
                "Release payload is missing `id`."
            )
        tag_name = payload.get("tag_name")
        if not isinstance(tag_name, str) or not tag_name:
            raise ValidationError(
                "Release payload is missing `tag_name`."
            )
        raw_name = payload.get("name")
        name = (
            str(raw_name)
            if isinstance(raw_name, str) and raw_name
            else tag_name
        )

        raw_body = payload.get("body")
        clean_body: Optional[str] = None
        if isinstance(raw_body, str) and raw_body:
            clean_body = redact_text(raw_body)

        html_url = payload.get("html_url")
        if not isinstance(html_url, str) or not html_url:
            html_url = ""

        author = payload.get("author")
        author_login: Optional[str] = None
        if isinstance(author, Mapping):
            login = author.get("login")
            if isinstance(login, str) and login:
                author_login = login

        raw_target = payload.get("target_commitish")
        target_commitish: Optional[str] = None
        if isinstance(raw_target, str) and raw_target:
            target_commitish = raw_target

        return ReleaseRef(
            release_id=str(release_id),
            tag_name=tag_name,
            name=name,
            body=clean_body,
            html_url=html_url,
            is_draft=bool(payload.get("draft", False)),
            is_prerelease=bool(payload.get("prerelease", False)),
            author_login=author_login,
            target_commitish=target_commitish,
            created_at=_parse_iso(payload.get("created_at")),
            published_at=_parse_iso(payload.get("published_at")),
        )

    # --- Presentation --------------------------------------------------

    def __repr__(self) -> str:  # pragma: no cover - trivial formatting
        return f"<GitHubReleaseService api_url={self._api_url!r}>"


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
