from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from ...domain.models import BranchRef, RepositoryRef
from ...errors import ConflictError, NotFoundError, ValidationError
from ...git.path_policy import validate_branch_name
from .client import GitHubClient


class GitHubBranchService:
    """Service coordinating remote GitHub branch querying, inspection, and creation via REST API."""

    def __init__(self, client: GitHubClient) -> None:
        self.client = client

    @staticmethod
    def parse_branch_ref(
        data: Mapping[str, Any], default_branch: Optional[str] = None
    ) -> BranchRef:
        """Transforms a raw GitHub branch API payload into a domain BranchRef."""
        name = str(data.get("name", ""))
        commit_data = data.get("commit")

        head_sha: Optional[str] = None
        if isinstance(commit_data, dict):
            head_sha = commit_data.get("sha")
        elif isinstance(commit_data, str):
            head_sha = commit_data

        is_default = bool(default_branch and name == default_branch)

        return BranchRef(
            name=name,
            head_sha=head_sha,
            is_default=is_default,
        )

    async def list_branches(
        self,
        repo: RepositoryRef,
        limit: int = 100,
        token: Optional[str] = None,
    ) -> Sequence[BranchRef]:
        """Lists branches for a remote repository with automated pagination.

        Raises:
            NetworkError: On transport timeouts or socket failures.
            SourceControlError: On GitHub REST API failure.
        """
        clamped_limit = max(1, min(limit, 100))
        endpoint = f"/repos/{repo.namespace}/{repo.name}/branches"
        params = {"per_page": clamped_limit}

        raw_items = await self.client.paginate(
            endpoint,
            params=params,
            token=token,
            max_pages=(clamped_limit + 99) // 100,
        )

        branches: list[BranchRef] = []
        for item in raw_items:
            if isinstance(item, dict) and "name" in item:
                branches.append(self.parse_branch_ref(item, default_branch=repo.default_branch))
            if len(branches) >= limit:
                break

        return tuple(branches)

    async def get_branch(
        self,
        repo: RepositoryRef,
        branch_name: str,
        token: Optional[str] = None,
    ) -> BranchRef:
        """Retrieves details of a specific remote branch.

        Raises:
            ValidationError: If branch_name violates naming rules.
            NotFoundError: If branch does not exist on remote repository.
        """
        clean_name = validate_branch_name(branch_name)
        endpoint = f"/repos/{repo.namespace}/{repo.name}/branches/{clean_name}"

        res = await self.client.get(endpoint, token=token)
        if not isinstance(res.data, dict):
            raise NotFoundError(
                f"Branch '{clean_name}' not found in repository '{repo.namespace}/{repo.name}'."
            )

        return self.parse_branch_ref(res.data, default_branch=repo.default_branch)

    async def branch_exists(
        self,
        repo: RepositoryRef,
        branch_name: str,
        token: Optional[str] = None,
    ) -> bool:
        """Checks whether a remote branch exists without raising NotFoundError."""
        try:
            await self.get_branch(repo, branch_name, token=token)
            return True
        except NotFoundError:
            return False

    async def create_branch(
        self,
        repo: RepositoryRef,
        branch_name: str,
        from_sha: str,
        token: Optional[str] = None,
    ) -> BranchRef:
        """Creates a new remote branch pointing to a specific commit SHA via Git Data API.

        Raises:
            ValidationError: If branch name or SHA is invalid.
            ConflictError: If the branch already exists on the remote repository.
            NotFoundError: If the target repository or base commit SHA does not exist.
        """
        clean_name = validate_branch_name(branch_name)
        clean_sha = from_sha.strip()
        if not clean_sha:
            raise ValidationError("Target commit SHA (from_sha) cannot be empty.")

        endpoint = f"/repos/{repo.namespace}/{repo.name}/git/refs"
        payload = {
            "ref": f"refs/heads/{clean_name}",
            "sha": clean_sha,
        }

        try:
            res = await self.client.post(endpoint, json_data=payload, token=token)
        except ValidationError as exc:
            lower_msg = str(exc).lower()
            if "already exists" in lower_msg or "reference already exists" in lower_msg:
                raise ConflictError(
                    f"Remote branch '{clean_name}' already exists in '{repo.namespace}/{repo.name}'.",
                    details={"branch": clean_name, "repository": f"{repo.namespace}/{repo.name}"},
                ) from exc
            raise

        created_sha = clean_sha
        if isinstance(res.data, dict):
            obj = res.data.get("object")
            if isinstance(obj, dict) and "sha" in obj:
                created_sha = str(obj["sha"])

        return BranchRef(
            name=clean_name,
            head_sha=created_sha,
            is_default=False,
        )
