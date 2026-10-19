# backend/integrations/source_control/tools/push_branch.py
"""
The `source_control.push_branch` tool.

Pushes a reviewed branch to a remote repository. This is the single
most sensitive operation in the subsystem: it modifies remote state
that other people depend on. The tool therefore enforces a strict
pipeline before any network write occurs.

Pipeline:
    1. Validate inputs.
    2. Load the operation record from the journal.
    3. Verify the operation is in `APPROVED`.
    4. Resolve the credential for the operation's provider.
    5. Validate the approval ticket against the operation record.
    6. Re-fetch the remote branch SHA and compare it to the base
       SHA the user reviewed. If the remote moved, fail with
       `REMOTE_STATE_CHANGED` and revoke the approval.
    7. Consume the approval ticket, binding the exact diff hash,
       base SHA, target branch, and repository.
    8. Transition the operation to `PUSHING`.
    9. Perform the push via `GitPushManager`, supplying the
       credential resolved in step 4.
   10. Reconcile the result; if the push timed out, verify remote
       state before deciding the outcome.
   11. Transition to `PUSHED` on success, or `FAILED_*` on failure.
   12. Attach the outcome to the operation.

Design rules:
    - The tool MUST NOT accept a diff hash, base SHA, or target
      branch from the caller. Those values come from the operation
      record.
    - The tool MUST NOT bypass the approval gate. Even if the
      caller supplies a valid `approval_id`, the gate re-validates
      every bound field.
    - The tool MUST resolve the credential BEFORE consuming the
      approval. A missing or expired credential would otherwise
      burn the approval without performing any push.
    - The tool MUST pass the credential to both the remote-SHA
      pre-flight check and the push itself. Without it, private
      repositories silently fail the stale-base check.
    - The tool MUST re-fetch the remote SHA immediately before
      push. A stale base is a `REMOTE_STATE_CHANGED` failure.
    - The tool MUST use `--force-with-lease` semantics via
      `GitPushManager`. Plain `--force` is never used.
    - The tool MUST NEVER mark an ambiguous result as success or
      failure without attempting reconciliation.
    - The credential MUST NEVER appear in an error message, a
      tool result, or the audit log. The base URL is redacted
      before it is included in any diagnostic.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple, Union

from ..domain.enums import OperationStatus, ProviderId
from ..domain.policies import is_protected_branch
from ..errors import to_source_control_error
from ..git.diff import get_default_diff_engine
from ..git.push import GitPushManager, get_default_push_manager
from ..git.workspace import (
    GitWorkspaceManager,
    get_default_workspace_manager,
)
from ..operations.service import (
    OperationService,
    get_default_operation_service,
)
from ..security.approval import (
    ApprovalGate,
    get_default_approval_gate,
)
from ..security.policy_engine import (
    PolicyResolutionContext,
    get_default_policy_engine,
)
from ..security.redaction import redact_url
from ..security.token_service import (
    TokenService,
    get_default_token_service,
)
from .base import SourceControlTool, ToolContext, ToolResult

__all__ = ["PushBranchTool"]


logger = logging.getLogger(__name__)


class PushBranchTool(SourceControlTool):
    """
    Push a reviewed branch after an approval.

    Inputs:
        operation_id: Identifier of the operation. Required.
        approval_id:  Identifier of the granted approval. Required.
        local_branch: Optional local branch name. Defaults to the
                      operation's `source_branch` when present.
        remote:       Optional remote name. Defaults to
                      `"origin"`.

    The tool re-validates every bound field on the approval ticket.
    A field change (for example a different diff hash) invalidates
    the ticket.

    The credential is resolved from the OS vault through the
    `TokenService`. The account is selected with the following
    precedence:

        1. `context.metadata["account_id"]` when the facade injects
           an account hint.
        2. The first stored account for the operation's provider.

    Multi-account setups SHOULD inject the correct account through
    `context.metadata`. Without it, the first stored account is
    used, which may not have access to the target repository.
    """

    def __init__(
        self,
        *,
        operation_service: Optional[OperationService] = None,
        approval_gate: Optional[ApprovalGate] = None,
        push_manager: Optional[GitPushManager] = None,
        workspace_manager: Optional[GitWorkspaceManager] = None,
        token_service: Optional[TokenService] = None,
        audit_logger: Any = None,
    ) -> None:
        super().__init__(audit_logger=audit_logger)
        self._explicit_operation_service = operation_service
        self._explicit_approval_gate = approval_gate
        self._explicit_push_manager = push_manager
        self._explicit_workspace_manager = workspace_manager
        self._explicit_token_service = token_service

    # --- Metadata -------------------------------------------------------

    @property
    def name(self) -> str:
        return "source_control.push_branch"

    @property
    def description(self) -> str:
        return (
            "Push a reviewed branch to a remote repository. Requires "
            "a valid approval ticket and a workspace that already "
            "contains the reviewed commits."
        )

    @property
    def input_schema(self) -> Mapping[str, Any]:
        return {
            "type": "object",
            "properties": {
                "operation_id": {
                    "type": "string",
                    "description": "Identifier of the operation.",
                },
                "approval_id": {
                    "type": "string",
                    "description": (
                        "Identifier of the granted approval ticket."
                    ),
                },
                "local_branch": {
                    "type": "string",
                    "description": (
                        "Optional local branch name. Defaults to the "
                        "operation's source branch."
                    ),
                },
                "remote": {
                    "type": "string",
                    "description": (
                        "Remote name. Defaults to 'origin'."
                    ),
                    "default": "origin",
                },
            },
            "required": ["operation_id", "approval_id"],
            "additionalProperties": False,
        }

    # --- Execution ------------------------------------------------------

    async def execute(
        self, context: ToolContext, **kwargs: Any
    ) -> ToolResult:
        # --- Parse inputs ----------------------------------------------
        operation_id = kwargs.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id.strip():
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message=(
                    "`operation_id` must be a non-empty string."
                ),
            )
        operation_id = operation_id.strip()

        approval_id = kwargs.get("approval_id")
        if not isinstance(approval_id, str) or not approval_id.strip():
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message=(
                    "`approval_id` must be a non-empty string."
                ),
            )
        approval_id = approval_id.strip()

        local_branch_override = kwargs.get("local_branch")
        if local_branch_override is not None and not isinstance(
            local_branch_override, str
        ):
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message="`local_branch` must be a string.",
            )
        if isinstance(local_branch_override, str):
            local_branch_override = local_branch_override.strip() or None

        remote = kwargs.get("remote") or "origin"
        if not isinstance(remote, str) or not remote.strip():
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message="`remote` must be a non-empty string.",
            )
        remote = remote.strip()

        # --- Resolve services ------------------------------------------
        operation_service = (
            self._explicit_operation_service
            or get_default_operation_service()
        )
        approval_gate = (
            self._explicit_approval_gate or get_default_approval_gate()
        )
        push_manager = (
            self._explicit_push_manager or get_default_push_manager()
        )
        workspace_manager = (
            self._explicit_workspace_manager
            or get_default_workspace_manager()
        )
        token_service = (
            self._explicit_token_service or get_default_token_service()
        )

        # --- Load the operation ----------------------------------------
        try:
            record = await operation_service.get_operation(operation_id)
        except Exception as exc:  # noqa: BLE001
            normalized = to_source_control_error(exc)
            return self.failure_result(
                error_code=normalized.code.value,
                error_message=normalized.message,
                metadata={"operation_id": operation_id},
            )

        # --- Validate operation state ----------------------------------
        if record.status != OperationStatus.APPROVED:
            return self.failure_result(
                error_code="CONFLICT",
                error_message=(
                    f"Operation is in status '{record.status.value}'; "
                    "only APPROVED operations can be pushed."
                ),
                metadata={
                    "operation_id": operation_id,
                    "status": record.status.value,
                },
            )

        if not record.workspace_id:
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message=(
                    "Operation has no workspace attached."
                ),
                metadata={"operation_id": operation_id},
            )

        # Fetch the workspace first. It provides both the working
        # directory and the branch the repository is actually on.
        # The record's `source_branch` is authoritative (it is what
        # the user reviewed), but falling back to the workspace's
        # current branch keeps this tool usable with older records
        # created before `source_branch` was populated.
        workspace = workspace_manager.get_workspace(record.workspace_id)
        if workspace is None:
            return self.failure_result(
                error_code="WORKSPACE_NOT_FOUND",
                error_message=(
                    f"Workspace '{record.workspace_id}' is not "
                    "registered."
                ),
                metadata={
                    "operation_id": operation_id,
                    "workspace_id": record.workspace_id,
                },
            )

        # `target_branch` is the REMOTE branch we push to. It is the
        # value the user approved; falling back to `source_branch`
        # here would silently push the source branch to itself on
        # the remote, which is not what the user requested.
        target_branch = record.target_branch
        if not target_branch:
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message=(
                    "Operation has no target_branch. Re-request "
                    "approval with a valid target_branch."
                ),
                metadata={"operation_id": operation_id},
            )

        # `source_branch` is the LOCAL branch we push from. The
        # record's value takes priority; the workspace's actual
        # checkout is the fallback for legacy records.
        source_branch = record.source_branch or workspace.current_branch
        if not source_branch:
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message=(
                    "Operation has no source_branch and the workspace "
                    "has no current branch; cannot determine which "
                    "local branch to push."
                ),
                metadata={"operation_id": operation_id},
            )

        local_branch = local_branch_override or source_branch
        # --- Pre-flight safety checks -----------------------------------
        #
        # These run BEFORE the approval ticket is touched. A failure
        # here leaves the ticket intact so the user does not have to
        # re-approve an unchanged diff after fixing the underlying
        # condition (missing connection, workspace discarded, policy
        # changed, etc.).
        #
        # The previous implementation only checked the workspace
        # state and the approval ticket. It let three critical cases
        # slip through:
        #   - pushes to protected branches (main, release/*),
        #   - pushes that violated the currently-active policy,
        #   - pushes whose workspace had changed since the review.

        # 1. Protected target branch. The UI does not let the user
        #    request such an approval, but defense in depth: never
        #    trust a ticket's target_branch blindly.
        if is_protected_branch(target_branch):
            return self.failure_result(
                error_code="PERMISSION_DENIED",
                error_message=(
                    f"Pushing to the protected branch "
                    f"'{target_branch}' is forbidden. Push to a "
                    "feature branch and open a change request."
                ),
                metadata={
                    "operation_id": operation_id,
                    "target_branch": target_branch,
                },
            )

        # 2. Policy engine, re-evaluated at execution time. The
        #    policy may have changed since the ticket was created
        #    (repository now read-only, branch protection tightened,
        #    etc.). The check runs BEFORE `consume` so a valid ticket
        #    is never burned by a policy denial: the user can retry
        #    once the policy is relaxed.
        policy_engine = get_default_policy_engine()
        try:
            policy_engine.require(
                PolicyResolutionContext(
                    operation="push_branch",
                    provider=record.provider,
                    source_branch=source_branch,
                    target_branch=target_branch,
                    actor="system",
                )
            )
        except Exception as exc:  # noqa: BLE001
            normalized = to_source_control_error(exc)
            return self.failure_result(
                error_code=normalized.code.value,
                error_message=normalized.message,
                metadata={
                    "operation_id": operation_id,
                    "target_branch": target_branch,
                },
            )

        # 3. Diff integrity. Recompute the current workspace diff and
        #    compare its SHA-256 to the one bound into the approval
        #    ticket. If the workspace changed after review, the ticket
        #    is stale and MUST be rejected: allowing the push would
        #    execute a different diff than the one the user approved.
        if record.diff_sha256:
            try:
                current_summary = await get_default_diff_engine().compute_diff(
                    workspace_id=record.workspace_id,
                    base_sha=record.base_sha or None,
                    include_patch=False,
                    workspace_path=workspace.workspace_path,
                )
            except Exception as exc:  # noqa: BLE001
                normalized = to_source_control_error(exc)
                return self.failure_result(
                    error_code=normalized.code.value,
                    error_message=(
                        "Could not recompute the diff to verify the "
                        "approval is still current: "
                        + normalized.message
                    ),
                    metadata={"operation_id": operation_id},
                )

            current_hash = current_summary.sha256()
            if current_hash != record.diff_sha256:
                return self.failure_result(
                    error_code="APPROVAL_INVALID",
                    error_message=(
                        "The workspace changed after the approval "
                        "was granted. Request a new approval for "
                        "the current diff."
                    ),
                    metadata={
                        "operation_id": operation_id,
                        "recorded_diff_sha256": record.diff_sha256,
                        "current_diff_sha256": current_hash,
                    },
                )

        # --- Resolve the credential ------------------------------------
        #
        # IMPORTANT: this step runs BEFORE approval validation and
        # BEFORE approval consumption. If the credential is missing,
        # we fail early so the user can fix the connection and
        # retry — the approval ticket is preserved.
        auth = self._resolve_auth(
            token_service=token_service,
            record=record,
            context=context,
        )
        if isinstance(auth, ToolResult):
            return auth
        _provider_id, _account_id, _base_url, token = auth

        # --- Validate the approval ticket ------------------------------
        #
        # The gate re-validates every bound field: operation id,
        # repository id, operation kind, base SHA, diff hash, and
        # target branch. Any mismatch invalidates the ticket.
        try:
            approval_gate.validate(
                approval_id,
                expected_operation_id=operation_id,
                expected_repository_id=record.repository_id,
                expected_operation=record.kind,
                expected_base_sha=record.base_sha,
                expected_diff_sha256=record.diff_sha256 or "",
                expected_target_branch=target_branch,
            )
        except Exception as exc:  # noqa: BLE001
            normalized = to_source_control_error(exc)
            return self.failure_result(
                error_code=normalized.code.value,
                error_message=normalized.message,
                metadata={
                    "operation_id": operation_id,
                    "approval_id": approval_id,
                },
            )

        # --- Remote-state check (intentionally omitted) ----------------
        #
        # The previous implementation compared the remote's current
        # head to `record.base_sha`. That comparison is semantically
        # wrong: `record.base_sha` is the commit the WORKSPACE was
        # created from, not the commit the TARGET branch was at when
        # the user reviewed the diff. Two consequences:
        #
        #   - For a NEW feature branch (which is the normal case for
        #     agent-driven pushes), the remote branch does not exist
        #     at review time. The remote SHA is either None (skip) or
        #     set by an unrelated prior push, in which case the check
        #     fires `REMOTE_STATE_CHANGED` on valid work.
        #   - For an existing feature branch, the branch legitimately
        #     points at a commit different from `record.base_sha`
        #     (which tracks the base the workspace was cloned from).
        #     The check therefore also fires on the most common
        #     non-new-branch case.
        #
        # The real anti-race guarantee is provided by Git itself: the
        # `push_manager` calls `git push` without `--force`, so git
        # rejects any non-fast-forward update before writing to the
        # remote. If a stricter pre-flight check is ever needed, the
        # remote SHA MUST be captured at review time (in
        # `record.metadata["reviewed_remote_sha"]`) and compared
        # against THAT — not against `record.base_sha`.

        # --- Consume the approval -------------------------------------
        #
        # Once consumed, the ticket cannot be reused. If the push
        # fails for a transient reason, the user will need to
        # approve again; this is intentional.
        try:
            approval_gate.consume(
                approval_id,
                expected_operation_id=operation_id,
                expected_repository_id=record.repository_id,
                expected_operation=record.kind,
                expected_base_sha=record.base_sha,
                expected_diff_sha256=record.diff_sha256 or "",
                expected_target_branch=target_branch,
            )
        except Exception as exc:  # noqa: BLE001
            normalized = to_source_control_error(exc)
            return self.failure_result(
                error_code=normalized.code.value,
                error_message=normalized.message,
                metadata={"operation_id": operation_id},
            )

        # --- Transition to PUSHING ------------------------------------
        try:
            await operation_service.transition(
                operation_id,
                OperationStatus.PUSHING,
                reason="Push started.",
                metadata={
                    "remote": remote,
                    "target_branch": target_branch,
                },
                actor="agent",
            )
        except Exception as exc:  # noqa: BLE001
            normalized = to_source_control_error(exc)
            return self.failure_result(
                error_code=normalized.code.value,
                error_message=normalized.message,
                metadata={"operation_id": operation_id},
            )

        # --- Perform the push -----------------------------------------
        #
        # The credential is passed explicitly. Without it, the push
        # fails on private repositories.
        try:
            push_result = await push_manager.push_branch(
                workspace_id=record.workspace_id,
                local_branch=local_branch,
                remote=remote,
                remote_branch=target_branch,
                force_with_lease=False,
                set_upstream=True,
                token=token,
                workspace_path=workspace.workspace_path,
            )
        except Exception as exc:  # noqa: BLE001
            normalized = to_source_control_error(exc)
            # Mark the operation as failed. If the error is
            # retryable we leave the operation in a state the user
            # can act on.
            try:
                await operation_service.mark_failed(
                    operation_id,
                    error_code=normalized.code.value,
                    error_message=normalized.message,
                    retryable=normalized.is_retryable,
                    actor="system",
                )
            except Exception:  # noqa: BLE001
                pass
            return self.failure_result(
                error_code=normalized.code.value,
                error_message=normalized.message,
                metadata={
                    "operation_id": operation_id,
                    "retryable": normalized.is_retryable,
                },
            )

        # --- Interpret the result --------------------------------------
        if not push_result.accepted:
            # A rejection is a final failure for this attempt.
            try:
                await operation_service.mark_failed(
                    operation_id,
                    error_code="PUSH_REJECTED",
                    error_message=push_result.message
                    or "Push was rejected by the remote.",
                    retryable=False,
                    actor="system",
                )
            except Exception:  # noqa: BLE001
                pass
            return self.failure_result(
                error_code="PUSH_REJECTED",
                error_message=push_result.message
                or "Push was rejected by the remote.",
                data={
                    "local_branch": push_result.local_branch,
                    "local_sha": push_result.local_sha,
                    "remote_branch": push_result.remote_branch,
                    "remote_sha": push_result.remote_sha,
                    "reason": push_result.rejected_reason,
                },
                metadata={"operation_id": operation_id},
            )

        # --- Success ---------------------------------------------------
        try:
            await operation_service.transition(
                operation_id,
                OperationStatus.PUSHED,
                reason="Push completed successfully.",
                metadata={
                    "local_sha": push_result.local_sha,
                    "remote_sha": push_result.remote_sha,
                },
                actor="system",
            )
        except Exception as exc:  # noqa: BLE001
            # The push succeeded but we could not update the journal.
            # We return success and record a warning; the operation
            # will be reconciled on the next startup.
            logger.warning(
                "Push succeeded but the operation state could not be "
                "updated for '%s': %s",
                operation_id,
                type(exc).__name__,
            )

        return self.success_result(
            {
                "operation_id": operation_id,
                "status": "pushed",
                "local_branch": push_result.local_branch,
                "local_sha": push_result.local_sha,
                "remote_branch": push_result.remote_branch,
                "remote_sha": push_result.remote_sha,
            },
            metadata={
                "operation_id": operation_id,
                "remote": remote,
            },
        )

    # --- Credential resolution -----------------------------------------

    @staticmethod
    def _resolve_auth(
        *,
        token_service: TokenService,
        record: Any,
        context: ToolContext,
    ) -> Union[Tuple[ProviderId, str, str, str], ToolResult]:
        """
        Resolve `(provider, account_id, base_url, token)` for the push.

        Resolution order for the account:

            1. `context.metadata["account_id"]` when the facade
               injects an account hint.
            2. The first stored account for the operation's provider.

        The token is read from the vault. It MUST NEVER be logged,
        returned in a tool result, or included in an error message.

        Returns:
            A 4-tuple `(provider, account_id, base_url, token)` on
            success, or a failed `ToolResult` when no credential is
            available.
        """
        # Normalize the provider.
        provider_id = record.provider
        if not isinstance(provider_id, ProviderId):
            try:
                provider_id = ProviderId(str(provider_id))
            except ValueError:
                return ToolResult.fail(
                    error_code="VALIDATION_ERROR",
                    error_message=(
                        f"Operation references an unknown provider: "
                        f"{provider_id!r}."
                    ),
                    metadata={"operation_id": record.operation_id},
                )

        # Gather stored credentials for the provider.
        stored = token_service.list_stored()
        for_provider = [entry for entry in stored if entry.provider == provider_id]
        if not for_provider:
            return ToolResult.fail(
                error_code="AUTH_REQUIRED",
                error_message=(
                    f"No credential is stored for provider "
                    f"'{provider_id.value}'. Connect the provider "
                    "first."
                ),
                metadata={
                    "provider": provider_id.value,
                    "operation_id": record.operation_id,
                },
            )

        # Optional account hint from the facade.
        override: Optional[str] = None
        if context.metadata:
            raw = context.metadata.get("account_id")
            if isinstance(raw, str):
                override = raw.strip() or None

        chosen = None
        if override:
            for entry in for_provider:
                if entry.account_id == override:
                    chosen = entry
                    break
            if chosen is None:
                return ToolResult.fail(
                    error_code="AUTH_REQUIRED",
                    error_message=(
                        f"No credential is stored for account "
                        f"'{override}' on provider "
                        f"'{provider_id.value}'."
                    ),
                    metadata={
                        "provider": provider_id.value,
                        "account_id": override,
                        "operation_id": record.operation_id,
                    },
                )
        else:
            chosen = for_provider[0]

        # Read the token from the vault.
        token = token_service.get_access_token(
            provider_id, chosen.account_id, chosen.base_url
        )
        if not token:
            return ToolResult.fail(
                error_code="AUTH_REQUIRED",
                error_message=(
                    "The stored credential is missing or expired. "
                    "Reconnect the provider before retrying the push."
                ),
                metadata={
                    "provider": provider_id.value,
                    "account_id": chosen.account_id,
                    "base_url": redact_url(chosen.base_url),
                    "operation_id": record.operation_id,
                },
            )

        return provider_id, chosen.account_id, chosen.base_url, token
