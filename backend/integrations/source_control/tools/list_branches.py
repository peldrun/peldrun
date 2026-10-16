# backend/integrations/source_control/tools/list_branches.py
"""
The `source_control.list_branches` tool.

Lists the branches of a remote repository. Each branch is returned
with its head SHA, its default flag, and its protection flag (when
the provider reports one).

Design rules:
    - The tool resolves the credential from the OS vault, never
      from the caller's arguments.
    - The tool returns a compact, provider-neutral representation
      of each branch.
    - The default branch is always returned first, followed by the
      remaining branches in the order imposed by the adapter.
    - The tool is read-only and requires no approval.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional

from ..domain.enums import ProviderId
from ..domain.models import BranchRef, RepositoryRef
from ..errors import to_source_control_error
from ..registry import ProviderRegistry, get_default_registry
from ..security.token_service import (
    TokenService,
    get_default_token_service,
)
from .base import SourceControlTool, ToolContext, ToolResult

__all__ = ["ListBranchesTool"]


logger = logging.getLogger(__name__)


class ListBranchesTool(SourceControlTool):
    """
    List branches for a repository.

    Inputs:
        provider:           Provider identifier. Defaults to
                            `"github"`.
        repository_id:      Stable repository identifier.
        account_id:         Optional stable account ID.
        include_protected:  When True (default), protected branches
                            are included in the result.
    """

    def __init__(
        self,
        *,
        registry: Optional[ProviderRegistry] = None,
        token_service: Optional[TokenService] = None,
        audit_logger: Any = None,
    ) -> None:
        super().__init__(audit_logger=audit_logger)
        self._explicit_registry = registry
        self._explicit_token_service = token_service

    # --- Metadata -------------------------------------------------------

    @property
    def name(self) -> str:
        return "source_control.list_branches"

    @property
    def description(self) -> str:
        return (
            "List the branches of a repository, with each branch's "
            "head SHA and protection flag when available."
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
                "include_protected": {
                    "type": "boolean",
                    "description": (
                        "When true (default), include protected "
                        "branches in the result."
                    ),
                    "default": True,
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

        account_id_override = kwargs.get("account_id")
        if account_id_override is not None and not isinstance(
            account_id_override, str
        ):
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message="`account_id` must be a string.",
            )
        if isinstance(account_id_override, str):
            account_id_override = account_id_override.strip() or None

        raw_include_protected = kwargs.get("include_protected", True)
        include_protected = bool(raw_include_protected)

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

        # --- Resolve the repository ------------------------------------
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

        # --- Fetch branches --------------------------------------------
        try:
            branches = await adapter.list_branches(
                repo=repo, token=token
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

        # --- Serialize ------------------------------------------------
        serialized: List[Dict[str, Any]] = []
        for branch in branches:
            if not isinstance(branch, BranchRef):
                continue
            if branch.is_protected and not include_protected:
                continue
            serialized.append(self._serialize_branch(branch))

        # Stable order: default first, then alphabetical by name.
        serialized.sort(
            key=lambda b: (
                0 if b.get("is_default") else 1,
                str(b.get("name") or ""),
            )
        )

        return self.success_result(
            {
                "count": len(serialized),
                "default_branch": repo.default_branch,
                "branches": serialized,
            },
            metadata={
                "provider": provider.value,
                "repository_id": repository_id,
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
    def _serialize_branch(branch: BranchRef) -> Dict[str, Any]:
        return {
            "name": branch.name,
            "head_sha": branch.head_sha,
            "is_default": branch.is_default,
            "is_protected": branch.is_protected,
        }
