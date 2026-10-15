from __future__ import annotations

import base64
from typing import Any, Mapping, Optional, Sequence

from ...domain.enums import ProviderId
from ...domain.models import RepositoryRef
from ...errors import NotFoundError, ValidationError
from .client import GitHubClient


class GitHubRepositoryService:
    """Service coordinating remote GitHub repository inspection, browsing, and file contents retrieval."""

    def __init__(self, client: GitHubClient) -> None:
        self.client = client

    @staticmethod
    def parse_repository_ref(data: Mapping[str, Any]) -> RepositoryRef:
        """Transforms a raw GitHub repository API payload into a domain RepositoryRef."""
        repo_id = str(data.get("id", ""))
        full_name = str(data.get("full_name", ""))

        if "/" in full_name:
            namespace, name = full_name.split("/", 1)
        else:
            owner_obj = data.get("owner")
            namespace = owner_obj.get("login", "") if isinstance(owner_obj, dict) else ""
            name = str(data.get("name", ""))

        owner_id = ""
        owner_data = data.get("owner")
        if isinstance(owner_data, dict):
            owner_id = str(owner_data.get("id", ""))

        is_private = bool(data.get("private", False))
        visibility = str(data.get("visibility", "private" if is_private else "public"))

        return RepositoryRef(
            provider=ProviderId.GITHUB,
            account_id=owner_id,
            repository_id=repo_id,
            namespace=namespace,
            name=name,
            clone_url=str(data.get("clone_url", "")),
            web_url=str(data.get("html_url", "")),
            default_branch=str(data.get("default_branch") or "main"),
            is_private=is_private,
            metadata={
                "full_name": full_name,
                "description": data.get("description"),
                "visibility": visibility,
                "fork": bool(data.get("fork", False)),
                "archived": bool(data.get("archived", False)),
                "stargazers_count": int(data.get("stargazers_count", 0)),
                "open_issues_count": int(data.get("open_issues_count", 0)),
            },
        )

    async def list_repositories(
        self,
        query: Optional[str] = None,
        limit: int = 100,
        token: Optional[str] = None,
    ) -> Sequence[RepositoryRef]:
        """Lists user-accessible repositories with optional search query filtering.

        Raises:
            NetworkError: On network or transport timeout.
            SourceControlError: On GitHub REST API failure.
        """
        clamped_limit = max(1, min(limit, 100))

        if query and query.strip():
            # Search query path using GitHub search endpoint
            search_params = {
                "q": query.strip(),
                "per_page": clamped_limit,
                "sort": "updated",
            }
            raw_items = await self.client.paginate(
                "/search/repositories",
                params=search_params,
                token=token,
                max_pages=(clamped_limit + 99) // 100,
            )
        else:
            # Direct accessible repositories path
            user_params = {
                "affiliation": "owner,collaborator,organization_member",
                "per_page": clamped_limit,
                "sort": "updated",
            }
            raw_items = await self.client.paginate(
                "/user/repos",
                params=user_params,
                token=token,
                max_pages=(clamped_limit + 99) // 100,
            )

        repos: list[RepositoryRef] = []
        for item in raw_items:
            if isinstance(item, dict) and "id" in item:
                repos.append(self.parse_repository_ref(item))
            if len(repos) >= limit:
                break

        return tuple(repos)

    async def get_repository(
        self,
        repository_id: str,
        token: Optional[str] = None,
    ) -> RepositoryRef:
        """Retrieves a single repository by its numeric ID or owner/name slug.

        Raises:
            ValidationError: If repository_id is empty.
            NotFoundError: If repository is not found or inaccessible.
        """
        clean_id = repository_id.strip()
        if not clean_id:
            raise ValidationError("Repository ID cannot be empty.")

        # Numeric ID maps to /repositories/:id; slug maps to /repos/:owner/:name
        if clean_id.isdigit():
            endpoint = f"/repositories/{clean_id}"
        elif "/" in clean_id:
            endpoint = f"/repos/{clean_id}"
        else:
            endpoint = f"/repositories/{clean_id}"

        res = await self.client.get(endpoint, token=token)
        if not isinstance(res.data, dict):
            raise NotFoundError(f"GitHub repository '{clean_id}' returned invalid response format.")

        return self.parse_repository_ref(res.data)

    async def get_repository_by_slug(
        self,
        owner: str,
        name: str,
        token: Optional[str] = None,
    ) -> RepositoryRef:
        """Convenience helper retrieving a repository explicitly by owner and name."""
        clean_owner = owner.strip()
        clean_name = name.strip()
        if not clean_owner or not clean_name:
            raise ValidationError("Repository owner and name must not be empty.")

        return await self.get_repository(f"{clean_owner}/{clean_name}", token=token)

    async def read_file(
        self,
        repo: RepositoryRef,
        file_path: str,
        ref: Optional[str] = None,
        token: Optional[str] = None,
    ) -> bytes:
        """Reads file contents at a given ref/commit using the GitHub Contents API.

        Raises:
            ValidationError: If file path is invalid or points to a directory.
            NotFoundError: If file does not exist at the specified ref.
        """
        clean_path = file_path.strip().lstrip("/")
        if not clean_path:
            raise ValidationError("File path cannot be empty.")

        endpoint = f"/repos/{repo.namespace}/{repo.name}/contents/{clean_path}"
        params: dict[str, Any] = {}
        if ref and ref.strip():
            params["ref"] = ref.strip()

        res = await self.client.get(endpoint, params=params, token=token)

        # Handle directory listings
        if isinstance(res.data, list):
            raise ValidationError(
                f"Path '{file_path}' in repository '{repo.namespace}/{repo.name}' points to a directory, not a file."
            )

        if not isinstance(res.data, dict):
            raise NotFoundError(
                f"Resource '{file_path}' in repository '{repo.namespace}/{repo.name}' not found."
            )

        target_type = res.data.get("type")
        if target_type != "file":
            raise ValidationError(
                f"Path '{file_path}' in repository '{repo.namespace}/{repo.name}' is a '{target_type}', not a file."
            )

        content_b64 = res.data.get("content")
        encoding = res.data.get("encoding", "base64")

        # Standard base64 payload decoding
        if content_b64 and encoding == "base64":
            cleaned_b64 = content_b64.replace("\n", "").replace("\r", "")
            try:
                return base64.b64decode(cleaned_b64)
            except Exception as exc:
                raise ValidationError(
                    f"Failed to decode base64 file content for '{file_path}': {exc}"
                ) from exc

        # Handle larger files where content is omitted and download_url is provided
        download_url = res.data.get("download_url")
        if download_url:
            raw_res = await self.client.get(str(download_url), token=token)
            if isinstance(raw_res.data, bytes):
                return raw_res.data
            if isinstance(raw_res.data, str):
                return raw_res.data.encode("utf-8")

        return b""
