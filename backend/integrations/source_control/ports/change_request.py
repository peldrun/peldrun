# backend/integrations/source_control/ports/change_request.py
"""
Change request (pull request / merge request) ports and value objects
for the Source Control Integration subsystem.

This module re-exports `ChangeRequestPort` from `ports.provider` and
adds the value objects used by the application layer to build, update,
and merge change requests:

    - `ChangeRequestDraft`       : parameters for creating a request.
    - `ChangeRequestUpdate`      : parameters for updating a request.
    - `ChangeRequestListOptions` : filters for listing requests.
    - `MergeOptions`             : parameters for merging a request.
    - `ReviewState`              : normalized review decision.
    - `MergeStrategy`            : normalized merge strategy.

The provider-neutral `ChangeRequestRef` is defined in
`domain.models` and is the only object returned by provider adapters.
The dataclasses here are used internally by the application service
and by the agent tools; they are not persisted directly.

Design rules:
    - A change request MUST always be bound to a specific repository,
      source branch, and target branch.
    - Creation defaults to `draft=True`. Promoting to ready-for-review
      is a separate explicit action.
    - Merging is disabled by default in PELDRUN policy. Adapters MUST
      raise `PolicyDeniedError` if the policy forbids it.
    - The `body` field MAY be augmented with provider-neutral
      metadata (operation ID, diff hash) for traceability, but the
      metadata MUST NOT contain secrets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Sequence

from ..domain.enums import ChangeRequestState  # noqa: F401 (re-exported)
from ..domain.models import (
    BranchRef,  # noqa: F401 (re-exported for convenience)
    ChangeRequestRef,
    RepositoryRef,
)
from ..errors import ValidationError
from .provider import ChangeRequestPort  # re-exported

__all__ = [
    # Re-exports
    "ChangeRequestPort",
    "ChangeRequestRef",
    "ChangeRequestState",
    # Enums
    "MergeStrategy",
    "ReviewState",
    # Value objects
    "ChangeRequestDraft",
    "ChangeRequestUpdate",
    "ChangeRequestListOptions",
    "MergeOptions",
    # Helpers
    "validate_draft",
]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class MergeStrategy(str, Enum):
    """
    Normalized merge strategy.

    Not every provider supports every strategy. The
    `CapabilityChecker` gates the corresponding merge operation; if
    the chosen strategy is unsupported, the adapter MUST raise
    `UnsupportedCapabilityError` rather than silently falling back.
    """

    MERGE_COMMIT = "merge_commit"
    SQUASH = "squash"
    REBASE = "rebase"
    FAST_FORWARD = "fast_forward"


class ReviewState(str, Enum):
    """
    Normalized state of the most recent review decision.

    Providers differ in how they model reviews. Adapters MUST map
    provider-specific states to one of these values, defaulting to
    `UNKNOWN` when no mapping is possible.
    """

    PENDING = "pending"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    COMMENTED = "commented"
    DISMISSED = "dismissed"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChangeRequestDraft:
    """
    Parameters for creating a change request.

    The `repository` is bound to the draft so that the adapter cannot
    accidentally target a different repository. The `source_branch`
    and `target_branch` MUST differ.

    Fields:
        repository:     The target repository.
        source_branch:  Branch containing the changes.
        target_branch:  Branch the request targets (e.g. `"main"`).
        title:          Short title (max 256 characters).
        body:           Description. Markdown is supported.
        draft:          When True, create the request as a draft.
        labels:         Optional labels (max 20).
        reviewers:      Optional reviewer usernames (max 15).
        assignees:      Optional assignee usernames (max 10).
        milestone:      Optional milestone name.
        metadata:       Provider-neutral extension bag. MUST NOT
                        contain secrets.
    """

    repository: RepositoryRef
    source_branch: str
    target_branch: str
    title: str
    body: str = ""
    draft: bool = True
    labels: Sequence[str] = field(default_factory=tuple)
    reviewers: Sequence[str] = field(default_factory=tuple)
    assignees: Sequence[str] = field(default_factory=tuple)
    milestone: Optional[str] = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ChangeRequestUpdate:
    """
    Partial update for an existing change request.

    Every field is optional; `None` means "leave unchanged". An empty
    `title` is treated as "no change", since providers reject empty
    titles. To clear a field, use an explicit sentinel value documented
    per provider.

    Fields:
        title:      New title, or `None`.
        body:       New body, or `None`.
        state:      New state (`OPEN`, `CLOSED`), or `None`.
        draft:      Promote or demote draft status, or `None`.
        labels:     Replace labels when provided (empty tuple clears).
        reviewers:  Replace reviewers when provided.
        base_branch: Retarget the request to a different base.
    """

    title: Optional[str] = None
    body: Optional[str] = None
    state: Optional[ChangeRequestState] = None
    draft: Optional[bool] = None
    labels: Optional[Sequence[str]] = None
    reviewers: Optional[Sequence[str]] = None
    base_branch: Optional[str] = None


@dataclass(frozen=True)
class ChangeRequestListOptions:
    """
    Filters for listing change requests.

    Filters are applied by the adapter using provider-native query
    parameters. Unsupported filters MUST be ignored rather than
    raising, so that a listing remains available across providers.
    """

    state: Optional[ChangeRequestState] = None
    source_branch: Optional[str] = None
    target_branch: Optional[str] = None
    author: Optional[str] = None
    reviewer: Optional[str] = None
    label: Optional[str] = None
    page: int = 1
    page_size: int = 30


@dataclass(frozen=True)
class MergeOptions:
    """
    Parameters for merging a change request.

    `expected_head_sha`, when provided, MUST be matched by the remote
    before the merge is accepted. This prevents merging a request
    whose head has been updated since the user reviewed it.
    """

    strategy: MergeStrategy = MergeStrategy.MERGE_COMMIT
    commit_title: Optional[str] = None
    commit_message: Optional[str] = None
    delete_source_branch: bool = False
    expected_head_sha: Optional[str] = None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


#: Maximum title length accepted by GitHub, GitLab, and Bitbucket.
_MAX_TITLE_LENGTH = 256

#: Maximum body length accepted by GitHub (GitLab and Bitbucket use
#: larger limits but we conservatively enforce GitHub's).
_MAX_BODY_LENGTH = 65_536

#: Maximum number of labels that may be attached to a single request.
_MAX_LABELS = 20

#: Maximum number of reviewers that may be requested.
_MAX_REVIEWERS = 15

#: Maximum number of assignees.
_MAX_ASSIGNEES = 10


def validate_draft(draft: ChangeRequestDraft) -> None:
    """
    Validate a `ChangeRequestDraft` before sending it to a provider.

    Raises `ValidationError` with a precise message if any constraint
    is violated. The function never mutates the draft.

    Checks:
        - source and target branches differ.
        - title is non-empty and within the length limit.
        - body is within the length limit.
        - labels, reviewers, and assignees do not exceed their limits.
    """
    if not draft.source_branch or not draft.target_branch:
        raise ValidationError(
            "Both source_branch and target_branch are required.",
            details={"field": "source_branch, target_branch"},
        )

    if draft.source_branch == draft.target_branch:
        raise ValidationError(
            "source_branch and target_branch must differ.",
            details={
                "source_branch": draft.source_branch,
                "target_branch": draft.target_branch,
            },
        )

    title = (draft.title or "").strip()
    if not title:
        raise ValidationError(
            "Change request title must not be empty.",
            details={"field": "title"},
        )
    if len(title) > _MAX_TITLE_LENGTH:
        raise ValidationError(
            f"Change request title exceeds {_MAX_TITLE_LENGTH} characters.",
            details={"field": "title", "length": len(title)},
        )

    if len(draft.body or "") > _MAX_BODY_LENGTH:
        raise ValidationError(
            f"Change request body exceeds {_MAX_BODY_LENGTH} characters.",
            details={"field": "body", "length": len(draft.body or "")},
        )

    if len(draft.labels) > _MAX_LABELS:
        raise ValidationError(
            f"Too many labels attached (max {_MAX_LABELS}).",
            details={"field": "labels", "count": len(draft.labels)},
        )
    if len(draft.reviewers) > _MAX_REVIEWERS:
        raise ValidationError(
            f"Too many reviewers requested (max {_MAX_REVIEWERS}).",
            details={"field": "reviewers", "count": len(draft.reviewers)},
        )
    if len(draft.assignees) > _MAX_ASSIGNEES:
        raise ValidationError(
            f"Too many assignees selected (max {_MAX_ASSIGNEES}).",
            details={"field": "assignees", "count": len(draft.assignees)},
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def to_provider_body(
    body: str,
    *,
    operation_id: Optional[str] = None,
    diff_sha256: Optional[str] = None,
) -> str:
    """
    Return a change request body augmented with a hidden HTML comment
    carrying provider-neutral metadata.

    The comment is not rendered by any provider and is used by PELDRUN
    to correlate the change request with its originating operation. The
    metadata MUST NOT contain secrets; only identifiers and hashes are
    allowed.

    The function is idempotent for the same inputs.
    """
    parts: list[str] = []
    metadata_fields: list[str] = []
    if operation_id:
        metadata_fields.append(f"operation_id={operation_id}")
    if diff_sha256:
        metadata_fields.append(f"diff_sha256={diff_sha256}")

    if metadata_fields:
        metadata = "; ".join(metadata_fields)
        parts.append(f"<!-- peldrun:{metadata} -->")

    if body:
        parts.append(body)

    return "\n\n".join(parts) if parts else ""
