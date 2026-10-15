from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


# ============================================================
# 1. Authentication & Connection DTOs
# ============================================================

class BeginAuthRequest(BaseModel):
    """Payload to initiate an OAuth authorization session."""

    redirect_uri: Optional[str] = Field(
        default=None,
        description="Optional explicit loopback redirect URI."
    )
    scopes: Optional[List[str]] = Field(
        default=None,
        description="Optional list of specific scopes requested from the provider."
    )
    instance_url: Optional[str] = Field(
        default=None,
        description="Base URL for self-managed instances (e.g., GitLab self-hosted)."
    )


class BeginAuthResponse(BaseModel):
    """Response containing authorization URL and state token."""

    auth_url: str = Field(description="Target provider authorization URL.")
    state: str = Field(description="Cryptographic CSRF verification state token.")


class ConnectionResponse(BaseModel):
    """Metadata representing an authenticated provider connection."""

    connection_id: str = Field(description="Unique identifier for the local connection.")
    provider: str = Field(description="Provider identifier (github, gitlab, bitbucket).")
    account_id: str = Field(description="Stable provider account ID.")
    username: str = Field(description="Username on the provider platform.")
    display_name: str = Field(default="", description="Display name of the user.")
    connected: bool = Field(default=True, description="Whether the connection is currently active.")
    connected_at: Optional[str] = Field(default=None, description="ISO timestamp of connection establishment.")
    base_url: str = Field(description="Base API URL for the provider.")
    verified_scopes: List[str] = Field(default_factory=list, description="Verified scopes granted to the token.")


class ValidateConnectionResponse(BaseModel):
    """Outcome of validating stored provider credentials."""

    valid: bool = Field(description="Whether the stored credentials remain valid.")
    username: str = Field(description="Resolved username from token validation.")
    message: Optional[str] = Field(default=None, description="Diagnostic detail or failure message.")


# ============================================================
# 2. Repository & Branch DTOs
# ============================================================

class RepositoryQuery(BaseModel):
    """Query parameters for searching or listing repositories."""

    query: Optional[str] = Field(default=None, description="Filter string for repository name or namespace.")
    page: int = Field(default=1, ge=1, description="Page number for pagination.")
    page_size: int = Field(default=30, ge=1, le=100, description="Number of repositories per page.")


class RepositoryResponse(BaseModel):
    """Normalized repository representation for frontend and agent tools."""

    repository_id: str = Field(description="Unique repository identifier.")
    provider: str = Field(description="Provider identifier.")
    account_id: str = Field(description="Owning account identifier.")
    full_name: str = Field(description="Full namespace and name (e.g. owner/repo).")
    namespace: str = Field(description="Owner or organization namespace.")
    name: str = Field(description="Repository name.")
    clone_url: str = Field(description="Safe clone URL without embedded credentials.")
    web_url: str = Field(description="Web browser URL.")
    default_branch: str = Field(description="Default branch name (e.g., main).")
    is_private: bool = Field(description="True if the repository is private.")
    is_fork: bool = Field(default=False, description="True if the repository is a fork.")
    description: str = Field(default="", description="Repository description.")


class BranchResponse(BaseModel):
    """Git branch metadata."""

    name: str = Field(description="Branch name.")
    head_sha: Optional[str] = Field(default=None, description="Current commit SHA at branch head.")
    is_default: bool = Field(default=False, description="True if this is the repository default branch.")
    is_protected: bool = Field(default=False, description="True if branch protection rules apply.")


class CreateBranchRequest(BaseModel):
    """Payload to create a new branch."""

    branch_name: str = Field(description="Desired new branch name.")
    from_sha: Optional[str] = Field(default=None, description="Commit SHA to branch from (defaults to HEAD).")


# ============================================================
# 3. Workspace & Local Environment DTOs
# ============================================================

class CreateWorkspaceRequest(BaseModel):
    """Payload to establish an isolated local Git worktree."""

    repository_id: str = Field(description="Target repository identifier.")
    branch_name: Optional[str] = Field(default=None, description="Starting branch for the workspace.")


class WorkspaceResponse(BaseModel):
    """Metadata describing an isolated Git workspace."""

    workspace_id: str = Field(description="Unique identifier for the workspace.")
    repository_id: str = Field(description="Bound repository identifier.")
    local_path: str = Field(description="Absolute local filesystem directory.")
    worktree_branch: str = Field(description="Checked-out local branch.")
    is_active: bool = Field(default=True, description="True if workspace is valid and available.")
    created_at: str = Field(description="ISO timestamp of workspace creation.")


# ============================================================
# 4. Diff & File Inspection DTOs
# ============================================================

class FileDiffDTO(BaseModel):
    """Single file modification details."""

    path: str = Field(description="Relative path of the modified file.")
    change_type: str = Field(description="Type of change: added, modified, deleted, renamed.")
    old_path: Optional[str] = Field(default=None, description="Original path if renamed.")
    additions: int = Field(default=0, description="Lines added.")
    deletions: int = Field(default=0, description="Lines deleted.")
    patch: str = Field(default="", description="Unified diff patch content.")
    is_binary: bool = Field(default=False, description="True if the file is binary.")


class DiffSummaryDTO(BaseModel):
    """Aggregated diff structure with cryptographic hash verification."""

    base_sha: str = Field(description="Base commit SHA against which diff was produced.")
    head_sha: str = Field(description="Head commit SHA.")
    diff_sha256: str = Field(description="SHA-256 fingerprint of the complete patch.")
    files: List[FileDiffDTO] = Field(default_factory=list, description="List of file modifications.")
    total_additions: int = Field(default=0, description="Total lines added across all files.")
    total_deletions: int = Field(default=0, description="Total lines deleted across all files.")
    file_count: int = Field(default=0, description="Total number of modified files.")
    contains_workflow_files: bool = Field(default=False, description="True if CI/CD workflows are touched.")
    contains_possible_secrets: bool = Field(default=False, description="True if scanner flagged possible secrets.")
    warnings: List[str] = Field(default_factory=list, description="Human-readable review warnings.")


# ============================================================
# 5. Verification & Checks DTOs
# ============================================================

class RunChecksRequest(BaseModel):
    """Request to execute local verification commands or CI checks."""

    command: Optional[str] = Field(
        default=None,
        description="Optional specific check command from policy allowlist."
    )


class CheckResultDTO(BaseModel):
    """Result of an individual check or test execution."""

    check_id: str = Field(description="Unique check identifier.")
    name: str = Field(description="Name or command of the check.")
    status: str = Field(description="Execution status (success, failure, queued, in_progress).")
    summary: str = Field(default="", description="Output summary or failure excerpt.")
    exit_code: Optional[int] = Field(default=None, description="Process exit code.")
    duration_seconds: Optional[float] = Field(default=None, description="Execution duration in seconds.")


class CheckSuiteResponse(BaseModel):
    """Consolidated check suite results."""

    suite_id: str = Field(description="Suite execution identifier.")
    overall_status: str = Field(description="Consolidated status across all checks.")
    checks: List[CheckResultDTO] = Field(default_factory=list, description="Individual check results.")
    has_failures: bool = Field(default=False, description="True if any required check failed.")


# ============================================================
# 6. Approval Tickets & Human Gate DTOs
# ============================================================

class ApprovalTicketDTO(BaseModel):
    """Cryptographically bound, single-use approval ticket."""

    approval_id: str = Field(description="Unique approval ticket ID.")
    operation_id: str = Field(description="Bound operation identifier.")
    repository_id: str = Field(description="Target repository identifier.")
    operation_kind: str = Field(description="Requested operation kind (e.g. push_branch).")
    base_sha: str = Field(description="Base commit SHA validated at proposal time.")
    diff_sha256: str = Field(description="Fingerprint of the exact diff approved.")
    source_branch: str = Field(description="Source branch to push or merge.")
    target_branch: str = Field(description="Target branch on the remote repository.")
    commit_message: str = Field(default="", description="Proposed commit message.")
    change_request_title: str = Field(default="", description="Proposed PR/MR title.")
    decision: str = Field(description="Decision status: pending, approved, rejected, expired, invalidated.")
    created_at: str = Field(description="ISO timestamp of ticket creation.")
    expires_at: Optional[str] = Field(default=None, description="ISO timestamp of ticket expiry.")


class ApproveOperationRequest(BaseModel):
    """Payload submitted when the user confirms an operation."""

    approval_id: str = Field(description="ID of the ticket being approved.")


class RejectOperationRequest(BaseModel):
    """Payload submitted when the user declines an operation."""

    reason: Optional[str] = Field(default=None, description="Optional explanation for rejection.")


# ============================================================
# 7. Durable Operations & Remote Mutations DTOs
# ============================================================

class CreateOperationRequest(BaseModel):
    """Payload to initiate a managed source control mutation."""

    repository_id: str = Field(description="Target repository identifier.")
    kind: str = Field(description="Operation kind (e.g. push_branch, create_change_request).")
    target_branch: Optional[str] = Field(default=None, description="Target remote branch.")
    commit_message: Optional[str] = Field(default=None, description="Commit message if committing.")


class OperationResponse(BaseModel):
    """Comprehensive state snapshot of a durable source control operation."""

    operation_id: str = Field(description="Unique operation identifier.")
    repository_id: str = Field(description="Associated repository identifier.")
    kind: str = Field(description="Operation kind.")
    status: str = Field(description="Current lifecycle state in the operation state machine.")
    source_branch: Optional[str] = Field(default=None, description="Source branch.")
    target_branch: Optional[str] = Field(default=None, description="Target branch.")
    base_sha: Optional[str] = Field(default=None, description="Base SHA.")
    head_sha: Optional[str] = Field(default=None, description="Head SHA.")
    diff_sha256: Optional[str] = Field(default=None, description="Diff fingerprint.")
    error: Optional[Dict[str, Any]] = Field(default=None, description="Normalized error details if failed.")
    created_at: str = Field(description="ISO timestamp of creation.")
    updated_at: str = Field(description="ISO timestamp of last update.")


class PushBranchRequest(BaseModel):
    """Payload to execute an approved branch push."""

    approval_id: str = Field(description="Single-use approved ticket ID.")
    force: bool = Field(default=False, description="Whether to allow force-push (prohibited by default policy).")


class PushBranchResponse(BaseModel):
    """Result of pushing a branch to the remote provider."""

    success: bool = Field(description="True if the push succeeded.")
    remote_branch: str = Field(description="Name of the remote branch updated or created.")
    commit_sha: str = Field(description="Commit SHA now at remote branch head.")
    web_url: Optional[str] = Field(default=None, description="Web URL to view branch on provider.")


class CreateChangeRequestDTO(BaseModel):
    """Payload to generate a Pull Request or Merge Request."""

    approval_id: str = Field(description="Single-use approved ticket ID.")
    title: str = Field(description="Title of the pull request.")
    body: str = Field(default="", description="Description or body of the pull request.")
    draft: bool = Field(default=True, description="True to create as a draft PR.")


class ChangeRequestResponse(BaseModel):
    """Result and metadata of a created or inspected change request."""

    provider: str = Field(description="Provider identifier.")
    repository_id: str = Field(description="Repository identifier.")
    number_or_iid: str = Field(description="PR number or MR internal ID.")
    title: str = Field(description="Change request title.")
    web_url: str = Field(description="Web URL to view the change request.")
    state: str = Field(description="State: open, draft, closed, merged.")
    source_branch: str = Field(description="Source/head branch name.")
    target_branch: str = Field(description="Target/base branch name.")
    is_draft: bool = Field(default=True, description="True if marked as draft.")


# ============================================================
# 8. Provider Capabilities & Catalog DTOs
# ============================================================

class ProviderDescriptorDTO(BaseModel):
    """Public metadata describing an available provider and its capabilities."""

    provider_id: str = Field(description="Unique provider identifier.")
    display_name: str = Field(description="Human-readable provider name.")
    capabilities: Dict[str, Any] = Field(description="Feature matrix and technical limits.")
