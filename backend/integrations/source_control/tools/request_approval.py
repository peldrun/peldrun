# backend/integrations/source_control/tools/request_approval.py
"""
The `source_control.request_approval` tool.

Creates a pending approval ticket for a remote side effect. The
ticket is NOT an approval: the user must still grant it through the
PELDRUN UI. This tool only creates the request.

The tool deliberately does NOT accept `base_sha` or `diff_sha256`
from the caller. Those values are read from the operation record,
which the application populates through the reviewed diff flow.
This prevents the agent from binding an approval to content that
was never reviewed.

Design rules:
    - The tool MUST NOT grant, reject, or consume approvals.
    - The tool MUST NOT accept a diff hash or base SHA from the
      caller.
    - The tool MUST require an existing operation with a populated
      `base_sha` and `diff_sha256`. If either is missing, the
      caller must first run `source_control.get_diff` and let the
      application attach the diff to the operation.
    - The tool MUST be idempotent in the sense that creating an
      approval ticket twice for the same operation is a no-op after
      the first success.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from ..domain.enums import OperationStatus
from ..errors import to_source_control_error
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


#: Operations for which approval can be requested. The set is
#: deliberately small: every entry corresponds to a remote side
#: effect that PELDRUN treats as requiring explicit consent.
SUPPORTED_OPERATIONS: tuple[str, ...] = (
    "push_branch",
    "create_change_request",
    "delete_remote_branch",
    "merge_change_request",
)


#: Default TTL for an approval ticket, in seconds. The value matches
#: the policy default and is only used when the caller does not
#: specify one.
DEFAULT_TTL_SECONDS: int = 15 * 60


class RequestApprovalTool(SourceControlTool):
    """
    Create a pending approval ticket for a remote side effect.

    Inputs:
        operation_id:  Identifier of the operation the approval is
                       for. Required.
        operation:     The remote side effect the approval covers
                       (e.g. `"push_branch"`). Required.
        target_branch: The remote branch the operation targets.
                       Required.
        ttl_seconds:   Optional TTL override.

    Output:
        `{"approval_id": <str>, "expires_at": <iso8601>, "status":
        "pending"}`. The approval is NOT granted; the user must
        grant it through the UI.
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

    # --- Metadata -------------------------------------------------------

    @property
    def name(self) -> str:
        return "source_control.request_approval"

    @property
    def description(self) -> str:
        return (
            "Create a pending approval request for a remote side "
            "effect. The approval is NOT granted by this tool; the "
            "user must grant it through the PELDRUN UI."
        )

    @property
    def input_schema(self) -> Mapping[str, Any]:
        return {
            "type": "object",
            "properties": {
                "operation_id": {
                    "type": "string",
                    "description": (
                        "Identifier of the operation the approval is "
                        "for."
                    ),
                },
                "operation": {
                    "type": "string",
                    "enum": list(SUPPORTED_OPERATIONS),
                    "description": (
                        "The remote side effect the approval covers."
                    ),
                },
                "target_branch": {
                    "type": "string",
                    "description": (
                        "The remote branch the operation targets."
                    ),
                },
                "ttl_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Optional TTL for the approval, in seconds."
                    ),
                },
            },
            "required": ["operation_id", "operation", "target_branch"],
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

        operation = kwargs.get("operation")
        if (
            not isinstance(operation, str)
            or operation not in SUPPORTED_OPERATIONS
        ):
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message=(
                    f"`operation` must be one of: "
                    f"{', '.join(SUPPORTED_OPERATIONS)}."
                ),
            )

        target_branch = kwargs.get("target_branch")
        if (
            not isinstance(target_branch, str)
            or not target_branch.strip()
        ):
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message=(
                    "`target_branch` must be a non-empty string."
                ),
            )
        target_branch = target_branch.strip()

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

        # --- Fetch the operation record --------------------------------
        try:
            record = await operation_service.get_operation(operation_id)
        except Exception as exc:  # noqa: BLE001
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
                error_message=(
                    f"Operation is in status '{record.status.value}' "
                    "and cannot request approval."
                ),
                metadata={
                    "operation_id": operation_id,
                    "status": record.status.value,
                },
            )

        if not record.base_sha:
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message=(
                    "Operation has no base SHA attached. Run "
                    "`source_control.get_diff` first."
                ),
                metadata={"operation_id": operation_id},
            )
        if not record.diff_sha256:
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message=(
                    "Operation has no reviewed diff attached. Run "
                    "`source_control.get_diff` first."
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
                target_branch=target_branch,
                ttl_seconds=ttl_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            normalized = to_source_control_error(exc)
            return self.failure_result(
                error_code=normalized.code.value,
                error_message=normalized.message,
                metadata={
                    "operation_id": operation_id,
                    "operation": operation,
                    "retryable": normalized.is_retryable,
                },
            )

        # --- Attach the approval to the operation ----------------------
        # Best-effort: attaching makes the operation display the
        # pending approval in the UI. Failures here MUST NOT mask
        # the successful ticket creation.
        try:
            await operation_service.attach_approval(
                operation_id, ticket.approval_id
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Failed to attach approval '%s' to operation '%s': %s",
                ticket.approval_id,
                operation_id,
                type(exc).__name__,
            )

        return self.success_result(
            {
                "approval_id": ticket.approval_id,
                "operation_id": ticket.operation_id,
                "operation": ticket.operation,
                "target_branch": ticket.target_branch,
                "base_sha": ticket.base_sha,
                "diff_sha256": ticket.diff_sha256,
                "status": ticket.status.value,
                "expires_at": ticket.expires_at.isoformat(),
                "requires_user_action": True,
            },
            metadata={
                "operation_id": operation_id,
                "operation": operation,
            },
        )
