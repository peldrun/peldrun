from __future__ import annotations

from typing import Any, Mapping, Optional
from pydantic import BaseModel, Field

from ..domain.enums import ProviderId
from ..domain.models import RepositoryRef
from ..errors import (
    AuthRequiredError,
    NotFoundError,
    PermissionDeniedError,
    SourceControlError,
    ValidationError,
)
from ..registry import ProviderRegistry, get_default_registry
from ..security.policy_engine import PolicyEngine, get_default_policy_engine
from ..security.token_service import TokenService, get_default_token_service


class ReadFileArgs(BaseModel):
    """Input argument schema for the read file tool."""

    repository_id: str = Field(
        ...,
        description="Target repository identifier (numeric ID or 'owner/repo' slug).",
    )
    file_path: str = Field(
        ...,
        description="Relative file path within the repository to inspect.",
    )
    provider: Optional[str] = Field(
        default="github",
        description="Source control provider identifier (e.g., 'github'). Defaults to 'github'.",
    )
    ref: Optional[str] = Field(
        default=None,
        description="Optional Git revision, commit SHA, branch, or tag. Defaults to repository default branch.",
    )
    max_bytes: int = Field(
        default=100_000,
        ge=1024,
        le=500_000,
        description="Maximum byte length of text content to return to avoid context overflow. Defaults to 100KB.",
    )


class ReadFileTool:
    """Agent tool that reads and inspects file contents from a remote repository at a specified ref."""

    name: str = "source_control_read_file"
    description: str = (
        "Reads file contents from a source repository at a specified commit, branch, or tag. "
        "Automatically handles character encoding, binary detection, and context window truncation. "
        "Never requires or accepts raw credentials."
    )
    args_schema: type[BaseModel] = ReadFileArgs

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
        file_path: str,
        provider: Optional[str] = "github",
        ref: Optional[str] = None,
        max_bytes: int = 100_000,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Asynchronously reads a remote file using the appropriate provider adapter."""
        # 1. Validate and resolve provider identity
        provider_str = (provider or "github").strip().lower()
        try:
            provider_id = ProviderId(provider_str)
        except ValueError:
            raise ValidationError(
                f"Unsupported source control provider '{provider_str}'. Supported providers: {[p.value for p in ProviderId]}"
            )

        clean_repo_id = repository_id.strip()
        clean_file_path = file_path.strip().lstrip("/")
        clean_ref = ref.strip() if ref and ref.strip() else None

        if not clean_repo_id:
            raise ValidationError("Repository ID cannot be empty.")
        if not clean_file_path:
            raise ValidationError("File path cannot be empty.")

        # 2. Enforce operational policy
        allowed, reason = self.policy_engine.is_operation_allowed(
            operation="read_file",
            provider=provider_id.value,
            repository=clean_repo_id,
        )
        if not allowed:
            raise PermissionDeniedError(
                f"Policy prohibits reading files from repository '{clean_repo_id}': {reason}",
                details={
                    "provider": provider_id.value,
                    "repository_id": clean_repo_id,
                    "reason": reason,
                },
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

        # 5. Retrieve repository metadata
        try:
            repo = await adapter.get_repository(clean_repo_id, token=token)
        except SourceControlError:
            raise
        except Exception as exc:
            raise SourceControlError(
                f"Failed to resolve repository '{clean_repo_id}': {exc}"
            ) from exc

        # 6. Fetch raw file bytes from remote provider
        try:
            raw_bytes = await adapter.read_file(
                repo=repo,
                file_path=clean_file_path,
                ref=clean_ref,
                token=token,
            )
        except SourceControlError:
            raise
        except Exception as exc:
            raise SourceControlError(
                f"Failed to read file '{clean_file_path}' from repository '{repo.namespace}/{repo.name}': {exc}"
            ) from exc

        effective_ref = clean_ref or repo.default_branch
        size_bytes = len(raw_bytes)

        # 7. Detect binary file by checking for null bytes
        if b"\x00" in raw_bytes:
            return {
                "status": "success",
                "provider": provider_id.value,
                "repository_id": repo.repository_id,
                "full_name": f"{repo.namespace}/{repo.name}",
                "file_path": clean_file_path,
                "ref": effective_ref,
                "is_binary": True,
                "size_bytes": size_bytes,
                "truncated": False,
                "content": None,
                "message": f"File '{clean_file_path}' is a binary asset ({size_bytes} bytes) and cannot be displayed as text.",
            }

        # 8. Decode text with fallback
        try:
            text_content = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            text_content = raw_bytes.decode("latin-1", errors="replace")

        # 9. Apply context budget truncation
        truncated = False
        clamped_max_bytes = max(1024, min(max_bytes, 500_000))

        if len(text_content.encode("utf-8")) > clamped_max_bytes:
            # Simple character-level slice with boundary notice
            text_content = (
                text_content[:clamped_max_bytes]
                + "\n\n... [Content truncated due to context window size limit] ..."
            )
            truncated = True

        return {
            "status": "success",
            "provider": provider_id.value,
            "repository_id": repo.repository_id,
            "full_name": f"{repo.namespace}/{repo.name}",
            "file_path": clean_file_path,
            "ref": effective_ref,
            "is_binary": False,
            "size_bytes": size_bytes,
            "truncated": truncated,
            "content": text_content,
        }

    async def aexecute(self, **kwargs: Any) -> dict[str, Any]:
        """Public async execution adapter conforming to standard agent tool calling contracts."""
        validated_args = self.args_schema(**kwargs)
        return await self._arun(**validated_args.model_dump())

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Synchronous wrapper raising an error to enforce non-blocking execution."""
        raise NotImplementedError(
            "ReadFileTool only supports asynchronous execution via aexecute() or _arun()."
        )
