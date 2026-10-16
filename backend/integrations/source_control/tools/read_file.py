# backend/integrations/source_control/tools/read_file.py
"""
The `source_control.read_file` tool.

Reads a text file from a remote repository at a specific ref
(branch, tag, or commit SHA) using the provider's contents API. The
result is returned to the agent as a string.

Design rules:
    - The tool resolves the credential from the OS vault, never
      from the caller's arguments.
    - The tool MUST NOT return binary content. When the provider
      cannot tell whether a file is text, the tool attempts a UTF-8
      decode and rejects content that contains NUL bytes, which is
      the standard signal of a binary file.
    - The tool bounds the size of the returned content at
      `MAX_FILE_BYTES`. Larger files are refused with a clear error
      so that the model's context is not blown by an accidental
      request for a multi-megabyte file.
    - The tool is read-only and requires no approval.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional

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
    "ReadFileTool",
    "MAX_FILE_BYTES",
]


logger = logging.getLogger(__name__)


#: Maximum number of bytes the tool will return to the agent. The
#: cap is deliberately lower than the provider's own limits so that
#: a single tool call cannot flood the model's context.
MAX_FILE_BYTES: int = 200_000


class ReadFileTool(SourceControlTool):
    """
    Read a single file from a remote repository.

    Inputs:
        provider:      Provider identifier. Defaults to `"github"`.
        repository_id: Stable repository identifier.
        file_path:     Path of the file within the repository.
        ref:           Optional branch, tag, or commit SHA.
        account_id:    Optional stable account ID.
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
        return "source_control.read_file"

    @property
    def description(self) -> str:
        return (
            "Read a text file from a remote repository at a given "
            "ref (branch, tag, or commit SHA)."
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
                "file_path": {
                    "type": "string",
                    "description": (
                        "Path of the file within the repository."
                    ),
                },
                "ref": {
                    "type": "string",
                    "description": (
                        "Optional branch, tag, or commit SHA. When "
                        "omitted, the repository's default branch "
                        "is used."
                    ),
                },
                "account_id": {
                    "type": "string",
                    "description": (
                        "Optional stable account identifier."
                    ),
                },
            },
            "required": ["repository_id", "file_path"],
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

        file_path = kwargs.get("file_path")
        if not isinstance(file_path, str) or not file_path.strip():
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message=(
                    "`file_path` must be a non-empty string."
                ),
            )
        file_path = file_path.strip().lstrip("/")

        ref = kwargs.get("ref")
        if ref is not None and not isinstance(ref, str):
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message="`ref` must be a string.",
            )
        if isinstance(ref, str):
            ref = ref.strip() or None

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

        # --- Resolve the repository ------------------------------------
        try:
            repo = await self._resolve_repository(
                adapter=adapter,
                provider=provider,
                account_id=account_id,
                base_url=base_url,
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

        # --- Read the file ---------------------------------------------
        try:
            content = await adapter.read_file(
                repo=repo,
                file_path=file_path,
                ref=ref,
                token=token,
            )
        except Exception as exc:  # noqa: BLE001
            normalized = to_source_control_error(exc)
            return self.failure_result(
                error_code=normalized.code.value,
                error_message=normalized.message,
                metadata={
                    "provider": provider.value,
                    "repository_id": repository_id,
                    "file_path": file_path,
                    "ref": ref,
                    "retryable": normalized.is_retryable,
                },
            )

        if not isinstance(content, str):
            return self.failure_result(
                error_code="PROVIDER_ERROR",
                error_message=(
                    "Provider returned a non-string file payload."
                ),
            )

        # --- Validate content ------------------------------------------
        encoded = content.encode("utf-8", errors="replace")
        if len(encoded) > MAX_FILE_BYTES:
            return self.failure_result(
                error_code="UNSUPPORTED_CAPABILITY",
                error_message=(
                    f"File exceeds the {MAX_FILE_BYTES}-byte limit "
                    "for this tool."
                ),
                metadata={
                    "size_bytes": len(encoded),
                    "max_bytes": MAX_FILE_BYTES,
                },
            )

        # Detect obvious binary content: NUL byte in the first few
        # kilobytes. This catches most images and archives without
        # pulling in a full binary-detection library.
        if _looks_binary(content):
            return self.failure_result(
                error_code="UNSUPPORTED_CAPABILITY",
                error_message=(
                    "File appears to be binary and cannot be read as "
                    "text."
                ),
                metadata={"file_path": file_path},
            )

        return self.success_result(
            {
                "content": content,
                "size_bytes": len(encoded),
                "ref": ref or repo.default_branch or None,
            },
            metadata={
                "provider": provider.value,
                "repository_id": repository_id,
                "file_path": file_path,
            },
        )

    # --- Internal: repository resolution -------------------------------

    @staticmethod
    async def _resolve_repository(
        *,
        adapter: Any,
        provider: ProviderId,
        account_id: str,
        base_url: str,
        repository_id: str,
        token: str,
    ) -> Optional[RepositoryRef]:
        """
        Return the `RepositoryRef` for `repository_id`.

        The adapter's `read_file` expects a `RepositoryRef` with a
        valid `namespace`/`name`, but the caller supplies only the
        stable ID. We therefore list repositories and match by ID.
        """
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


def _looks_binary(content: str) -> bool:
    """
    Return True when `content` looks like binary data.

    The check is intentionally simple: a NUL byte within the first
    8 KiB of the decoded content is a strong indicator of a binary
    file. The decoding step in the adapter already replaced invalid
    UTF-8 sequences with the replacement character, so a legitimate
    text file will never contain a NUL byte.
    """
    if not content:
        return False
    sample = content[:8192]
    return "\x00" in sample
