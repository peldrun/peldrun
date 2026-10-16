# backend/integrations/source_control/domain/models.py
"""
Provider-neutral domain models for the Source Control Integration subsystem.

This module contains the canonical entities and value objects used
across every layer of the subsystem (ports, adapters, application
services, and interface layers). Models defined here MUST be:

    - Provider-neutral: no GitHub/GitLab/Bitbucket terminology.
    - Infrastructure-neutral: no HTTP, Git, or storage imports.
    - Immutable: `frozen=True` dataclasses. Any change produces a new
      instance via `dataclasses.replace`.
    - Timezone-aware: every datetime carries `tzinfo=timezone.utc`.

The module intentionally keeps dataclasses small. Cross-aggregate
consistency is enforced by application services, not by the models.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from .enums import (
    ApprovalStatus,
    ChangeRequestState,
    CheckConclusion,
    CheckStatus,
    ConnectionStatus,
    FileChangeType,
    OperationEventType,
    OperationStatus,
    ProviderId,
)

__all__ = [
    # Provider identity
    "ProviderAccount",
    "ProviderCapabilities",
    # Repository + Git references
    "RepositoryRef",
    "BranchRef",
    "CommitRef",
    # Change request (PR / MR)
    "ChangeRequestRef",
    # Local workspace
    "WorkspaceRef",
    "WorkspaceStatus",
    # Diff + file changes
    "FileDiff",
    "DiffSummary",
    # Checks
    "CheckRunRef",
    "CheckSummary",
    # Operation lifecycle
    "OperationRecord",
    "OperationEvent",
    # Approval
    "ApprovalTicket",
    # Authentication
    "AuthSession",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def _canonical_json(payload: Mapping[str, Any]) -> str:
    """
    Produce a deterministic JSON representation of a mapping.

    Used to compute stable hashes for approvals and diff snapshots.
    Sorting keys and using a compact separator ensures the same logical
    content always hashes to the same value, regardless of insertion
    order.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _sha256_of(payload: Mapping[str, Any]) -> str:
    """Return a lowercase hex sha256 digest of the canonical JSON payload."""
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Provider identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderAccount:
    """
    A connected provider account (user or bot identity).

    `account_id` MUST be the provider's stable identifier, never a
    display name. `base_url` distinguishes self-managed GitLab or
    GitHub Enterprise instances from the public cloud.
    """

    provider: ProviderId
    account_id: str
    username: str
    base_url: str
    connected: bool = True
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    email: Optional[str] = None
    scopes: Sequence[str] = field(default_factory=tuple)
    last_validated_at: Optional[datetime] = None

    def identity_key(self) -> str:
        """
        Return a stable key uniquely identifying this account.

        Two accounts are the same if and only if they share the same
        provider, base URL, and stable account ID. This key is safe to
        use as a vault reference or database primary key.
        """
        return f"{self.provider.value}:{self.base_url}:{self.account_id}"


@dataclass(frozen=True)
class ProviderCapabilities:
    """
    Declares which operations a provider adapter supports.

    The UI MUST hide or disable actions whose capability is `False`
    and surface a clear reason. Adapters MUST NOT emulate unsupported
    operations silently.
    """

    list_repositories: bool = True
    read_files: bool = True
    create_branch: bool = True
    push_branch: bool = True
    create_change_request: bool = True
    list_checks: bool = False
    comments: bool = False
    merge_change_request: bool = False
    delete_branch: bool = False
    self_managed_instance: bool = False


# ---------------------------------------------------------------------------
# Repository and Git references
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepositoryRef:
    """
    A reference to a remote repository.

    `repository_id` MUST be the provider's stable identifier (e.g. the
    numeric ID on GitHub) so that renames of the namespace or name do
    not break references.
    """

    provider: ProviderId
    account_id: str
    repository_id: str
    namespace: str
    name: str
    clone_url: str
    web_url: str
    default_branch: str
    is_private: bool
    description: Optional[str] = None
    base_url: Optional[str] = None

    def full_name(self) -> str:
        """Return `namespace/name`, the human-readable repository path."""
        return f"{self.namespace}/{self.name}"

    def identity_key(self) -> str:
        """Return a stable key uniquely identifying this repository."""
        return f"{self.provider.value}:{self.base_url or ''}:{self.repository_id}"


@dataclass(frozen=True)
class BranchRef:
    """
    A reference to a branch on a remote repository.
    """

    name: str
    head_sha: Optional[str] = None
    is_default: bool = False
    is_protected: bool = False


@dataclass(frozen=True)
class CommitRef:
    """
    A reference to a single commit.
    """

    sha: str
    message: str
    author_name: str
    author_email: str
    committed_at: datetime
    parent_shas: Sequence[str] = field(default_factory=tuple)


@dataclass(frozen=True)
class ChangeRequestRef:
    """
    A provider-neutral reference to a pull/merge request.

    `number_or_iid` is a string because GitHub uses `number` and GitLab
    uses `iid`, both of which are integers rendered as strings for
    uniformity.
    """

    provider: ProviderId
    repository_id: str
    number_or_iid: str
    web_url: str
    state: ChangeRequestState
    source_branch: str
    target_branch: str
    title: str = ""
    is_draft: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    merged_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Local workspace
# ---------------------------------------------------------------------------


class WorkspaceStatus(str, str):
    """
    Lightweight string-typed status for a workspace.

    Defined as a subclass of `str` (not `enum.Enum`) so that it can be
    serialized transparently in JSON without needing custom encoders,
    while still providing named constants for readability.
    """

    CREATING = "creating"
    READY = "ready"
    DIRTY = "dirty"
    CONFLICTED = "conflicted"
    CLEANED = "cleaned"
    FAILED = "failed"


@dataclass(frozen=True)
class WorkspaceRef:
    """
    A reference to a local Git workspace owned by PELDRUN.

    A workspace is created under a PELDRUN-controlled root directory and
    is either a plain clone or a Git worktree. Every workspace is bound
    to a single repository and a single base commit; the workspace is
    the unit of isolation for local file edits.

    Fields:
        workspace_id:    Opaque, PELDRUN-generated identifier.
        repository_id:   Stable ID of the source repository.
        provider:        Provider that owns the repository.
        workspace_path:  Absolute path on disk (validated by path policy).
        current_branch:  Branch currently checked out in the workspace.
        base_sha:        Commit the workspace was created from.
        head_sha:        Latest commit known to PELDRUN (may lag HEAD).
        is_worktree:     True if the workspace is a Git worktree.
        is_clean:        True if there are no uncommitted changes.
        status:          High-level lifecycle status of the workspace.
        created_at:      Timezone-aware creation timestamp.
        updated_at:      Timezone-aware last-update timestamp.
        metadata:        Provider-agnostic extension bag (never secrets).
    """

    workspace_id: str
    repository_id: str
    provider: ProviderId
    workspace_path: str
    current_branch: str
    base_sha: str
    head_sha: str = ""
    is_worktree: bool = False
    is_clean: bool = True
    status: str = WorkspaceStatus.READY
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def with_status(self, status: str, **overrides: Any) -> "WorkspaceRef":
        """
        Return a new instance with the given status and optional field
        overrides. Never mutates the original instance.
        """
        return replace(
            self,
            status=status,
            updated_at=_utc_now(),
            **overrides,
        )


# ---------------------------------------------------------------------------
# Diff and file changes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileDiff:
    """
    A single file change within a diff.

    `patch` MAY be truncated for very large files; in that case
    `is_truncated` MUST be True and `patch` MUST NOT be treated as the
    full content.
    """

    path: str
    change_type: FileChangeType
    additions: int = 0
    deletions: int = 0
    old_path: Optional[str] = None
    patch: Optional[str] = None
    is_binary: bool = False
    is_truncated: bool = False


@dataclass(frozen=True)
class DiffSummary:
    """
    A canonical, hashable snapshot of a workspace diff.

    The `sha256` field is computed from the canonical JSON of the file
    list and MUST be stable across runs, platforms, and orderings. It
    is used as the binding value in approval tickets: any material
    change to the diff MUST change this hash.
    """

    files: Sequence[FileDiff] = field(default_factory=tuple)
    base_sha: str = ""
    head_sha: str = ""
    total_additions: int = 0
    total_deletions: int = 0

    def sha256(self) -> str:
        """
        Return the canonical sha256 of this diff.

        The hash is order-independent for the file list: files are
        sorted by `path` before hashing. Only material fields are
        included (paths, change types, line counts, binary flag).
        Patches are excluded to keep the hash stable across formatting
        differences in the diff renderer.
        """
        material = {
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "files": [
                {
                    "path": f.path,
                    "old_path": f.old_path,
                    "change_type": f.change_type.value,
                    "additions": f.additions,
                    "deletions": f.deletions,
                    "is_binary": f.is_binary,
                }
                for f in sorted(self.files, key=lambda x: x.path)
            ],
        }
        return _sha256_of(material)

    def touched_paths(self) -> Sequence[str]:
        """Return the sorted list of paths affected by this diff."""
        return tuple(sorted({f.path for f in self.files}))


# ---------------------------------------------------------------------------
# Checks (CI status)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckRunRef:
    """
    A reference to a single CI check run.
    """

    name: str
    status: CheckStatus = CheckStatus.UNKNOWN
    conclusion: CheckConclusion = CheckConclusion.UNKNOWN
    details_url: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


@dataclass(frozen=True)
class CheckSummary:
    """
    Aggregated CI status for a commit or branch.
    """

    total: int = 0
    succeeded: int = 0
    failed: int = 0
    pending: int = 0
    skipped: int = 0
    runs: Sequence[CheckRunRef] = field(default_factory=tuple)

    def all_passed(self) -> bool:
        """Return True only if every check succeeded or was skipped."""
        if self.total == 0:
            return False
        return self.failed == 0 and self.pending == 0


# ---------------------------------------------------------------------------
# Operation lifecycle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OperationEvent:
    """
    A single append-only event in the operation journal.

    Events are immutable. Each event has a monotonically increasing
    `sequence` within its parent operation. The `payload` mapping MUST
    NOT contain secrets; redaction happens before the event is created.
    """

    operation_id: str
    sequence: int
    event_type: OperationEventType
    occurred_at: datetime
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OperationRecord:
    """
    Durable state of a source-control operation.

    This is the aggregate root for the operation lifecycle. All
    transitions MUST go through `operations.service.OperationService`;
    direct mutation is prohibited by the frozen dataclass.

    Fields:
        operation_id:     Opaque, PELDRUN-generated identifier.
        run_id:           The agent run this operation belongs to.
        provider:         Provider that owns the repository.
        repository_id:    Stable ID of the repository.
        kind:             Operation kind (e.g. "push_branch").
        status:           Current lifecycle status.
        base_sha:         Commit the operation was based on.
        source_branch:    Branch the changes are on.
        target_branch:    Branch the change request targets.
        workspace_id:     Workspace used for local edits (if any).
        diff_sha256:      Canonical hash of the reviewed diff.
        approval_id:      ID of the consumed approval (if any).
        created_at:       Timezone-aware creation timestamp.
        updated_at:       Timezone-aware last-update timestamp.
        error_code:       Last error category (if any).
        error_message:    Last redacted error message (if any).
        change_request:   Change request created by this operation.
        metadata:         Extension bag (never secrets).
    """

    operation_id: str
    run_id: str
    provider: ProviderId
    repository_id: str
    kind: str
    status: OperationStatus = OperationStatus.CREATED
    base_sha: str = ""
    source_branch: Optional[str] = None
    target_branch: Optional[str] = None
    workspace_id: Optional[str] = None
    diff_sha256: Optional[str] = None
    approval_id: Optional[str] = None
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = field(default_factory=_utc_now)
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    change_request: Optional[ChangeRequestRef] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def is_terminal(self) -> bool:
        """Return True if the operation has reached a terminal status."""
        return self.status in {
            OperationStatus.PUSHED,
            OperationStatus.CHANGE_REQUEST_CREATED,
            OperationStatus.CANCELLED,
            OperationStatus.FAILED_FINAL,
            OperationStatus.REJECTED,
        }

    def with_status(
        self, status: OperationStatus, **overrides: Any
    ) -> "OperationRecord":
        """Return a new instance with the given status and overrides."""
        return replace(
            self,
            status=status,
            updated_at=_utc_now(),
            **overrides,
        )


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalTicket:
    """
    A single-use, content-bound approval for a remote side effect.

    The ticket binds the user's approval to an exact operation snapshot.
    Before the side effect executes, the backend MUST recompute the
    current diff hash and re-fetch the remote base SHA, then verify that
    both still match the values recorded here. Any material change
    invalidates the ticket.

    This is the ONLY definition of `ApprovalTicket` in the subsystem.
    Do NOT duplicate it in `security.approval`.
    """

    approval_id: str
    operation_id: str
    repository_id: str
    operation: str
    base_sha: str
    diff_sha256: str
    target_branch: str
    expires_at: datetime
    approved_by_user: bool = False
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: datetime = field(default_factory=_utc_now)
    consumed_at: Optional[datetime] = None

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        """Return True if the ticket has passed its expiry time."""
        moment = now or _utc_now()
        return moment >= self.expires_at

    def is_consumable(self, now: Optional[datetime] = None) -> bool:
        """
        Return True only if the ticket can still be consumed.

        A ticket is consumable when it is pending or granted, not yet
        expired, and not already consumed.
        """
        if self.status not in (ApprovalStatus.PENDING, ApprovalStatus.GRANTED):
            return False
        if self.status == ApprovalStatus.CONSUMED:
            return False
        if self.consumed_at is not None:
            return False
        if self.is_expired(now):
            return False
        return True


# ---------------------------------------------------------------------------
# Authentication sessions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthSession:
    """
    A short-lived authorization session used during the OAuth callback.

    This model is intentionally free of secrets. The actual access and
    refresh tokens are stored in the OS credential vault and referenced
    only by `vault_key`.
    """

    session_id: str
    provider: ProviderId
    state: str
    code_verifier: Optional[str] = None
    redirect_uri: Optional[str] = None
    base_url: Optional[str] = None
    created_at: datetime = field(default_factory=_utc_now)
    expires_at: Optional[datetime] = None
    consumed: bool = False

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        """Return True if the session has expired or was already used."""
        if self.consumed:
            return True
        if self.expires_at is None:
            return False
        moment = now or _utc_now()
        return moment >= self.expires_at
