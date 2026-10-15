from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .enums import (
    ApprovalDecision,
    ChangeRequestState,
    CheckStatus,
    FileChangeType,
    OperationKind,
    ProviderId,
)


@dataclass(frozen=True)
class ProviderAccount:
    """Authenticated account metadata for a source control provider."""

    provider: ProviderId
    account_id: str
    username: str
    base_url: str
    display_name: str = ""
    connected: bool = True
    connected_at: Optional[datetime] = None
    verified_scopes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RepositoryRef:
    """Normalized repository reference across all providers."""

    provider: ProviderId
    account_id: str
    repository_id: str
    namespace: str
    name: str
    clone_url: str
    web_url: str
    default_branch: str
    is_private: bool
    is_fork: bool = False
    description: str = ""

    @property
    def full_name(self) -> str:
        """Returns the qualified repository name (e.g., owner/repo)."""
        return f"{self.namespace}/{self.name}" if self.namespace else self.name


@dataclass(frozen=True)
class BranchRef:
    """Git branch reference metadata."""

    name: str
    head_sha: Optional[str] = None
    is_default: bool = False
    is_protected: bool = False


@dataclass(frozen=True)
class CommitRef:
    """Git commit metadata."""

    sha: str
    message: str
    author_name: str
    author_email: str
    committed_at: Optional[datetime] = None
    parents: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class FileDiff:
    """Granular file-level change description in a diff."""

    path: str
    change_type: FileChangeType
    old_path: Optional[str] = None
    additions: int = 0
    deletions: int = 0
    patch: str = ""
    is_binary: bool = False
    is_submodule: bool = False


@dataclass(frozen=True)
class DiffSummary:
    """Aggregated diff representation with cryptographic hash verification."""

    base_sha: str
    head_sha: str
    diff_sha256: str
    files: tuple[FileDiff, ...] = field(default_factory=tuple)
    total_additions: int = 0
    total_deletions: int = 0
    file_count: int = 0
    contains_workflow_files: bool = False
    contains_possible_secrets: bool = False
    warnings: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ApprovalTicket:
    """Cryptographically bound, single-use ticket granting execution of a specific mutation."""

    approval_id: str
    operation_id: str
    repository_id: str
    operation_kind: OperationKind
    base_sha: str
    diff_sha256: str
    source_branch: str
    target_branch: str
    commit_message: str
    change_request_title: str = ""
    change_request_body: str = ""
    decision: ApprovalDecision = ApprovalDecision.PENDING
    created_at: datetime = field(default_factory=datetime.utcnow)
    expires_at: Optional[datetime] = None
    decided_at: Optional[datetime] = None
    approved_by: Optional[str] = None
    rejection_reason: Optional[str] = None

    @property
    def is_valid(self) -> bool:
        """Determines if the approval ticket is currently approved and not expired."""
        if self.decision != ApprovalDecision.APPROVED:
            return False
        if self.expires_at is not None and datetime.utcnow() >= self.expires_at:
            return False
        return True


@dataclass(frozen=True)
class CheckResult:
    """Individual execution result for a test, lint, or CI check."""

    check_id: str
    name: str
    status: CheckStatus
    summary: str = ""
    details_url: Optional[str] = None
    exit_code: Optional[int] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


@dataclass(frozen=True)
class CheckSuite:
    """Aggregated execution status for all checks run against a workspace or commit."""

    suite_id: str
    overall_status: CheckStatus
    checks: tuple[CheckResult, ...] = field(default_factory=tuple)
    has_failures: bool = False


@dataclass(frozen=True)
class ChangeRequestRef:
    """Normalized pull request or merge request reference."""

    provider: ProviderId
    repository_id: str
    number_or_iid: str
    title: str
    web_url: str
    state: ChangeRequestState
    source_branch: str
    target_branch: str
    is_draft: bool = True
    author_username: str = ""
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass(frozen=True)
class WorkspaceRef:
    """Metadata tracking an isolated local Git worktree."""

    workspace_id: str
    repository_id: str
    local_path: str
    worktree_branch: str
    created_at: datetime = field(default_factory=datetime.utcnow)
    is_active: bool = True
