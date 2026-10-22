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
    is_configured: bool = Field(default=False, description="Whether OAuth App credentials are configured.")
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


# ----------------------------------------------------------------------
# Git read-only operations (history, tree, historical file content)
# ----------------------------------------------------------------------
#
# These schemas describe the responses of the read-only Git endpoints
# used by the Repository Browser and Commit History views. They are
# deliberately kept separate from the workspace/diff schemas above:
# those describe an editable working tree, while these describe the
# immutable committed history reachable from a ref.


class CommitEntryResponse(BaseSCResponse):
    """A single commit entry in the workspace's history.

    The list endpoint returns commits ordered newest-first, matching
    `git log`'s default order. Pagination is stable: two calls with
    non-overlapping `skip` values return non-overlapping pages,
    provided the underlying history is unchanged.

    Attributes:
        sha:          Full commit SHA-1 or SHA-256.
        message:      Full commit message (subject line + body).
        author_name:  Author name as recorded in the commit header.
        author_email: Author email as recorded in the commit header.
        committed_at: Timezone-aware commit timestamp.
        parent_shas:  Parent commit SHAs. Empty for the root
                      commit; two or more for a merge commit.
    """

    sha: str = Field(..., description="Full commit SHA.")
    message: str = Field(default="", description="Full commit message.")
    author_name: str = Field(default="", description="Author name.")
    author_email: str = Field(default="", description="Author email.")
    committed_at: datetime = Field(
        default_factory=_utc_now,
        description="Timezone-aware commit timestamp.",
    )
    parent_shas: Sequence[str] = Field(
        default_factory=tuple,
        description="Parent commit SHAs. Empty for the root commit.",
    )


class CommitWithStatsResponse(CommitEntryResponse):
    """A commit entry augmented with aggregate file-change statistics.

    Returned by the single-commit endpoint, where the extra Git
    invocation needed to compute the stats is justified. The list
    endpoint uses `CommitEntryResponse` to keep responses small: a
    page of 50 commits with stats would require 50 additional `git
    show --numstat` invocations, which is neither necessary nor
    cheap.

    Attributes:
        files_changed: Number of files touched by the commit.
        insertions:    Total number of added lines across all files.
        deletions:     Total number of removed lines across all files.
    """

    files_changed: int = Field(
        default=0,
        description="Number of files touched by the commit.",
    )
    insertions: int = Field(
        default=0,
        description="Total number of added lines across all files.",
    )
    deletions: int = Field(
        default=0,
        description="Total number of removed lines across all files.",
    )


class TreeNodeResponse(BaseSCResponse):
    """A single node in the repository's hierarchical file tree.

    The tree is shaped for direct consumption by `react-arborist`
    and similar tree components. Directories are ordered before
    files at every level, and each group is sorted
    case-insensitively by name, so the frontend receives a stable,
    predictable order.

    The `type` field is a string rather than a literal enum because
    the frontend's Zod schema does not currently import a
    corresponding enum, and adding one here would force a lockstep
    change on both sides for a value that has exactly two members.

    Attributes:
        name:     Final path component (e.g. "src" or "main.py").
        path:     Full path from the repository root, using "/".
        type:     Either "file" or "directory".
        size:     File size in bytes; None for directories.
        children: Child nodes for directories; empty for files.
    """

    name: str = Field(..., description="Final path component.")
    path: str = Field(
        ..., description="Full path from the repository root."
    )
    type: str = Field(
        ...,
        description="Node type: 'file' or 'directory'.",
    )
    size: Optional[int] = Field(
        default=None,
        description="File size in bytes; None for directories.",
    )
    children: Sequence["TreeNodeResponse"] = Field(
        default_factory=tuple,
        description="Child nodes (directories only; empty for files).",
    )


class FileContentAtRefResponse(BaseSCResponse):
    """The content of a file at a specific Git reference.

    Unlike the workspace diff endpoint, which reads the working
    tree, this response reflects the COMMITTED content at `ref`.
    It is the correct API for browsing historical files in the
    Repository Browser: a user who selects a commit from the
    history view expects to see the file as it was at that commit,
    not as it is right now.

    Binary detection:
        The backend inspects the raw `git show` output for NUL
        bytes. A NUL byte cannot appear in valid UTF-8 text and is
        the standard heuristic for binary content. When detected,
        `is_binary` is set to True and `content` is None; the UI
        MUST render a placeholder ("Binary file") rather than
        attempting to display the content.

    Attributes:
        workspace_id: The workspace the file was read from.
        ref:          Branch, tag, or commit SHA.
        path:         File path relative to the repository root.
        is_binary:    True when the file is binary.
        size_bytes:   Size of the file in bytes, before truncation.
        is_truncated: True when the content was truncated at the
                      configured byte budget.
        content:      Decoded UTF-8 text, or None for binary files.
    """

    workspace_id: str = Field(..., description="Source workspace ID.")
    ref: str = Field(
        ..., description="Git ref the file was read from."
    )
    path: str = Field(
        ..., description="File path within the repository."
    )
    is_binary: bool = Field(
        default=False,
        description="Whether the file is binary.",
    )
    size_bytes: int = Field(
        default=0,
        description="File size in bytes (before truncation).",
    )
    is_truncated: bool = Field(
        default=False,
        description="Whether the content was truncated.",
    )
    content: Optional[str] = Field(
        default=None,
        description=(
            "Decoded text content, or None for binary files."
        ),
    )


# ----------------------------------------------------------------------
# Pull Requests (list, detail, create, merge, reviews, comments)
# ----------------------------------------------------------------------
#
# These schemas describe the responses of the pull-request endpoints.
# They are the wire format consumed by the Pull Requests, Review
# Editor, and PR Detail views in the frontend.
#
# The `number` field is a string, not an int. This mirrors the
# provider-neutral `ChangeRequestRef.number_or_iid`, which is a
# string so that it can represent both GitHub's integer `number`
# and GitLab's integer `iid` without a type mismatch. Rendering it
# as a string also avoids URL-encoding quirks in the frontend.


class PullRequestResponse(BaseSCResponse):
    """A single pull request entry.

    The `state` field is a string rather than an enum because the
    frontend already narrows it via its own Zod schema; keeping it
    as a plain string here avoids a lockstep change on both sides
    for a value that has exactly five members.
    """

    provider: str = Field(..., description="Provider identifier.")
    repository_id: str = Field(..., description="Repository identifier.")
    number: str = Field(..., description="PR number (string).")
    web_url: str = Field(..., description="Sanitized browser URL.")
    state: str = Field(
        ...,
        description=(
            "PR state: 'open', 'draft', 'closed', 'merged', or "
            "'unknown'."
        ),
    )
    source_branch: str = Field(..., description="Head branch.")
    target_branch: str = Field(..., description="Base branch.")
    title: str = Field(default="", description="PR title.")
    body: Optional[str] = Field(
        default=None, description="PR body (Markdown)."
    )
    is_draft: bool = Field(
        default=False, description="Whether the PR is a draft."
    )
    author_login: Optional[str] = Field(
        default=None, description="Author login, when available."
    )
    created_at: Optional[datetime] = Field(
        default=None, description="Creation timestamp."
    )
    updated_at: Optional[datetime] = Field(
        default=None, description="Last-update timestamp."
    )
    merged_at: Optional[datetime] = Field(
        default=None, description="Merge timestamp, when merged."
    )


class PullRequestListResponse(BaseSCResponse):
    """A page of pull requests plus the requested state filter."""

    items: Sequence[PullRequestResponse] = Field(
        default_factory=tuple,
        description="The pull requests in this page.",
    )
    total: int = Field(
        default=0,
        description="Number of items in this page.",
    )
    state: str = Field(
        default="open",
        description="The state filter that was applied.",
    )


class PullRequestReviewResponse(BaseSCResponse):
    """A single review on a pull request."""

    review_id: str = Field(..., description="Review identifier.")
    state: str = Field(
        ...,
        description=(
            "Review state: 'pending', 'approved', "
            "'changes_requested', 'commented', 'dismissed', or "
            "'unknown'."
        ),
    )
    body: Optional[str] = Field(
        default=None, description="Review body (Markdown)."
    )
    author_login: Optional[str] = Field(
        default=None, description="Reviewer login."
    )
    submitted_at: Optional[datetime] = Field(
        default=None, description="Submission timestamp."
    )


class PullRequestCommentResponse(BaseSCResponse):
    """A single comment on a pull request (general or inline)."""

    comment_id: str = Field(..., description="Comment identifier.")
    body: str = Field(..., description="Comment body (Markdown).")
    author_login: Optional[str] = Field(
        default=None, description="Author login."
    )
    created_at: Optional[datetime] = Field(
        default=None, description="Creation timestamp."
    )
    updated_at: Optional[datetime] = Field(
        default=None, description="Last-update timestamp."
    )
    path: Optional[str] = Field(
        default=None,
        description="File path for inline comments; None for general.",
    )
    line: Optional[int] = Field(
        default=None,
        description="Line number for inline comments.",
    )
    side: Optional[str] = Field(
        default=None,
        description="Diff side for inline comments ('LEFT' or 'RIGHT').",
    )
    in_reply_to_id: Optional[str] = Field(
        default=None,
        description="Parent comment ID when this is a reply.",
    )


class MergePullRequestResponse(BaseSCResponse):
    """Result of a merge attempt."""

    status: str = Field(
        default="success",
        description="'success', 'accepted', or 'conflict'.",
    )
    merged: bool = Field(
        default=False,
        description="True when the merge was applied.",
    )
    sha: Optional[str] = Field(
        default=None,
        description="SHA of the merge commit, when merged.",
    )
    message: str = Field(
        default="",
        description="Human-readable result message.",
    )


# ----------------------------------------------------------------------
# Issues (list, detail, comments)
# ----------------------------------------------------------------------
#
# These schemas describe the responses of the issue endpoints.
# They mirror GitHub's `/repos/{owner}/{repo}/issues` resources,
# with two deliberate simplifications:
#
#   - `labels` is a flat list of names rather than the full GitHub
#     label objects. The UI does not currently render label colors,
#     and flattening saves a schema (and a mapper) that would
#     otherwise only be used by the frontend.
#   - `is_pull_request` is exposed even though the list endpoint
#     filters PRs out by default. The field lets a future revision
#     switch to a unified "Issues and Pull Requests" listing
#     without a schema change.

class IssueResponse(BaseSCResponse):
    """A single issue entry."""

    provider: str = Field(..., description="Provider identifier.")
    repository_id: str = Field(..., description="Repository identifier.")
    number: str = Field(..., description="Issue number (string).")
    web_url: str = Field(..., description="Sanitized browser URL.")
    state: str = Field(
        ...,
        description="'open' or 'closed'.",
    )
    title: str = Field(default="", description="Issue title.")
    body: Optional[str] = Field(
        default=None, description="Issue body (Markdown)."
    )
    author_login: Optional[str] = Field(
        default=None, description="Author login."
    )
    labels: Sequence[str] = Field(
        default_factory=tuple, description="Label names."
    )
    comments_count: int = Field(
        default=0, description="Number of comments."
    )
    is_pull_request: bool = Field(
        default=False,
        description=(
            "True when the underlying GitHub resource is a pull "
            "request (PRs are issues under the hood)."
        ),
    )
    state_reason: Optional[str] = Field(
        default=None,
        description=(
            "Closure reason: 'completed', 'not_planned', "
            "'reopened', or None."
        ),
    )
    created_at: Optional[datetime] = Field(
        default=None, description="Creation timestamp."
    )
    updated_at: Optional[datetime] = Field(
        default=None, description="Last-update timestamp."
    )
    closed_at: Optional[datetime] = Field(
        default=None, description="Closure timestamp, when closed."
    )


class IssueListResponse(BaseSCResponse):
    """A page of issues plus the requested state filter."""

    items: Sequence[IssueResponse] = Field(
        default_factory=tuple,
        description="The issues in this page.",
    )
    total: int = Field(
        default=0,
        description="Number of items in this page.",
    )
    state: str = Field(
        default="open",
        description="The state filter that was applied.",
    )


class IssueCommentResponse(BaseSCResponse):
    """A single comment on an issue."""

    comment_id: str = Field(..., description="Comment identifier.")
    body: str = Field(..., description="Comment body (Markdown).")
    author_login: Optional[str] = Field(
        default=None, description="Author login."
    )
    created_at: Optional[datetime] = Field(
        default=None, description="Creation timestamp."
    )
    updated_at: Optional[datetime] = Field(
        default=None, description="Last-update timestamp."
    )


# ----------------------------------------------------------------------
# Merge / rebase / conflict resolution (local workspace operations)
# ----------------------------------------------------------------------
#
# These schemas describe the responses of the workspace-level
# merge, rebase, and conflict-resolution endpoints. They are
# distinct from the pull-request merge schemas above: those drive
# the REMOTE merge via a provider API, while these drive a LOCAL
# Git merge/rebase inside a workspace.


class ConflictFileResponse(BaseSCResponse):
    """A single conflicted file in a workspace.

    The three boolean flags correspond to Git's three-stage index
    for a conflicted path:

        - ``has_base``   : stage 1 (common ancestor) exists.
        - ``has_ours``   : stage 2 (local side) exists.
        - ``has_theirs`` : stage 3 (incoming side) exists.

    A delete/modify conflict, for example, has ``has_ours`` XOR
    ``has_theirs`` set; an add/add conflict has both set and
    ``has_base`` unset.
    """

    path: str = Field(..., description="Workspace-relative path.")
    has_base: bool = Field(
        default=False,
        description="Whether the base (stage 1) version exists.",
    )
    has_ours: bool = Field(
        default=False,
        description="Whether the local (stage 2) version exists.",
    )
    has_theirs: bool = Field(
        default=False,
        description="Whether the incoming (stage 3) version exists.",
    )
    is_delete_modify: bool = Field(
        default=False,
        description=(
            "True when exactly one side deleted the file and the "
            "other modified it."
        ),
    )
    is_add_add: bool = Field(
        default=False,
        description=(
            "True when both sides added the file and there is no "
            "common ancestor."
        ),
    )


class ConflictListResponse(BaseSCResponse):
    """The current in-progress operation and its conflicted files."""

    workspace_id: str = Field(..., description="Workspace identifier.")
    operation_state: str = Field(
        ...,
        description=(
            "Current state: 'none', 'merging', 'rebasing', "
            "'cherry_picking', or 'reverting'."
        ),
    )
    files: Sequence[ConflictFileResponse] = Field(
        default_factory=tuple,
        description="Conflicted files in the workspace.",
    )
    total: int = Field(
        default=0, description="Number of conflicted files."
    )


class MergeOperationResponse(BaseSCResponse):
    """Result of a merge, rebase, or finalize operation.

    ``kind`` distinguishes the three outcomes the caller must
    handle differently:

        - ``"clean"``       : the operation completed successfully.
        - ``"conflicted"``  : the operation paused; the caller
                              should open the conflict resolver.
        - ``"up_to_date"``  : nothing to do.
    """

    workspace_id: str = Field(..., description="Workspace identifier.")
    kind: str = Field(
        ...,
        description=(
            "Result kind: 'clean', 'conflicted', or 'up_to_date'."
        ),
    )
    operation_state: str = Field(
        ...,
        description=(
            "State after the operation: 'none', 'merging', or "
            "'rebasing'."
        ),
    )
    conflicts: Sequence[ConflictFileResponse] = Field(
        default_factory=tuple,
        description=(
            "Conflicted files, present when kind is 'conflicted'."
        ),
    )
    head_sha: Optional[str] = Field(
        default=None,
        description="HEAD SHA after the operation.",
    )
    message: str = Field(
        default="", description="Human-readable status."
    )


class ResolveConflictsResponse(BaseSCResponse):
    """Result of a conflict-resolution action.

    Reports what changed and what remains. The caller inspects
    ``remaining`` to decide whether to finalize the operation
    (``remaining == 0``) or continue resolving.
    """

    workspace_id: str = Field(..., description="Workspace identifier.")
    operation_state: str = Field(
        ...,
        description="Current operation state (after resolution).",
    )
    resolved_count: int = Field(
        default=0,
        description="Number of files resolved by this call.",
    )
    remaining: Sequence[ConflictFileResponse] = Field(
        default_factory=tuple,
        description="Conflicted files that remain unresolved.",
    )
    message: str = Field(
        default="", description="Human-readable status."
    )


# ----------------------------------------------------------------------
# Tags (local) and Releases (GitHub-only)
# ----------------------------------------------------------------------
#
# TagResponse describes a LOCAL Git tag.
# ReleaseResponse describes a REMOTE GitHub release.
# The two are intentionally distinct: a tag is a local pointer, a
# release is a GitHub-hosted artifact. A release references a tag
# but is not the same thing.


class TagResponse(BaseSCResponse):
    """A local Git tag.

    ``sha`` is always the SHA of the underlying COMMIT, never the
    SHA of an annotated tag object. Callers that need the tag
    object's SHA (rare) can derive it from ``is_annotated`` plus
    the repository's object database; the commit SHA is what users
    and the UI actually care about.
    """

    name: str = Field(..., description="Tag name (short form).")
    sha: str = Field(..., description="Commit SHA the tag points to.")
    is_annotated: bool = Field(
        default=False,
        description="True for annotated tags, False for lightweight.",
    )
    message: Optional[str] = Field(
        default=None,
        description="Annotation message (annotated tags only).",
    )
    tagger_name: Optional[str] = Field(
        default=None,
        description="Tagger name (annotated tags only).",
    )
    tagger_email: Optional[str] = Field(
        default=None,
        description="Tagger email (annotated tags only).",
    )
    tagged_at: Optional[datetime] = Field(
        default=None,
        description="Tagger timestamp (annotated tags only).",
    )


class TagListResponse(BaseSCResponse):
    """A page of local Git tags plus the requested offset."""

    items: Sequence[TagResponse] = Field(
        default_factory=tuple,
        description="The tags in this page.",
    )
    total: int = Field(
        default=0,
        description="Number of tags in this page.",
    )


class ReleaseResponse(BaseSCResponse):
    """A GitHub release."""

    release_id: str = Field(
        ..., description="GitHub release ID (string)."
    )
    tag_name: str = Field(
        ..., description="Tag the release targets."
    )
    name: str = Field(
        default="", description="Release title."
    )
    body: Optional[str] = Field(
        default=None, description="Release notes (Markdown)."
    )
    html_url: str = Field(
        default="", description="Sanitized browser URL."
    )
    is_draft: bool = Field(
        default=False, description="True for drafts."
    )
    is_prerelease: bool = Field(
        default=False, description="True for prereleases."
    )
    author_login: Optional[str] = Field(
        default=None, description="Author login."
    )
    target_commitish: Optional[str] = Field(
        default=None,
        description=(
            "Branch or commit SHA the tag was created from, when "
            "the tag did not already exist."
        ),
    )
    created_at: Optional[datetime] = Field(
        default=None, description="Creation timestamp."
    )
    published_at: Optional[datetime] = Field(
        default=None,
        description="Publication timestamp; None for drafts.",
    )


class ReleaseListResponse(BaseSCResponse):
    """A page of GitHub releases."""

    items: Sequence[ReleaseResponse] = Field(
        default_factory=tuple,
        description="The releases in this page.",
    )
    total: int = Field(
        default=0,
        description="Number of releases in this page.",
    )


# Resolve forward references for the recursive tree model.
#
# `from __future__ import annotations` at the top of this module
# turns every type hint into a string at runtime, which means
# Pydantic sees `Sequence["TreeNodeResponse"]` as a forward
# reference that must be resolved against the module's namespace.
# The rebuild MUST happen after the class is fully defined;
# placing it here (at the very end of the module) guarantees that
# every class the forward reference could touch already exists.
TreeNodeResponse.model_rebuild()
