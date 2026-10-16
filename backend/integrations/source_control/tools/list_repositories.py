# backend/integrations/source_control/tools/list_repositories.py
"""
The `source_control.list_repositories` tool.

This tool reports the repositories that the authenticated account
can access on a given provider. It performs a network call to the
provider's API and therefore requires a credential.

The credential is resolved from the OS credential vault through the
`TokenService`. The tool NEVER accepts a raw token from the caller:
the model does not need to know the token, and passing it through
the model would risk leaking it into the agent's context.

Design rules:
    - The tool MUST resolve the credential from the vault, never
      from the caller's arguments.
    - The tool MUST accept an optional `account_id` so that callers
      can select which stored account to use when more than one is
      available.
    - The tool MUST gracefully handle the case where no credential
      is stored for the requested provider.
    - The tool MUST NOT return raw provider payloads. It returns a
      compact, provider-neutral representation of the repositories.
    - The tool MUST be deterministic in the order of its output for
      a fixed provider response; it relies on the adapter to impose
      that order.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional

from ..domain.enums import ProviderId
from ..domain.models import RepositoryRef
from ..errors import to_source_control_error
from ..registry import ProviderRegistry, get_default_registry
from ..security.token_service import (
    TokenService,
    get_default_token_service,
)
from .base import SourceControlTool, ToolContext, ToolResult

__all__ = [
    "ListRepositoriesTool",
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
]


logger = logging.getLogger(__name__)


#: Default page size for the listing.
DEFAULT_PAGE_SIZE: int = 30

#: Maximum page size accepted by the tool. GitHub's own maximum is
#: 100; the tool enforces the same bound so that a caller cannot
#: request an unbounded page.
MAX_PAGE_SIZE: int = 100


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class ListRepositoriesTool(SourceControlTool):
    """
    List repositories for a provider account.

    Inputs:
        provider:  Provider identifier. Defaults to `"github"`.
        account_id: Optional stable account ID. When omitted, the
            first stored account for the provider is used.
        query:     Optional case-insensitive substring filter applied
                   to `namespace/name`.
        page:      1-based page number (default 1).
        page_size: Repositories per page (default 30, max 100).

    Output:
        `{"count": <int>, "repositories": [ ... ]}` where each
        repository is a compact dict with `id`, `full_name`,
        `namespace`, `name`, `default_branch`, `is_private`,
        `web_url`, and `clone_url`.
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
        return "source_control.list_repositories"

    @property
    def description(self) -> str:
        return (
            "List the repositories that the authenticated account can "
            "access on the given source-control provider."
        )

    @property
    def input_schema(self) -> Mapping[str, Any]:
        return {
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "enum": ["github", "gitlab", "bitbucket"],
                    "description": (
                        "Provider identifier. Defaults to 'github'."
                    ),
                    "default": "github",
                },
                "account_id": {
                    "type": "string",
                    "description": (
                        "Optional stable account identifier. When "
                        "omitted, the first stored account for the "
                        "provider is used."
                    ),
                },
                "query": {
                    "type": "string",
                    "description": (
                        "Optional case-insensitive substring filter "
                        "applied to 'namespace/name'."
                    ),
                },
                "page": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "1-based page number.",
                    "default": 1,
                },
                "page_size": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_PAGE_SIZE,
                    "description": "Repositories per page.",
                    "default": DEFAULT_PAGE_SIZE,
                },
            },
            "required": [],
            "additionalProperties": False,
        }

    # --- Execution ------------------------------------------------------

    async def execute(
        self, context: ToolContext, **kwargs: Any
    ) -> ToolResult:
        """
        Resolve credentials, call the adapter, and return the list.
        """
        # --- Parse inputs ----------------------------------------------
        provider = self._parse_provider(kwargs.get("provider"))
        if isinstance(provider, ToolResult):
            return provider

        account_id_override = self._parse_optional_str(
            kwargs.get("account_id"), "account_id"
        )
        if isinstance(account_id_override, ToolResult):
            return account_id_override

        query = self._parse_optional_str(
            kwargs.get("query"), "query"
        )
        if isinstance(query, ToolResult):
            return query

        page = self._parse_positive_int(
            kwargs.get("page"), "page", default=1
        )
        if isinstance(page, ToolResult):
            return page

        page_size = self._parse_positive_int(
            kwargs.get("page_size"), "page_size", default=DEFAULT_PAGE_SIZE
        )
        if isinstance(page_size, ToolResult):
            return page_size
        if page_size > MAX_PAGE_SIZE:
            page_size = MAX_PAGE_SIZE

        # --- Resolve registry and adapter ------------------------------
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

        # --- Call the adapter ------------------------------------------
        try:
            repositories = await adapter.list_repositories(
                query=query,
                page=page,
                page_size=page_size,
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
                    "retryable": normalized.is_retryable,
                },
            )

        serialized = [
            self._serialize_repository(repo)
            for repo in repositories
            if isinstance(repo, RepositoryRef)
        ]

        return self.success_result(
            {
                "count": len(serialized),
                "repositories": serialized,
            },
            metadata={
                "provider": provider.value,
                "account_id": account_id,
                "page": page,
                "page_size": page_size,
            },
        )

    # --- Input parsing --------------------------------------------------

    @staticmethod
    def _parse_provider(value: Any) -> ProviderId | ToolResult:
        """
        Parse the `provider` input. Defaults to GitHub.

        Returns a `ProviderId` on success, or a failed `ToolResult`
        on invalid input.
        """
        if value is None or value == "":
            return ProviderId.GITHUB
        try:
            return ProviderId(str(value).strip().lower())
        except ValueError:
            return ToolResult.fail(
                error_code="VALIDATION_ERROR",
                error_message=(
                    f"Unsupported provider: {value!r}. Expected one "
                    "of 'github', 'gitlab', or 'bitbucket'."
                ),
            )

    @staticmethod
    def _parse_optional_str(
        value: Any, field_name: str
    ) -> Optional[str] | ToolResult:
        """
        Return `None` for an absent value, a string for a valid
        value, and a failed `ToolResult` for an invalid value.
        """
        if value is None:
            return None
        if not isinstance(value, str):
            return ToolResult.fail(
                error_code="VALIDATION_ERROR",
                error_message=(
                    f"`{field_name}` must be a string."
                ),
            )
        trimmed = value.strip()
        return trimmed or None

    @staticmethod
    def _parse_positive_int(
        value: Any, field_name: str, *, default: int
    ) -> int | ToolResult:
        """
        Parse a positive integer input. Absent values fall back to
        the default.
        """
        if value is None or value == "":
            return default
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return ToolResult.fail(
                error_code="VALIDATION_ERROR",
                error_message=f"`{field_name}` must be an integer.",
            )
        if parsed < 1:
            return ToolResult.fail(
                error_code="VALIDATION_ERROR",
                error_message=f"`{field_name}` must be at least 1.",
            )
        return parsed

    # --- Account resolution --------------------------------------------

    @staticmethod
    def _resolve_account(
        *,
        token_service: TokenService,
        provider: ProviderId,
        account_id_override: Optional[str],
    ) -> tuple[str, str] | ToolResult:
        """
        Return `(account_id, base_url)` for the credential to use.

        Resolution order:
            1. The explicit `account_id` override, when provided. In
               this case the base URL is taken from the matching
               stored entry; if no matching entry exists, the call
               fails with `AUTH_REQUIRED`.
            2. The first stored account for the given provider.

        The returned `ToolResult` carries a stable error code when
        no credential is available.
        """
        stored = token_service.list_stored()

        # Filter by provider.
        for_provider = [
            entry for entry in stored if entry.provider == provider
        ]

        if not for_provider:
            return ToolResult.fail(
                error_code="AUTH_REQUIRED",
                error_message=(
                    f"No credential is stored for provider "
                    f"'{provider.value}'. Connect the provider first."
                ),
                metadata={"provider": provider.value},
            )

        # If the caller pinned an account, find it.
        if account_id_override:
            for entry in for_provider:
                if entry.account_id == account_id_override:
                    return entry.account_id, entry.base_url
            return ToolResult.fail(
                error_code="AUTH_REQUIRED",
                error_message=(
                    f"No credential is stored for account "
                    f"'{account_id_override}' on provider "
                    f"'{provider.value}'."
                ),
                metadata={
                    "provider": provider.value,
                    "account_id": account_id_override,
                },
            )

        # Otherwise pick the first stored account deterministically.
        # `list_stored` already sorts by (provider, base_url,
        # account_id), so the first entry is stable across calls.
        chosen = for_provider[0]
        return chosen.account_id, chosen.base_url

    # --- Serialization --------------------------------------------------

    @staticmethod
    def _serialize_repository(repo: RepositoryRef) -> Dict[str, Any]:
        """
        Return a compact, provider-neutral representation of a
        repository. No provider-internal identifiers beyond
        `repository_id` are exposed.
        """
        return {
            "id": repo.repository_id,
            "namespace": repo.namespace,
            "name": repo.name,
            "full_name": repo.full_name(),
            "default_branch": repo.default_branch,
            "is_private": repo.is_private,
            "web_url": repo.web_url,
            "clone_url": repo.clone_url,
        }
