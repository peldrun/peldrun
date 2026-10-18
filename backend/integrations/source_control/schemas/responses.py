from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence
from pydantic import BaseModel, ConfigDict, Field


def _utc_now() -> datetime:
    """
    Return the current time as a timezone-aware UTC datetime.

    Used as a `default_factory` for schema fields that must default
    to the moment of construction. Defined at module scope so that
    every schema shares the same implementation; this guarantees that
    all default timestamps are timezone-aware and interoperate
    correctly with the fields whose values are populated from domain
    objects (which are also timezone-aware).
    """
    return datetime.now(timezone.utc)


class BaseSCResponse(BaseModel):
    """Base response schema enforcing immutability and attribute compatibility."""

    model_config = ConfigDict(
        frozen=True,
        from_attributes=True,
    )


class ActionSuccessResponse(BaseSCResponse):
    """Generic status response acknowledging a completed action."""

    status: str = Field(
        default="success",
        description="Execution status indicator ('success', 'accepted', 'completed').",
    )
    message: str = Field(
        ...,
        description="Human-readable informational summary of the result.",
    )


class ProviderCapabilitiesResponse(BaseSCResponse):
    """Authoritative technical feature support matrix for a provider adapter."""

    list_repositories: bool = Field(default=True)
    read_files: bool = Field(default=True)
    create_branch: bool = Field(default=True)
    push_branch: bool = Field(default=True)
    create_change_request: bool = Field(default=True)
    list_checks: bool = Field(default=False)
    comments: bool = Field(default=False)
    merge_change_request: bool = Field(default=False)
    self_managed_instance: bool = Field(default=False)


class ProviderDescriptorResponse(BaseSCResponse):
    """Metadata describing a registered source control provider."""

    provider_id: str = Field(..., description="Provider unique identifier (e.g. 'github').")
    display_name: str = Field(..., description="User-friendly display name (e.g. 'GitHub Cloud').")
    is_connected: bool = Field(..., description="Whether valid active credentials exist in vault.")
    capabilities: ProviderCapabilitiesResponse = Field(..., description="Declared capabilities.")
    web_base_url: str = Field(..., description="Upstream web base URL.")
    api_base_url: str = Field(..., description="Upstream API base URL.")


class ConnectBeginResponse(BaseSCResponse):
    """Payload returned to trigger user authorization in the system browser."""

    provider: str = Field(..., description="Target source control provider identifier.")
    authorization_url: str = Field(..., description="Full browser URL for OAuth 2.0 PKCE handshake.")
    state: str = Field(..., description="Cryptographically random CSRF verification state.")
    expires_in_seconds: int = Field(default=600, description="Handshake timeout window in seconds.")


class ConnectionStatusResponse(BaseSCResponse):
    """Authoritative connection status and identity for a specific provider."""

    provider: str = Field(..., description="Provider identifier.")
    is_connected: bool = Field(..., description="Whether authenticated session is active.")
    account_id: Optional[str] = Field(default=None, description="Stable numeric/alphanumeric user ID.")
    username: Optional[str] = Field(default=None, description="Authenticated account handle or login.")
    base_url: str = Field(default="https://github.com", description="Upstream instance base URL.")
    granted_scopes: Sequence[str] = Field(default_factory=tuple, description="Scopes verified on provider.")
    last_validated_at: Optional[datetime] = Field(default=None, description="Timestamp of last check.")


class RepositoryResponse(BaseSCResponse):
    """Sanitized metadata representing an accessible remote repository."""

    provider: str = Field(..., description="Originating provider ID.")
    repository_id: str = Field(..., description="Stable repository identifier or numeric ID.")
    namespace: str = Field(..., description="Owner or organization slug.")
    name: str = Field(..., description="Repository slug.")
    full_name: str = Field(..., description="Combined 'namespace/name' path.")
    default_branch: str = Field(default="main", description="Repository default branch name.")
    web_url: str = Field(..., description="Clean sanitized web browsing URL.")
    clone_url: str = Field(..., description="Clean sanitized Git clone URL without credentials.")
    is_private: bool = Field(default=False, description="Whether repository is private.")
    description: Optional[str] = Field(default=None, description="Repository description.")
    metadata: Mapping[str, Any] = Field(default_factory=dict, description="Additional properties.")


class BranchResponse(BaseSCResponse):
    """Metadata representing a remote or local Git branch."""

    name: str = Field(..., description="Branch reference name.")
    head_sha: Optional[str] = Field(default=None, description="Commit SHA at branch HEAD.")
    is_default: bool = Field(default=False, description="Whether this is the default branch.")
    is_protected: bool = Field(default=False, description="Whether branch has protection policies.")


class FileReadResponse(BaseSCResponse):
    """Payload containing file contents or binary asset descriptors."""

    repository_id: str = Field(..., description="Target repository ID.")
    file_path: str = Field(..., description="Relative path of file within repository.")
    ref: str = Field(..., description="Git commit, branch, or tag evaluated.")
    is_binary: bool = Field(default=False, description="Whether file contains binary bytes.")
    size_bytes: int = Field(..., description="Total size in bytes.")
    is_truncated: bool = Field(default=False, description="Whether content was budget-truncated.")
    content: Optional[str] = Field(default=None, description="Decoded UTF-8 text if not binary.")


class FileDiffResponse(BaseSCResponse):
    """Metrics and properties of a single modified file in a unified diff."""

    path: str = Field(..., description="Current relative path of the file.")
    change_type: str = Field(..., description="Modification type (e.g. 'modified', 'added', 'deleted').")
    insertions: int = Field(default=0, description="Number of added lines.")
    deletions: int = Field(default=0, description="Number of removed lines.")
    old_path: Optional[str] = Field(default=None, description="Previous path if renamed or copied.")
    is_binary: bool = Field(default=False, description="Whether file is classified as binary.")
    is_workflow_file: bool = Field(default=False, description="Whether file modifies CI/CD workflows.")


class DiffSummaryResponse(BaseSCResponse):
    """Unified diff summary with cryptographic snapshot fingerprint."""

    base_sha: str = Field(..., description="Base commit SHA anchored for comparison.")
    head_sha: str = Field(..., description="Target or active commit SHA.")
    diff_sha256: str = Field(..., description="Cryptographic SHA-256 fingerprint of the unified diff.")
    total_files: int = Field(default=0, description="Count of modified files.")
    insertions: int = Field(default=0, description="Total inserted lines.")
    deletions: int = Field(default=0, description="Total deleted lines.")
    touches_workflow_files: bool = Field(default=False, description="Elevated warning flag for CI files.")
    workflow_files: Sequence[str] = Field(default_factory=tuple, description="List of modified CI files.")
    has_binary_files: bool = Field(default=False, description="Whether diff includes binary changes.")
    files: Sequence[FileDiffResponse] = Field(default_factory=tuple, description="Itemized file changes.")
    diff_sample: Optional[str] = Field(default=None, description="Redacted unified diff text sample.")
    is_truncated: bool = Field(default=False, description="Whether diff text sample was truncated.")


class CheckRunResponse(BaseSCResponse):
    """Individual CI/CD check run or commit status entry."""

    name: str = Field(..., description="Check or context identifier name.")
    status: str = Field(..., description="Lifecycle status ('queued', 'in_progress', 'completed').")
    conclusion: Optional[str] = Field(default=None, description="Outcome ('success', 'failure', etc.).")
    web_url: Optional[str] = Field(default=None, description="Clean URL to inspect build logs.")
    started_at: Optional[datetime] = Field(default=None, description="Start timestamp.")
    completed_at: Optional[datetime] = Field(default=None, description="Completion timestamp.")


class CheckSummaryResponse(BaseSCResponse):
    """Aggregate evaluation of all checks on a given commit reference."""

    ref: str = Field(..., description="Git commit SHA or branch reference.")
    overall_state: str = Field(..., description="Aggregate state ('success', 'failure', 'pending', 'neutral').")
    is_passing: bool = Field(default=False, description="Convenience flag: overall_state is success/neutral.")
    total_count: int = Field(default=0, description="Total checks discovered.")
    success_count: int = Field(default=0, description="Passed checks count.")
    failure_count: int = Field(default=0, description="Failed checks count.")
    pending_count: int = Field(default=0, description="In-progress or queued count.")
    message: str = Field(..., description="Descriptive status summary.")
    runs: Sequence[CheckRunResponse] = Field(default_factory=tuple, description="Itemized check list.")


class ApprovalTicketResponse(BaseSCResponse):
    """Frozen cryptographic approval proposal awaiting explicit human review."""

    approval_id: str = Field(..., description="Unique ticket identifier.")
    operation_id: str = Field(..., description="Bound durable operation ID.")
    operation_type: str = Field(..., description="Requested action ('push_branch', 'create_change_request').")
    repository_id: str = Field(..., description="Target repository ID.")
    target_branch: str = Field(..., description="Destination branch.")
    base_sha: str = Field(..., description="Base commit SHA validated at review time.")
    diff_sha256: str = Field(..., description="Cryptographic fingerprint of approved diff.")
    total_files: int = Field(default=0, description="Total affected files.")
    touches_workflow_files: bool = Field(default=False, description="Warning if CI/CD files are touched.")
    is_protected_target: bool = Field(default=False, description="Warning if destination is protected.")
    approved_by_user: bool = Field(default=False, description="Whether human approved the ticket.")
    expires_at: datetime = Field(..., description="Timestamp when ticket expires.")
    created_at: datetime = Field(
        default_factory=_utc_now,
        description=(
            "Timestamp when the response was constructed. NOTE: this "
            "defaults to 'now' because the caller does not currently "
            "propagate the ticket's true creation time. Callers SHOULD "
            "pass `created_at=<ticket>.created_at` explicitly so that "
            "the response reflects the actual ticket creation moment."
        ),
    )


class WorkspaceResponse(BaseSCResponse):
    """State of an isolated local Git workspace or worktree."""

    workspace_id: str = Field(..., description="Unique workspace identifier.")
    repository_id: str = Field(..., description="Bound repository identifier.")
    current_branch: str = Field(..., description="Active branch checked out in workspace.")
    base_sha: str = Field(..., description="Base commit SHA workspace was initialized from.")
    head_sha: Optional[str] = Field(default=None, description="Current local HEAD commit SHA.")
    is_dirty: bool = Field(default=False, description="Whether unstaged/uncommitted changes exist.")
    created_at: datetime = Field(..., description="Creation timestamp.")


class PushBranchResponse(BaseSCResponse):
    """Result of an authenticated push to remote repository."""

    status: str = Field(default="success")
    approval_id: str = Field(..., description="Consumed approval ticket ID.")
    operation_id: str = Field(..., description="Bound operation ID.")
    branch_name: str = Field(..., description="Remote branch name pushed.")
    pushed_sha: str = Field(..., description="Commit SHA now active at remote HEAD.")
    remote_name: str = Field(default="origin", description="Remote name pushed to.")
    upstream_ref: Optional[str] = Field(default=None, description="Upstream tracking reference.")
    message: str = Field(..., description="Informational outcome message.")


class ChangeRequestResponse(BaseSCResponse):
    """Metadata representing a created or updated pull/merge request."""

    status: str = Field(default="success")
    provider: str = Field(..., description="Provider identifier.")
    repository_id: str = Field(..., description="Target repository ID.")
    number_or_iid: str = Field(..., description="Pull request number or ID.")
    web_url: str = Field(..., description="Clean sanitized browser URL to view PR.")
    state: str = Field(..., description="Lifecycle state ('open', 'closed', 'merged').")
    source_branch: str = Field(..., description="Feature branch containing changes.")
    target_branch: str = Field(..., description="Destination base branch.")
    title: str = Field(..., description="Sanitized PR title.")
    is_draft: bool = Field(default=True, description="Whether opened as draft.")
    message: str = Field(..., description="Outcome message.")


class OperationEventResponse(BaseSCResponse):
    """Historical timeline event within a durable workflow operation."""

    event_id: str = Field(..., description="Unique event identifier.")
    from_state: str = Field(..., description="Previous state.")
    to_state: str = Field(..., description="Resulting state.")
    timestamp: datetime = Field(..., description="Transition timestamp.")
    metadata: Mapping[str, Any] = Field(default_factory=dict, description="Redacted event metadata.")


class OperationResponse(BaseSCResponse):
    """Complete durable workflow operation record."""

    operation_id: str = Field(..., description="Unique operation identifier.")
    operation_type: str = Field(..., description="Operation type.")
    provider: str = Field(..., description="Provider identifier.")
    repository_id: str = Field(..., description="Target repository ID.")
    target_branch: str = Field(..., description="Destination branch.")
    current_state: str = Field(..., description="Current state machine state.")
    workspace_id: Optional[str] = Field(default=None, description="Bound workspace ID.")
    approval_id: Optional[str] = Field(default=None, description="Associated approval ticket ID.")
    change_request_url: Optional[str] = Field(default=None, description="Clean URL to PR if created.")
    error_code: Optional[str] = Field(default=None, description="Normalized error category if failed.")
    error_message: Optional[str] = Field(default=None, description="Sanitized error description.")
    created_at: datetime = Field(..., description="Creation timestamp.")
    updated_at: datetime = Field(..., description="Last transition timestamp.")
    events: Sequence[OperationEventResponse] = Field(default_factory=tuple, description="Timeline events.")


class AuditLogEntryResponse(BaseSCResponse):
    """Immutable security audit log record."""

    event_id: str = Field(..., description="Audit record ID.")
    timestamp: datetime = Field(..., description="Event timestamp.")
    event_type: str = Field(..., description="Audit classification.")
    user_id: str = Field(default="user", description="Acting user or system entity.")
    provider: Optional[str] = Field(default=None, description="Provider involved.")
    repository: Optional[str] = Field(default=None, description="Repository involved.")
    operation_id: Optional[str] = Field(default=None, description="Associated operation ID.")
    details: Mapping[str, Any] = Field(default_factory=dict, description="Redacted context details.")
