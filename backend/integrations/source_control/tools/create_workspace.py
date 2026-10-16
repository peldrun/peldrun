# backend/integrations/source_control/tools/create_workspace.py
"""
The `source_control.create_workspace` tool.

Creates an isolated local Git workspace for a repository. The
workspace is owned by PELDRUN and lives under the configured
workspace root. Subsequent local operations (branch creation, file
edits, diff inspection) happen inside this workspace.

The tool performs two steps:

    1. Resolve the repository reference (network call to the
       provider).
    2. Materialize the workspace on disk (local Git operations).

The tool does NOT push, fetch on behalf of the user, or touch the
remote after the initial clone. It is safe to run without approval
because it only affects a PELDRUN-owned directory.

Design rules:
    - The tool resolves the credential from the OS vault, never
      from the caller's arguments.
    - The tool MUST refuse to create a workspace outside the
      configured workspace root. The path policy in
      `GitWorkspaceManager` enforces this; the tool does not bypass
      it.
    - The tool MUST NOT silently overwrite an existing workspace. If
      the manager returns an error, the tool surfaces it.
    - The tool is idempotent only in the sense that it will not
      corrupt state; each call creates a new workspace with a new
      ID.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional

from ..domain.enums import ProviderId
from ..domain.models import RepositoryRef
from ..errors import to_source_control_error
from ..git.workspace import GitWorkspaceManager, get_default_git_executable
from ..git.path_policy import get_default_path_policy
from ..registry import ProviderRegistry, get_default_registry
from ..security.token_service import (
    TokenService,
    get_default_token_service,
)
from .base import SourceControlTool, ToolContext, ToolResult

__all__ = ["CreateWorkspaceTool"]


logger = logging.getLogger(__name__)


class CreateWorkspaceTool(SourceControlTool):
    """
    Create a local Git workspace for a repository.

    Inputs:
        provider:      Provider identifier. Defaults to `"github"`.
        repository_id: Stable repository identifier.
        account_id:    Optional stable account ID.
        base_sha:      Optional commit SHA to check out. When
                       omitted, the repository's default branch is
                       used.
        branch_name:   Optional branch name for the new workspace.
                       When omitted, the default branch name is
                       used.
        use_worktree:  When True (default), materialize the
                       workspace using a lightweight worktree
                       layout instead of a full clone.
        depth:         Optional shallow clone depth.
    """

    def __init__(
        self,
        *,
        registry: Optional[ProviderRegistry] = None,
        token_service: Optional[TokenService] = None,
        workspace_manager: Optional[GitWorkspaceManager] = None,
        audit_logger: Any = None,
    ) -> None:
        super().__init__(audit_logger=audit_logger)
        self._explicit_registry = registry
        self._explicit_token_service = token_service
        self._explicit_workspace_manager = workspace_manager

    # --- Metadata -------------------------------------------------------

    @property
    def name(self) -> str:
        return "source_control.create_workspace"

    @property
    def description(self) -> str:
        return (
            "Create an isolated local Git workspace for a repository, "
            "ready for subsequent branch and file operations."
        )

    @property
    def input_schema(self) -> Mapping[str, Any]:
        return {
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "enum": ["github", "gitlab", "bitbucket"],
                    "default": "github",
                },
                "repository_id": {
                    "type": "string",
                    "description": "Stable repository identifier.",
                },
                "account_id": {
                    "type": "string",
                    "description": (
                        "Optional stable account identifier."
                    ),
                },
                "base_sha": {
                    "type": "string",
                    "description": (
                        "Optional commit SHA to check out."
                    ),
                },
                "branch_name": {
                    "type": "string",
                    "description": (
                        "Optional branch name for the new workspace."
                    ),
                },
                "use_worktree": {
                    "type": "boolean",
                    "description": (
                        "When true (default), use a lightweight "
                        "worktree layout."
                    ),
                    "default": True,
                },
                "depth": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Optional shallow clone depth."
                    ),
                },
            },
            "required": ["repository_id"],
            "additionalProperties": False,
        }

    # --- Execution ------------------------------------------------------

    async def execute(
        self, context: ToolContext, **kwargs: Any
    ) -> ToolResult:
        # --- Parse inputs ----------------------------------------------
        provider = self._parse_provider(kwargs.get("provider"))
        if isinstance(provider, ToolResult):
            return provider

        repository_id = kwargs.get("repository_id")
        if not isinstance(repository_id, str) or not repository_id.strip():
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message=(
                    "`repository_id` must be a non-empty string."
                ),
            )
        repository_id = repository_id.strip()

        account_id_override = self._parse_optional_str(
            kwargs.get("account_id"), "account_id"
        )
        if isinstance(account_id_override, ToolResult):
            return account_id_override

        base_sha = self._parse_optional_str(
            kwargs.get("base_sha"), "base_sha"
        )
        if isinstance(base_sha, ToolResult):
            return base_sha

        branch_name = self._parse_optional_str(
            kwargs.get("branch_name"), "branch_name"
        )
        if isinstance(branch_name, ToolResult):
            return branch_name

        use_worktree = bool(kwargs.get("use_worktree", True))

        depth_raw = kwargs.get("depth")
        depth: Optional[int] = None
        if depth_raw is not None and depth_raw != "":
            try:
                depth = int(depth_raw)
            except (TypeError, ValueError):
                return self.failure_result(
                    error_code="VALIDATION_ERROR",
                    error_message="`depth` must be an integer.",
                )
            if depth < 1:
                return self.failure_result(
                    error_code="VALIDATION_ERROR",
                    error_message="`depth` must be at least 1.",
                )

        # --- Resolve adapter -------------------------------------------
        registry = self._explicit_registry or get_default_registry()
        adapter = registry.try_get(provider)
        if adapter is None:
            return self.failure_result(
                error_code="NOT_FOUND",
                error_message=(
                    f"Provider '{provider.value}' is not registered."
                ),
                metadata={"provider": provider.value},
            )

        # --- Resolve credential ----------------------------------------
        token_service = (
            self._explicit_token_service or get_default_token_service()
        )
        resolved = self._resolve_account(
            token_service=token_service,
            provider=provider,
            account_id_override=account_id_override,
        )
        if isinstance(resolved, ToolResult):
            return resolved
        account_id, base_url = resolved

        token = token_service.get_access_token(
            provider, account_id, base_url
        )
        if not token:
            return self.failure_result(
                error_code="AUTH_REQUIRED",
                error_message=(
                    "No credential is stored for the selected account."
                ),
                metadata={
                    "provider": provider.value,
                    "account_id": account_id,
                },
            )

        # --- Resolve the repository reference --------------------------
        try:
            repo = await self._resolve_repository(
                adapter=adapter,
                repository_id=repository_id,
                token=token,
            )
        except Exception as exc:  # noqa: BLE001
            normalized = to_source_control_error(exc)
            return self.failure_result(
                error_code=normalized.code.value,
                error_message=normalized.message,
                metadata={"provider": provider.value},
            )

        if repo is None:
            return self.failure_result(
                error_code="NOT_FOUND",
                error_message=(
                    f"Repository '{repository_id}' was not found."
                ),
                metadata={
                    "provider": provider.value,
                    "repository_id": repository_id,
                },
            )

        # --- Create the workspace --------------------------------------
        manager = self._explicit_workspace_manager or GitWorkspaceManager(
            git=get_default_git_executable(),
            path_policy=get_default_path_policy(),
        )

        try:
            workspace = await manager.create_workspace(
                repo=repo,
                provider=provider,
                base_sha=base_sha,
                branch_name=branch_name,
                source_branch=branch_name,
                use_worktree=use_worktree,
                depth=depth,
                metadata={
                    "created_by": "source_control.create_workspace",
                    "operation_id": context.operation_id or "",
                    "run_id": context.run_id or "",
                },
            )
        except Exception as exc:  # noqa: BLE001
            normalized = to_source_control_error(exc)
            return self.failure_result(
                error_code=normalized.code.value,
                error_message=normalized.message,
                metadata={
                    "provider": provider.value,
                    "repository_id": repository_id,
                    "retryable": normalized.is_retryable,
                },
            )

        return self.success_result(
            {
                "workspace_id": workspace.workspace_id,
                "workspace_path": workspace.workspace_path,
                "repository_id": workspace.repository_id,
                "current_branch": workspace.current_branch,
                "base_sha": workspace.base_sha,
                "head_sha": workspace.head_sha,
                "is_clean": workspace.is_clean,
                "status": workspace.status,
                "is_worktree": workspace.is_worktree,
            },
            metadata={
                "provider": provider.value,
                "account_id": account_id,
            },
        )

    # --- Internal: repository resolution -------------------------------

    @staticmethod
    async def _resolve_repository(
        *,
        adapter: Any,
        repository_id: str,
        token: str,
    ) -> Optional[RepositoryRef]:
        """Return the `RepositoryRef` for `repository_id`."""
        listing = await adapter.list_repositories(
            page=1, page_size=100, token=token
        )
        for repo in listing:
            if (
                isinstance(repo, RepositoryRef)
                and repo.repository_id == repository_id
            ):
                return repo
        return None

    # --- Account resolution --------------------------------------------

    @staticmethod
    def _resolve_account(
        *,
        token_service: TokenService,
        provider: ProviderId,
        account_id_override: Optional[str],
    ) -> tuple[str, str] | ToolResult:
        stored = token_service.list_stored()
        for_provider = [
            entry for entry in stored if entry.provider == provider
        ]
        if not for_provider:
            return ToolResult.fail(
                error_code="AUTH_REQUIRED",
                error_message=(
                    f"No credential is stored for provider "
                    f"'{provider.value}'."
                ),
            )
        if account_id_override:
            for entry in for_provider:
                if entry.account_id == account_id_override:
                    return entry.account_id, entry.base_url
            return ToolResult.fail(
                error_code="AUTH_REQUIRED",
                error_message=(
                    f"No credential is stored for account "
                    f"'{account_id_override}'."
                ),
            )
        chosen = for_provider[0]
        return chosen.account_id, chosen.base_url

    # --- Helpers --------------------------------------------------------

    @staticmethod
    def _parse_provider(value: Any) -> ProviderId | ToolResult:
        if value is None or value == "":
            return ProviderId.GITHUB
        try:
            return ProviderId(str(value).strip().lower())
        except ValueError:
            return ToolResult.fail(
                error_code="VALIDATION_ERROR",
                error_message=(
                    f"Unsupported provider: {value!r}."
                ),
            )

    @staticmethod
    def _parse_optional_str(
        value: Any, field_name: str
    ) -> Optional[str] | ToolResult:
        if value is None:
            return None
        if not isinstance(value, str):
            return ToolResult.fail(
                error_code="VALIDATION_ERROR",
                error_message=f"`{field_name}` must be a string.",
            )
        trimmed = value.strip()
        return trimmed or None
