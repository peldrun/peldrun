from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
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
from ..git.workspace import GitWorkspaceManager
from ..registry import ProviderRegistry, get_default_registry
from ..security.policy_engine import PolicyEngine, get_default_policy_engine
from ..security.redaction import redact_url
from ..security.token_service import TokenService, get_default_token_service

# Resilient imports for check models
try:
    from ..domain.models import CheckRunRef, CheckSummaryRef
except ImportError:
    from dataclasses import dataclass, field
    from datetime import datetime

    @dataclass(frozen=True)
    class CheckRunRef:  # type: ignore[no-redef]
        name: str
        status: str
        conclusion: Optional[str] = None
        web_url: Optional[str] = None
        started_at: Optional[datetime] = None
        completed_at: Optional[datetime] = None

    @dataclass(frozen=True)
    class CheckSummaryRef:  # type: ignore[no-redef]
        ref: str
        total_count: int
        success_count: int
        failure_count: int
        pending_count: int
        overall_state: str
        runs: tuple[CheckRunRef, ...] = field(default_factory=tuple)


class RunChecksArgs(BaseModel):
    """Input argument schema for the run/inspect checks tool."""

    repository_id: str = Field(
        ...,
        description="Target repository identifier (numeric ID or 'owner/repo' slug).",
    )
    ref: Optional[str] = Field(
        default=None,
        description="Git commit SHA, branch name, or tag to inspect CI checks for. If omitted, workspace HEAD or default branch is used.",
    )
    workspace_id: Optional[str] = Field(
        default=None,
        description="Optional workspace ID to automatically resolve active HEAD commit SHA if ref is not specified.",
    )
    provider: Optional[str] = Field(
        default="github",
        description="Source control provider identifier (e.g., 'github'). Defaults to 'github'.",
    )


class RunChecksTool:
    """Agent tool that inspects and evaluates remote CI/CD checks, actions, and commit statuses."""

    name: str = "source_control_run_checks"
    description: str = (
        "Inspects and summarizes remote CI/CD check runs and commit statuses for a repository ref or workspace HEAD. "
        "Evaluates overall pass/fail/pending state and lists individual check metrics without exposing secrets. "
        "Never requires or accepts raw credentials."
    )
    args_schema: type[BaseModel] = RunChecksArgs

    def __init__(
        self,
        registry: Optional[ProviderRegistry] = None,
        token_service: Optional[TokenService] = None,
        policy_engine: Optional[PolicyEngine] = None,
        workspace_manager: Optional[GitWorkspaceManager] = None,
        default_workspace_dir: Optional[str | Path] = None,
    ) -> None:
        base_dir = Path(default_workspace_dir or os.getcwd()).resolve()
        self.registry = registry or get_default_registry()
        self.token_service = token_service or get_default_token_service()
        self.policy_engine = policy_engine or get_default_policy_engine()
        self.workspace_manager = workspace_manager or GitWorkspaceManager(base_workspace_dir=base_dir)

    async def _arun(
        self,
        repository_id: str,
        ref: Optional[str] = None,
        workspace_id: Optional[str] = None,
        provider: Optional[str] = "github",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Asynchronously queries the provider for check summaries and commit statuses."""
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

        # 2. Enforce operational policy
        allowed, reason = self.policy_engine.is_operation_allowed(
            operation="run_checks",
            provider=provider_id.value,
            repository=clean_repo_id,
        )
        if not allowed:
            raise PermissionDeniedError(
                f"Policy prohibits querying checks for repository '{clean_repo_id}': {reason}",
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

        # 5. Retrieve target repository metadata
        try:
            repo = await adapter.get_repository(clean_repo_id, token=token)
        except SourceControlError:
            raise
        except Exception as exc:
            raise SourceControlError(
                f"Failed to resolve repository '{clean_repo_id}': {exc}"
            ) from exc

        # 6. Determine target reference (explicit ref -> workspace HEAD -> default branch)
        effective_ref = ref.strip() if ref and ref.strip() else None

        if not effective_ref and workspace_id and workspace_id.strip():
            clean_ws_id = workspace_id.strip()
            ws_ref = self.workspace_manager.get_workspace(clean_ws_id)
            if ws_ref:
                effective_ref = ws_ref.head_sha or ws_ref.current_branch

        if not effective_ref:
            effective_ref = repo.default_branch

        # 7. Query aggregate checks summary from provider
        try:
            summary: CheckSummaryRef = await adapter.get_check_summary(
                repo=repo,
                ref=effective_ref,
                token=token,
            )
        except SourceControlError:
            raise
        except Exception as exc:
            raise SourceControlError(
                f"Failed to retrieve check summary for '{effective_ref}' in '{repo.namespace}/{repo.name}': {exc}"
            ) from exc

        # 8. Format compact, LLM-safe check summary
        compact_runs = [
            {
                "name": r.name,
                "status": r.status,
                "conclusion": r.conclusion,
                "web_url": redact_url(r.web_url) if r.web_url else None,
            }
            for r in summary.runs
        ]

        is_passing = summary.overall_state in {"success", "neutral"}

        if summary.total_count == 0:
            status_message = f"No CI/CD checks or commit statuses registered for '{effective_ref}'."
        elif summary.overall_state == "success":
            status_message = f"All {summary.total_count} CI/CD check(s) passed successfully."
        elif summary.overall_state == "failure":
            status_message = f"{summary.failure_count} of {summary.total_count} CI/CD check(s) failed."
        else:
            status_message = f"{summary.pending_count} of {summary.total_count} CI/CD check(s) are still pending/in progress."

        return {
            "status": "success",
            "provider": provider_id.value,
            "repository_id": repo.repository_id,
            "full_name": f"{repo.namespace}/{repo.name}",
            "ref": summary.ref,
            "overall_state": summary.overall_state,
            "is_passing": is_passing,
            "total_count": summary.total_count,
            "success_count": summary.success_count,
            "failure_count": summary.failure_count,
            "pending_count": summary.pending_count,
            "message": status_message,
            "runs": compact_runs,
        }

    async def aexecute(self, **kwargs: Any) -> dict[str, Any]:
        """Public async execution adapter conforming to standard agent tool calling contracts."""
        validated_args = self.args_schema(**kwargs)
        return await self._arun(**validated_args.model_dump())

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Synchronous wrapper raising an error to enforce non-blocking execution."""
        raise NotImplementedError(
            "RunChecksTool only supports asynchronous execution via aexecute() or _arun()."
        )
