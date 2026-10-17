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
    # Remote CI checks
    "CheckRunRef",
    "CheckSummary",
    "CheckSummaryRef",
    # Local check execution
    "CheckResult",
    "CheckSuite",
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


class WorkspaceStatus(str):
    """
    Lightweight string-typed namespace for workspace status values.

    Defined as a subclass of `str` (NOT `enum.Enum`) so that the
    status can be serialized transparently in JSON without a custom
    encoder, while still providing named constants for readability.

    Note: this class intentionally has a SINGLE base class (`str`).
    An earlier revision declared `class WorkspaceStatus(str, str)`,
    which raises `TypeError: duplicate base class str` at import
    time. Subclassing `str` alone is sufficient to make instances
    behave like strings.
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

    The `sha256()` method computes a stable hash from the canonical
    JSON of the file list. The hash MUST be stable across runs,
    platforms, and orderings. It is used as the binding value in
    approval tickets: any material change to the diff MUST change
    this hash.
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
# Remote CI checks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckRunRef:
    """
    A reference to a single CI check run reported by a remote provider.

    The `status` and `conclusion` fields follow the GitHub Actions
    vocabulary, which is the most expressive of the three supported
    providers. Adapters for GitLab and Bitbucket translate their own
    vocabulary into these values.
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

    The `total` field is the number of checks observed. `all_passed()`
    returns True only when there is at least one check and none failed
    or is still pending.
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


#: Alias for `CheckSummary`.
#:
#: Some callers (notably `providers/github/mapper.py`) refer to the
#: aggregated CI summary as `CheckSummaryRef`. This alias keeps that
#: name working without introducing a second, structurally identical
#: class. New code SHOULD use `CheckSummary`.
CheckSummaryRef = CheckSummary


# ---------------------------------------------------------------------------
# Local check execution
# ---------------------------------------------------------------------------
#
# `CheckResult` and `CheckSuite` describe the outcome of running check
# commands LOCALLY inside a workspace. They are distinct from the
# remote-CI types above (`CheckRunRef`, `CheckSummary`):
#
#     - `CheckResult` / `CheckSuite` : produced by the
#       `source_control.run_checks` tool after executing a
#       pre-registered command.
#     - `CheckRunRef` / `CheckSummary` : reported by a remote provider
#       (GitHub Actions, GitLab CI, Bitbucket Pipelines).


@dataclass(frozen=True)
class CheckResult:
    """
    Result of a single local check execution.

    Fields:
        check_id:           Stable identifier for this execution.
        name:               Name of the registered check that ran.
        status:             High-level outcome: `"success"`,
                            `"failure"`, `"skipped"`, or
                            `"timed_out"`.
        exit_code:          Process exit status, when available.
        duration_seconds:   Wall-clock duration of the execution.
        stdout:             Redacted, truncated standard output.
        stderr:             Redacted, truncated standard error.
        summary:            Optional human-readable summary.
    """

    check_id: str
    name: str
    status: str = "skipped"
    exit_code: Optional[int] = None
    duration_seconds: Optional[float] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    summary: str = ""

    @property
    def passed(self) -> bool:
        """
        Return True if the check completed successfully.

        A check that was skipped is considered passing for the
        purposes of suite aggregation, matching the behavior of most
        CI systems.
        """
        return self.status in {"success", "skipped"}


@dataclass(frozen=True)
class CheckSuite:
    """
    Collection of local check results for a single workspace.

    Aggregates the results of running multiple checks (typically from
    a single `run_checks` invocation) into one decision-oriented
    summary.

    Fields:
        suite_id:        Stable identifier for this suite execution.
        overall_status:  `"success"` if every check passed,
                         `"failure"` if any failed, `"skipped"` if
                         every check was skipped.
        checks:          The individual results, in execution order.
        has_failures:    Convenience flag mirroring `overall_status`.
    """

    suite_id: str
    overall_status: str = "skipped"
    checks: Sequence[CheckResult] = field(default_factory=tuple)
    has_failures: bool = False

    @classmethod
    def from_results(
        cls, suite_id: str, results: Sequence[CheckResult]
    ) -> "CheckSuite":
        """
        Build a suite from a sequence of check results.

        The overall status is computed as:
            - `"success"` if every check passed or was skipped,
              with at least one actual pass.
            - `"failure"` if any check failed.
            - `"skipped"` if every check was skipped.
        """
        results_tuple = tuple(results)
        if not results_tuple:
            return cls(
                suite_id=suite_id,
                overall_status="skipped",
                checks=(),
                has_failures=False,
            )

        has_failure = any(
            result.status in {"failure", "timed_out"}
            for result in results_tuple
        )
        has_success = any(
            result.status == "success" for result in results_tuple
        )

        if has_failure:
            overall = "failure"
        elif has_success:
            overall = "success"
        else:
            overall = "skipped"

        return cls(
            suite_id=suite_id,
            overall_status=overall,
            checks=results_tuple,
            has_failures=has_failure,
        )


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
