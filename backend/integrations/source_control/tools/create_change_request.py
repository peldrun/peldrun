from __future__ import annotations

from typing import Any, Mapping, Optional
from pydantic import BaseModel, Field

from ..domain.enums import ProviderId
from ..domain.models import ChangeRequestRef, RepositoryRef
from ..errors import (
    AuthRequiredError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    SourceControlError,
    ValidationError,
)
from ..git.path_policy import validate_branch_name
from ..registry import ProviderRegistry, get_default_registry
from ..security.approval import (
    ApprovalGate,
    ApprovalTicket,
    get_default_approval_gate,
)
from ..security.policy_engine import PolicyEngine, get_default_policy_engine
from ..security.redaction import redact_text, redact_url
from ..security.token_service import TokenService, get_default_token_service

# Resilient import for operation service
try:
    from ..operations.service import OperationService, get_default_operation_service
except ImportError:
    OperationService = None  # type: ignore[assignment, misc]
    def get_default_operation_service() -> Any:
        return None


class CreateChangeRequestArgs(BaseModel):
    """Input argument schema for the create change request tool."""

    repository_id: str = Field(
        ...,
        description="Target repository identifier (numeric ID or 'owner/repo' slug).",
    )
    source_branch: str = Field(
        ...,
        description="Head/feature branch containing the pushed changes (e.g., 'feature/my-task').",
    )
    target_branch: str = Field(
        ...,
        description="Base branch to merge changes into (e.g., 'main' or 'develop').",
    )
    title: str = Field(
        ...,
        description="Descriptive title for the pull/merge request.",
    )
    body: Optional[str] = Field(
        default="",
        description="Detailed description or markdown rationale for the pull/merge request.",
    )
    draft: bool = Field(
        default=True,
        description="Whether to open the change request as a draft. Defaults to True for safety.",
    )
    approval_id: Optional[str] = Field(
        default=None,
        description="Optional cryptographic approval ticket ID if required by current policy.",
    )
    operation_id: Optional[str] = Field(
        default=None,
        description="Optional durable operation identifier to bind this change request to.",
    )
    provider: Optional[str] = Field(
        default="github",
        description="Source control provider identifier (e.g., 'github'). Defaults to 'github'.",
    )


class CreateChangeRequestTool:
    """Agent tool that opens a remote Pull Request or Merge Request on the source control provider."""

    name: str = "source_control_create_change_request"
    description: str = (
        "Creates a remote pull request or merge request from a pushed feature branch into a target base branch. "
        "Enforces policy validation, secret redaction on titles and descriptions, and single-use approval verification. "
        "Never requires or accepts raw credentials."
    )
    args_schema: type[BaseModel] = CreateChangeRequestArgs

    def __init__(
        self,
        registry: Optional[ProviderRegistry] = None,
        token_service: Optional[TokenService] = None,
        policy_engine: Optional[PolicyEngine] = None,
        approval_gate: Optional[ApprovalGate] = None,
        operation_service: Optional[Any] = None,
    ) -> None:
        self.registry = registry or get_default_registry()
        self.token_service = token_service or get_default_token_service()
        self.policy_engine = policy_engine or get_default_policy_engine()
        self.approval_gate = approval_gate or get_default_approval_gate()
        self.operation_service = operation_service or get_default_operation_service()

    async def _arun(
        self,
        repository_id: str,
        source_branch: str,
        target_branch: str,
        title: str,
        body: Optional[str] = "",
        draft: bool = True,
        approval_id: Optional[str] = None,
        operation_id: Optional[str] = None,
        provider: Optional[str] = "github",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Asynchronously creates a remote pull request with policy and approval enforcement."""
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

        # 2. Validate branch names
        clean_source = validate_branch_name(source_branch)
        clean_target = validate_branch_name(target_branch)

        if clean_source == clean_target:
            raise ValidationError(
                f"Source branch and target branch cannot be the same: '{clean_source}'."
            )

        clean_title = redact_text(title.strip()) if title else ""
        if not clean_title:
            raise ValidationError("Pull request title cannot be empty.")

        clean_body = redact_text(body.strip()) if body else ""

        # 3. Enforce operational policy
        allowed, reason = self.policy_engine.is_operation_allowed(
            operation="create_change_request",
            provider=provider_id.value,
            repository=clean_repo_id,
            target_branch=clean_target,
        )
        if not allowed:
            raise PermissionDeniedError(
                f"Policy prohibits creating pull request into '{clean_target}': {reason}",
                details={
                    "provider": provider_id.value,
                    "repository_id": clean_repo_id,
                    "target_branch": clean_target,
                    "reason": reason,
                },
            )

        # 4. Optional approval ticket consumption if provided
        if approval_id and approval_id.strip():
            clean_appr_id = approval_id.strip()
            try:
                ticket: ApprovalTicket = self.approval_gate.consume_approval(clean_appr_id)
                if ticket.operation not in {"create_change_request", "push_branch"}:
                    raise ValidationError(
                        f"Approval ticket '{clean_appr_id}' was granted for '{ticket.operation}', "
                        "not 'create_change_request'."
                    )
            except PermissionDeniedError:
                raise
            except Exception as exc:
                raise PermissionDeniedError(
                    f"Failed to validate approval ticket '{clean_appr_id}': {exc}"
                ) from exc

        # 5. Retrieve ephemeral provider access token
        token = await self.token_service.get_access_token(provider_id)
        if not token:
            raise AuthRequiredError(
                f"No active credentials found for '{provider_id.value}'. Please connect your account first in Settings.",
                details={"provider": provider_id.value},
            )

        # 6. Resolve provider adapter and repository metadata
        adapter = self.registry.get(provider_id.value)
        try:
            repo: RepositoryRef = await adapter.get_repository(clean_repo_id, token=token)
        except SourceControlError:
            raise
        except Exception as exc:
            raise SourceControlError(
                f"Failed to resolve repository '{clean_repo_id}': {exc}"
            ) from exc

        # 7. Execute remote change request creation
        try:
            change_request: ChangeRequestRef = await adapter.create_change_request(
                repo=repo,
                source_branch=clean_source,
                target_branch=clean_target,
                title=clean_title,
                body=clean_body,
                draft=draft,
                token=token,
            )
        except ConflictError:
            raise
        except SourceControlError:
            raise
        except Exception as exc:
            raise SourceControlError(
                f"Failed to create change request in '{repo.namespace}/{repo.name}': {exc}"
            ) from exc

        # 8. Transition durable operation if wired
        bound_op_id = operation_id or (approval_id if approval_id else None)
        if self.operation_service and bound_op_id:
            try:
                from ..operations.models import OperationState

                await self.operation_service.transition_state(
                    operation_id=bound_op_id,
                    target_state=OperationState.CHANGE_REQUEST_CREATED,
                    metadata={
                        "change_request_number": change_request.number_or_iid,
                        "change_request_url": change_request.web_url,
                        "source_branch": clean_source,
                        "target_branch": clean_target,
                        "state": change_request.state,
                    },
                )
            except Exception:
                pass

        return {
            "status": "success",
            "provider": provider_id.value,
            "repository_id": repo.repository_id,
            "full_name": f"{repo.namespace}/{repo.name}",
            "change_request_number": change_request.number_or_iid,
            "web_url": redact_url(change_request.web_url),
            "state": change_request.state,
            "source_branch": change_request.source_branch,
            "target_branch": change_request.target_branch,
            "draft": draft,
            "message": (
                f"Successfully opened pull request #{change_request.number_or_iid} "
                f"('{clean_source}' -> '{clean_target}') on {provider_id.value.title()}."
            ),
        }

    async def aexecute(self, **kwargs: Any) -> dict[str, Any]:
        """Public async execution adapter conforming to standard agent tool calling contracts."""
        validated_args = self.args_schema(**kwargs)
        return await self._arun(**validated_args.model_dump())

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Synchronous wrapper raising an error to enforce non-blocking execution."""
        raise NotImplementedError(
            "CreateChangeRequestTool only supports asynchronous execution via aexecute() or _arun()."
        )
