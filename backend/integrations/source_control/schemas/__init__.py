# backend/integrations/source_control/schemas/__init__.py
"""
API schemas for the Source Control Integration subsystem.

This package exposes two families of Pydantic models:

    - Request schemas (`requests.py`): payloads sent by the HTTP
      client to the source-control router. They enforce strict
      validation (forbid extra fields, strip whitespace, immutable
      instances) so that malformed input is rejected before it
      reaches the application layer.
    - Response schemas (`responses.py`): payloads returned by the
      router to the client. They are read-only and can be built
      directly from domain models via `from_attributes=True`.

The `__init__.py` re-exports every public schema so that callers
can write::

    from backend.integrations.source_control.schemas import (
        CreateBranchRequest,
        RepositoryResponse,
    )

without needing to know which submodule each class lives in. This
is the canonical import surface for the HTTP layer and for tests.

Design rules:
    - This module MUST NOT define any new schema classes. It only
      re-exports what `requests.py` and `responses.py` provide.
    - The public API of the package is the union of the two
      submodules' `__all__` lists plus the two base classes.
    - Adding a new schema to `requests.py` or `responses.py`
      requires updating the corresponding import block and the
      `__all__` list below.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

from .requests import (
    ApproveOperationRequest,
    BaseSCRequest,
    CheckoutBranchRequest,
    ConnectBeginRequest,
    CreateBranchRequest,
    CreateChangeRequestPayload,
    CreateIssueCommentRequest,
    CreateIssueRequest,
    CreateOperationRequest,
    CreatePullRequestCommentRequest,
    CreatePullRequestRequest,
    CreateReleaseRequest,
    CreateTagRequest,
    CreateWorkspaceRequest,
    DisconnectRequest,
    MergeBranchesRequest,
    MergePullRequestRequest,
    PushBranchRequest,
    RebaseBranchRequest,
    RejectOperationRequest,
    ResolveConflictsRequest,
    ReviewCommentPayload,
    RunChecksRequest,
    SaveProviderConfigRequest,
    SubmitPullRequestReviewRequest,
    UpdateIssueStateRequest,
    ValidateConnectionRequest,
)

# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

from .responses import (
    ActionSuccessResponse,
    ApprovalTicketResponse,
    AuditLogEntryResponse,
    BaseSCResponse,
    BranchResponse,
    ChangeRequestResponse,
    CheckRunResponse,
    CheckSummaryResponse,
    CommitEntryResponse,
    CommitWithStatsResponse,
    ConflictFileResponse,
    ConflictListResponse,
    ConnectBeginResponse,
    ConnectionStatusResponse,
    DiffSummaryResponse,
    FileContentAtRefResponse,
    FileDiffResponse,
    FileReadResponse,
    IssueCommentResponse,
    IssueListResponse,
    IssueResponse,
    MergeOperationResponse,
    MergePullRequestResponse,
    OperationEventResponse,
    OperationResponse,
    ProviderCapabilitiesResponse,
    ProviderDescriptorResponse,
    PullRequestCommentResponse,
    PullRequestListResponse,
    PullRequestResponse,
    PullRequestReviewResponse,
    PushBranchResponse,
    ReleaseListResponse,
    ReleaseResponse,
    RepositoryResponse,
    ResolveConflictsResponse,
    TagListResponse,
    TagResponse,
    TreeNodeResponse,
    WorkspaceResponse,
)

__all__ = [
    # -----------------------------------------------------------------------
    # Request schemas
    # -----------------------------------------------------------------------
    "BaseSCRequest",
    "ConnectBeginRequest",
    "SaveProviderConfigRequest",
    "ValidateConnectionRequest",
    "DisconnectRequest",
    "CreateWorkspaceRequest",
    "RunChecksRequest",
    "CreateBranchRequest",
    "CreateOperationRequest",
    "ApproveOperationRequest",
    "RejectOperationRequest",
    "PushBranchRequest",
    "CreateChangeRequestPayload",
    "CheckoutBranchRequest",
    "CreatePullRequestRequest",
    "MergePullRequestRequest",
    "SubmitPullRequestReviewRequest",
    "CreatePullRequestCommentRequest",
    "ReviewCommentPayload",
    "CreateIssueRequest",
    "CreateIssueCommentRequest",
    "UpdateIssueStateRequest",
    "MergeBranchesRequest",
    "RebaseBranchRequest",
    "ResolveConflictsRequest",
    "CreateTagRequest",
    "CreateReleaseRequest",
    # -----------------------------------------------------------------------
    # Response schemas
    # -----------------------------------------------------------------------
    "BaseSCResponse",
    "ActionSuccessResponse",
    "ProviderCapabilitiesResponse",
    "ProviderDescriptorResponse",
    "ConnectBeginResponse",
    "ConnectionStatusResponse",
    "RepositoryResponse",
    "BranchResponse",
    "FileReadResponse",
    "FileDiffResponse",
    "DiffSummaryResponse",
    "CheckRunResponse",
    "CheckSummaryResponse",
    "ApprovalTicketResponse",
    "WorkspaceResponse",
    "PushBranchResponse",
    "ChangeRequestResponse",
    "OperationEventResponse",
    "OperationResponse",
    "AuditLogEntryResponse",
    "CommitEntryResponse",
    "CommitWithStatsResponse",
    "FileContentAtRefResponse",
    "TreeNodeResponse",
    "PullRequestResponse",
    "PullRequestListResponse",
    "PullRequestReviewResponse",
    "PullRequestCommentResponse",
    "MergePullRequestResponse",
    "IssueResponse",
    "IssueListResponse",
    "IssueCommentResponse",
    "ConflictFileResponse",
    "ConflictListResponse",
    "MergeOperationResponse",
    "ResolveConflictsResponse",
    "TagResponse",
    "TagListResponse",
    "ReleaseResponse",
    "ReleaseListResponse",
]
