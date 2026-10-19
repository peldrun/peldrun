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
    2. Materialize the workspace on disk (local Git operations, plus
       an authenticated clone when the repository is private).

The tool does NOT push, fetch on behalf of the user, or touch the
remote after the initial clone. It is safe to run without approval
because it only affects a PELDRUN-owned directory.

Design rules:
    - The tool resolves the credential from the OS vault, never
      from the caller's arguments.
    - The tool MUST pass the credential to
      `GitWorkspaceManager.create_workspace(auth_token=...)` so that
      private repositories can be cloned. The workspace manager
      embeds the token in the clone URL only for the duration of the
      network operation and sanitizes `.git/config` immediately
      afterwards.
    - The tool MUST refuse to create a workspace outside the
      configured workspace root. The path policy in
      `GitWorkspaceManager` enforces this; the tool does not bypass
      it.
    - The tool MUST NOT silently overwrite an existing workspace. If
      the manager returns an error, the tool surfaces it.
    - The tool is idempotent only in the sense that it will not
      corrupt state; each call creates a new workspace with a new
      ID.
    - The credential MUST NEVER appear in an error message, a tool
      result, or the audit log. The base URL is redacted before it
      is included in any diagnostic.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional, Tuple, Union

from ..domain.enums import ProviderId
from ..domain.models import RepositoryRef
from ..errors import to_source_control_error
from ..git.workspace import (
    GitWorkspaceManager,
    get_default_workspace_manager,
)
from ..registry import ProviderRegistry, get_default_registry
from ..providers import register_builtin_providers
from ..security.redaction import redact_url
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

    The credential is resolved from the OS vault through the
    `TokenService`. The account is selected with the following
    precedence:

        1. The `account_id` argument to the tool.
        2. `context.metadata["account_id"]` when the facade injects
           an account hint.
        3. The first stored account for the provider.

    The credential is passed to `GitWorkspaceManager.create_workspace`
    so that private repositories can be cloned. The token is NEVER
    persisted in `.git/config`.
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

        account_id_arg = self._parse_optional_str(
            kwargs.get("account_id"), "account_id"
        )
        if isinstance(account_id_arg, ToolResult):
            return account_id_arg

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
        # Defensive: Ensure built-in providers are registered
        register_builtin_providers(registry=registry)

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
        #
        # The credential is resolved BEFORE any network call. If it
        # is missing, we fail early so the user can fix the
        # connection and retry without burning state.
        token_service = (
            self._explicit_token_service or get_default_token_service()
        )
        auth = self._resolve_auth(
            token_service=token_service,
            provider=provider,
            explicit_account_id=account_id_arg,
            context=context,
        )
        if isinstance(auth, ToolResult):
            return auth
        account_id, base_url, token = auth

        # --- Resolve the repository reference --------------------------
        try:
            # If repository_id contains a slash (e.g., "mtaman/mtaman"),
            # try to fetch it directly instead of searching a list.
            if "/" in repository_id:
                repo = await adapter.get_repository(repository_id, token=token)
            else:
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
                metadata={
                    "provider": provider.value,
                    "account_id": account_id,
                    "base_url": redact_url(base_url),
                },
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
        #
        # Use the process-wide default manager unless one was
        # explicitly injected. Constructing a new manager per call
        # would lose the workspace registry between invocations.
        manager = (
            self._explicit_workspace_manager
            or get_default_workspace_manager()
        )

        # Extract chat_id from context to align workspace with session files
        chat_id = context.run_id or context.metadata.get("chat_id")

        from pathlib import Path
        import os

        target_path = None
        if chat_id:
            # Construct path to the chat session's files directory
            # Assuming standard PELDRUN storage structure: storage/chats/<chat_id>/files
            target_path = Path(os.getcwd()) / "storage" / "chats" / chat_id / "files"

        try:
            workspace = await manager.create_workspace(
                repo=repo,
                provider=provider,
                base_sha=base_sha,
                branch_name=branch_name,
                source_branch=branch_name,
                use_worktree=use_worktree,
                depth=depth,
                target_path=target_path,  # Pass the dynamic path
                metadata={
                    "created_by": "source_control.create_workspace",
                    "operation_id": context.operation_id or "",
                    "run_id": context.run_id or "",
                },
                auth_token=token,
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
                "status": (
                    workspace.status.value
                    if hasattr(workspace.status, "value")
                    else str(workspace.status)
                ),
                "is_worktree": workspace.is_worktree,
            },
            metadata={
                "provider": provider.value,
                "account_id": account_id,
            },
        )

    # --- Credential resolution -----------------------------------------

    @staticmethod
    def _resolve_auth(
        *,
        token_service: TokenService,
        provider: ProviderId,
        explicit_account_id: Optional[str],
        context: ToolContext,
    ) -> Union[Tuple[str, str, str], ToolResult]:
        """
        Resolve `(account_id, base_url, token)` for the call.

        Resolution order for the account:

            1. The explicit `account_id` argument to the tool.
            2. `context.metadata["account_id"]` when the facade
               injects an account hint.
            3. The first stored account for the provider.

        Returns a 3-tuple `(account_id, base_url, token)` on success
        or a failed `ToolResult` when no credential is available.
        The token MUST NEVER be logged, returned in a tool result,
        or included in an error message.
        """
        stored = token_service.list_stored()
        for_provider = [
            entry for entry in stored if entry.provider == provider
        ]

        # FALLBACK: If list_stored() fails (due to key parsing issues), read connections.json directly
        if not for_provider:
            try:
                import json
                from pathlib import Path
                # Resolve the path to backend/data/source_control/connections.json
                # __file__ is backend/integrations/source_control/tools/create_workspace.py
                # parent x4 goes up to the `backend` directory
                backend_root = Path(__file__).resolve().parent.parent.parent.parent
                conn_file = backend_root / "data" / "source_control" / "connections.json"
                if conn_file.exists():
                    data = json.loads(conn_file.read_text(encoding="utf-8"))
                    for conn in data.get("connections", []):
                        if conn.get("provider") == provider.value:
                            # Create a mock StoredToken object or just extract values
                            from dataclasses import dataclass

                            @dataclass
                            class MockStoredToken:
                                provider: str
                                account_id: str
                                base_url: str

                            mock_entry = MockStoredToken(
                                provider=conn.get("provider"),
                                account_id=conn.get("account_id"),
                                base_url=conn.get("base_url"),
                            )
                            for_provider.append(mock_entry)
            except Exception as e:
                pass # Fallback failed, continue to error below

        if not for_provider:
            return ToolResult.fail(
                error_code="AUTH_REQUIRED",
                error_message=(
                    f"No credential is stored for provider "
                    f"'{provider.value}'. Connect the provider "
                    "first."
                ),
                metadata={"provider": provider.value},
            )

        # Account hint from context (fallback when the explicit
        # argument is not provided).
        context_account: Optional[str] = None
        if context.metadata:
            raw = context.metadata.get("account_id")
            if isinstance(raw, str):
                context_account = raw.strip() or None

        chosen_override = explicit_account_id or context_account

        chosen = None
        if chosen_override:
            for entry in for_provider:
                if entry.account_id == chosen_override:
                    chosen = entry
                    break
            if chosen is None:
                return ToolResult.fail(
                    error_code="AUTH_REQUIRED",
                    error_message=(
                        f"No credential is stored for account "
                        f"'{chosen_override}' on provider "
                        f"'{provider.value}'."
                    ),
                    metadata={
                        "provider": provider.value,
                        "account_id": chosen_override,
                    },
                )
        else:
            chosen = for_provider[0]

        token = token_service.get_access_token(
            provider, chosen.account_id, chosen.base_url
        )
        if not token:
            return ToolResult.fail(
                error_code="AUTH_REQUIRED",
                error_message=(
                    "The stored credential is missing or expired. "
                    "Reconnect the provider before creating a "
                    "workspace."
                ),
                metadata={
                    "provider": provider.value,
                    "account_id": chosen.account_id,
                    "base_url": redact_url(chosen.base_url),
                },
            )

        return chosen.account_id, chosen.base_url, token

    # --- Internal: repository resolution -------------------------------

    @staticmethod
    async def _resolve_repository(
        *,
        adapter: Any,
        repository_id: str,
        token: str,
    ) -> Optional[RepositoryRef]:
        """
        Return the `RepositoryRef` for `repository_id`.

        The tool does not know the repository's namespace or name
        ahead of time, so it uses the provider's listing endpoint
        and matches by stable ID. The `token` is required; private
        repositories cannot be resolved without it.

        Adapters in this subsystem accept either `(page, page_size)`
        or `(limit)`; the method tries `page/page_size` first and
        falls back to `limit`.
        """
        try:
            listing = await adapter.list_repositories(
                page=1, page_size=100, token=token
            )
        except TypeError:
            # Some adapters (including the mock used in tests) use
            # `limit=` instead of `page=`/`page_size=`.
            try:
                listing = await adapter.list_repositories(
                    query=None, limit=100, token=token
                )
            except Exception:  # noqa: BLE001
                return None
        except Exception:  # noqa: BLE001
            return None

        for repo in listing:
            if (
                isinstance(repo, RepositoryRef)
                and repo.repository_id == repository_id
            ):
                return repo
        return None

    # --- Helpers --------------------------------------------------------

    @staticmethod
    def _parse_provider(value: Any) -> Union[ProviderId, ToolResult]:
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
    ) -> Union[Optional[str], ToolResult]:
        if value is None:
            return None
        if not isinstance(value, str):
            return ToolResult.fail(
                error_code="VALIDATION_ERROR",
                error_message=f"`{field_name}` must be a string.",
            )
        trimmed = value.strip()
        return trimmed or None
