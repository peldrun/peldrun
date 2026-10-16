from __future__ import annotations

from typing import Any, Optional, Sequence
from fastapi import APIRouter, Depends, HTTPException, Query, status

# Resilient imports for source control subsystem
try:
    from ...integrations.source_control.errors import (
        AuthExpiredError,
        AuthRequiredError,
        ConflictError,
        NotFoundError,
        PermissionDeniedError,
        RateLimitedError,
        RemoteStateChangedError,
        SourceControlError,
        ValidationError,
    )
    from ...integrations.source_control.schemas import (
        ActionSuccessResponse,
        ApprovalTicketResponse,
        ApproveOperationRequest,
        AuditLogEntryResponse,
        BranchResponse,
        ChangeRequestResponse,
        CheckSummaryResponse,
        ConnectBeginRequest,
        ConnectBeginResponse,
        ConnectionStatusResponse,
        CreateBranchRequest,
        CreateChangeRequestPayload,
        CreateOperationRequest,
        CreateWorkspaceRequest,
        DiffSummaryResponse,
        DisconnectRequest,
        FileReadResponse,
        OperationResponse,
        ProviderDescriptorResponse,
        PushBranchRequest,
        PushBranchResponse,
        RejectOperationRequest,
        RepositoryResponse,
        RunChecksRequest,
        ValidateConnectionRequest,
        WorkspaceResponse,
    )
except (ImportError, ValueError):
    from backend.integrations.source_control.errors import (
        AuthExpiredError,
        AuthRequiredError,
        ConflictError,
        NotFoundError,
        PermissionDeniedError,
        RateLimitedError,
        RemoteStateChangedError,
        SourceControlError,
        ValidationError,
    )
    from backend.integrations.source_control.schemas import (
        ActionSuccessResponse,
        ApprovalTicketResponse,
        ApproveOperationRequest,
        AuditLogEntryResponse,
        BranchResponse,
        ChangeRequestResponse,
        CheckSummaryResponse,
        ConnectBeginRequest,
        ConnectBeginResponse,
        ConnectionStatusResponse,
        CreateBranchRequest,
        CreateChangeRequestPayload,
        CreateOperationRequest,
        CreateWorkspaceRequest,
        DiffSummaryResponse,
        DisconnectRequest,
        FileReadResponse,
        OperationResponse,
        ProviderDescriptorResponse,
        PushBranchRequest,
        PushBranchResponse,
        RejectOperationRequest,
        RepositoryResponse,
        RunChecksRequest,
        ValidateConnectionRequest,
        WorkspaceResponse,
    )

# Facade Dependency injection helper
try:
    from ..services.source_control_facade import (
        SourceControlFacade,
        get_source_control_facade,
    )
except (ImportError, ValueError):
    try:
        from backend.omweb.services.source_control_facade import (
            SourceControlFacade,
            get_source_control_facade,
        )
    except (ImportError, ValueError):
        class SourceControlFacade:  # type: ignore[no-redef]
            pass

        def get_source_control_facade() -> Any:
            raise NotImplementedError("SourceControlFacade service is not yet loaded.")


router = APIRouter(
    prefix="/api/source-control",
    tags=["Source Control Integration"],
)


def handle_source_control_error(exc: Exception) -> None:
    """Translates normalized domain errors into structured FastAPI HTTPExceptions."""
    if isinstance(exc, NotFoundError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )
    elif isinstance(exc, (AuthRequiredError, AuthExpiredError)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        )
    elif isinstance(exc, PermissionDeniedError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        )
    elif isinstance(exc, ConflictError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )
    elif isinstance(exc, ValidationError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
    elif isinstance(exc, RateLimitedError):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
        )
    elif isinstance(exc, RemoteStateChangedError):
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail=str(exc),
        )
    elif isinstance(exc, SourceControlError):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        )
    raise exc


# ---------------------------------------------------------------------------
# 1. Providers & Connection Management
# ---------------------------------------------------------------------------


@router.get("/providers", response_model=Sequence[ProviderDescriptorResponse])
async def list_providers(
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> Sequence[ProviderDescriptorResponse]:
    """Lists all registered source control providers and their technical capabilities."""
    try:
        return await facade.list_providers()
    except Exception as exc:
        handle_source_control_error(exc)
        return ()


@router.get("/connections", response_model=Sequence[ConnectionStatusResponse])
async def list_connections(
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> Sequence[ConnectionStatusResponse]:
    """Lists the connection and authentication statuses for all supported providers."""
    try:
        return await facade.list_connections()
    except Exception as exc:
        handle_source_control_error(exc)
        return ()


@router.post("/connections/{provider}/begin", response_model=ConnectBeginResponse)
async def begin_connection(
    provider: str,
    payload: ConnectBeginRequest,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> ConnectBeginResponse:
    """Initiates an OAuth 2.0 PKCE authentication handshake for the specified provider."""
    try:
        return await facade.begin_connection(provider, payload)
    except Exception as exc:
        handle_source_control_error(exc)
        raise


@router.get("/oauth/callback/{provider}", response_model=ActionSuccessResponse)
async def oauth_callback(
    provider: str,
    code: str = Query(..., description="Authorization code returned by upstream provider."),
    state: str = Query(..., description="CSRF state parameter matching session."),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> ActionSuccessResponse:
    """Handles OAuth 2.0 PKCE callback and securely exchanges code for ephemeral tokens."""
    try:
        return await facade.complete_oauth_callback(provider, code=code, state=state)
    except Exception as exc:
        handle_source_control_error(exc)
        raise


@router.post("/connections/{provider}/validate", response_model=ConnectionStatusResponse)
async def validate_connection(
    provider: str,
    payload: Optional[ValidateConnectionRequest] = None,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> ConnectionStatusResponse:
    """Introspects and validates an existing provider connection and updates status."""
    try:
        req = payload or ValidateConnectionRequest()
        return await facade.validate_connection(provider, req)
    except Exception as exc:
        handle_source_control_error(exc)
        raise


@router.delete("/connections/{provider}", response_model=ActionSuccessResponse)
async def disconnect_provider(
    provider: str,
    payload: Optional[DisconnectRequest] = None,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> ActionSuccessResponse:
    """Disconnects provider, purges credentials from vault, and optionally revokes token."""
    try:
        req = payload or DisconnectRequest()
        return await facade.disconnect_provider(provider, req)
    except Exception as exc:
        handle_source_control_error(exc)
        raise


# ---------------------------------------------------------------------------
# 2. Repositories, Branches & Remote Inspection
# ---------------------------------------------------------------------------


@router.get("/repositories", response_model=Sequence[RepositoryResponse])
async def list_repositories(
    provider: Optional[str] = Query(default=None, description="Optional provider filter."),
    query: Optional[str] = Query(default=None, description="Optional keyword search query."),
    limit: int = Query(default=30, ge=1, le=100, description="Maximum repositories to return."),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> Sequence[RepositoryResponse]:
    """Lists repositories accessible to the authenticated user across connected providers."""
    try:
        return await facade.list_repositories(provider=provider, query=query, limit=limit)
    except Exception as exc:
        handle_source_control_error(exc)
        return ()


@router.get("/repositories/{repository_id:path}/branches", response_model=Sequence[BranchResponse])
async def list_branches(
    repository_id: str,
    provider: Optional[str] = Query(default="github"),
    limit: int = Query(default=50, ge=1, le=100),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> Sequence[BranchResponse]:
    """Lists branches for a given repository."""
    try:
        return await facade.list_branches(repository_id=repository_id, provider=provider, limit=limit)
    except Exception as exc:
        handle_source_control_error(exc)
        return ()


@router.post("/repositories/{repository_id:path}/branches", response_model=BranchResponse)
async def create_branch(
    repository_id: str,
    payload: CreateBranchRequest,
    provider: Optional[str] = Query(default="github"),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> BranchResponse:
    """Creates a new branch on the remote repository pointing to the specified commit SHA."""
    try:
        return await facade.create_branch(repository_id=repository_id, payload=payload, provider=provider)
    except Exception as exc:
        handle_source_control_error(exc)
        raise


@router.get("/repositories/{repository_id:path}/file", response_model=FileReadResponse)
async def read_file(
    repository_id: str,
    file_path: str = Query(..., description="Relative file path within repository."),
    ref: Optional[str] = Query(default=None, description="Commit, branch, or tag reference."),
    provider: Optional[str] = Query(default="github"),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> FileReadResponse:
    """Reads a remote file at the specified revision with binary and size protection."""
    try:
        return await facade.read_file(
            repository_id=repository_id, file_path=file_path, ref=ref, provider=provider
        )
    except Exception as exc:
        handle_source_control_error(exc)
        raise


@router.get("/repositories/{repository_id:path}", response_model=RepositoryResponse)
async def get_repository(
    repository_id: str,
    provider: Optional[str] = Query(default="github"),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> RepositoryResponse:
    """Retrieves metadata for a specific repository by numeric ID or slug."""
    try:
        return await facade.get_repository(repository_id=repository_id, provider=provider)
    except Exception as exc:
        handle_source_control_error(exc)
        raise


# ---------------------------------------------------------------------------
# 3. Workspaces, Diffs & CI Checks
# ---------------------------------------------------------------------------


@router.post("/workspaces", response_model=WorkspaceResponse)
async def create_workspace(
    payload: CreateWorkspaceRequest,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> WorkspaceResponse:
    """Initializes an isolated local Git workspace or worktree for agent tasks."""
    try:
        return await facade.create_workspace(payload)
    except Exception as exc:
        handle_source_control_error(exc)
        raise


@router.get("/workspaces/{workspace_id}", response_model=WorkspaceResponse)
async def get_workspace(
    workspace_id: str,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> WorkspaceResponse:
    """Retrieves status and metadata of an isolated local Git workspace."""
    try:
        return await facade.get_workspace(workspace_id)
    except Exception as exc:
        handle_source_control_error(exc)
        raise


@router.get("/workspaces/{workspace_id}/diff", response_model=DiffSummaryResponse)
async def inspect_workspace_diff(
    workspace_id: str,
    base_ref: Optional[str] = Query(default=None, description="Base ref to compare against."),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> DiffSummaryResponse:
    """Computes unified diff, detects CI workflow edits, and computes cryptographic diff SHA-256."""
    try:
        return await facade.inspect_workspace_diff(workspace_id=workspace_id, base_ref=base_ref)
    except Exception as exc:
        handle_source_control_error(exc)
        raise


@router.post("/workspaces/{workspace_id}/checks", response_model=CheckSummaryResponse)
async def run_checks(
    workspace_id: str,
    payload: Optional[RunChecksRequest] = None,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> CheckSummaryResponse:
    """Inspects commit statuses and CI check runs for the active workspace commit."""
    try:
        req = payload or RunChecksRequest()
        return await facade.run_checks(workspace_id=workspace_id, payload=req)
    except Exception as exc:
        handle_source_control_error(exc)
        raise


# ---------------------------------------------------------------------------
# 4. Durable Operations, Approvals & Remote Execution
# ---------------------------------------------------------------------------


@router.post("/operations", response_model=OperationResponse)
async def create_operation(
    payload: CreateOperationRequest,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> OperationResponse:
    """Registers a new durable source control workflow operation."""
    try:
        return await facade.create_operation(payload)
    except Exception as exc:
        handle_source_control_error(exc)
        raise


@router.get("/operations/{operation_id}", response_model=OperationResponse)
async def get_operation(
    operation_id: str,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> OperationResponse:
    """Retrieves state, history, and timeline events of a durable workflow operation."""
    try:
        return await facade.get_operation(operation_id)
    except Exception as exc:
        handle_source_control_error(exc)
        raise


@router.post("/operations/{operation_id}/approval", response_model=ApprovalTicketResponse)
async def request_operation_approval(
    operation_id: str,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> ApprovalTicketResponse:
    """Computes cryptographic diff SHA-256 and freezes proposal for explicit human review."""
    try:
        return await facade.request_operation_approval(operation_id)
    except Exception as exc:
        handle_source_control_error(exc)
        raise


@router.post("/operations/{operation_id}/approve", response_model=ApprovalTicketResponse)
async def approve_operation(
    operation_id: str,
    payload: ApproveOperationRequest,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> ApprovalTicketResponse:
    """Explicit human approval endpoint granting single-use authorization for remote writes."""
    try:
        return await facade.approve_operation(operation_id, payload)
    except Exception as exc:
        handle_source_control_error(exc)
        raise


@router.post("/operations/{operation_id}/reject", response_model=ActionSuccessResponse)
async def reject_operation(
    operation_id: str,
    payload: RejectOperationRequest,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> ActionSuccessResponse:
    """Explicit human rejection endpoint invalidating proposal and recording audit explanation."""
    try:
        return await facade.reject_operation(operation_id, payload)
    except Exception as exc:
        handle_source_control_error(exc)
        raise


@router.post("/operations/{operation_id}/push", response_model=PushBranchResponse)
async def push_approved_branch(
    operation_id: str,
    payload: PushBranchRequest,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> PushBranchResponse:
    """Executes authenticated git push after verifying single-use approval ticket and diff integrity."""
    try:
        return await facade.push_approved_branch(operation_id, payload)
    except Exception as exc:
        handle_source_control_error(exc)
        raise


@router.post("/operations/{operation_id}/change-request", response_model=ChangeRequestResponse)
async def create_change_request(
    operation_id: str,
    payload: CreateChangeRequestPayload,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> ChangeRequestResponse:
    """Opens a remote Pull Request or Merge Request on the source control provider."""
    try:
        return await facade.create_change_request(operation_id, payload)
    except Exception as exc:
        handle_source_control_error(exc)
        raise


# ---------------------------------------------------------------------------
# 5. Security Audit Log
# ---------------------------------------------------------------------------


@router.get("/audit", response_model=Sequence[AuditLogEntryResponse])
async def list_audit_events(
    limit: int = Query(default=100, ge=1, le=500, description="Max audit entries."),
    operation_id: Optional[str] = Query(default=None, description="Filter by operation ID."),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> Sequence[AuditLogEntryResponse]:
    """Retrieves immutable append-only security audit log entries."""
    try:
        return await facade.list_audit_events(limit=limit, operation_id=operation_id)
    except Exception as exc:
        handle_source_control_error(exc)
        return ()
