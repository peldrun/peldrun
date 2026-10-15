from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Optional
from pydantic import BaseModel, Field

from ..domain.enums import ProviderId
from ..domain.models import DiffSummary, RepositoryRef
from ..domain.policies import is_protected_branch
from ..errors import (
    AuthRequiredError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    RemoteStateChangedError,
    SourceControlError,
    ValidationError,
)
from ..git.diff import GitDiffEngine
from ..git.path_policy import validate_branch_name
from ..git.push import GitPushManager, PushResult
from ..git.workspace import GitWorkspaceManager
from ..registry import ProviderRegistry, get_default_registry
from ..security.approval import (
    ApprovalGate,
    ApprovalTicket,
    get_default_approval_gate,
)
from ..security.policy_engine import PolicyEngine, get_default_policy_engine
from ..security.token_service import TokenService, get_default_token_service

# Resilient import for operation service
try:
    from ..operations.service import OperationService, get_default_operation_service
except ImportError:
    OperationService = None  # type: ignore[assignment, misc]
    def get_default_operation_service() -> Any:
        return None


class PushBranchArgs(BaseModel):
    """Input argument schema for the push branch tool."""

    approval_id: str = Field(
        ...,
        description="The cryptographic approval ticket ID granted explicitly by the user via the UI.",
    )
    workspace_id: Optional[str] = Field(
        default=None,
        description="Unique identifier of the isolated Git workspace containing the changes.",
    )
    workspace_path: Optional[str] = Field(
        default=None,
        description="Local directory path of the git repository/workspace (alternative to workspace_id).",
    )
    remote_name: str = Field(
        default="origin",
        description="Remote repository name to push to. Defaults to 'origin'.",
    )
    set_upstream: bool = Field(
        default=True,
        description="Whether to set the upstream tracking reference (-u). Defaults to True.",
    )


class PushBranchTool:
    """Agent tool that pushes approved local workspace commits to a remote branch."""

    name: str = "source_control_push_branch"
    description: str = (
        "Pushes an approved local branch to the remote repository. "
        "Strictly requires a pre-validated, single-use approval ticket. Verifies that the local diff "
        "and remote base commit have not changed since approval was granted. Never accepts raw credentials."
    )
    args_schema: type[BaseModel] = PushBranchArgs

    def __init__(
        self,
        approval_gate: Optional[ApprovalGate] = None,
        policy_engine: Optional[PolicyEngine] = None,
        token_service: Optional[TokenService] = None,
        registry: Optional[ProviderRegistry] = None,
        workspace_manager: Optional[GitWorkspaceManager] = None,
        diff_engine: Optional[GitDiffEngine] = None,
        push_manager: Optional[GitPushManager] = None,
        operation_service: Optional[Any] = None,
        default_workspace_dir: Optional[str | Path] = None,
    ) -> None:
        base_dir = Path(default_workspace_dir or os.getcwd()).resolve()
        self.approval_gate = approval_gate or get_default_approval_gate()
        self.policy_engine = policy_engine or get_default_policy_engine()
        self.token_service = token_service or get_default_token_service()
        self.registry = registry or get_default_registry()
        self.workspace_manager = workspace_manager or GitWorkspaceManager(base_workspace_dir=base_dir)
        self.diff_engine = diff_engine or GitDiffEngine()
        self.push_manager = push_manager or GitPushManager()
        self.operation_service = operation_service or get_default_operation_service()

    async def _arun(
        self,
        approval_id: str,
        workspace_id: Optional[str] = None,
        workspace_path: Optional[str] = None,
        remote_name: str = "origin",
        set_upstream: bool = True,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Asynchronously validates approval integrity and executes remote branch push."""
        clean_approval_id = approval_id.strip()
        if not clean_approval_id:
            raise ValidationError("Approval ID cannot be empty.")

        # 1. Resolve workspace directory
        target_dir: Optional[Path] = None
        current_branch: Optional[str] = None
        provider_id_val: str = "github"
        repo_ref_obj: Optional[RepositoryRef] = None

        if workspace_id and workspace_id.strip():
            clean_ws_id = workspace_id.strip()
            ws_ref = self.workspace_manager.get_workspace(clean_ws_id)
            if ws_ref is None:
                raise NotFoundError(f"Workspace with ID '{clean_ws_id}' was not found.")
            target_dir = Path(ws_ref.workspace_path)
            current_branch = ws_ref.current_branch
        elif workspace_path and workspace_path.strip():
            raw_path = Path(workspace_path.strip())
            if not raw_path.exists():
                raise NotFoundError(f"Specified workspace directory '{workspace_path}' does not exist.")
            target_dir = raw_path.resolve()
        else:
            raise ValidationError("Either 'workspace_id' or 'workspace_path' must be provided.")

        if not target_dir.is_dir():
            raise ValidationError(f"Target workspace path '{target_dir}' is not a directory.")

        # 2. Consume and validate the approval ticket
        try:
            ticket: ApprovalTicket = self.approval_gate.consume_approval(clean_approval_id)
        except PermissionDeniedError:
            raise
        except Exception as exc:
            raise PermissionDeniedError(
                f"Failed to validate approval ticket '{clean_approval_id}': {exc}",
                details={"approval_id": clean_approval_id},
            ) from exc

        if ticket.operation != "push_branch":
            raise ValidationError(
                f"Approval ticket '{clean_approval_id}' was granted for '{ticket.operation}', not 'push_branch'."
            )

        target_branch = validate_branch_name(ticket.target_branch)

        # 3. Guard against protected branch pushes
        if is_protected_branch(target_branch):
            raise PermissionDeniedError(
                f"Pushing directly to protected branch '{target_branch}' is strictly prohibited. "
                "Push to a feature branch and submit a pull request instead.",
                details={"branch": target_branch},
            )

        # 4. Enforce operational policy
        allowed, reason = self.policy_engine.is_operation_allowed(
            operation="push_branch",
            target_branch=target_branch,
            workspace=str(target_dir),
        )
        if not allowed:
            raise PermissionDeniedError(
                f"Policy prohibits push to branch '{target_branch}': {reason}",
                details={"target_branch": target_branch, "reason": reason},
            )

        # 5. Recompute current diff to ensure changes did not mutate after approval
        try:
            current_diff: DiffSummary = await self.diff_engine.compute_diff(
                cwd=target_dir,
                base_ref=ticket.base_sha,
            )
        except Exception as exc:
            raise SourceControlError(
                f"Failed to verify diff consistency prior to push: {exc}"
            ) from exc

        if current_diff.diff_sha256 != ticket.diff_sha256:
            raise RemoteStateChangedError(
                f"Workspace changes mutated after approval was granted. Expected diff SHA-256 "
                f"'{ticket.diff_sha256}', but computed '{current_diff.diff_sha256}'. Please request a new approval.",
                details={
                    "expected_diff_sha256": ticket.diff_sha256,
                    "actual_diff_sha256": current_diff.diff_sha256,
                },
            )

        # 6. Retrieve ephemeral provider access token
        token = await self.token_service.get_access_token(ProviderId.GITHUB)
        if not token:
            raise AuthRequiredError(
                "No active provider credentials found in vault to execute authenticated push. "
                "Please authenticate the provider first in Settings."
            )

        # 7. Execute authenticated remote push via GitPushManager
        effective_local_branch = current_branch or target_branch
        try:
            push_res: PushResult = await self.push_manager.push_branch(
                cwd=target_dir,
                branch_name=effective_local_branch,
                remote_name=remote_name.strip() or "origin",
                target_branch=target_branch,
                token=token,
                force_with_lease=True,
                set_upstream=set_upstream,
            )
        except ConflictError:
            raise
        except SourceControlError:
            raise
        except Exception as exc:
            raise SourceControlError(
                f"Git push failed for branch '{target_branch}': {exc}"
            ) from exc

        # 8. Transition durable operation if wired
        if self.operation_service and ticket.operation_id:
            try:
                from ..operations.models import OperationState

                await self.operation_service.transition_state(
                    operation_id=ticket.operation_id,
                    target_state=OperationState.PUSHED,
                    metadata={
                        "approval_id": clean_approval_id,
                        "branch_name": target_branch,
                        "pushed_commit_sha": push_res.pushed_sha,
                        "remote_name": remote_name,
                        "upstream": push_res.upstream_ref,
                    },
                )
            except Exception:
                pass

        return {
            "status": "success",
            "approval_id": clean_approval_id,
            "operation_id": ticket.operation_id,
            "branch_name": target_branch,
            "pushed_sha": push_res.pushed_sha,
            "remote_name": remote_name,
            "upstream_ref": push_res.upstream_ref,
            "message": (
                f"Successfully pushed branch '{target_branch}' to remote '{remote_name}'. "
                f"Remote ref is now at {push_res.pushed_sha[:7] if push_res.pushed_sha else 'HEAD'}."
            ),
        }

    async def aexecute(self, **kwargs: Any) -> dict[str, Any]:
        """Public async execution adapter conforming to standard agent tool calling contracts."""
        validated_args = self.args_schema(**kwargs)
        return await self._arun(**validated_args.model_dump())

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Synchronous wrapper raising an error to enforce non-blocking execution."""
        raise NotImplementedError(
            "PushBranchTool only supports asynchronous execution via aexecute() or _arun()."
        )
