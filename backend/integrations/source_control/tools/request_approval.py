# backend/integrations/source_control/tools/request_approval.py
"""
The `source_control.request_approval` tool.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from ..domain.enums import OperationStatus
from ..errors import WorkspaceDirtyError, to_source_control_error
from ..git.commit import (
    CommitOptions,
    get_default_commit_manager,
)
from ..git.diff import get_default_diff_engine
from ..git.path_policy import is_valid_branch_name
from ..git.workspace import get_default_workspace_manager
from ..operations.service import (
    OperationService,
    get_default_operation_service,
)
from ..security.approval import (
    ApprovalGate,
    get_default_approval_gate,
)
from .base import SourceControlTool, ToolContext, ToolResult

__all__ = [
    "RequestApprovalTool",
    "SUPPORTED_OPERATIONS",
    "DEFAULT_TTL_SECONDS",
]

logger = logging.getLogger(__name__)

SUPPORTED_OPERATIONS: tuple[str, ...] = (
    "push_branch",
    "create_change_request",
    "delete_remote_branch",
    "merge_change_request",
)

DEFAULT_TTL_SECONDS: int = 15 * 60


class RequestApprovalTool(SourceControlTool):
    """
    Create a pending approval ticket for a remote side effect.
    """

    def __init__(
        self,
        *,
        operation_service: Optional[OperationService] = None,
        approval_gate: Optional[ApprovalGate] = None,
        audit_logger: Any = None,
    ) -> None:
        super().__init__(audit_logger=audit_logger)
        self._explicit_operation_service = operation_service
        self._explicit_approval_gate = approval_gate

    @property
    def name(self) -> str:
        return "source_control.request_approval"

    @property
    def description(self) -> str:
        return (
            "Create a pending approval request for a remote side effect. "
            "The approval is NOT granted by this tool; the user must grant it through the PELDRUN UI."
        )

    @property
    def input_schema(self) -> Mapping[str, Any]:
        return {
            "type": "object",
            "properties": {
                "workspace_id": {
                    "type": "string",
                    "description": "Identifier of the workspace (optional if operation_id is provided)."
                },
                "operation_id": {
                    "type": "string",
                    "description": "Identifier of the operation (optional if workspace_id is provided)."
                },
                "operation": {
                    "type": "string",
                    "enum": list(SUPPORTED_OPERATIONS),
                    "default": "push_branch",
                    "description": "The remote side effect the approval covers."
                },
                "source_branch": {
                    "type": "string",
                    "description": (
                        "Local branch the change lives on. Optional; "
                        "defaults to the workspace's current branch. "
                        "For create_change_request, this is the head "
                        "branch of the PR and MUST differ from "
                        "target_branch."
                    )
                },
                "target_branch": {
                    "type": "string",
                    "description": (
                        "Remote branch the operation targets. "
                        "REQUIRED when creating a new operation "
                        "(workspace_id without operation_id). For "
                        "push_branch: the remote branch to push to. "
                        "For create_change_request: the PR base branch "
                        "(for example 'main')."
                    )
                },
                "ttl_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional TTL for the approval, in seconds."
                },
            },
            "required": [],
            "additionalProperties": False,
        }

    async def execute(
        self, context: ToolContext, **kwargs: Any
    ) -> ToolResult:
        # --- Parse inputs ----------------------------------------------
        workspace_id = kwargs.get("workspace_id")
        if isinstance(workspace_id, str):
            workspace_id = workspace_id.strip()
            if workspace_id.lower() in ("none", "null", ""):
                workspace_id = None

        operation_id = kwargs.get("operation_id")
        if isinstance(operation_id, str):
            operation_id = operation_id.strip()
            if operation_id.lower() in ("none", "null", ""):
                operation_id = None

        # Auto-resolve workspace_id from the most recent workspace
        # created in this chat session when neither operation_id nor
        # workspace_id was supplied. This compensates for LLM models
        # (including GigaChat-3-Pro) that succeed at create_workspace
        # but forget to forward the returned `workspace_id` to the
        # subsequent request_approval call — even though the model
        # demonstrably knows the value (it correctly passes it to
        # push_branch). Without this fallback, the model loops on
        # "Either operation_id or workspace_id must be provided" until
        # max_steps, and no approval ticket is ever created.
        #
        # Safety: the fallback ONLY activates when the caller supplies
        # neither identifier. It picks the workspace with the highest
        # created_at among workspaces whose metadata.chat_id matches
        # the current session's chat_id. If the caller supplies either
        # identifier, the fallback is skipped entirely.
        if not operation_id and not workspace_id:
            chat_id_ctx = ""
            if isinstance(context.metadata, dict):
                raw_chat = context.metadata.get("chat_id")
                if isinstance(raw_chat, str):
                    chat_id_ctx = raw_chat.strip()
            if chat_id_ctx:
                try:
                    from ..git.workspace import get_default_workspace_manager

                    mgr = get_default_workspace_manager()
                    candidates = [
                        ws
                        for ws in mgr.list_workspaces()
                        if isinstance(ws.metadata, dict)
                        and ws.metadata.get("chat_id") == chat_id_ctx
                    ]
                    # list_workspaces() returns oldest-first; use the
                    # newest match.
                    if candidates:
                        workspace_id = candidates[-1].workspace_id
                        logger.info(
                            "request_approval: auto-resolved "
                            "workspace_id=%r from the most recent "
                            "workspace for chat_id=%r",
                            workspace_id,
                            chat_id_ctx,
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "request_approval: auto-resolve of "
                        "workspace_id failed (%s); the caller will "
                        "receive the standard validation error.",
                        type(exc).__name__,
                    )

        if not operation_id and not workspace_id:
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message=(
                    "Either `operation_id` or `workspace_id` must be "
                    "provided. The `workspace_id` is returned by the "
                    "preceding `create_workspace` call; pass it here."
                ),
            )

        # Accept common aliases and fall back to push_branch
        raw_op = str(kwargs.get("operation") or "push_branch").strip().lower()
        operation_aliases = {
            "commit": "push_branch",
            "push": "push_branch",
            "pr": "create_change_request",
            "pull_request": "create_change_request",
            "merge": "merge_change_request",
        }
        operation = operation_aliases.get(raw_op, raw_op)
        if operation not in SUPPORTED_OPERATIONS:
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message=(
                    f"Unsupported operation: {raw_op!r}. "
                    f"Supported: {list(SUPPORTED_OPERATIONS)}."
                ),
            )

        # Ignore title/body (they belong to create_change_request)
        kwargs.pop("title", None)
        kwargs.pop("body", None)
        kwargs.pop("draft", None)

        # target_branch: required for new operations (the caller must
        # know where the change is going), resolved from the record
        # for existing ones. The old "main" default silently directed
        # pushes at the protected default branch and disagreed with
        # the record for re-request scenarios.
        target_branch_input = kwargs.get("target_branch")
        if isinstance(target_branch_input, str):
            target_branch_input = target_branch_input.strip() or None
        else:
            target_branch_input = None

        # source_branch: optional override. When omitted, the
        # workspace's current branch is used as the change's head.
        source_branch_input = kwargs.get("source_branch")
        if isinstance(source_branch_input, str):
            source_branch_input = source_branch_input.strip() or None
        else:
            source_branch_input = None

        # A new operation is created when workspace_id is given without
        # an existing operation_id. This flag drives both the required
        # target_branch validation below and the ticket branch choice
        # at the end of the method.
        creating_new = bool(workspace_id and not operation_id)
        if creating_new:
            if not target_branch_input:
                return self.failure_result(
                    error_code="VALIDATION_ERROR",
                    error_message=(
                        "`target_branch` is required when creating a "
                        "new operation. For push_branch, pass the "
                        "remote branch to push to. For "
                        "create_change_request, pass the PR base "
                        "branch (for example 'main')."
                    ),
                    metadata={"workspace_id": workspace_id},
                )
            if not is_valid_branch_name(target_branch_input):
                return self.failure_result(
                    error_code="VALIDATION_ERROR",
                    error_message=(
                        f"Invalid `target_branch`: "
                        f"{target_branch_input!r}."
                    ),
                    metadata={"workspace_id": workspace_id},
                )
            if source_branch_input and not is_valid_branch_name(
                source_branch_input
            ):
                return self.failure_result(
                    error_code="VALIDATION_ERROR",
                    error_message=(
                        f"Invalid `source_branch`: "
                        f"{source_branch_input!r}."
                    ),
                    metadata={"workspace_id": workspace_id},
                )

        ttl_raw = kwargs.get("ttl_seconds")
        ttl_seconds: Optional[int] = None
        if ttl_raw is not None and ttl_raw != "":
            try:
                ttl_seconds = int(ttl_raw)
            except (TypeError, ValueError):
                return self.failure_result(
                    error_code="VALIDATION_ERROR",
                    error_message="`ttl_seconds` must be an integer.",
                )
            if ttl_seconds < 1:
                return self.failure_result(
                    error_code="VALIDATION_ERROR",
                    error_message="`ttl_seconds` must be at least 1.",
                )

        # --- Resolve services ------------------------------------------
        operation_service = (
            self._explicit_operation_service
            or get_default_operation_service()
        )
        approval_gate = (
            self._explicit_approval_gate or get_default_approval_gate()
        )

        # --- Create operation if only workspace_id is provided ---------
        if not operation_id and workspace_id:
            ws_manager = get_default_workspace_manager()
            ws = ws_manager.get_workspace(workspace_id)
            if not ws:
                return self.failure_result(
                    error_code="WORKSPACE_NOT_FOUND",
                    error_message=f"Workspace '{workspace_id}' not found.",
                )

            try:
                # Stage and commit any pending edits using the async
                # CommitManager. The previous implementation used
                # synchronous `subprocess.run`, which blocked the
                # event loop for up to 60 seconds per invocation and
                # silently swallowed every failure. A failed commit
                # left the workspace without a born HEAD (or with
                # stale staging), which caused `git diff` to fail
                # downstream with an error the agent could not
                # interpret.
                #
                # The CommitManager raises WorkspaceDirtyError when
                # there is nothing to commit. That is a legitimate
                # state — the agent's edits may have been committed
                # manually, or none were made — so we treat it as a
                # no-op and proceed to diff computation.
                commit_manager = get_default_commit_manager()
                try:
                    await commit_manager.create_commit(
                        workspace_id=workspace_id,
                        message=f"PELDRUN: {operation}",
                        options=CommitOptions(
                            stage_all=True,
                            allow_empty=False,
                        ),
                        workspace_path=ws.workspace_path,
                    )
                except WorkspaceDirtyError:
                    # Nothing to commit; proceed to diff.
                    pass

                # Resolve the change's head branch. The explicit input
                # wins; otherwise we use the branch the workspace is
                # actually on. This value is stored on the record as
                # source_branch and is consumed by push_branch (as the
                # local ref) and create_change_request (as the PR
                # head).
                effective_source_branch = (
                    source_branch_input or ws.current_branch
                )
                if not is_valid_branch_name(effective_source_branch):
                    return self.failure_result(
                        error_code="VALIDATION_ERROR",
                        error_message=(
                            f"Invalid `source_branch`: "
                            f"{effective_source_branch!r}."
                        ),
                        metadata={"workspace_id": workspace_id},
                    )

                # For create_change_request, source and target MUST
                # differ: a PR from a branch to itself is meaningless
                # and would be rejected by the provider. For
                # push_branch they may be equal (push feature-x to
                # feature-x).
                if (
                    operation == "create_change_request"
                    and effective_source_branch == target_branch_input
                ):
                    return self.failure_result(
                        error_code="VALIDATION_ERROR",
                        error_message=(
                            f"`source_branch` and `target_branch` are "
                            f"identical ({effective_source_branch!r}); "
                            "for create_change_request, source_branch "
                            "is the branch being merged and "
                            "target_branch is the PR base. They must "
                            "differ."
                        ),
                        metadata={"workspace_id": workspace_id},
                    )

                # 1. Create operation dynamically
                record = await operation_service.create_operation(
                    repository_id=ws.repository_id,
                    kind=operation,
                    provider=ws.provider,
                    source_branch=effective_source_branch,
                    target_branch=target_branch_input,
                    workspace_id=workspace_id,
                )
                operation_id = record.operation_id

                # 2. Advance the state machine correctly (CREATED -> REVIEW_REQUIRED)
                # This prevents the ConflictError that was causing the infinite loop.
                for state in (
                    OperationStatus.REPOSITORY_RESOLVED,
                    OperationStatus.WORKSPACE_READY,
                    OperationStatus.EDITING,
                ):
                    try:
                        await operation_service.transition(operation_id, state, actor="system")
                    except Exception:
                        pass # Ignore if already in that state

                # 3. Compute and attach the diff.
                git_base_sha = ws.base_sha or None
                diff_engine = get_default_diff_engine()
                summary = await diff_engine.compute_diff(
                    workspace_id=workspace_id,
                    base_sha=git_base_sha,
                    include_patch=True,
                    workspace_path=ws.workspace_path,
                )

                # Reject an empty diff. Creating an approval ticket
                # for a diff that contains no file changes would
                # present the user with an empty review and let the
                # agent believe its request succeeded when there was
                # nothing to approve. Surface the state explicitly
                # instead.
                if not summary.files:
                    return self.failure_result(
                        error_code="VALIDATION_ERROR",
                        error_message=(
                            "No changes detected in the workspace. "
                            "Edit at least one file before requesting "
                            "approval; an empty diff cannot be "
                            "approved."
                        ),
                        metadata={"workspace_id": workspace_id},
                    )

                diff_hash = summary.sha256()

                # Store the ACTUAL Git commit SHA the diff was computed
                # against. The ApprovalGate accepts both SHA-1 (40) and
                # SHA-256 (64) for base_sha, so no transformation is
                # needed. `summary.base_sha` is also what push_branch
                # later compares against the remote's SHA; storing a
                # transformed hash here would make every push reject as
                # REMOTE_STATE_CHANGED.
                effective_base_sha = summary.base_sha or ws.base_sha or ""
                if not effective_base_sha:
                    return self.failure_result(
                        error_code="VALIDATION_ERROR",
                        error_message=(
                            "Cannot request approval: the workspace has "
                            "no base commit SHA."
                        ),
                        metadata={"workspace_id": workspace_id},
                    )

                await operation_service.attach_diff(
                    operation_id=operation_id,
                    diff_sha256=diff_hash,
                    base_sha=effective_base_sha,
                )

                # 4. Transition to REVIEW_REQUIRED
                await operation_service.transition(
                    operation_id,
                    OperationStatus.REVIEW_REQUIRED,
                    actor="system",
                )
            except Exception as exc:
                normalized = to_source_control_error(exc)
                return self.failure_result(
                    error_code=normalized.code.value,
                    error_message=normalized.message,
                    metadata={"workspace_id": workspace_id},
                )

        # --- Fetch the operation record --------------------------------
        try:
            record = await operation_service.get_operation(operation_id)
        except Exception as exc:
            normalized = to_source_control_error(exc)
            return self.failure_result(
                error_code=normalized.code.value,
                error_message=normalized.message,
                metadata={"operation_id": operation_id},
            )

        # --- Validate the operation state ------------------------------
        if record.status in {
            OperationStatus.CANCELLED,
            OperationStatus.FAILED_FINAL,
            OperationStatus.REJECTED,
        }:
            return self.failure_result(
                error_code="CONFLICT",
                error_message=f"Operation is in status '{record.status.value}' and cannot request approval.",
                metadata={"operation_id": operation_id},
            )

        if not record.base_sha or not record.diff_sha256:
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message="Operation has no base SHA or diff attached.",
                metadata={"operation_id": operation_id},
            )

        # --- Determine the ticket's target branch ----------------------
        #
        # For an operation created in THIS call, record.target_branch
        # already equals the input. For an existing operation (only
        # operation_id was passed), record.target_branch is the
        # authoritative value: push_branch / create_change_request
        # re-validate every bound field on the ticket against the
        # record at consumption time, so using the input's default
        # would surface as an ApprovalInvalidError on the first
        # attempt to execute.
        ticket_target_branch = (
            record.target_branch or target_branch_input
        )
        if not ticket_target_branch:
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message=(
                    "Operation record has no target_branch and no "
                    "`target_branch` was supplied."
                ),
                metadata={"operation_id": operation_id},
            )

        # --- Create the ticket -----------------------------------------
        try:
            ticket = approval_gate.create_ticket(
                operation_id=operation_id,
                repository_id=record.repository_id,
                operation=operation,
                base_sha=record.base_sha,
                diff_sha256=record.diff_sha256,
                target_branch=ticket_target_branch,
                ttl_seconds=ttl_seconds,
            )
        except Exception as exc:
            normalized = to_source_control_error(exc)
            return self.failure_result(
                error_code=normalized.code.value,
                error_message=normalized.message,
                metadata={"operation_id": operation_id, "operation": operation},
            )

        # --- Attach the approval to the operation ----------------------
        try:
            await operation_service.attach_approval(operation_id, ticket.approval_id)
        except Exception:
            pass

        # --- Return success and instruct the agent to stop -------------
        #
        # The payload carries the fields the UI needs to render the
        # ChatOperationCard WITHOUT a second round-trip to the
        # operation endpoint. Adding `repository_id` and
        # `source_branch` here removes the "unknown" placeholder the
        # card used to display and lets the review header show the
        # real repository and branch names on first paint.
        return self.success_result(
            {
                "approval_id": ticket.approval_id,
                "operation_id": ticket.operation_id,
                "operation": ticket.operation,
                "repository_id": record.repository_id,
                "source_branch": record.source_branch or "",
                "target_branch": ticket.target_branch,
                "status": ticket.status.value,
                "expires_at": ticket.expires_at.isoformat(),
                "message": "Approval ticket created. Please wait for the user to review and approve in the UI. DO NOT ask the user manually.",
            },
            metadata={"operation_id": operation_id, "operation": operation},
        )
