# backend/integrations/source_control/tools/create_change_request.py
"""
The `source_control.create_change_request` tool.

Creates a pull request (GitHub, Bitbucket) or merge request (GitLab)
for a previously pushed branch. The tool requires its own approval
ticket: the approval for the push covers the push, not the pull
request. Users must review the PR/MR title and body before they are
submitted.

Pipeline:
    1. Validate inputs.
    2. Load the operation record from the journal.
    3. Verify the operation is in `PUSHED` (the normal case) or, if
       the caller is resuming a partially completed flow, in
       `APPROVED`. In either case the source branch MUST have been
       pushed already.
    4. Consume the approval ticket.
    5. Create the change request via the provider adapter.
    6. Attach the change request to the operation.
    7. Transition the operation to `CHANGE_REQUEST_CREATED`.

Design rules:
    - The tool MUST NOT accept `source_branch`, `target_branch`,
      `repository_id`, or the provider from the caller. Those
      values come from the operation record and the provider
      registry.
    - The tool MUST consume its own approval ticket. Reusing the
      push approval for a change request is a critical security
      bug.
    - The tool MUST default to draft change requests. Promoting to
      ready-for-review is a separate, explicit action.
    - The tool MUST NOT modify the branch after push. Only the PR
      metadata (title, body) is provider-visible content the agent
      is allowed to influence.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from ..domain.enums import OperationStatus
from ..domain.models import ChangeRequestRef
from ..errors import to_source_control_error
from ..operations.service import (
    OperationService,
    get_default_operation_service,
)
from ..registry import ProviderRegistry, get_default_registry
from ..security.approval import (
    ApprovalGate,
    get_default_approval_gate,
)
from .base import SourceControlTool, ToolContext, ToolResult

__all__ = [
    "CreateChangeRequestTool",
    "MAX_TITLE_LENGTH",
    "MAX_BODY_LENGTH",
]


logger = logging.getLogger(__name__)


#: Maximum title length accepted by the tool. Matches GitHub's own
#: limit. The provider adapter enforces the same limit, so this
#: check is a fast failure rather than a hard constraint.
MAX_TITLE_LENGTH: int = 256

#: Maximum body length accepted by the tool.
MAX_BODY_LENGTH: int = 65_536


class CreateChangeRequestTool(SourceControlTool):
    """
    Create a change request (PR/MR) for a pushed branch.

    Inputs:
        operation_id: Identifier of the operation. Required.
        approval_id:  Identifier of the approval ticket. Required.
        title:        Title of the change request. Required.
        body:         Optional description. Markdown is supported.
        draft:        When True (default), create the request as a
                      draft.

    The source and target branches are read from the operation
    record. The provider is read from the record's provider field.
    """

    def __init__(
        self,
        *,
        operation_service: Optional[OperationService] = None,
        approval_gate: Optional[ApprovalGate] = None,
        registry: Optional[ProviderRegistry] = None,
        audit_logger: Any = None,
    ) -> None:
        super().__init__(audit_logger=audit_logger)
        self._explicit_operation_service = operation_service
        self._explicit_approval_gate = approval_gate
        self._explicit_registry = registry

    # --- Metadata -------------------------------------------------------

    @property
    def name(self) -> str:
        return "source_control.create_change_request"

    @property
    def description(self) -> str:
        return (
            "Create a draft pull request or merge request for a "
            "branch that has already been pushed. Requires its own "
            "approval ticket; the push approval is not reused."
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
                        "Identifier of the approval ticket for the "
                        "change request."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": (
                        "Title of the change request. The user "
                        "reviewed this title."
                    ),
                },
                "body": {
                    "type": "string",
                    "description": (
                        "Optional description. Markdown is supported."
                    ),
                },
                "draft": {
                    "type": "boolean",
                    "description": (
                        "When true (default), create the request as "
                        "a draft."
                    ),
                    "default": True,
                },
            },
            "required": ["operation_id", "approval_id", "title"],
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

        title = kwargs.get("title")
        if not isinstance(title, str) or not title.strip():
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message=(
                    "`title` must be a non-empty string."
                ),
            )
        title = title.strip()
        if len(title) > MAX_TITLE_LENGTH:
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message=(
                    f"`title` exceeds {MAX_TITLE_LENGTH} characters."
                ),
            )

        body = kwargs.get("body", "")
        if body is None:
            body = ""
        if not isinstance(body, str):
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message="`body` must be a string.",
            )
        if len(body) > MAX_BODY_LENGTH:
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message=(
                    f"`body` exceeds {MAX_BODY_LENGTH} characters."
                ),
            )

        draft = bool(kwargs.get("draft", True))

        # --- Resolve services ------------------------------------------
        operation_service = (
            self._explicit_operation_service
            or get_default_operation_service()
        )
        approval_gate = (
            self._explicit_approval_gate or get_default_approval_gate()
        )
        registry = self._explicit_registry or get_default_registry()

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
        #
        # The canonical path is PUSHED -> CHANGE_REQUEST_CREATED.
        # We also accept APPROVED so that a resumed flow that failed
        # after push but before recording the transition can still
        # create the PR (the "attach_change_request" step is
        # idempotent).
        allowed_statuses = {
            OperationStatus.PUSHED,
            OperationStatus.APPROVED,
        }
        if record.status not in allowed_statuses:
            return self.failure_result(
                error_code="CONFLICT",
                error_message=(
                    f"Operation is in status '{record.status.value}'. "
                    "A change request can only be created for a "
                    "pushed operation."
                ),
                metadata={
                    "operation_id": operation_id,
                    "status": record.status.value,
                },
            )

        source_branch = record.source_branch
        target_branch = record.target_branch
        if not source_branch or not target_branch:
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message=(
                    "Operation is missing source_branch or "
                    "target_branch; cannot create a change request."
                ),
                metadata={"operation_id": operation_id},
            )

        if source_branch == target_branch:
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message=(
                    "source_branch and target_branch are identical."
                ),
                metadata={"operation_id": operation_id},
            )

        # --- Resolve the provider adapter ------------------------------
        adapter = registry.try_get(record.provider)
        if adapter is None:
            return self.failure_result(
                error_code="NOT_FOUND",
                error_message=(
                    f"Provider '{record.provider.value}' is not "
                    "registered."
                ),
                metadata={"operation_id": operation_id},
            )

        # --- Resolve the repository reference --------------------------
        #
        # The change request creation needs a `RepositoryRef`. We
        # rebuild it from the operation and the provider's metadata.
        # For now, we expect the operation's `metadata` to carry
        # enough information; otherwise the caller must run
        # `source_control.get_repository` first.
        repo = await self._resolve_repository(
            adapter=adapter,
            record=record,
        )
        if repo is None:
            return self.failure_result(
                error_code="NOT_FOUND",
                error_message=(
                    "Could not resolve the repository for the "
                    "operation."
                ),
                metadata={
                    "operation_id": operation_id,
                    "repository_id": record.repository_id,
                },
            )

        # --- Validate the approval ticket ------------------------------
        try:
            approval_gate.validate(
                approval_id,
                expected_operation_id=operation_id,
                expected_repository_id=record.repository_id,
                expected_operation="create_change_request",
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

        # --- Consume the approval -------------------------------------
        try:
            approval_gate.consume(
                approval_id,
                expected_operation_id=operation_id,
                expected_repository_id=record.repository_id,
                expected_operation="create_change_request",
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

        # --- Create the change request ---------------------------------
        try:
            change_request = await adapter.create_change_request(
                repo=repo,
                source_branch=source_branch,
                target_branch=target_branch,
                title=title,
                body=body,
                draft=draft,
                token=None,
            )
        except Exception as exc:  # noqa: BLE001
            normalized = to_source_control_error(exc)
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

        if not isinstance(change_request, ChangeRequestRef):
            return self.failure_result(
                error_code="PROVIDER_ERROR",
                error_message=(
                    "Provider returned an unexpected change-request "
                    "payload."
                ),
                metadata={"operation_id": operation_id},
            )

        # --- Attach the change request to the operation ----------------
        try:
            await operation_service.attach_change_request(
                operation_id, change_request
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Failed to attach change request to operation '%s': %s",
                operation_id,
                type(exc).__name__,
            )

        # --- Transition to CHANGE_REQUEST_CREATED ----------------------
        try:
            await operation_service.transition(
                operation_id,
                OperationStatus.CHANGE_REQUEST_CREATED,
                reason="Change request created successfully.",
                metadata={
                    "number_or_iid": change_request.number_or_iid,
                    "web_url": change_request.web_url,
                },
                actor="system",
            )
        except Exception as exc:  # noqa: BLE001
            # The change request exists; the state transition is
            # informational. We log and continue.
            logger.warning(
                "Change request created but transition failed for "
                "'%s': %s",
                operation_id,
                type(exc).__name__,
            )

        return self.success_result(
            {
                "operation_id": operation_id,
                "status": "created",
                "number_or_iid": change_request.number_or_iid,
                "web_url": change_request.web_url,
                "state": change_request.state.value,
                "is_draft": change_request.is_draft,
                "source_branch": change_request.source_branch,
                "target_branch": change_request.target_branch,
            },
            metadata={
                "operation_id": operation_id,
                "provider": record.provider.value,
                "draft": draft,
            },
        )

    # --- Internal: repository resolution -------------------------------

    async def _resolve_repository(
        self,
        *,
        adapter: Any,
        record: Any,
    ) -> Optional[Any]:
        """
        Resolve the `RepositoryRef` for the operation.

        The operation carries only the stable repository ID. The
        adapter's `create_change_request` expects a fully populated
        `RepositoryRef`. We therefore list repositories and match by
        ID. This mirrors the fallback used by the other tools and
        keeps the tool provider-neutral until the protocol exposes a
        direct by-ID lookup.
        """
        from ..domain.models import RepositoryRef

        try:
            listing = await adapter.list_repositories(
                page=1, page_size=100, token=None
            )
        except Exception:  # noqa: BLE001
            return None

        for repo in listing:
            if (
                isinstance(repo, RepositoryRef)
                and repo.repository_id == record.repository_id
            ):
                return repo
        return None
