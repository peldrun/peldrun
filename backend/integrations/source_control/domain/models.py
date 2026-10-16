from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Sequence

from .enums import ApprovalDecision, FileChangeType, OperationKind, ProviderId


@dataclass(frozen=True)
class ProviderAccount:
    """Immutable representation of a verified source control provider user account."""

    provider: ProviderId
    account_id: str
    username: str
    base_url: str = "https://github.com"
    connected: bool = True


@dataclass(frozen=True)
class RepositoryRef:
    """Immutable reference identifying a remote source control repository."""

    provider: ProviderId
    account_id: str
    repository_id: str
    namespace: str
    name: str
    clone_url: str
    web_url: str
    default_branch: str = "main"
    is_private: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BranchRef:
    """Immutable reference identifying a Git branch."""

    name: str
    head_sha: Optional[str] = None
    is_default: bool = False


@dataclass(frozen=True)
class CommitRef:
    """Immutable reference identifying a Git commit."""

    sha: str = ""
    message: str = ""
    author_name: str = ""
    author_email: str = ""
    committed_at: Optional[datetime] = None
    commit_sha: Optional[str] = None
    author: Optional[str] = None
    created_at: Optional[datetime] = None
    parent_shas: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        effective_sha = self.commit_sha or self.sha
        object.__setattr__(self, "sha", effective_sha)
        object.__setattr__(self, "commit_sha", effective_sha)

        effective_time = self.created_at or self.committed_at or datetime.utcnow()
        object.__setattr__(self, "committed_at", effective_time)
        object.__setattr__(self, "created_at", effective_time)

        effective_author = self.author or self.author_name
        object.__setattr__(self, "author", effective_author)
        object.__setattr__(
            self, "author_name", self.author_name or effective_author or "Unknown"
        )


@dataclass(frozen=True)
class ChangeRequestRef:
    """Immutable reference identifying a Pull Request or Merge Request."""

    provider: ProviderId
    repository_id: str
    number_or_iid: str
    web_url: str
    state: str
    source_branch: str
    target_branch: str
    title: str = ""
    body: str = ""
    is_draft: bool = False
    created_at: Optional[datetime] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FileDiff:
    """Immutable record capturing changes for a single file in a diff."""

    path: str
    change_type: FileChangeType
    insertions: int
    deletions: int
    old_path: Optional[str] = None
    is_binary: bool = False


@dataclass(frozen=True)
class DiffSummary:
    """Immutable aggregated summary of changes across an entire diff."""

    base_sha: str
    head_sha: str
    total_files: int
    insertions: int
    deletions: int
    files: tuple[FileDiff, ...] = ()
    diff_sha256: str = ""


@dataclass(frozen=True)
class CheckRunRef:
    """Immutable descriptor for an individual CI check run or commit status."""

    name: str
    status: str
    conclusion: Optional[str] = None
    web_url: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


@dataclass(frozen=True)
class CheckSummaryRef:
    """Immutable aggregated execution summary of all checks run against a commit ref."""

    ref: str
    total_count: int
    success_count: int
    failure_count: int
    pending_count: int
    overall_state: str  # "success", "failure", "pending", "neutral"
    runs: tuple[CheckRunRef, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ApprovalTicket:
    """Immutable representation of a human approval authorization ticket."""

    approval_id: str
    operation_id: str
    repository_id: str
    operation: str = "push_branch"
    base_sha: str = ""
    diff_sha256: str = ""
    target_branch: str = ""
    source_branch: Optional[str] = None
    expires_at: Optional[datetime] = None
    approved_by_user: bool = False
    user_id: Optional[str] = None
    notes: Optional[str] = None
    rejection_reason: Optional[str] = None
    is_consumed: bool = False
    decision: ApprovalDecision = ApprovalDecision.PENDING
    operation_kind: Optional[OperationKind] = None
    created_at: Optional[datetime] = None
    decided_at: Optional[datetime] = None
    approved_by: Optional[str] = None
    commit_message: str = ""
    change_request_title: str = ""
    change_request_body: str = ""
    total_files: int = 0
    touches_workflow_files: bool = False
    is_protected_target: bool = False


# Canonical aliases ensuring backwards-compatible imports across ports and facade
CheckResult = CheckRunRef
CheckSuite = CheckSummaryRef

__all__ = [
    "ProviderAccount",
    "RepositoryRef",
    "BranchRef",
    "CommitRef",
    "ChangeRequestRef",
    "FileDiff",
    "DiffSummary",
    "CheckRunRef",
    "CheckSummaryRef",
    "CheckResult",
    "CheckSuite",
    "ApprovalTicket",
]
