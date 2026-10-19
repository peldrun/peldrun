# backend/omweb/routers/source_control.py

"""
HTTP router for the Source Control Integration subsystem.

AUTHENTICATION MODEL:
    This router does NOT enforce authentication at the FastAPI
    layer. PELDRUN is designed as a local-first application, and the
    expected deployment is one of:

        - A single-user desktop installation behind the operating
          system's user boundary.
        - A local reverse proxy that terminates TLS and performs
          authentication BEFORE forwarding to this router.

    Deployments that expose this router to a network MUST mount it
    behind an authenticating reverse proxy (or add a
    `Depends(get_current_user)` to every endpoint). The router
    itself only enforces DOMAIN authorization: the approval gate and
    the policy engine decide whether a specific operation is allowed
    for the target repository.

ERROR MODEL:
    Every `SourceControlError` subclass carries its own
    `http_status` hint in `errors.py`. This router delegates to that
    hint via `handle_source_control_error` so that adding a new
    error subclass automatically produces the correct HTTP response
    without requiring a change here. Do NOT add a hard-coded status
    in this module for a specific error class; if a specific
    endpoint needs a different status, wrap the exception in an
    `HTTPException` at the endpoint level.

RATE LIMITING / LONG OPERATIONS:
    This router does not implement rate limiting or background
    execution. Push operations can take up to ~120 seconds; callers
    SHOULD be prepared for slow responses, and deployments SHOULD
    configure the reverse proxy's timeout accordingly. A future
    revision may add `BackgroundTasks` for the two slowest endpoints
    (`push_approved_branch`, `create_change_request`); doing so
    requires an API contract change (task id + polling endpoint),
    so it is intentionally out of scope for the current revision.
"""

from __future__ import annotations

import logging
from typing import Any, NoReturn, Optional, Sequence

from fastapi import APIRouter, Depends, HTTPException, Query, status, Request
from fastapi.responses import HTMLResponse

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
        SaveProviderConfigRequest,
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
        SaveProviderConfigRequest,
        ValidateConnectionRequest,
        WorkspaceResponse,
    )

# Facade dependency injection helper
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
            raise NotImplementedError(
                "SourceControlFacade service is not yet loaded."
            )


logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/api/source-control",
    tags=["Source Control Integration"],
)


def handle_source_control_error(exc: Exception) -> NoReturn:
    """
    Translate a source-control domain error into a structured FastAPI
    HTTPException.

    The mapping is delegated to `SourceControlError.http_status`,
    which every error subclass declares as a class-level default in
    `errors.py`. This keeps the router and the error taxonomy in
    sync: adding a new error class with a specific HTTP status only
    requires editing `errors.py`, not this router.

    The previous implementation hard-coded an `isinstance` chain for
    a small subset of error classes and mapped everything else to
    502. That silently misclassified errors whose `errors.py`
    declaration differs from 502:

        - `ApprovalRequiredError`      -> 428 (was 502)
        - `ApprovalInvalidError`       -> 409 (was 502)
        - `ApprovalExpiredError`       -> 410 (was 502)
        - `ApprovalConsumedError`      -> 409 (was 502)
        - `PolicyDeniedError`          -> 403 (was 502)
        - `ProtectedBranchError`       -> 403 (was 502)
        - `WorkflowFileError`          -> 428 (was 502)
        - `WorkspaceNotFoundError`     -> 404 (was 502)
        - `WorkspaceDirtyError`        -> 409 (was 502)
        - `PathTraversalError`         -> 400 (was 502)
        - `CommandTimeoutError`        -> 504 (was 502)
        - `CommandFailedError`         -> 500 (was 502)
        - `UnsupportedCapabilityError` -> 501 (was 502)
        - `RemoteStateChangedError`    -> 409 (was 412)

    The two most visible consequences of the old mapping were:

        - A `PolicyDeniedError` (for example, a push to a protected
          branch) reached the UI as "Bad Gateway", which suggested a
          server fault rather than a policy refusal.

        - A `RemoteStateChangedError` returned 412 Precondition
          Failed, contradicting its own `default_http_status = 409`
          in `errors.py`. Two clients relying on different documents
          would disagree about the meaning.

    This function never returns; it always raises.
    """
    # HTTPException must never be double-wrapped; let FastAPI handle it.
    if isinstance(exc, HTTPException):
        raise exc

    if isinstance(exc, SourceControlError):
        # `http_status` is set in the base constructor from the
        # class-level `default_http_status`, so every instance carries
        # an int. Fall back to 500 defensively for exotic cases
        # (e.g. a subclass that forgot to set a default and somehow
        # bypassed the constructor).
        http_status = getattr(exc, "http_status", None) or 500
        # `str(exc)` is safe: the message was redacted by the base
        # constructor (`errors.py::SourceControlError.__init__` runs
        # `redact_message` before `Exception.__init__`), and the
        # `details` mapping is not surfaced to the client.
        raise HTTPException(
            status_code=http_status,
            detail=str(exc),
        )

    # Unknown exception: log with full stack trace and return a generic
    # 500. Never expose internal details to the client.
    logger.exception(
        "Unhandled exception in source-control router: %s",
        type(exc).__name__,
    )
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Internal server error",
    )


# ---------------------------------------------------------------------------
# 1. Providers & Connection Management
# ---------------------------------------------------------------------------


@router.get("/providers", response_model=Sequence[ProviderDescriptorResponse])
async def list_providers(
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> Sequence[ProviderDescriptorResponse]:
    """Lists all registered source control providers and their capabilities."""
    try:
        return await facade.list_providers()
    except Exception as exc:
        handle_source_control_error(exc)


@router.get("/connections/{provider}/redirect-uri")
async def get_redirect_uri(
    provider: str,
    origin: Optional[str] = Query(default=None, description="The frontend origin (e.g., http://localhost:3000)"),
    request: Request = None,
):
    """Dynamically returns the OAuth callback URI for the frontend."""
    if origin:
        base_url = origin.rstrip("/")
    else:
        # Fallback to the request headers if origin is not explicitly provided
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
        base_url = f"{scheme}://{host}"

    return {
        "redirect_uri": f"{base_url}/api/source-control/oauth/callback/{provider}"
    }

@router.get("/connections", response_model=Sequence[ConnectionStatusResponse])
async def list_connections(
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> Sequence[ConnectionStatusResponse]:
    """Lists the connection and authentication statuses for all providers."""
    try:
        return await facade.list_connections()
    except Exception as exc:
        handle_source_control_error(exc)


@router.post("/connections/{provider}/config", response_model=ActionSuccessResponse)
async def save_provider_config(
    provider: str,
    payload: SaveProviderConfigRequest,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> ActionSuccessResponse:
    """
    Saves custom OAuth App credentials (Client ID & Secret) for a provider.
    Required for local-first deployments where a central OAuth broker is not used.
    """
    try:
        return await facade.save_provider_config(provider, payload)
    except Exception as exc:
        handle_source_control_error(exc)


@router.get("/connections/{provider}/config")
async def get_provider_config(
    provider: str,
    facade: SourceControlFacade = Depends(get_source_control_facade),
):
    """Retrieve the configured Client ID (Secret is never returned)."""
    try:
        return await facade.get_provider_config(provider)
    except Exception as exc:
        handle_source_control_error(exc)


@router.delete("/connections/{provider}/config", response_model=ActionSuccessResponse)
async def delete_provider_config(
    provider: str,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> ActionSuccessResponse:
    """Delete both the Client ID and Client Secret for a provider."""
    try:
        return await facade.delete_provider_config(provider)
    except Exception as exc:
        handle_source_control_error(exc)


@router.post("/connections/{provider}/begin", response_model=ConnectBeginResponse)
async def begin_connection(
    provider: str,
    payload: ConnectBeginRequest,
    request: Request,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> ConnectBeginResponse:
    """Initiates an OAuth 2.0 PKCE authentication handshake."""
    try:
        # Construct the redirect URI based on the dynamically provided origin
        if payload.origin:
            base_url = payload.origin.rstrip("/")
        else:
            scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
            host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
            base_url = f"{scheme}://{host}"

        # oauth_flow strictly requires 127.0.0.1 instead of localhost
        if "localhost" in base_url:
            base_url = base_url.replace("localhost", "127.0.0.1")

        dynamic_redirect = f"{base_url}/api/source-control/oauth/callback/{provider}"

        # Pass the dynamically generated URI to the facade
        payload = payload.model_copy(update={"redirect_uri": dynamic_redirect})

        return await facade.begin_connection(provider, payload)
    except Exception as exc:
        handle_source_control_error(exc)


@router.get("/oauth/callback/{provider}", response_class=HTMLResponse)
async def oauth_callback(
    provider: str,
    code: str = Query(
        ..., description="Authorization code returned by upstream provider."
    ),
    state: str = Query(..., description="CSRF state parameter matching session."),
    facade: SourceControlFacade = Depends(get_source_control_facade),
):
    """Handles OAuth 2.0 PKCE callback and exchanges code for tokens."""
    try:
        result = await facade.complete_oauth_callback(
            provider, code=code, state=state
        )
        return f"""
        <html>
            <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                <h2 style="color: #16a34a;">Successfully Connected!</h2>
                <p>{result.message}</p>
                <p>You can close this window and return to PELDRUN.</p>
                <script>
                    setTimeout(() => window.close(), 2000);
                </script>
            </body>
        </html>
        """
    except Exception as exc:
        return f"""
        <html>
            <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                <h2 style="color: #dc2626;">Connection Failed</h2>
                <p>Please return to PELDRUN and try again.</p>
                <p style="color: gray; font-size: 12px;">{str(exc)}</p>
                <script>
                    setTimeout(() => window.close(), 5000);
                </script>
            </body>
        </html>
        """


@router.post(
    "/connections/{provider}/validate",
    response_model=ConnectionStatusResponse,
)
async def validate_connection(
    provider: str,
    payload: Optional[ValidateConnectionRequest] = None,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> ConnectionStatusResponse:
    """Introspects and validates an existing provider connection."""
    try:
        req = payload or ValidateConnectionRequest()
        return await facade.validate_connection(provider, req)
    except Exception as exc:
        handle_source_control_error(exc)


@router.delete("/connections/{provider}", response_model=ActionSuccessResponse)
async def disconnect_provider(
    provider: str,
    payload: Optional[DisconnectRequest] = None,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> ActionSuccessResponse:
    """Disconnects provider, purges credentials, and optionally revokes token."""
    try:
        req = payload or DisconnectRequest()
        return await facade.disconnect_provider(provider, req)
    except Exception as exc:
        handle_source_control_error(exc)


# ---------------------------------------------------------------------------
# 2. Repositories, Branches & Remote Inspection
# ---------------------------------------------------------------------------


@router.get("/repositories", response_model=Sequence[RepositoryResponse])
async def list_repositories(
    provider: Optional[str] = Query(
        default=None, description="Optional provider filter."
    ),
    query: Optional[str] = Query(
        default=None, description="Optional keyword search query."
    ),
    limit: int = Query(
        default=30,
        ge=1,
        le=100,
        description="Maximum repositories to return.",
    ),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> Sequence[RepositoryResponse]:
    """Lists repositories accessible to the authenticated user."""
    try:
        return await facade.list_repositories(
            provider=provider, query=query, limit=limit
        )
    except Exception as exc:
        handle_source_control_error(exc)


@router.get(
    "/repositories/{repository_id:path}/branches",
    response_model=Sequence[BranchResponse],
)
async def list_branches(
    repository_id: str,
    provider: Optional[str] = Query(default="github"),
    limit: int = Query(default=50, ge=1, le=100),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> Sequence[BranchResponse]:
    """Lists branches for a given repository."""
    try:
        return await facade.list_branches(
            repository_id=repository_id, provider=provider, limit=limit
        )
    except Exception as exc:
        handle_source_control_error(exc)


@router.post(
    "/repositories/{repository_id:path}/branches",
    response_model=BranchResponse,
)
async def create_branch(
    repository_id: str,
    payload: CreateBranchRequest,
    provider: Optional[str] = Query(default="github"),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> BranchResponse:
    """Creates a new branch on the remote repository."""
    try:
        return await facade.create_branch(
            repository_id=repository_id, payload=payload, provider=provider
        )
    except Exception as exc:
        handle_source_control_error(exc)


@router.get(
    "/repositories/{repository_id:path}/file",
    response_model=FileReadResponse,
)
async def read_file(
    repository_id: str,
    file_path: str = Query(
        ..., description="Relative file path within repository."
    ),
    ref: Optional[str] = Query(
        default=None, description="Commit, branch, or tag reference."
    ),
    provider: Optional[str] = Query(default="github"),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> FileReadResponse:
    """Reads a remote file at the specified revision."""
    try:
        return await facade.read_file(
            repository_id=repository_id,
            file_path=file_path,
            ref=ref,
            provider=provider,
        )
    except Exception as exc:
        handle_source_control_error(exc)


@router.get(
    "/repositories/{repository_id:path}",
    response_model=RepositoryResponse,
)
async def get_repository(
    repository_id: str,
    provider: Optional[str] = Query(default="github"),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> RepositoryResponse:
    """Retrieves metadata for a specific repository."""
    try:
        return await facade.get_repository(
            repository_id=repository_id, provider=provider
        )
    except Exception as exc:
        handle_source_control_error(exc)


# ---------------------------------------------------------------------------
# 3. Workspaces, Diffs & CI Checks
# ---------------------------------------------------------------------------


@router.post("/workspaces", response_model=WorkspaceResponse)
async def create_workspace(
    payload: CreateWorkspaceRequest,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> WorkspaceResponse:
    """Initializes an isolated local Git workspace for agent tasks."""
    try:
        return await facade.create_workspace(payload)
    except Exception as exc:
        handle_source_control_error(exc)


@router.get("/workspaces/{workspace_id}", response_model=WorkspaceResponse)
async def get_workspace(
    workspace_id: str,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> WorkspaceResponse:
    """Retrieves status and metadata of an isolated local workspace."""
    try:
        return await facade.get_workspace(workspace_id)
    except Exception as exc:
        handle_source_control_error(exc)


@router.get(
    "/workspaces/{workspace_id}/diff",
    response_model=DiffSummaryResponse,
)
async def inspect_workspace_diff(
    workspace_id: str,
    base_ref: Optional[str] = Query(
        default=None, description="Base ref to compare against."
    ),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> DiffSummaryResponse:
    """Computes unified diff and a cryptographic SHA-256 fingerprint."""
    try:
        return await facade.inspect_workspace_diff(
            workspace_id=workspace_id, base_ref=base_ref
        )
    except Exception as exc:
        handle_source_control_error(exc)


@router.post(
    "/workspaces/{workspace_id}/checks",
    response_model=CheckSummaryResponse,
)
async def run_checks(
    workspace_id: str,
    payload: Optional[RunChecksRequest] = None,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> CheckSummaryResponse:
    """Inspects commit statuses and CI check runs for a workspace."""
    try:
        req = payload or RunChecksRequest()
        return await facade.run_checks(workspace_id=workspace_id, payload=req)
    except Exception as exc:
        handle_source_control_error(exc)


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


@router.get("/operations/{operation_id}", response_model=OperationResponse)
async def get_operation(
    operation_id: str,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> OperationResponse:
    """Retrieves state, history, and timeline events of an operation."""
    try:
        return await facade.get_operation(operation_id)
    except Exception as exc:
        handle_source_control_error(exc)


@router.post(
    "/operations/{operation_id}/approval",
    response_model=ApprovalTicketResponse,
)
async def request_operation_approval(
    operation_id: str,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> ApprovalTicketResponse:
    """Computes diff SHA-256 and freezes proposal for human review."""
    try:
        return await facade.request_operation_approval(operation_id)
    except Exception as exc:
        handle_source_control_error(exc)


@router.post(
    "/operations/{operation_id}/approve",
    response_model=ApprovalTicketResponse,
)
async def approve_operation(
    operation_id: str,
    payload: ApproveOperationRequest,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> ApprovalTicketResponse:
    """Grants single-use authorization for remote writes."""
    try:
        return await facade.approve_operation(operation_id, payload)
    except Exception as exc:
        handle_source_control_error(exc)


@router.post(
    "/operations/{operation_id}/reject",
    response_model=ActionSuccessResponse,
)
async def reject_operation(
    operation_id: str,
    payload: RejectOperationRequest,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> ActionSuccessResponse:
    """Rejects a pending operation and records the audit explanation."""
    try:
        return await facade.reject_operation(operation_id, payload)
    except Exception as exc:
        handle_source_control_error(exc)


@router.post(
    "/operations/{operation_id}/push",
    response_model=PushBranchResponse,
)
async def push_approved_branch(
    operation_id: str,
    payload: PushBranchRequest,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> PushBranchResponse:
    """Executes git push after verifying single-use approval and diff integrity."""
    try:
        return await facade.push_approved_branch(operation_id, payload)
    except Exception as exc:
        handle_source_control_error(exc)


@router.post(
    "/operations/{operation_id}/change-request",
    response_model=ChangeRequestResponse,
)
async def create_change_request(
    operation_id: str,
    payload: CreateChangeRequestPayload,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> ChangeRequestResponse:
    """Opens a remote Pull Request or Merge Request on the provider."""
    try:
        return await facade.create_change_request(operation_id, payload)
    except Exception as exc:
        handle_source_control_error(exc)


# ---------------------------------------------------------------------------
# 5. Security Audit Log
# ---------------------------------------------------------------------------


@router.get("/audit", response_model=Sequence[AuditLogEntryResponse])
async def list_audit_events(
    limit: int = Query(default=100, ge=1, le=500, description="Max audit entries."),
    operation_id: Optional[str] = Query(
        default=None, description="Filter by operation ID."
    ),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> Sequence[AuditLogEntryResponse]:
    """Retrieves immutable append-only security audit log entries."""
    try:
        return await facade.list_audit_events(
            limit=limit, operation_id=operation_id
        )
    except Exception as exc:
        handle_source_control_error(exc)
