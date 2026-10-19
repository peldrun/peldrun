# backend/integrations/source_control/tools/get_operation.py
"""
Read-only tools for inspecting the durable operation journal.

Two tools live here because they share the same service and the same
read-only semantics:

    - ``GetOperationTool``   : fetch one operation by ID.
    - ``ListOperationsTool`` : enumerate recent operations with filters.

The tools are read-only by design. They never transition the state
machine, never consume approvals, and never touch the remote. Their
only purpose is to let the agent (and the UI) answer questions that
must be accurate at the exact moment they are asked:

    - What is the current status of operation <id>?
    - Is the approval ticket still valid, or has it been consumed?
    - Which operations are waiting for approval in this workspace?

Without these tools, an agent that needs to resume after a human
approval has no way to look up the operation and the approval ticket
it created in a previous turn. The agent's only source of truth
becomes its own conversation history, which the sidecar truncates
and the context manager compresses. The result is an agent that
either replays the request (burning a new approval ticket) or
invents an operation ID (rejected by every downstream tool).
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional, Sequence

from ..domain.enums import ProviderId
from ..errors import to_source_control_error
from ..operations.service import (
    OperationService,
    get_default_operation_service,
)
from .base import SourceControlTool, ToolContext, ToolResult

__all__ = [
    "GetOperationTool",
    "ListOperationsTool",
    "DEFAULT_LIST_LIMIT",
    "MAX_LIST_LIMIT",
    "MAX_EVENTS_IN_RESULT",
]


logger = logging.getLogger(__name__)


#: Default number of operations returned by `ListOperationsTool`.
DEFAULT_LIST_LIMIT: int = 20

#: Maximum number of operations that can be requested in a single
#: `ListOperationsTool` call.
MAX_LIST_LIMIT: int = 100

#: Maximum number of events included in a `GetOperationTool` result.
#: A well-behaved operation has a handful of transitions; the cap
#: protects against pathological cases (e.g. an operation that keeps
#: flapping between FAILED_RETRYABLE and PUSHING) that would otherwise
#: flood the agent's context.
MAX_EVENTS_IN_RESULT: int = 100


class GetOperationTool(SourceControlTool):
    """
    Fetch a single operation by ID.

    Inputs:
        operation_id:   Identifier of the operation. Required.
        include_events: When True (default), include the event
                        history. Set to False to keep the response
                        compact.

    The result reflects the journal at the moment of the call. There
    is no caching, no snapshot semantics, and no reconciliation.
    """

    def __init__(
        self,
        *,
        operation_service: Optional[OperationService] = None,
        audit_logger: Any = None,
    ) -> None:
        super().__init__(audit_logger=audit_logger)
        self._explicit_operation_service = operation_service

    @property
    def name(self) -> str:
        return "source_control.get_operation"

    @property
    def description(self) -> str:
        return (
            "Fetch the current state of a source-control operation by "
            "its ID. Use this to verify that an approval ticket is "
            "still pending (or was approved) before acting on it, and "
            "to retrieve the source/target branches and diff hash "
            "that a later tool (push_branch, create_change_request) "
            "will require."
        )

    @property
    def input_schema(self) -> Mapping[str, Any]:
        return {
            "type": "object",
            "properties": {
                "operation_id": {
                    "type": "string",
                    "description": (
                        "Identifier of the operation to fetch."
                    ),
                },
                "include_events": {
                    "type": "boolean",
                    "description": (
                        "When true (default), include the event "
                        "history for the operation."
                    ),
                    "default": True,
                },
            },
            "required": ["operation_id"],
            "additionalProperties": False,
        }

    async def execute(
        self, context: ToolContext, **kwargs: Any
    ) -> ToolResult:
        operation_id = kwargs.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id.strip():
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message="`operation_id` must be a non-empty string.",
            )
        operation_id = operation_id.strip()

        include_events = bool(kwargs.get("include_events", True))

        operation_service = (
            self._explicit_operation_service
            or get_default_operation_service()
        )

        try:
            record = await operation_service.try_get_operation(operation_id)
        except Exception as exc:  # noqa: BLE001
            normalized = to_source_control_error(exc)
            return self.failure_result(
                error_code=normalized.code.value,
                error_message=normalized.message,
                metadata={"operation_id": operation_id},
            )

        if record is None:
            return self.failure_result(
                error_code="NOT_FOUND",
                error_message=(
                    f"Operation '{operation_id}' was not found."
                ),
                metadata={"operation_id": operation_id},
            )

        payload: dict[str, Any] = {
            "operation_id": record.operation_id,
            "kind": record.kind,
            "status": record.status.value,
            "provider": (
                record.provider.value
                if isinstance(record.provider, ProviderId)
                else str(record.provider)
            ),
            "repository_id": record.repository_id,
            "source_branch": record.source_branch,
            "target_branch": record.target_branch,
            "workspace_id": record.workspace_id,
            "approval_id": record.approval_id,
            "base_sha": record.base_sha,
            "diff_sha256": record.diff_sha256,
            "change_request_url": (
                record.change_request.web_url
                if record.change_request is not None
                else None
            ),
            "error_code": record.error_code,
            "error_message": record.error_message,
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
            "is_terminal": record.is_terminal(),
        }

        if include_events:
            try:
                events = await operation_service.list_events(operation_id)
            except Exception as exc:  # noqa: BLE001
                payload["events"] = []
                payload["events_error"] = type(exc).__name__
            else:
                recent = list(events)[-MAX_EVENTS_IN_RESULT:]
                payload["events"] = [
                    {
                        "sequence": ev.sequence,
                        "event_type": (
                            ev.event_type.value
                            if hasattr(ev.event_type, "value")
                            else str(ev.event_type)
                        ),
                        "occurred_at": ev.occurred_at.isoformat(),
                        "payload": dict(ev.payload or {}),
                    }
                    for ev in recent
                ]
                payload["events_truncated"] = (
                    len(events) > MAX_EVENTS_IN_RESULT
                )

        return self.success_result(
            payload,
            metadata={"operation_id": operation_id},
        )


class ListOperationsTool(SourceControlTool):
    """
    Enumerate recent operations.

    Inputs:
        limit:        Maximum number of operations to return
                      (1-100, default 20).
        workspace_id: Optional filter on the workspace ID.
        kind:         Optional filter on the operation kind
                      (e.g. 'push_branch', 'create_change_request').
        status:       Optional filter on the current status
                      (e.g. 'review_required', 'approved', 'pushed').

    Results are ordered by the journal's default ordering (newest
    first). Filtering happens after fetching a bounded window, so a
    very restrictive filter over a very old journal may return fewer
    rows than requested. Callers that need exhaustive pagination
    SHOULD narrow the filter instead of increasing `limit`.
    """

    def __init__(
        self,
        *,
        operation_service: Optional[OperationService] = None,
        audit_logger: Any = None,
    ) -> None:
        super().__init__(audit_logger=audit_logger)
        self._explicit_operation_service = operation_service

    @property
    def name(self) -> str:
        return "source_control.list_operations"

    @property
    def description(self) -> str:
        return (
            "List recent source-control operations, optionally "
            "filtered by workspace, kind, or status. Use this to "
            "discover pending approvals or to find the operation "
            "that corresponds to a previous turn."
        )

    @property
    def input_schema(self) -> Mapping[str, Any]:
        return {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_LIST_LIMIT,
                    "default": DEFAULT_LIST_LIMIT,
                    "description": (
                        f"Maximum number of operations to return "
                        f"(1-{MAX_LIST_LIMIT})."
                    ),
                },
                "workspace_id": {
                    "type": "string",
                    "description": (
                        "Optional filter: only operations bound to "
                        "this workspace are returned."
                    ),
                },
                "kind": {
                    "type": "string",
                    "description": (
                        "Optional filter: only operations of this "
                        "kind (e.g. 'push_branch', "
                        "'create_change_request')."
                    ),
                },
                "status": {
                    "type": "string",
                    "description": (
                        "Optional filter: only operations in this "
                        "status (e.g. 'review_required', 'approved', "
                        "'pushed')."
                    ),
                },
            },
            "required": [],
            "additionalProperties": False,
        }

    async def execute(
        self, context: ToolContext, **kwargs: Any
    ) -> ToolResult:
        limit_raw = kwargs.get("limit", DEFAULT_LIST_LIMIT)
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message="`limit` must be an integer.",
            )
        if limit < 1 or limit > MAX_LIST_LIMIT:
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message=(
                    f"`limit` must be between 1 and {MAX_LIST_LIMIT}."
                ),
            )

        workspace_filter = kwargs.get("workspace_id")
        if workspace_filter is not None and not isinstance(
            workspace_filter, str
        ):
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message="`workspace_id` must be a string.",
            )
        if isinstance(workspace_filter, str):
            workspace_filter = workspace_filter.strip() or None

        kind_filter = kwargs.get("kind")
        if kind_filter is not None and not isinstance(kind_filter, str):
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message="`kind` must be a string.",
            )
        if isinstance(kind_filter, str):
            kind_filter = kind_filter.strip() or None

        status_filter = kwargs.get("status")
        if status_filter is not None and not isinstance(
            status_filter, str
        ):
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message="`status` must be a string.",
            )
        if isinstance(status_filter, str):
            status_filter = status_filter.strip().lower() or None

        operation_service = (
            self._explicit_operation_service
            or get_default_operation_service()
        )

        # Over-fetch to give the filters a chance to match. The
        # journal's own listing is bounded; a filter that cannot find
        # `limit` matches within the fetched window simply returns
        # fewer rows.
        fetch_limit = max(limit * 5, MAX_LIST_LIMIT)
        try:
            records: Sequence[Any] = await operation_service.list_operations(
                limit=fetch_limit
            )
        except Exception as exc:  # noqa: BLE001
            normalized = to_source_control_error(exc)
            return self.failure_result(
                error_code=normalized.code.value,
                error_message=normalized.message,
            )

        filtered = []
        for rec in records:
            if workspace_filter and rec.workspace_id != workspace_filter:
                continue
            if kind_filter and rec.kind != kind_filter:
                continue
            if status_filter and rec.status.value != status_filter:
                continue
            filtered.append(rec)
            if len(filtered) >= limit:
                break

        operations = [
            {
                "operation_id": rec.operation_id,
                "kind": rec.kind,
                "status": rec.status.value,
                "source_branch": rec.source_branch,
                "target_branch": rec.target_branch,
                "workspace_id": rec.workspace_id,
                "approval_id": rec.approval_id,
                "created_at": rec.created_at.isoformat(),
                "updated_at": rec.updated_at.isoformat(),
                "is_terminal": rec.is_terminal(),
            }
            for rec in filtered
        ]

        return self.success_result({
            "count": len(operations),
            "operations": operations,
        })
