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
        CheckoutBranchRequest,
        CheckSummaryResponse,
        CommitEntryResponse,
        CommitWithStatsResponse,
        ConnectBeginRequest,
        ConnectBeginResponse,
        ConnectionStatusResponse,
        CreateBranchRequest,
        CreateChangeRequestPayload,
        CreateOperationRequest,
        CreateWorkspaceRequest,
        DiffSummaryResponse,
        DisconnectRequest,
        FileContentAtRefResponse,
        FileReadResponse,
        OperationResponse,
        ProviderDescriptorResponse,
        PushBranchRequest,
        PushBranchResponse,
        RejectOperationRequest,
        RepositoryResponse,
        RunChecksRequest,
        SaveProviderConfigRequest,
        TreeNodeResponse,
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
        CheckoutBranchRequest,
        CheckSummaryResponse,
        CommitEntryResponse,
        CommitWithStatsResponse,
        ConnectBeginRequest,
        ConnectBeginResponse,
        ConnectionStatusResponse,
        CreateBranchRequest,
        CreateChangeRequestPayload,
        CreateOperationRequest,
        CreateWorkspaceRequest,
        DiffSummaryResponse,
        DisconnectRequest,
        FileContentAtRefResponse,
        FileReadResponse,
        OperationResponse,
        ProviderDescriptorResponse,
        PushBranchRequest,
        PushBranchResponse,
        RejectOperationRequest,
        RepositoryResponse,
        RunChecksRequest,
        SaveProviderConfigRequest,
        TreeNodeResponse,
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


# Pull request schemas — Phase 2 additions.
#
# Why a separate try/except block and not an edit to the imports
# above: the module has two import paths (`from ...integrations`
# try and `from backend.integrations` fallback). Adding the new
# names into only one path would produce ImportError on the other.
# A self-contained block guarantees resolution on every path.
try:
    from ...integrations.source_control.schemas import (
        CreatePullRequestCommentRequest,
        CreatePullRequestRequest,
        MergePullRequestRequest,
        MergePullRequestResponse,
        PullRequestCommentResponse,
        PullRequestListResponse,
        PullRequestResponse,
        PullRequestReviewResponse,
        SubmitPullRequestReviewRequest,
    )
except (ImportError, ValueError):
    from backend.integrations.source_control.schemas import (
        CreatePullRequestCommentRequest,
        CreatePullRequestRequest,
        MergePullRequestRequest,
        MergePullRequestResponse,
        PullRequestCommentResponse,
        PullRequestListResponse,
        PullRequestResponse,
        PullRequestReviewResponse,
        SubmitPullRequestReviewRequest,
    )


# Issue schemas — Phase 3 additions. Self-contained try/except for
# the same reason the PR block above is: the module has two import
# paths, and adding the names to only one would break the other.
try:
    from ...integrations.source_control.schemas import (
        CreateIssueCommentRequest,
        CreateIssueRequest,
        IssueCommentResponse,
        IssueListResponse,
        IssueResponse,
        UpdateIssueStateRequest,
    )
except (ImportError, ValueError):
    from backend.integrations.source_control.schemas import (
        CreateIssueCommentRequest,
        CreateIssueRequest,
        IssueCommentResponse,
        IssueListResponse,
        IssueResponse,
        UpdateIssueStateRequest,
    )


# Merge / rebase / conflict schemas — Phase 4 additions. Same
# self-contained try/except pattern as the two blocks above.
try:
    from ...integrations.source_control.schemas import (
        ConflictListResponse,
        MergeBranchesRequest,
        MergeOperationResponse,
        RebaseBranchRequest,
        ResolveConflictsRequest,
        ResolveConflictsResponse,
    )
except (ImportError, ValueError):
    from backend.integrations.source_control.schemas import (
        ConflictListResponse,
        MergeBranchesRequest,
        MergeOperationResponse,
        RebaseBranchRequest,
        ResolveConflictsRequest,
        ResolveConflictsResponse,
    )


# Tag + release schemas — Phase 5 additions. Same self-contained
# try/except pattern as all the earlier blocks.
try:
    from ...integrations.source_control.schemas import (
        CreateReleaseRequest,
        CreateTagRequest,
        ReleaseListResponse,
        ReleaseResponse,
        SourceControlSettingsResponse,
        TagListResponse,
        TagResponse,
        UpdateSourceControlSettingsRequest,
    )
except (ImportError, ValueError):
    from backend.integrations.source_control.schemas import (
        CreateReleaseRequest,
        CreateTagRequest,
        ReleaseListResponse,
        ReleaseResponse,
        SourceControlSettingsResponse,
        TagListResponse,
        TagResponse,
        UpdateSourceControlSettingsRequest,
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

    # Check for domain errors across namespace variations
    is_domain_error = (
        isinstance(exc, SourceControlError)
        or hasattr(exc, "http_status")
        or hasattr(exc, "code")
        or exc.__class__.__name__ in {
            "SourceControlError",
            "NotFoundError",
            "ValidationError",
            "ConflictError",
            "PermissionDeniedError",
            "AuthRequiredError",
            "AuthExpiredError",
            "RateLimitedError",
            "RemoteStateChangedError",
            "ApprovalRequiredError",
            "ApprovalInvalidError",
            "ApprovalExpiredError",
            "ApprovalConsumedError",
            "PolicyDeniedError",
            "ProtectedBranchError",
            "WorkspaceNotFoundError",
        }
    )

    if is_domain_error:
        http_status = getattr(exc, "http_status", None)
        if not http_status:
            name = exc.__class__.__name__
            if name in ("NotFoundError", "WorkspaceNotFoundError"):
                http_status = status.HTTP_404_NOT_FOUND
            elif name in ("ValidationError", "PathTraversalError"):
                http_status = status.HTTP_400_BAD_REQUEST
            elif name in ("PermissionDeniedError", "PolicyDeniedError", "ProtectedBranchError"):
                http_status = status.HTTP_403_FORBIDDEN
            elif name in ("AuthRequiredError", "AuthExpiredError"):
                http_status = status.HTTP_401_UNAUTHORIZED
            elif name in ("ConflictError", "WorkspaceDirtyError", "ApprovalConsumedError", "ApprovalInvalidError"):
                http_status = status.HTTP_409_CONFLICT
            else:
                http_status = status.HTTP_500_INTERNAL_SERVER_ERROR

        raise HTTPException(
            status_code=http_status,
            detail=str(exc),
        )

    # Unknown exception: log with full stack trace and return a generic 500.
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


# ---------------------------------------------------------------------------
# Pull Requests (GitHub-only in this phase)
# ---------------------------------------------------------------------------
#
# These routes are declared BEFORE the generic
# `/repositories/{repository_id:path}` route below. FastAPI
# matches routes in declaration order, and `{...:path}` is
# greedy: if the catch-all is declared first, it swallows a
# request like `/repositories/owner/repo/pulls/1` as
# `repository_id = "owner/repo/pulls/1"` and the PR routes never
# match. The section-2 ordering already follows this rule for the
# branches and file routes; the PR routes follow the same pattern.


@router.get(
    "/repositories/{repository_id:path}/pulls",
    response_model=PullRequestListResponse,
)
async def list_pull_requests(
    repository_id: str,
    state: Optional[str] = Query(
        default=None,
        description="PR state filter: open, closed, merged, or all.",
    ),
    source_branch: Optional[str] = Query(
        default=None, description="Optional head-branch filter."
    ),
    target_branch: Optional[str] = Query(
        default=None, description="Optional base-branch filter."
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100),
    provider: Optional[str] = Query(default="github"),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> PullRequestListResponse:
    """Lists pull requests for a repository."""
    try:
        return await facade.list_pull_requests(
            repository_id=repository_id,
            provider=provider,
            state=state,
            source_branch=source_branch,
            target_branch=target_branch,
            page=page,
            page_size=page_size,
        )
    except Exception as exc:
        handle_source_control_error(exc)


@router.get(
    "/repositories/{repository_id:path}/pulls/{number}",
    response_model=PullRequestResponse,
)
async def get_pull_request(
    repository_id: str,
    number: str,
    provider: Optional[str] = Query(default="github"),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> PullRequestResponse:
    """Fetches a single pull request by number."""
    try:
        return await facade.get_pull_request(
            repository_id=repository_id,
            number=number,
            provider=provider,
        )
    except Exception as exc:
        handle_source_control_error(exc)


@router.post(
    "/repositories/{repository_id:path}/pulls",
    response_model=PullRequestResponse,
)
async def create_pull_request(
    repository_id: str,
    payload: CreatePullRequestRequest,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> PullRequestResponse:
    """Creates a new pull request.

    The URL's `repository_id` and the body's `repository_id` MUST
    match. A mismatch is rejected explicitly rather than silently
    overriding one of the two values: a caller that copy-pastes a
    body from one PR to another would otherwise open the PR on the
    wrong repository without any warning. The `provider` field is
    taken from the body (`CreatePullRequestRequest.provider`), not
    from a query parameter, because a single request can only
    target one provider.
    """
    try:
        if payload.repository_id != repository_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"repository_id in body ('{payload.repository_id}') "
                    f"does not match the URL ('{repository_id}')."
                ),
            )
        return await facade.create_pull_request(payload)
    except Exception as exc:
        handle_source_control_error(exc)


@router.put(
    "/repositories/{repository_id:path}/pulls/{number}/merge",
    response_model=MergePullRequestResponse,
)
async def merge_pull_request(
    repository_id: str,
    number: str,
    payload: MergePullRequestRequest,
    provider: Optional[str] = Query(default="github"),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> MergePullRequestResponse:
    """Merges an open pull request.

    The policy engine is consulted inside the facade before the
    merge is sent. A policy denial (for example, the repository
    forbids merges) surfaces as a 403 and no remote state is
    changed.
    """
    try:
        return await facade.merge_pull_request(
            repository_id=repository_id,
            number=number,
            payload=payload,
            provider=provider,
        )
    except Exception as exc:
        handle_source_control_error(exc)


@router.post(
    "/repositories/{repository_id:path}/pulls/{number}/reviews",
    response_model=PullRequestReviewResponse,
)
async def submit_pull_request_review(
    repository_id: str,
    number: str,
    payload: SubmitPullRequestReviewRequest,
    provider: Optional[str] = Query(default="github"),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> PullRequestReviewResponse:
    """Submits a review (APPROVE / REQUEST_CHANGES / COMMENT)."""
    try:
        return await facade.submit_pr_review(
            repository_id=repository_id,
            number=number,
            payload=payload,
            provider=provider,
        )
    except Exception as exc:
        handle_source_control_error(exc)


@router.post(
    "/repositories/{repository_id:path}/pulls/{number}/comments",
    response_model=PullRequestCommentResponse,
)
async def create_pull_request_comment(
    repository_id: str,
    number: str,
    payload: CreatePullRequestCommentRequest,
    provider: Optional[str] = Query(default="github"),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> PullRequestCommentResponse:
    """Creates a general or inline comment on a pull request.

    Dispatches on the payload shape: providing both `path` and
    `line` creates an inline comment on that file/line; omitting
    both creates a general (conversation) comment.
    """
    try:
        return await facade.create_pr_comment(
            repository_id=repository_id,
            number=number,
            payload=payload,
            provider=provider,
        )
    except Exception as exc:
        handle_source_control_error(exc)


@router.get(
    "/repositories/{repository_id:path}/pulls/{number}/timeline",
)
async def get_pull_request_timeline(
    repository_id: str,
    number: str,
    provider: Optional[str] = Query(default="github"),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> dict:
    """Returns reviews, general comments, and inline comments for a PR.

    The response is a composite of three collections and is
    returned as a plain dict rather than a Pydantic model because
    the frontend consumes the three collections independently. The
    keys and value shapes are:

        {
            "reviews": PullRequestReviewResponse[],
            "general_comments": PullRequestCommentResponse[],
            "inline_comments": PullRequestCommentResponse[],
        }
    """
    try:
        return dict(
            await facade.list_pr_reviews_and_comments(
                repository_id=repository_id,
                number=number,
                provider=provider,
            )
        )
    except Exception as exc:
        handle_source_control_error(exc)


# ---------------------------------------------------------------------------
# Issues (GitHub-only in this phase)
# ---------------------------------------------------------------------------
#
# These routes are declared BEFORE the generic
# `/repositories/{repository_id:path}` route below. FastAPI matches
# routes in declaration order, and `{...:path}` is greedy: if the
# catch-all is declared first, it swallows
# `/repositories/owner/repo/issues/1` as
# `repository_id = "owner/repo/issues/1"` and the issue routes
# never match. The PR routes above follow the same rule.

@router.get(
    "/repositories/{repository_id:path}/issues",
    response_model=IssueListResponse,
)
async def list_issues(
    repository_id: str,
    state: str = Query(
        default="open",
        description="Issue state filter: 'open', 'closed', or 'all'.",
    ),
    labels: Optional[str] = Query(
        default=None,
        description="Comma-separated list of labels to filter by.",
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100),
    provider: Optional[str] = Query(default="github"),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> IssueListResponse:
    """Lists issues for a repository.

    Pull requests are filtered out by the service; the response
    contains only real issues. Pass ``state="all"`` to include both
    open and closed issues.
    """
    try:
        labels_list: Optional[Sequence[str]] = None
        if labels:
            labels_list = [
                label.strip()
                for label in labels.split(",")
                if label.strip()
            ]
        return await facade.list_issues(
            repository_id=repository_id,
            provider=provider,
            state=state,
            labels=labels_list,
            page=page,
            page_size=page_size,
        )
    except Exception as exc:
        handle_source_control_error(exc)


@router.get(
    "/repositories/{repository_id:path}/issues/{number}",
    response_model=IssueResponse,
)
async def get_issue(
    repository_id: str,
    number: str,
    provider: Optional[str] = Query(default="github"),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> IssueResponse:
    """Fetches a single issue by number."""
    try:
        return await facade.get_issue(
            repository_id=repository_id,
            number=number,
            provider=provider,
        )
    except Exception as exc:
        handle_source_control_error(exc)


@router.post(
    "/repositories/{repository_id:path}/issues",
    response_model=IssueResponse,
)
async def create_issue(
    repository_id: str,
    payload: CreateIssueRequest,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> IssueResponse:
    """Creates a new issue.

    The URL's `repository_id` and the body's `repository_id` MUST
    match. A mismatch is rejected explicitly rather than silently
    overriding one of the two values, mirroring the same guard on
    the pull-request creation endpoint.
    """
    try:
        if payload.repository_id != repository_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"repository_id in body ('{payload.repository_id}') "
                    f"does not match the URL ('{repository_id}')."
                ),
            )
        return await facade.create_issue(payload)
    except Exception as exc:
        handle_source_control_error(exc)


@router.patch(
    "/repositories/{repository_id:path}/issues/{number}",
    response_model=IssueResponse,
)
async def update_issue_state(
    repository_id: str,
    number: str,
    payload: UpdateIssueStateRequest,
    provider: Optional[str] = Query(default="github"),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> IssueResponse:
    """Changes an issue's state (close or reopen)."""
    try:
        return await facade.update_issue_state(
            repository_id=repository_id,
            number=number,
            payload=payload,
            provider=provider,
        )
    except Exception as exc:
        handle_source_control_error(exc)


@router.get(
    "/repositories/{repository_id:path}/issues/{number}/comments",
    response_model=Sequence[IssueCommentResponse],
)
async def list_issue_comments(
    repository_id: str,
    number: str,
    provider: Optional[str] = Query(default="github"),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> Sequence[IssueCommentResponse]:
    """Lists the comments on an issue."""
    try:
        return await facade.list_issue_comments(
            repository_id=repository_id,
            number=number,
            provider=provider,
        )
    except Exception as exc:
        handle_source_control_error(exc)


@router.post(
    "/repositories/{repository_id:path}/issues/{number}/comments",
    response_model=IssueCommentResponse,
)
async def create_issue_comment(
    repository_id: str,
    number: str,
    payload: CreateIssueCommentRequest,
    provider: Optional[str] = Query(default="github"),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> IssueCommentResponse:
    """Adds a comment to an existing issue."""
    try:
        return await facade.create_issue_comment(
            repository_id=repository_id,
            number=number,
            payload=payload,
            provider=provider,
        )
    except Exception as exc:
        handle_source_control_error(exc)


# ---------------------------------------------------------------------------
# 9. GitHub Releases (remote, GitHub-only)
# ---------------------------------------------------------------------------
#
# Same ordering constraint as the PR and issue routes above: these
# are declared BEFORE the generic
# `/repositories/{repository_id:path}` route so that the catch-all
# does not swallow them.

@router.get(
    "/repositories/{repository_id:path}/releases",
    response_model=ReleaseListResponse,
)
async def list_releases(
    repository_id: str,
    include_drafts: bool = Query(
        default=False,
        description="Include draft releases.",
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100),
    provider: Optional[str] = Query(default="github"),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> ReleaseListResponse:
    """Lists releases for a repository.

    Drafts are excluded by default. Pass ``include_drafts=true`` to
    include them.
    """
    try:
        return await facade.list_releases(
            repository_id=repository_id,
            provider=provider,
            include_drafts=include_drafts,
            page=page,
            page_size=page_size,
        )
    except Exception as exc:
        handle_source_control_error(exc)


@router.get(
    "/repositories/{repository_id:path}/releases/{release_id}",
    response_model=ReleaseResponse,
)
async def get_release(
    repository_id: str,
    release_id: int,
    provider: Optional[str] = Query(default="github"),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> ReleaseResponse:
    """Fetches a single release by its numeric GitHub ID."""
    try:
        return await facade.get_release(
            repository_id=repository_id,
            release_id=str(release_id),
            provider=provider,
        )
    except Exception as exc:
        handle_source_control_error(exc)


@router.post(
    "/repositories/{repository_id:path}/releases",
    response_model=ReleaseResponse,
)
async def create_release(
    repository_id: str,
    payload: CreateReleaseRequest,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> ReleaseResponse:
    """Creates a GitHub release.

    The URL's `repository_id` and the body's `repository_id` MUST
    match, mirroring the same guard on PR and issue creation.
    """
    try:
        if payload.repository_id != repository_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"repository_id in body ('{payload.repository_id}') "
                    f"does not match the URL ('{repository_id}')."
                ),
            )
        return await facade.create_release(payload)
    except Exception as exc:
        handle_source_control_error(exc)


@router.delete(
    "/repositories/{repository_id:path}/releases/{release_id}",
    response_model=ActionSuccessResponse,
)
async def delete_release(
    repository_id: str,
    release_id: int,
    provider: Optional[str] = Query(default="github"),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> ActionSuccessResponse:
    """Deletes a GitHub release.

    The underlying Git tag is NOT deleted; it remains on the
    remote until a separate tag-delete operation runs.
    """
    try:
        return await facade.delete_release(
            repository_id=repository_id,
            release_id=str(release_id),
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
    """
    Initializes an isolated local Git workspace.

    When ``chat_id`` is provided in the payload, the workspace is
    created inside that chat's files directory so that the Editor
    Tab can operate on its contents directly. This is used by
    Manual Mode; Agent Mode leaves ``chat_id`` unset and the facade
    falls back to its default storage location.
    """
    try:
        return await facade.create_workspace(
            payload, chat_id=getattr(payload, "chat_id", None)
        )
    except Exception as exc:
        handle_source_control_error(exc)


@router.post("/workspaces/cleanup")
async def cleanup_workspaces(
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> dict:
    """
    Prune orphaned workspace entries from the registry.

    A workspace becomes orphaned when the owning chat or project is
    deleted: the physical directory under
    `storage/chats/<chat>/files/` disappears but the registry entry
    remains. This endpoint drops those stale entries. It does NOT
    touch any surviving workspace directory.

    NOTE: this route MUST be declared before the
    `GET /workspaces/{workspace_id}` route. FastAPI matches routes
    in declaration order, and a later literal path would be shadowed
    by the earlier parameterized route.
    """
    try:
        return await facade.cleanup_workspaces()
    except Exception as exc:
        handle_source_control_error(exc)


@router.get(
    "/workspaces/by-chat/{chat_id}",
    response_model=Optional[WorkspaceResponse],
)
async def get_workspace_by_chat(
    chat_id: str,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> Optional[WorkspaceResponse]:
    """Return the workspace bound to a chat, or null if none exists."""
    try:
        return await facade.get_workspace_by_chat(chat_id)
    except Exception as exc:
        handle_source_control_error(exc)

@router.post("/workspaces/{workspace_id}/ensure-editable-branch")
async def ensure_editable_branch(
    workspace_id: str,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> dict:
    """Create a feature branch if on a protected branch."""
    try:
        branch = await facade.ensure_editable_branch(workspace_id)
        return {"branch_name": branch}
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


@router.delete(
    "/workspaces/{workspace_id}",
    response_model=ActionSuccessResponse,
)
async def delete_workspace(
    workspace_id: str,
    force_delete_files: bool = Query(
        default=False,
        description=(
            "When true, also remove the workspace's files from disk. "
            "Required to make the chat's files folder available for a "
            "new workspace (create_workspace refuses to clone into a "
            "directory that already contains a .git folder)."
        ),
    ),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> ActionSuccessResponse:
    """
    Delete a workspace from the registry and, optionally, its files.

    This endpoint exists primarily for Manual Mode's "Change
    Repository" flow: when the user has created a workspace but
    wants to start over with a different repository, they need to
    first delete the existing one because `POST /workspaces`
    refuses to create a second workspace in the same chat folder.

    The two flags are independent:

        - Without `force_delete_files`, only the registry entry is
          removed. The files stay on disk, so a follow-up
          `create_workspace` will still fail with the same
          "workspace already exists" error — but the workspace is
          no longer tracked.

        - With `force_delete_files=true`, the entire workspace
          directory (including its .git) is removed from disk, so
          the chat's files folder becomes available again.

    A missing workspace ID is a 404, not a silent no-op.
    """
    try:
        return await facade.delete_workspace(
            workspace_id, force_delete_files=force_delete_files
        )
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


@router.post("/operations/cleanup")
async def cleanup_operations(
    dry_run: bool = Query(
        default=False,
        description=(
            "When true, report what WOULD be removed without "
            "mutating the operation journal."
        ),
    ),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> dict:
    """
    Prune journal operations whose bound workspace no longer exists.

    A workspace becomes orphaned when the chat or project that
    owned it is deleted: the physical directory under
    `storage/chats/<chat>/files/` disappears, but the durable
    operation journal keeps the record. Those operations surface
    in the UI as ghost review cards whose diff can never load and
    whose Approve button can never succeed.

    This endpoint removes those stale entries. Companion of
    `POST /workspaces/cleanup`, which prunes the workspace
    registry. Neither endpoint touches any surviving workspace
    directory or any still-valid operation.

    NOTE: this route MUST be declared before the
    `GET /operations/{operation_id}` route. FastAPI matches routes
    in declaration order, and a later literal path would be shadowed
    by the earlier parameterized route.
    """
    try:
        return await facade.cleanup_orphaned_operations(
            dry_run=dry_run
        )
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


# ---------------------------------------------------------------------------
# 10. Source Control subsystem settings
# ---------------------------------------------------------------------------
#
# These endpoints manage the persisted settings file under
# `backend/data/source_control/settings.json`. They do NOT manage
# OAuth credentials; those stay in `/integrations` and the vault.


@router.get(
    "/settings",
    response_model=SourceControlSettingsResponse,
)
async def get_source_control_settings(
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> SourceControlSettingsResponse:
    """Read the current Source Control subsystem settings.

    Returns the factory defaults when no settings file exists yet,
    so the frontend can call this endpoint on first run without a
    404.
    """
    try:
        return await facade.read_settings()
    except Exception as exc:
        handle_source_control_error(exc)


@router.put(
    "/settings",
    response_model=SourceControlSettingsResponse,
)
async def update_source_control_settings(
    payload: UpdateSourceControlSettingsRequest,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> SourceControlSettingsResponse:
    """Merge and persist settings changes.

    A partial payload is supported: only the fields the caller
    provides are updated. Omitted fields keep their current values.
    """
    try:
        return await facade.write_settings(payload)
    except Exception as exc:
        handle_source_control_error(exc)


# ---------------------------------------------------------------------------
# 7. Merge / rebase / conflict resolution (local workspace operations)
# ---------------------------------------------------------------------------
#
# These endpoints are strictly local: they never call a provider API
# and never touch a token. They are audited for the same reason every
# other mutating operation is audited.
#
# Ordering note:
#   Every route is under `/workspaces/{workspace_id}/...`. The
#   `{workspace_id}` parameter is a single segment (not a `:path`
#   converter), so these routes cannot shadow or be shadowed by the
#   existing `/workspaces/{workspace_id}/checkout` or
#   `/workspaces/{workspace_id}/diff` routes. They are declared at
#   the end of the router for readability, not because of a matching
#   constraint.

@router.post(
    "/workspaces/{workspace_id}/merge",
    response_model=MergeOperationResponse,
)
async def merge_workspace_branches(
    workspace_id: str,
    payload: MergeBranchesRequest,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> MergeOperationResponse:
    """Merge one local branch into another inside the workspace.

    On conflicts, returns ``kind="conflicted"`` with the current
    conflict list. The caller must then call
    `POST /workspaces/{id}/conflicts/resolve` and finally
    `POST /workspaces/{id}/finalize`.
    """
    try:
        return await facade.merge_workspace_branches(
            workspace_id=workspace_id, payload=payload
        )
    except Exception as exc:
        handle_source_control_error(exc)


@router.post(
    "/workspaces/{workspace_id}/rebase",
    response_model=MergeOperationResponse,
)
async def rebase_workspace_branch(
    workspace_id: str,
    payload: RebaseBranchRequest,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> MergeOperationResponse:
    """Rebase one local branch onto another inside the workspace."""
    try:
        return await facade.rebase_workspace_branch(
            workspace_id=workspace_id, payload=payload
        )
    except Exception as exc:
        handle_source_control_error(exc)


@router.post(
    "/workspaces/{workspace_id}/finalize",
    response_model=MergeOperationResponse,
)
async def finalize_workspace_operation(
    workspace_id: str,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> MergeOperationResponse:
    """Finalize whichever merge or rebase is currently in progress.

    The caller is expected to have resolved and staged all
    conflicts first; the operation refuses to commit when
    unresolved conflicts remain.
    """
    try:
        return await facade.finalize_workspace_operation(
            workspace_id=workspace_id
        )
    except Exception as exc:
        handle_source_control_error(exc)


@router.post(
    "/workspaces/{workspace_id}/abort",
    response_model=ConflictListResponse,
)
async def abort_workspace_operation(
    workspace_id: str,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> ConflictListResponse:
    """Abort the in-progress merge / rebase / cherry-pick / revert.

    Returns a state report with ``operation_state="none"`` on
    success. Idempotent: calling this when no operation is in
    progress raises a 409 (there is nothing to abort).
    """
    try:
        return await facade.abort_workspace_operation(
            workspace_id=workspace_id
        )
    except Exception as exc:
        handle_source_control_error(exc)


@router.get(
    "/workspaces/{workspace_id}/conflicts",
    response_model=ConflictListResponse,
)
async def list_workspace_conflicts(
    workspace_id: str,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> ConflictListResponse:
    """Return the current operation state and conflicted files.

    Safe to call at any time; returns ``operation_state="none"``
    and an empty list when no operation is in progress.
    """
    try:
        return await facade.list_workspace_conflicts(
            workspace_id=workspace_id
        )
    except Exception as exc:
        handle_source_control_error(exc)


@router.post(
    "/workspaces/{workspace_id}/conflicts/resolve",
    response_model=ResolveConflictsResponse,
)
async def resolve_workspace_conflicts(
    workspace_id: str,
    payload: ResolveConflictsRequest,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> ResolveConflictsResponse:
    """Resolve conflicted files by taking a side or marking manual edits.

    Strategies: ``"ours"``, ``"theirs"``, or ``"manual"``. The
    response reports how many files were resolved and which files
    remain conflicted.
    """
    try:
        return await facade.resolve_workspace_conflicts(
            workspace_id=workspace_id, payload=payload
        )
    except Exception as exc:
        handle_source_control_error(exc)


# ---------------------------------------------------------------------------
# 8. Local Git tags (workspace)
# ---------------------------------------------------------------------------
#
# These endpoints operate on the LOCAL Git repository in the
# workspace. They never call a provider API and never touch a token.
# Creating a tag locally does not push it to the remote.

@router.get(
    "/workspaces/{workspace_id}/tags",
    response_model=TagListResponse,
)
async def list_workspace_tags(
    workspace_id: str,
    max_tags: int = Query(default=5000, ge=1, le=10_000),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> TagListResponse:
    """Lists every local Git tag in the workspace."""
    try:
        return await facade.list_workspace_tags(
            workspace_id=workspace_id, max_tags=max_tags
        )
    except Exception as exc:
        handle_source_control_error(exc)


@router.post(
    "/workspaces/{workspace_id}/tags",
    response_model=TagResponse,
)
async def create_workspace_tag(
    workspace_id: str,
    payload: CreateTagRequest,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> TagResponse:
    """Creates a local Git tag in the workspace.

    A tag with a non-empty ``message`` is created as an annotated
    tag; without one it is created as a lightweight tag.
    """
    try:
        return await facade.create_workspace_tag(
            workspace_id=workspace_id, payload=payload
        )
    except Exception as exc:
        handle_source_control_error(exc)


@router.delete(
    "/workspaces/{workspace_id}/tags/{name:path}",
    response_model=ActionSuccessResponse,
)
async def delete_workspace_tag(
    workspace_id: str,
    name: str,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> ActionSuccessResponse:
    """Deletes a local Git tag from the workspace.

    The ``{name:path}`` converter is intentional: tag names may
    legitimately contain slashes (e.g. ``v1.0.0/rc1``), and a
    plain ``{name}`` capture would reject them with a 404. The
    facade re-validates the name against Git's tag-name rules.
    """
    try:
        return await facade.delete_workspace_tag(
            workspace_id=workspace_id, name=name
        )
    except Exception as exc:
        handle_source_control_error(exc)


# ---------------------------------------------------------------------------
# 6. Git read-only operations (history, tree, historical file content)
# ---------------------------------------------------------------------------
#
# These endpoints are strictly read-only: they inspect the committed
# history of a workspace and never mutate the working tree, the
# index, or any branch. They are consumed by the Repository Browser
# and Commit History views.
#
# Path layout note:
#   Every route below is under `/workspaces/{workspace_id}/...`, so
#   none of them can shadow the parameterized `GET /workspaces/{id}`
#   route (which is a single-segment path). FastAPI matches by
#   segment, so `/workspaces/abc/log` cannot collide with
#   `/workspaces/abc`.


@router.get(
    "/workspaces/{workspace_id}/log",
    response_model=Sequence[CommitEntryResponse],
)
async def get_workspace_log(
    workspace_id: str,
    limit: int = Query(
        default=50,
        ge=1,
        le=500,
        description="Maximum number of commits to return per page.",
    ),
    skip: int = Query(
        default=0,
        ge=0,
        description="Number of commits to skip from the tip of the ref.",
    ),
    ref: Optional[str] = Query(
        default=None,
        description="Branch, tag, or SHA. Defaults to HEAD.",
    ),
    path: Optional[str] = Query(
        default=None,
        description="Optional path filter.",
    ),
    author: Optional[str] = Query(
        default=None,
        description="Optional author name/email filter.",
    ),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> Sequence[CommitEntryResponse]:
    """Return a page of commits for a workspace.

    Pagination is stable: two calls with non-overlapping `skip`
    values return non-overlapping pages, provided the underlying
    history is unchanged. An empty sequence is a valid result
    (empty repository, or all commits filtered out).
    """
    try:
        return await facade.read_workspace_log(
            workspace_id=workspace_id,
            limit=limit,
            skip=skip,
            ref=ref,
            path=path,
            author=author,
        )
    except Exception as exc:
        handle_source_control_error(exc)


@router.get(
    "/workspaces/{workspace_id}/commits/{sha}",
    response_model=CommitWithStatsResponse,
)
async def get_workspace_commit(
    workspace_id: str,
    sha: str,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> CommitWithStatsResponse:
    """Return a single commit with aggregate file-change statistics."""
    try:
        return await facade.read_workspace_commit(
            workspace_id=workspace_id, sha=sha
        )
    except Exception as exc:
        handle_source_control_error(exc)


@router.get(
    "/workspaces/{workspace_id}/files/tree",
    response_model=Sequence[TreeNodeResponse],
)
async def get_workspace_tree(
    workspace_id: str,
    ref: Optional[str] = Query(
        default=None,
        description="Branch, tag, or SHA. Defaults to HEAD.",
    ),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> Sequence[TreeNodeResponse]:
    """Return the hierarchical file tree at a ref.

    The tree reflects COMMITTED content, not the working tree. Use
    the workspace status / diff endpoints when the caller wants to
    browse the working tree.
    """
    try:
        return await facade.list_workspace_tree(
            workspace_id=workspace_id, ref=ref
        )
    except Exception as exc:
        handle_source_control_error(exc)


@router.get(
    "/workspaces/{workspace_id}/file-content",
    response_model=FileContentAtRefResponse,
)
async def get_workspace_file_content(
    workspace_id: str,
    ref: str = Query(
        ...,
        description="Git ref (branch, tag, or SHA).",
    ),
    path: str = Query(
        ...,
        description="File path relative to the repository root.",
    ),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> FileContentAtRefResponse:
    """Read a file at a specific committed ref.

    Unlike the workspace-diff endpoint, which reads the working
    tree, this endpoint returns the COMMITTED content. It is the
    correct API for browsing historical files in the Repository
    Browser.
    """
    try:
        return await facade.read_file_at_ref(
            workspace_id=workspace_id, ref=ref, path=path
        )
    except Exception as exc:
        handle_source_control_error(exc)


@router.post(
    "/workspaces/{workspace_id}/checkout",
    response_model=WorkspaceResponse,
)
async def checkout_workspace_branch(
    workspace_id: str,
    payload: CheckoutBranchRequest,
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> WorkspaceResponse:
    """Check out an existing local branch inside the workspace.

    This is a local-only operation: it does not touch the remote.
    A non-forced checkout is refused when the workspace has
    uncommitted changes, so user work is never silently discarded.
    """
    try:
        return await facade.checkout_workspace_branch(
            workspace_id=workspace_id, payload=payload
        )
    except Exception as exc:
        handle_source_control_error(exc)
