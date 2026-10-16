# backend/integrations/source_control/providers/github/repositories.py
"""
GitHub repository service for the Source Control Integration
subsystem.

This service implements the repository-specific subset of the
`SourceControlProvider` protocol for GitHub. It is used by
`GitHubProviderAdapter` and is not intended to be called directly by
the application layer.

The service knows about three GitHub REST endpoints:

    - `GET /user/repos`                        (list repositories)
    - `GET /repos/{owner}/{repo}`              (repository details)
    - `GET /repos/{owner}/{repo}/contents/{path}` (read file content)

All HTTP is delegated to `GitHubClient`. Error mapping, rate-limit
handling, and retries are the client's responsibility. This service
only handles GitHub-specific data shaping: building URLs, mapping
JSON to `RepositoryRef`, and decoding file content.

Design rules:
    - The service is stateless with respect to per-request data. It
      is safe to share across coroutines.
    - The service never logs file content, tokens, or user data.
    - The service never embeds credentials in URLs.
    - The service refuses to fetch files larger than
      `_MAX_CONTENTS_API_BYTES`. Larger files must be fetched via
      the Git Data API or cloned locally.
"""

from __future__ import annotations

import base64
import binascii
import logging
from typing import Any, List, Mapping, Optional, Sequence
from urllib.parse import quote

from ...domain.enums import ProviderId
from ...domain.models import RepositoryRef
from ...errors import (
    NotFoundError,
    PermissionDeniedError,
    ProviderError,
    UnsupportedCapabilityError,
    ValidationError,
)
from .client import (
    DEFAULT_API_URL,
    GitHubApiConfig,
    GitHubClient,
    MAX_PAGE_SIZE,
)

__all__ = [
    "GitHubRepositoryService",
    "MAX_CONTENTS_API_BYTES",
]


logger = logging.getLogger(__name__)


#: GitHub's documented maximum file size for the contents API. Files
#: larger than this are rejected by the API and must be fetched via
#: the Git Data API. The value is slightly below the documented
#: one mebibyte to leave a small safety margin.
MAX_CONTENTS_API_BYTES: int = 1_000_000

#: Affiliation values sent to `/user/repos`. The union of the three
#: covers every repository the authenticated user can access.
_AFFILIATION = "owner,collaborator,organization_member"

#: Repository fields we request from GitHub. Keeping the field set
#: small reduces response size and makes the mapping explicit.
_REPO_FIELDS = (
    "id",
    "name",
    "full_name",
    "private",
    "owner",
    "html_url",
    "clone_url",
    "default_branch",
    "description",
    "archived",
    "disabled",
)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class GitHubRepositoryService:
    """
    Repository operations against the GitHub REST API.

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

    # --- List repositories ---------------------------------------------

    async def list_repositories(
        self,
        *,
        token: str,
        page: int = 1,
        page_size: int = 30,
    ) -> Sequence[RepositoryRef]:
        """
        Return repositories accessible to the authenticated user.

        Uses `GET /user/repos` with the union of all affiliations.
        When `page_size` exceeds `MAX_PAGE_SIZE`, it is silently
        capped. Callers that need to enumerate every repository are
        responsible for paging through results.

        Args:
            token: Access token. Required.
            page: 1-based page number.
            page_size: Repositories per page (capped at 100).

        Raises:
            ValidationError: If `page` or `page_size` is invalid.
            PermissionDeniedError: If the token cannot list
                repositories.
        """
        if not token:
            raise ValidationError("token must not be empty.")
        if page < 1:
            raise ValidationError("page must be >= 1.")
        if page_size < 1:
            raise ValidationError("page_size must be >= 1.")
        capped_size = min(page_size, MAX_PAGE_SIZE)

        params = {
            "affiliation": _AFFILIATION,
            "per_page": capped_size,
            "page": page,
            "sort": "full_name",
            "direction": "asc",
        }

        payload = await self._client.get_json(
            "/user/repos",
            token=token,
            params=params,
        )

        if not isinstance(payload, list):
            raise ProviderError(
                "GitHub returned an unexpected payload for /user/repos.",
                details={"type": type(payload).__name__},
            )

        repositories: List[RepositoryRef] = []
        for item in payload[:capped_size]:
            if not isinstance(item, Mapping):
                continue
            try:
                repositories.append(self._map_repository(item))
            except ValidationError:
                # A single malformed entry MUST NOT abort the whole
                # listing. Skip it and continue.
                logger.debug(
                    "Skipping malformed repository entry in "
                    "/user/repos response."
                )
                continue
        return tuple(repositories)

    # --- Repository details --------------------------------------------

    async def get_repository(
        self,
        *,
        repo: RepositoryRef,
        token: str,
    ) -> RepositoryRef:
        """
        Return fresh metadata for a repository.

        Args:
            repo: The repository reference. Only `namespace` and
                `name` are used to build the URL.
            token: Access token. Required.

        Raises:
            ValidationError: If `repo` is not a `RepositoryRef`.
            NotFoundError: If the repository no longer exists.
        """
        if not isinstance(repo, RepositoryRef):
            raise ValidationError(
                "repo must be a RepositoryRef.",
                details={"type": type(repo).__name__},
            )
        if not token:
            raise ValidationError("token must not be empty.")

        path = self._build_repo_path(repo)
        payload = await self._client.get_json(path, token=token)

        if not isinstance(payload, Mapping):
            raise ProviderError(
                "GitHub returned an unexpected payload for repository "
                "details.",
                details={"path": path},
            )
        return self._map_repository(payload)

    # --- Read file -----------------------------------------------------

    async def read_file(
        self,
        *,
        repo: RepositoryRef,
        path: str,
        ref: Optional[str],
        token: str,
    ) -> str:
        """
        Read a text file via the GitHub contents API.

        The contents API returns file content as base64. The service
        decodes the content as UTF-8 with replacement for invalid
        sequences.

        The contents API cannot return files larger than ~1 MiB.
        Larger files raise `UnsupportedCapabilityError` so that the
        caller can suggest a local clone as an alternative.

        Args:
            repo: The repository reference.
            path: Path of the file, relative to the repository root.
            ref: Optional branch, tag, or commit SHA.
            token: Access token. Required.

        Raises:
            ValidationError: If `path` is empty, or if `path` points
                to a directory rather than a file.
            NotFoundError: If the file does not exist at `ref`.
            UnsupportedCapabilityError: If the file is too large for
                the contents API.
        """
        if not isinstance(repo, RepositoryRef):
            raise ValidationError(
                "repo must be a RepositoryRef.",
                details={"type": type(repo).__name__},
            )
        if not path or not isinstance(path, str):
            raise ValidationError("path must be a non-empty string.")
        if not token:
            raise ValidationError("token must not be empty.")

        repo_path = self._build_repo_path(repo)
        encoded_file_path = quote(path.lstrip("/"), safe="/")
        contents_path = f"{repo_path}/contents/{encoded_file_path}"

        params: Optional[dict] = None
        if ref:
            params = {"ref": ref}

        try:
            payload = await self._client.get_json(
                contents_path,
                token=token,
                params=params,
            )
        except PermissionDeniedError as exc:
            if self._is_file_too_large(exc):
                raise UnsupportedCapabilityError(
                    "The file is too large to be read via the GitHub "
                    "contents API. Clone the repository locally to "
                    "read it.",
                    details={
                        "path": path,
                        "reason": "file_too_large",
                        "max_bytes": MAX_CONTENTS_API_BYTES,
                    },
                ) from exc
            raise
        except NotFoundError:
            raise
        except ProviderError:
            raise

        # The contents API returns a list when the path is a
        # directory. The caller asked for a file, so this is a
        # validation error rather than a not-found error.
        if isinstance(payload, list):
            raise ValidationError(
                "Path refers to a directory, not a file.",
                details={"path": path},
            )

        if not isinstance(payload, Mapping):
            raise ProviderError(
                "GitHub returned an unexpected payload for file "
                "content.",
                details={"path": path},
            )

        file_type = str(payload.get("type") or "")
        if file_type and file_type != "file":
            # Symlinks and submodules are also not text files.
            raise UnsupportedCapabilityError(
                f"Path is not a regular file (type: {file_type}).",
                details={"path": path, "type": file_type},
            )

        size = payload.get("size")
        if isinstance(size, int) and size > MAX_CONTENTS_API_BYTES:
            raise UnsupportedCapabilityError(
                "The file is too large to be read via the GitHub "
                "contents API.",
                details={
                    "path": path,
                    "size": size,
                    "max_bytes": MAX_CONTENTS_API_BYTES,
                },
            )

        encoding = str(payload.get("encoding") or "").lower()
        raw_content = payload.get("content")
        if not isinstance(raw_content, str):
            # Very large files can be returned with `content: ""` and
            # `encoding: "none"` when the caller uses the
            # `Accept: application/vnd.github.raw` header, but we do
            # not send that header. Treat an absent content field as
            # an unsupported case.
            raise UnsupportedCapabilityError(
                "GitHub did not return the file content.",
                details={"path": path, "encoding": encoding},
            )

        if encoding and encoding != "base64":
            raise UnsupportedCapabilityError(
                f"Unsupported content encoding: {encoding}.",
                details={"path": path, "encoding": encoding},
            )

        decoded = self._decode_base64_content(raw_content, path=path)
        return decoded.decode("utf-8", errors="replace")

    # --- Internal: URL construction ------------------------------------

    @staticmethod
    def _build_repo_path(repo: RepositoryRef) -> str:
        """
        Return the `/repos/{owner}/{name}` path for a repository.

        Both components are URL-encoded. GitHub usernames and
        repository names cannot contain slashes, but encoding is a
        cheap safety measure and matches GitHub's own URL scheme.
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

    def _map_repository(self, payload: Mapping[str, Any]) -> RepositoryRef:
        """
        Map a GitHub repository payload to `RepositoryRef`.

        Raises `ValidationError` when a required field is missing.
        """
        repo_id = payload.get("id")
        if repo_id is None:
            raise ValidationError("Repository payload is missing `id`.")

        name = payload.get("name")
        if not isinstance(name, str) or not name:
            raise ValidationError(
                "Repository payload is missing a valid `name`."
            )

        owner = payload.get("owner")
        if not isinstance(owner, Mapping):
            raise ValidationError(
                "Repository payload is missing a valid `owner`."
            )
        namespace = owner.get("login")
        if not isinstance(namespace, str) or not namespace:
            raise ValidationError(
                "Repository payload is missing `owner.login`."
            )

        clone_url = payload.get("clone_url")
        if not isinstance(clone_url, str) or not clone_url:
            raise ValidationError(
                "Repository payload is missing `clone_url`."
            )

        web_url = payload.get("html_url")
        if not isinstance(web_url, str) or not web_url:
            raise ValidationError(
                "Repository payload is missing `html_url`."
            )

        default_branch = payload.get("default_branch")
        if not isinstance(default_branch, str) or not default_branch:
            # Empty repositories have no default branch yet.
            default_branch = "main"

        return RepositoryRef(
            provider=ProviderId.GITHUB,
            account_id=str(owner.get("id") or ""),
            repository_id=str(repo_id),
            namespace=namespace,
            name=name,
            clone_url=clone_url,
            web_url=web_url,
            default_branch=default_branch,
            is_private=bool(payload.get("private", False)),
            description=_opt_str(payload.get("description")),
            base_url=self._api_url,
        )

    # --- Internal: content decoding ------------------------------------

    @staticmethod
    def _decode_base64_content(content: str, *, path: str) -> bytes:
        """
        Decode base64 content returned by the contents API.

        GitHub inserts newlines into the base64 payload; they are
        removed before decoding. A `binascii.Error` indicates the
        API returned malformed content, which we surface as a
        `ProviderError`.
        """
        stripped = content.replace("\n", "").replace("\r", "").strip()
        try:
            return base64.b64decode(stripped, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ProviderError(
                "GitHub returned malformed base64 content.",
                details={"path": path},
                cause=exc,
            ) from exc

    @staticmethod
    def _is_file_too_large(exc: PermissionDeniedError) -> bool:
        """
        Return True when a 403 response indicates a too-large file.

        GitHub returns 403 with a descriptive message when the file
        exceeds the contents API size limit. The message is checked
        case-insensitively for known phrases.
        """
        details = exc.details or {}
        message = str(details.get("provider_message") or "").lower()
        if not message:
            # Fall back to the exception's own message; the client
            # redacts it but keeps the provider description.
            message = str(exc).lower()
        return (
            "too large" in message
            or "1 mb" in message
            or "blobs up to" in message
        )

    # --- Presentation ---------------------------------------------------

    def __repr__(self) -> str:  # pragma: no cover - trivial formatting
        return f"<GitHubRepositoryService api_url={self._api_url!r}>"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _opt_str(value: Any) -> Optional[str]:
    """Return `value` as a stripped string, or `None` when empty."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None
