# backend/integrations/source_control/tools/get_repository.py
"""
The `source_control.get_repository` tool.

This tool fetches fresh metadata for a single repository from the
provider's API. Unlike `list_repositories`, it requires the caller
to know the repository's stable identifier, which the agent
typically obtains from a prior `list_repositories` call.

Design rules:
    - The tool resolves the credential from the OS vault, never
      from the caller's arguments.
    - The tool returns a compact, provider-neutral representation.
    - Missing repositories produce a `NOT_FOUND` failure with a
      clear message.
    - The tool is read-only and requires no approval.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional

from ..domain.enums import ProviderId
from ..domain.models import RepositoryRef
from ..errors import NotFoundError, to_source_control_error
from ..registry import ProviderRegistry, get_default_registry
from ..security.token_service import (
    TokenService,
    get_default_token_service,
)
from .base import SourceControlTool, ToolContext, ToolResult

__all__ = ["GetRepositoryTool"]


logger = logging.getLogger(__name__)


class GetRepositoryTool(SourceControlTool):
    """
    Fetch details for a single repository.

    Inputs:
        provider:      Provider identifier. Defaults to `"github"`.
        repository_id: Stable repository identifier. Required.
        account_id:    Optional stable account ID. When omitted,
                       the first stored account for the provider is
                       used.
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
        return "source_control.get_repository"

    @property
    def description(self) -> str:
        return (
            "Fetch metadata for a single repository by its stable "
            "identifier."
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

        # --- Build a minimal RepositoryRef -----------------------------
        # The adapter's `get_repository` validates that BOTH
        # `namespace` and `name` are non-empty before building the
        # API URL. A raw `repository_id` like "mtaman/mtaman" must
        # therefore be split into its two components; otherwise the
        # adapter raises `ValidationError("Repository namespace and
        # name must not be empty")` before it has a chance to
        # resolve anything.
        #
        # A bare id (no "/") keeps `namespace=""`; the adapter's
        # validation will reject it, but the fallback listing below
        # catches that case and matches by name.
        if "/" in repository_id:
            _ns, _, _nm = repository_id.partition("/")
        else:
            _ns, _nm = "", repository_id

        placeholder = RepositoryRef(
            provider=provider,
            account_id=account_id,
            repository_id=repository_id,
            namespace=_ns,
            name=_nm,
            clone_url="",
            web_url="",
            default_branch="",
            is_private=False,
            base_url=base_url,
        )

        # Some adapters (GitHub) resolve by `namespace/name`, while
        # the caller provided a stable ID. We therefore first list
        # the repositories and pick the matching one. This keeps the
        # tool provider-neutral at the cost of one extra listing on
        # providers that cannot resolve by ID directly.
        try:
            repo = await self._fetch_repository(
                adapter=adapter,
                placeholder=placeholder,
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
                    "repository_id": repository_id,
                    "retryable": normalized.is_retryable,
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
                    "account_id": account_id,
                    "repository_id": repository_id,
                },
            )

        return self.success_result(
            {"repository": self._serialize_repository(repo)},
            metadata={
                "provider": provider.value,
                "account_id": account_id,
            },
        )

    # --- Internal: repository fetch ------------------------------------

    @staticmethod
    async def _fetch_repository(
        *,
        adapter: Any,
        placeholder: RepositoryRef,
        token: str,
    ) -> Optional[RepositoryRef]:
        """
        Return a `RepositoryRef` for the placeholder.

        The adapter is first asked for the repository by ID. When
        the adapter's implementation resolves by ID directly (the
        intended behavior), it returns the refreshed reference. When
        it cannot, the list endpoint is used and the result is
        filtered by `repository_id`.
        """
        try:
            refreshed = await adapter.get_repository(
                repo=placeholder, token=token
            )
            if isinstance(refreshed, RepositoryRef):
                return refreshed
        except (NotFoundError, ValidationError):
            # NotFoundError: the adapter accepted the placeholder's
            #   shape but the repository does not exist.
            # ValidationError: the adapter rejected the placeholder
            #   (e.g., namespace/name still empty). Both mean the
            #   fast path cannot resolve; fall through to listing.
            pass
        except Exception:  # noqa: BLE001
            # Any other adapter-specific rejection is handled the
            # same way: try the fallback.
            pass

        # Fallback: list repositories and match. The caller-supplied
        # `repository_id` may be:
        #   - the provider's stable ID (e.g., GitHub numeric),
        #   - the full name "owner/repo",
        #   - the bare repository name.
        # Match in that order of specificity.
        try:
            listing = await adapter.list_repositories(
                page=1, page_size=100, token=token
            )
        except Exception:  # noqa: BLE001
            return None

        target = placeholder.repository_id.strip()
        for repo in listing:
            if not isinstance(repo, RepositoryRef):
                continue
            if repo.repository_id == target:
                return repo
        for repo in listing:
            if not isinstance(repo, RepositoryRef):
                continue
            if repo.full_name() == target:
                return repo
        for repo in listing:
            if not isinstance(repo, RepositoryRef):
                continue
            if repo.name == target:
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
                metadata={"provider": provider.value},
            )
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
    def _serialize_repository(repo: RepositoryRef) -> Dict[str, Any]:
        return {
            "id": repo.repository_id,
            "namespace": repo.namespace,
            "name": repo.name,
            "full_name": repo.full_name(),
            "default_branch": repo.default_branch,
            "is_private": repo.is_private,
            "web_url": repo.web_url,
            "clone_url": repo.clone_url,
            "description": repo.description,
        }
