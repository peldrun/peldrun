from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Optional, Sequence

from .enums import OperationKind


@dataclass(frozen=True)
class BranchPolicy:
    """Rules governing branch mutation and protection constraints."""

    protected_patterns: tuple[str, ...] = (
        "main",
        "master",
        "release/*",
        "prod*",
        "staging*",
    )
    allow_direct_push: bool = False
    require_pull_request: bool = True


@dataclass(frozen=True)
class WorkflowProtectionPolicy:
    """Rules guarding against unauthorized modifications to CI/CD workflows."""

    workflow_patterns: tuple[str, ...] = (
        ".github/workflows/*",
        ".gitlab-ci.yml",
        "bitbucket-pipelines.yml",
    )
    require_elevated_approval: bool = True
    warn_on_modification: bool = True


@dataclass(frozen=True)
class SourceControlPolicy:
    """Top-level security and execution policy for source control operations."""

    allow_remote_push: bool = True
    require_approval_for_push: bool = True
    require_approval_for_pr: bool = True
    allow_merging: bool = False
    allow_branch_deletion: bool = False
    max_files_in_diff: int = 250
    max_diff_bytes: int = 5 * 1024 * 1024  # 5 MB safety ceiling
    approval_ttl_minutes: int = 60
    branch_policy: BranchPolicy = field(default_factory=BranchPolicy)
    workflow_policy: WorkflowProtectionPolicy = field(
        default_factory=WorkflowProtectionPolicy
    )


@dataclass(frozen=True)
class PolicyEvaluationResult:
    """Outcome of evaluating an intended operation against current policies."""

    allowed: bool
    requires_approval: bool
    requires_elevated_approval: bool = False
    warnings: tuple[str, ...] = field(default_factory=tuple)
    denial_reason: Optional[str] = None


def is_protected_branch(
    branch_name: str, policy: Optional[SourceControlPolicy] = None
) -> bool:
    """Checks whether the specified branch matches any protected branch pattern."""
    active_policy = policy or SourceControlPolicy()
    clean_name = branch_name.strip()
    return any(
        fnmatch.fnmatchcase(clean_name, pat)
        for pat in active_policy.branch_policy.protected_patterns
    )


def is_workflow_path(
    file_path: str, policy: Optional[SourceControlPolicy] = None
) -> bool:
    """Checks whether the relative path points to a sensitive CI/CD workflow file."""
    active_policy = policy or SourceControlPolicy()
    normalized = file_path.replace("\\", "/").strip().lstrip("/")
    return any(
        fnmatch.fnmatchcase(normalized, pat)
        for pat in active_policy.workflow_policy.workflow_patterns
    )


def evaluate_operation(
    operation: OperationKind,
    target_branch: str = "",
    changed_files: Sequence[str] = (),
    policy: Optional[SourceControlPolicy] = None,
) -> PolicyEvaluationResult:
    """Evaluates an intended source control operation against the active policy.

    Enforces mandatory checks for branch protection, sensitive file modifications,
    and side-effect approvals.
    """
    active_policy = policy or SourceControlPolicy()
    warnings: list[str] = []

    # Detect modifications to sensitive CI/CD workflow files
    contains_workflows = any(
        is_workflow_path(f, active_policy) for f in changed_files
    )
    if contains_workflows and active_policy.workflow_policy.warn_on_modification:
        warnings.append(
            "Changes include sensitive CI/CD workflow automation files."
        )

    # 1. Read-only and local inspection operations
    read_only_kinds = {
        OperationKind.LIST_REPOSITORIES,
        OperationKind.READ_METADATA,
        OperationKind.READ_FILE,
        OperationKind.LIST_BRANCHES,
        OperationKind.CREATE_WORKSPACE,
        OperationKind.CREATE_BRANCH,
        OperationKind.INSPECT_DIFF,
        OperationKind.RUN_CHECKS,
        OperationKind.REQUEST_APPROVAL,
    }
    if operation in read_only_kinds:
        return PolicyEvaluationResult(
            allowed=True,
            requires_approval=False,
            warnings=tuple(warnings),
        )

    # 2. Remote push operations
    if operation == OperationKind.PUSH_BRANCH:
        if not active_policy.allow_remote_push:
            return PolicyEvaluationResult(
                allowed=False,
                requires_approval=False,
                denial_reason="Remote push operations are globally disabled by policy.",
            )

        if target_branch and is_protected_branch(target_branch, active_policy):
            if not active_policy.branch_policy.allow_direct_push:
                return PolicyEvaluationResult(
                    allowed=False,
                    requires_approval=False,
                    denial_reason=(
                        f"Direct push to protected branch '{target_branch}' is "
                        "prohibited. Use a feature branch and pull request."
                    ),
                )

        requires_elevated = (
            contains_workflows
            and active_policy.workflow_policy.require_elevated_approval
        )
        return PolicyEvaluationResult(
            allowed=True,
            requires_approval=active_policy.require_approval_for_push,
            requires_elevated_approval=requires_elevated,
            warnings=tuple(warnings),
        )

    # 3. Change request (PR/MR) creation
    if operation == OperationKind.CREATE_CHANGE_REQUEST:
        requires_elevated = (
            contains_workflows
            and active_policy.workflow_policy.require_elevated_approval
        )
        return PolicyEvaluationResult(
            allowed=True,
            requires_approval=active_policy.require_approval_for_pr,
            requires_elevated_approval=requires_elevated,
            warnings=tuple(warnings),
        )

    # 4. Change request merging
    if operation == OperationKind.MERGE_CHANGE_REQUEST:
        if not active_policy.allow_merging:
            return PolicyEvaluationResult(
                allowed=False,
                requires_approval=False,
                denial_reason=(
                    "Automated merging of change requests is disabled by default. "
                    "Merge operations must be conducted via the provider web interface."
                ),
            )
        return PolicyEvaluationResult(
            allowed=True,
            requires_approval=True,
            warnings=tuple(warnings),
        )

    # 5. Branch deletion
    if operation == OperationKind.DELETE_BRANCH:
        if not active_policy.allow_branch_deletion:
            return PolicyEvaluationResult(
                allowed=False,
                requires_approval=False,
                denial_reason="Branch deletion is disabled by policy.",
            )
        if target_branch and is_protected_branch(target_branch, active_policy):
            return PolicyEvaluationResult(
                allowed=False,
                requires_approval=False,
                denial_reason=f"Protected branch '{target_branch}' cannot be deleted.",
            )
        return PolicyEvaluationResult(
            allowed=True,
            requires_approval=True,
            warnings=tuple(warnings),
        )

    # Default fallback for unrecognized operations
    return PolicyEvaluationResult(
        allowed=False,
        requires_approval=False,
        denial_reason=f"Operation '{operation.value}' is not permitted under current policy.",
    )
