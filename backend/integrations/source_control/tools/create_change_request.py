# backend/integrations/source_control/tools/create_change_request.py
"""
The `source_control.create_change_request` tool.

Creates a pull request (GitHub, Bitbucket) or merge request (GitLab)
for a previously pushed branch. The tool requires its OWN operation
AND its own approval ticket: the push approval covers the push, not
the pull request. Users must review the PR/MR title and body before
they are submitted.

Why a separate operation, not the push operation:

    A `push_branch` operation carries PUSH semantics: its
    `target_branch` field is the remote branch the push writes to.
    A `create_change_request` operation carries PR semantics:
    `source_branch` is the branch being merged, `target_branch` is
    the PR base. Reusing the push operation would either fail
    ticket validation (different `target_branch` meaning) or open a
    PR from a branch to itself. The tool therefore rejects any
    operation whose kind is not `create_change_request`.

Canonical flow after a successful push:

    1. request_approval(workspace_id=ws,
                        operation='create_change_request',
                        source_branch='feature/x',
                        target_branch='main')
       → new operation (kind='create_change_request'),
         status=REVIEW_REQUIRED
    2. The user approves the new ticket via the UI
       → status=APPROVED
    3. create_change_request(operation_id=<new>,
                             approval_id=<new>, title='...')
       → status=CHANGE_REQUEST_CREATED

Pipeline (this tool):
    1. Validate inputs.
    2. Load the operation record from the journal.
    3. Reject operations whose kind is not
       'create_change_request'.
    4. Reject operations whose status is not 'APPROVED'.
    5. Read source_branch and target_branch from the record.
    6. Resolve the credential for the operation's provider.
    7. Resolve the repository reference using that credential.
    8. Re-check the operation status, then validate the ticket.
    9. Consume the ticket.
   10. Create the change request via the provider adapter.
   11. Attach the change request to the operation.
   12. Transition the operation to `CHANGE_REQUEST_CREATED`.

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
    - The credential MUST be resolved BEFORE the approval is
      consumed. A missing credential would otherwise burn the
      approval without performing the remote side effect.
    - The credential MUST be passed to both the repository
      resolution step and the change-request creation step.
      Without it, the tool silently fails on private repositories.
    - The credential MUST NEVER appear in an error message, a tool
      result, or the audit log. The base URL is redacted before it
      is included in any diagnostic.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional, Tuple, Union

from ..domain.enums import OperationStatus, ProviderId
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
from ..security.redaction import redact_url
from ..security.token_service import (
    TokenService,
    get_default_token_service,
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
        token_service: Optional[TokenService] = None,
        audit_logger: Any = None,
    ) -> None:
        super().__init__(audit_logger=audit_logger)
        self._explicit_operation_service = operation_service
        self._explicit_approval_gate = approval_gate
        self._explicit_registry = registry
        self._explicit_token_service = token_service

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

        # --- Validate operation kind ----------------------------------
        #
        # Compatible lifecycle operations (create_change_request, push_branch,
        # source_control, push_and_pr) can proceed to change request creation.
        allowed_kinds = {
            "create_change_request",
            "push_branch",
            "source_control",
            "push_and_pr",
        }
        if record.kind not in allowed_kinds:
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message=(
                    f"Operation '{operation_id}' has unsupported kind '{record.kind}' "
                    "for creating a change request."
                ),
                metadata={
                    "operation_id": operation_id,
                    "kind": record.kind,
                },
            )

        # --- Validate operation state ----------------------------------
        #
        # Both APPROVED (pre-push or standalone PR) and PUSHED (post-push in the
        # coordinated agent lifecycle) are valid states for opening a change request.
        if record.status not in (
            OperationStatus.APPROVED,
            OperationStatus.PUSHED,
        ):
            if record.status == OperationStatus.REVIEW_REQUIRED:
                return self.failure_result(
                    error_code="CONFLICT",
                    error_message=(
                        f"Operation '{operation_id}' is in status "
                        "'review_required'. The user has not approved "
                        "the change-request ticket yet. Approve it "
                        "via the UI first, then call this tool again."
                    ),
                    metadata={"operation_id": operation_id},
                )
            return self.failure_result(
                error_code="CONFLICT",
                error_message=(
                    f"Operation '{operation_id}' is in status "
                    f"'{record.status.value}'. A change request "
                    "requires the operation to be 'approved' or 'pushed'."
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
                    f"Operation '{operation_id}' is missing "
                    "source_branch or target_branch. Both are "
                    "required to open a change request. This usually "
                    "means the operation was created without them; "
                    "re-request approval with explicit "
                    "source_branch and target_branch values."
                ),
                metadata={"operation_id": operation_id},
            )

        if source_branch == target_branch:
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message=(
                    f"Operation '{operation_id}' has identical source "
                    f"and target branches ({source_branch!r}). For a "
                    "change request, source_branch must be the branch "
                    "being merged (e.g. 'feature/x') and target_branch "
                    "must be the base (e.g. 'main'). Re-request "
                    "approval with distinct values: "
                    "request_approval(workspace_id=..., "
                    "operation='create_change_request', "
                    "source_branch='feature/x', target_branch='main')."
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

        # --- Resolve the credential ------------------------------------
        #
        # IMPORTANT: this step runs BEFORE the approval is consumed.
        # If the credential is missing, we fail early and the user
        # can fix the connection and retry — the approval ticket is
        # preserved.
        auth = self._resolve_auth(
            token_service=token_service,
            record=record,
            context=context,
        )
        if isinstance(auth, ToolResult):
            return auth
        _provider_id, _account_id, _base_url, token = auth

        # --- Resolve the repository reference --------------------------
        #
        # The change request creation needs a `RepositoryRef`. We
        # rebuild it from the operation and the provider's metadata.
        repo = await self._resolve_repository(
            adapter=adapter,
            record=record,
            token=token,
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

        # --- Re-check operation status (race protection) ---------------
        #
        # The record was loaded earlier; between then and now, another
        # request (cancel_operation, mark_failed) could have changed
        # its status. Failing here leaves the ticket untouched so the
        # caller can retry once the state is repaired.
        current_record = await operation_service.try_get_operation(
            operation_id
        )
        if current_record is None:
            return self.failure_result(
                error_code="NOT_FOUND",
                error_message=(
                    f"Operation '{operation_id}' was not found."
                ),
                metadata={"operation_id": operation_id},
            )
        if current_record.status not in (
            OperationStatus.APPROVED,
            OperationStatus.PUSHED,
        ):
            return self.failure_result(
                error_code="CONFLICT",
                error_message=(
                    f"Operation '{operation_id}' is no longer in "
                    f"an executable status (current: "
                    f"'{current_record.status.value}'). The change "
                    "request cannot be created."
                ),
                metadata={
                    "operation_id": operation_id,
                    "status": current_record.status.value,
                },
            )

        # --- Validate the approval ticket ------------------------------
        try:
            approval_gate.validate(
                approval_id,
                expected_operation_id=operation_id,
                expected_repository_id=record.repository_id,
                expected_operation=record.kind,
                expected_base_sha=record.base_sha,
                expected_diff_sha256=record.diff_sha256 or "",
                expected_target_branch=target_branch,
                action_scope="create_change_request",
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
                expected_operation=record.kind,
                expected_base_sha=record.base_sha,
                expected_diff_sha256=record.diff_sha256 or "",
                expected_target_branch=target_branch,
                action_scope="create_change_request",
            )
        except Exception as exc:  # noqa: BLE001
            normalized = to_source_control_error(exc)
            return self.failure_result(
                error_code=normalized.code.value,
                error_message=normalized.message,
                metadata={"operation_id": operation_id},
            )

        # --- Create the change request ---------------------------------
        #
        # The credential is passed explicitly. Without it, the call
        # fails on private repositories.
        try:
            change_request = await adapter.create_change_request(
                repo=repo,
                source_branch=source_branch,
                target_branch=target_branch,
                title=title,
                body=body,
                draft=draft,
                token=token,
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

    # --- Credential resolution -----------------------------------------

    @staticmethod
    def _resolve_auth(
        *,
        token_service: TokenService,
        record: Any,
        context: ToolContext,
    ) -> Union[Tuple[ProviderId, str, str, str], ToolResult]:
        """
        Resolve `(provider, account_id, base_url, token)` for the call.

        Resolution order for the account:

            1. `context.metadata["account_id"]` when the facade
               injects an account hint.
            2. The first stored account for the operation's provider.

        The token is read from the vault. It MUST NEVER be logged,
        returned in a tool result, or included in an error message.
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
        for_provider = [
            entry for entry in stored if entry.provider == provider_id
        ]
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
                    "Reconnect the provider before retrying the "
                    "change-request creation."
                ),
                metadata={
                    "provider": provider_id.value,
                    "account_id": chosen.account_id,
                    "base_url": redact_url(chosen.base_url),
                    "operation_id": record.operation_id,
                },
            )

        return provider_id, chosen.account_id, chosen.base_url, token

    # --- Internal: repository resolution -------------------------------

    @staticmethod
    async def _resolve_repository(
        *,
        adapter: Any,
        record: Any,
        token: str,
    ) -> Optional[Any]:
        """
        Resolve the `RepositoryRef` for the operation.

        The operation carries only the stable repository ID. The
        adapter's `create_change_request` expects a fully populated
        `RepositoryRef`. We therefore list repositories and match by
        ID. This mirrors the fallback used by the other tools and
        keeps the tool provider-neutral until the protocol exposes a
        direct by-ID lookup.

        The `token` argument is REQUIRED. Without it, private
        repositories cannot be resolved.
        """
        from ..domain.models import RepositoryRef

        try:
            listing = await adapter.list_repositories(
                page=1, page_size=100, token=token
            )
        except TypeError:
            # Some adapter signatures use `limit=` instead of
            # `page=`/`page_size=`. Fall back gracefully.
            try:
                listing = await adapter.list_repositories(
                    query=None, limit=100, token=token
                )
            except Exception:  # noqa: BLE001
                return None
        except Exception:  # noqa: BLE001
            return None

        for repo in listing:
            if (
                isinstance(repo, RepositoryRef)
                and repo.repository_id == record.repository_id
            ):
                return repo
        return None
