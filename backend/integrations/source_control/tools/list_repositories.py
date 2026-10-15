from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence
from pydantic import BaseModel, Field

from ..domain.enums import ProviderId
from ..domain.models import RepositoryRef
from ..errors import (
    AuthRequiredError,
    PermissionDeniedError,
    SourceControlError,
    ValidationError,
)
from ..registry import ProviderRegistry, get_default_registry
from ..security.policy_engine import PolicyEngine, get_default_policy_engine
from ..security.redaction import redact_url
from ..security.token_service import TokenService, get_default_token_service


class ListRepositoriesArgs(BaseModel):
    """Input argument schema for the list repositories tool."""

    provider: Optional[str] = Field(
        default="github",
        description="Target source control provider identifier (e.g., 'github'). Defaults to 'github'.",
    )
    query: Optional[str] = Field(
        default=None,
        description="Optional search filter to search repositories by name, topic, or keyword.",
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Maximum number of repositories to return (between 1 and 100). Defaults to 20.",
    )


class ListRepositoriesTool:
    """Agent tool that discovers and lists source repositories accessible to the authenticated user."""

    name: str = "source_control_list_repositories"
    description: str = (
        "Discovers and lists source control repositories accessible to the current user. "
        "Allows optional keyword filtering and count limiting. Never requires or accepts raw credentials."
    )
    args_schema: type[BaseModel] = ListRepositoriesArgs

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
        provider: Optional[str] = "github",
        query: Optional[str] = None,
        limit: int = 20,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Asynchronously queries the provider for accessible repositories."""
        # 1. Validate and resolve provider identity
        provider_str = (provider or "github").strip().lower()
        try:
            provider_id = ProviderId(provider_str)
        except ValueError:
            raise ValidationError(
                f"Unsupported source control provider '{provider_str}'. Supported providers: {[p.value for p in ProviderId]}"
            )

        # 2. Enforce operational policy
        allowed, reason = self.policy_engine.is_operation_allowed(
            operation="list_repositories",
            provider=provider_id.value,
        )
        if not allowed:
            raise PermissionDeniedError(
                f"Policy prohibits listing repositories for provider '{provider_id.value}': {reason}",
                details={"provider": provider_id.value, "reason": reason},
            )

        # 3. Retrieve ephemeral credentials from secure vault
        token = await self.token_service.get_access_token(provider_id)
        if not token:
            raise AuthRequiredError(
                f"No active credentials found for '{provider_id.value}'. Please connect your account first in Settings.",
                details={"provider": provider_id.value},
            )

        # 4. Resolve provider adapter from registry
        adapter = self.registry.get(provider_id.value)
        clamped_limit = max(1, min(limit, 100))

        # 5. Execute remote repository discovery
        try:
            repos: Sequence[RepositoryRef] = await adapter.list_repositories(
                query=query.strip() if query else None,
                limit=clamped_limit,
                token=token,
            )
        except SourceControlError:
            raise
        except Exception as exc:
            raise SourceControlError(
                f"Failed to retrieve repositories from '{provider_id.value}': {exc}"
            ) from exc

        # 6. Format compact, LLM-safe repository summary without leaking internal states
        compact_repos = [
            {
                "repository_id": r.repository_id,
                "full_name": f"{r.namespace}/{r.name}",
                "name": r.name,
                "namespace": r.namespace,
                "default_branch": r.default_branch,
                "is_private": r.is_private,
                "web_url": redact_url(r.web_url),
                "description": r.metadata.get("description") if r.metadata else None,
            }
            for r in repos
        ]

        return {
            "status": "success",
            "provider": provider_id.value,
            "total_count": len(compact_repos),
            "repositories": compact_repos,
        }

    async def aexecute(self, **kwargs: Any) -> dict[str, Any]:
        """Public async execution adapter conforming to standard agent tool calling contracts."""
        validated_args = self.args_schema(**kwargs)
        return await self._arun(**validated_args.model_dump())

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Synchronous wrapper raising an error to enforce non-blocking execution."""
        raise NotImplementedError(
            "ListRepositoriesTool only supports asynchronous execution via aexecute() or _arun()."
        )
