from __future__ import annotations

from typing import Any, Mapping, Optional
from pydantic import BaseModel, Field

from ..domain.enums import ProviderId
from ..domain.models import BranchRef, RepositoryRef
from ..domain.policies import is_protected_branch
from ..errors import (
    AuthRequiredError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    SourceControlError,
    ValidationError,
)
from ..git.path_policy import validate_branch_name
from ..registry import ProviderRegistry, get_default_registry
from ..security.policy_engine import PolicyEngine, get_default_policy_engine
from ..security.token_service import TokenService, get_default_token_service


class CreateBranchArgs(BaseModel):
    """Input argument schema for the create branch tool."""

    repository_id: str = Field(
        ...,
        description="Target repository identifier (numeric ID or 'owner/repo' slug).",
    )
    branch_name: str = Field(
        ...,
        description="Name of the new branch to create (must follow git ref naming rules, e.g. 'feature/task-1').",
    )
    from_sha: Optional[str] = Field(
        default=None,
        description="Optional starting commit SHA or base ref. Defaults to repository default branch HEAD.",
    )
    provider: Optional[str] = Field(
        default="github",
        description="Source control provider identifier (e.g., 'github'). Defaults to 'github'.",
    )


class CreateBranchTool:
    """Agent tool that creates a new working branch in a source repository."""

    name: str = "source_control_create_branch"
    description: str = (
        "Creates a new Git branch in a target source repository starting from a commit SHA or default branch HEAD. "
        "Validates branch naming syntax, checks branch protection policies, and resolves credentials securely. "
        "Never requires or accepts raw credentials."
    )
    args_schema: type[BaseModel] = CreateBranchArgs

    def __init__(
        self,
        registry: Optional[ProviderRegistry] = None,
        token_service: Optional[TokenService] = None,
        policy_engine: Optional[PolicyEngine] = None,
    ) -> None:
        self.registry = registry or get_default_registry()
        self.token_service = token_service or get_default_token_service()
        self.policy_engine = policy_engine or get_default_policy_engine()

    async def _arun(
        self,
        repository_id: str,
        branch_name: str,
        from_sha: Optional[str] = None,
        provider: Optional[str] = "github",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Asynchronously creates a new branch using the appropriate provider adapter."""
        # 1. Validate and resolve provider identity
        provider_str = (provider or "github").strip().lower()
        try:
            provider_id = ProviderId(provider_str)
        except ValueError:
            raise ValidationError(
                f"Unsupported source control provider '{provider_str}'. Supported providers: {[p.value for p in ProviderId]}"
            )

        clean_repo_id = repository_id.strip()
        if not clean_repo_id:
            raise ValidationError("Repository ID cannot be empty.")

        # 2. Validate branch name syntax and reject protected names
        clean_branch_name = validate_branch_name(branch_name)
        if is_protected_branch(clean_branch_name):
            raise PermissionDeniedError(
                f"Cannot create branch with protected name '{clean_branch_name}'. "
                "Please choose an isolated feature branch name (e.g. 'feature/...', 'fix/...').",
                details={"branch_name": clean_branch_name},
            )

        # 3. Enforce operational policy
        allowed, reason = self.policy_engine.is_operation_allowed(
            operation="create_branch",
            provider=provider_id.value,
            repository=clean_repo_id,
            target_branch=clean_branch_name,
        )
        if not allowed:
            raise PermissionDeniedError(
                f"Policy prohibits branch creation in repository '{clean_repo_id}': {reason}",
                details={
                    "provider": provider_id.value,
                    "repository_id": clean_repo_id,
                    "branch_name": clean_branch_name,
                    "reason": reason,
                },
            )

        # 4. Retrieve ephemeral credentials from secure vault
        token = await self.token_service.get_access_token(provider_id)
        if not token:
            raise AuthRequiredError(
                f"No active credentials found for '{provider_id.value}'. Please connect your account first in Settings.",
                details={"provider": provider_id.value},
            )

        # 5. Resolve provider adapter and repository metadata
        adapter = self.registry.get(provider_id.value)
        try:
            repo = await adapter.get_repository(clean_repo_id, token=token)
        except SourceControlError:
            raise
        except Exception as exc:
            raise SourceControlError(
                f"Failed to resolve repository '{clean_repo_id}': {exc}"
            ) from exc

        # 6. Determine starting commit SHA if omitted
        effective_sha = from_sha.strip() if from_sha and from_sha.strip() else None
        if not effective_sha:
            try:
                default_branch_ref = await adapter.get_branch(
                    repo, repo.default_branch, token=token
                )
                effective_sha = default_branch_ref.head_sha
            except Exception as exc:
                raise SourceControlError(
                    f"Failed to resolve base commit SHA from default branch '{repo.default_branch}': {exc}"
                ) from exc

        if not effective_sha:
            raise ValidationError(
                f"Could not determine a valid starting commit SHA for branch '{clean_branch_name}'."
            )

        # 7. Execute remote branch creation
        try:
            branch_ref: BranchRef = await adapter.create_branch(
                repo=repo,
                branch_name=clean_branch_name,
                from_sha=effective_sha,
                token=token,
            )
        except SourceControlError:
            raise
        except Exception as exc:
            raise SourceControlError(
                f"Failed to create branch '{clean_branch_name}' in repository '{repo.namespace}/{repo.name}': {exc}"
            ) from exc

        return {
            "status": "success",
            "provider": provider_id.value,
            "repository_id": repo.repository_id,
            "full_name": f"{repo.namespace}/{repo.name}",
            "branch_name": branch_ref.name,
            "head_sha": branch_ref.head_sha,
            "is_default": branch_ref.is_default,
            "from_sha": effective_sha,
            "message": f"Branch '{branch_ref.name}' created successfully at commit {effective_sha[:7] if effective_sha else ''}.",
        }

    async def aexecute(self, **kwargs: Any) -> dict[str, Any]:
        """Public async execution adapter conforming to standard agent tool calling contracts."""
        validated_args = self.args_schema(**kwargs)
        return await self._arun(**validated_args.model_dump())

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Synchronous wrapper raising an error to enforce non-blocking execution."""
        raise NotImplementedError(
            "CreateBranchTool only supports asynchronous execution via aexecute() or _arun()."
        )
