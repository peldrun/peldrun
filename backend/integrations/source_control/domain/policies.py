# backend/integrations/source_control/domain/policies.py
"""
Domain policies for the Source Control Integration subsystem.

This module defines the pure business rules that govern:

    - Which branches are considered protected and require elevated
      approval before any write.
    - Which files are considered CI/workflow files whose modification
      requires elevated approval.
    - Which operations require explicit human approval, and which
      additional warnings must be shown.
    - Which file-path patterns must be rejected (secrets, binaries,
      unwanted artefacts).

Policies are pure functions of their inputs. They MUST NOT perform
I/O, read environment variables, or depend on any infrastructure. The
`security.policy_engine` module is responsible for evaluating policies
against a concrete operation and producing an auditable decision.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Optional, Sequence

from .enums import ProviderId
from .models import BranchRef, RepositoryRef

__all__ = [
    "BranchPolicy",
    "WorkflowPolicy",
    "ApprovalPolicy",
    "SourceControlPolicy",
    "PolicyDecision",
    "is_protected_branch",
    "is_workflow_path",
    "is_always_allowed_path",
    "evaluate_policy",
    "DEFAULT_PROTECTED_BRANCH_PATTERNS",
    "DEFAULT_WORKFLOW_PATH_PATTERNS",
    "DEFAULT_SECRET_PATH_PATTERNS",
]


# ---------------------------------------------------------------------------
# Default pattern tables
# ---------------------------------------------------------------------------


# Branches whose name matches any of these patterns are considered
# protected by default and require elevated approval before push.
# Patterns follow `fnmatch` syntax and are compared case-insensitively.
DEFAULT_PROTECTED_BRANCH_PATTERNS: tuple[str, ...] = (
    # Primary integration branches
    "main",
    "master",
    "trunk",
    # Development and release branches
    "develop",
    "development",
    "dev",
    "release",
    "release/*",
    "releases/*",
    # Staging and production branches
    "stage",
    "staging",
    "staging/*",
    "prod",
    "prod/*",
    "production",
    "production/*",
    # Hotfix branches are usually short-lived but still protected
    "hotfix/*",
    # Long-lived infrastructure branches
    "infra",
    "infra/*",
)


# Files matching any of these patterns are considered CI/workflow
# definitions. Modifying them requires an elevated approval and
# produces a prominent warning in the review UI. Patterns are matched
# case-sensitively because CI providers treat these paths
# case-sensitively.
DEFAULT_WORKFLOW_PATH_PATTERNS: tuple[str, ...] = (
    # GitHub Actions
    ".github/workflows/*",
    ".github/workflows/**",
    ".github/actions/*",
    ".github/actions/**",
    # GitLab CI
    ".gitlab-ci.yml",
    ".gitlab-ci.yaml",
    ".gitlab/**",
    # Bitbucket Pipelines
    "bitbucket-pipelines.yml",
    "bitbucket-pipelines.yaml",
    # CircleCI
    ".circleci/*",
    ".circleci/**",
    # Travis CI
    ".travis.yml",
    ".travis.yaml",
    # Jenkins
    "Jenkinsfile",
    "**/Jenkinsfile",
    # Azure Pipelines
    "azure-pipelines.yml",
    "azure-pipelines.yaml",
    ".azure/**",
    # Drone CI
    ".drone.yml",
    ".drone.yaml",
    # Buildkite
    ".buildkite/*",
    ".buildkite/**",
    # Argo Workflows / Argo CD
    ".argo/**",
    # Terraform / infrastructure-as-code
    "*.tf",
    "*.tfvars",
    "terraform.tfstate",
    # Container build
    "Dockerfile",
    "**/Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    # Nix
    "flake.nix",
    "shell.nix",
    "default.nix",
)


# Files matching any of these patterns are treated as potential secret
# carriers. Matching these patterns does NOT block a change outright,
# but the review UI MUST highlight them and the approval flow MUST
# require an explicit override with a warning.
DEFAULT_SECRET_PATH_PATTERNS: tuple[str, ...] = (
    # Environment files
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    # Credentials and keys
    "*.pem",
    "*.key",
    "*.pfx",
    "*.p12",
    "*.jks",
    "*.keystore",
    "id_rsa",
    "id_rsa.*",
    "id_ed25519",
    "id_ed25519.*",
    # Cloud provider credentials
    ".aws/credentials",
    ".aws/config",
    "**/.aws/credentials",
    "gcloud/**",
    ".config/gcloud/**",
    # Kubernetes
    "kubeconfig",
    "**/kubeconfig",
    ".kube/config",
    # Package manager auth
    ".npmrc",
    ".pypirc",
    ".netrc",
    # Terraform state (often contains secrets)
    "*.tfstate",
    "*.tfstate.backup",
    # Private key stores
    "*.p8",
    "*.p10",
    "*.p12",
)


# ---------------------------------------------------------------------------
# Policy dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BranchPolicy:
    """
    Rules for branch protection and default-branch writes.

    A branch is considered protected if its name matches any pattern in
    `protected_patterns`, compared case-insensitively after trimming
    surrounding whitespace.
    """

    protected_patterns: Sequence[str] = field(
        default_factory=lambda: DEFAULT_PROTECTED_BRANCH_PATTERNS
    )
    allow_default_branch_push: bool = False
    allow_force_push: bool = False
    require_approval_for_new_branch: bool = False

    def matches(self, branch_name: str) -> bool:
        """
        Return True if `branch_name` matches any protected pattern.

        Comparison is performed case-insensitively on a trimmed copy of
        the input. This accommodates user input like `" Master "` and
        `"MAIN"` while keeping Git's own case sensitivity intact for
        actual operations.
        """
        if not branch_name:
            return False
        cleaned = branch_name.strip().lower()
        for pattern in self.protected_patterns:
            if fnmatch.fnmatchcase(cleaned, pattern.lower()):
                return True
        return False


@dataclass(frozen=True)
class WorkflowPolicy:
    """
    Rules for CI/workflow files and secret-bearing paths.
    """

    workflow_patterns: Sequence[str] = field(
        default_factory=lambda: DEFAULT_WORKFLOW_PATH_PATTERNS
    )
    secret_patterns: Sequence[str] = field(
        default_factory=lambda: DEFAULT_SECRET_PATH_PATTERNS
    )
    require_elevated_approval_for_workflow: bool = True
    require_explicit_override_for_secret_paths: bool = True

    def is_workflow_path(self, path: str) -> bool:
        """
        Return True if `path` matches a CI/workflow pattern.

        Matching is case-sensitive because CI providers treat these
        paths case-sensitively on Linux and macOS. On Windows the Git
        index preserves the case as committed.
        """
        return _matches_any(path, self.workflow_patterns)

    def is_secret_path(self, path: str) -> bool:
        """Return True if `path` matches a potential secret pattern."""
        return _matches_any(path, self.secret_patterns)


@dataclass(frozen=True)
class ApprovalPolicy:
    """
    Rules for which operations require explicit human approval.
    """

    require_approval_for_push: bool = True
    require_approval_for_change_request: bool = True
    require_approval_for_merge: bool = True
    require_approval_for_branch_delete: bool = True
    require_approval_for_default_branch_push: bool = True
    approval_ttl_seconds: int = 15 * 60  # 15 minutes
    approval_single_use: bool = True


@dataclass(frozen=True)
class SourceControlPolicy:
    """
    Root policy aggregate for a source-control connection.

    A single instance governs every operation executed against a given
    repository. Policy resolution is the responsibility of
    `security.policy_engine.resolve_policy`, which may consult the
    repository, the connection, and the user's settings.
    """

    provider: Optional[ProviderId] = None
    repository_id: Optional[str] = None
    branch: BranchPolicy = field(default_factory=BranchPolicy)
    workflow: WorkflowPolicy = field(default_factory=WorkflowPolicy)
    approval: ApprovalPolicy = field(default_factory=ApprovalPolicy)
    allow_read: bool = True
    allow_local_edits: bool = True
    allow_run_checks: bool = True
    allow_push_new_branch: bool = True
    allow_push_existing_branch: bool = False
    allow_create_change_request: bool = True
    allow_merge: bool = False
    allow_delete_branch: bool = False
    allow_modify_settings: bool = False

    # Convenience proxies -------------------------------------------------

    def is_protected_branch(self, branch_name: str) -> bool:
        """Return True if the branch is protected under this policy."""
        return self.branch.matches(branch_name)

    def is_workflow_path(self, path: str) -> bool:
        """Return True if the path is a workflow/CI definition."""
        return self.workflow.is_workflow_path(path)

    def is_secret_path(self, path: str) -> bool:
        """Return True if the path is a potential secret carrier."""
        return self.workflow.is_secret_path(path)


# ---------------------------------------------------------------------------
# Policy decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyDecision:
    """
    Result of evaluating a policy against a concrete request.

    `allowed` reflects the policy outcome only. It does NOT mean the
    operation may proceed: the approval layer is evaluated separately.
    The two decisions are combined by the application service.
    """

    allowed: bool
    reason: Optional[str] = None
    requires_approval: bool = False
    warnings: Sequence[str] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Module-level convenience functions (kept for backward compatibility)
# ---------------------------------------------------------------------------


def is_protected_branch(
    branch_name: str, policy: Optional[BranchPolicy] = None
) -> bool:
    """
    Return True if `branch_name` is protected under `policy`.

    This function exists for backward compatibility with callers that
    do not hold a full `SourceControlPolicy`. New code SHOULD prefer
    `SourceControlPolicy.is_protected_branch`.

    The comparison is case-insensitive and trims surrounding
    whitespace, matching the behavior expected by the test suite.
    """
    active = policy or BranchPolicy()
    return active.matches(branch_name)


def is_workflow_path(
    path: str, policy: Optional[WorkflowPolicy] = None
) -> bool:
    """
    Return True if `path` is a CI/workflow definition.

    Exists for backward compatibility. New code SHOULD prefer
    `SourceControlPolicy.is_workflow_path`.
    """
    active = policy or WorkflowPolicy()
    return active.is_workflow_path(path)


def is_always_allowed_path(path: str) -> bool:
    """
    Return True for paths that must always be rejected because they
    would leak sensitive data or break the repository.

    `.git/**` is rejected to prevent accidental Git-internal writes.
    Secret-bearing paths are NOT rejected here; they are surfaced as
    warnings and handled by the approval layer.
    """
    if not path:
        return False
    normalized = path.replace("\\", "/")
    blocked = (
        ".git/",
        ".git\\",
        "../",
        "..\\",
    )
    return any(token in normalized for token in blocked)


# ---------------------------------------------------------------------------
# High-level evaluation
# ---------------------------------------------------------------------------


def evaluate_policy(
    policy: SourceControlPolicy,
    *,
    operation: str,
    repository: Optional[RepositoryRef] = None,
    target_branch: Optional[str] = None,
    source_branch: Optional[str] = None,
    target_branch_ref: Optional[BranchRef] = None,
    changed_paths: Sequence[str] = (),
) -> PolicyDecision:
    """
    Evaluate `policy` against a concrete operation and return a
    `PolicyDecision`.

    This function is pure and side-effect free. It is the single entry
    point used by the application layer to decide whether an operation
    is allowed, whether it requires approval, and which warnings must
    be surfaced to the user.

    Rules:
        - Reading operations are always allowed if `allow_read` is True.
        - Pushes to protected branches are denied by default.
        - Pushes that modify workflow/CI files require approval and
          produce a warning.
        - Pushes that modify potential secret paths require explicit
          override and produce a warning.
        - Merging and destructive operations are denied by default.
    """
    warnings: list[str] = []
    requires_approval = False

    # Repository identity check
    if repository is not None and policy.repository_id is not None:
        if policy.repository_id != repository.repository_id:
            return PolicyDecision(
                allowed=False,
                reason="Repository does not match the active policy.",
            )

    # Provider check
    if repository is not None and policy.provider is not None:
        if policy.provider != repository.provider:
            return PolicyDecision(
                allowed=False,
                reason="Provider does not match the active policy.",
            )

    # Read-only operations
    if operation in {"read_file", "list_branches", "get_repository", "list_repositories"}:
        if not policy.allow_read:
            return PolicyDecision(allowed=False, reason="Reads are disabled.")
        return PolicyDecision(allowed=True)

    # Local edit operations
    if operation in {"create_workspace", "edit_file", "create_branch"}:
        if not policy.allow_local_edits:
            return PolicyDecision(
                allowed=False, reason="Local edits are disabled."
            )
        return PolicyDecision(allowed=True)

    # Check execution
    if operation == "run_checks":
        if not policy.allow_run_checks:
            return PolicyDecision(
                allowed=False, reason="Running checks is disabled."
            )
        requires_approval = True
        warnings.append(
            "Running checks executes untrusted repository code."
        )
        return PolicyDecision(
            allowed=True,
            requires_approval=requires_approval,
            warnings=tuple(warnings),
        )

    # Push operations
    if operation in {"push_branch", "push"}:
        if target_branch is None:
            return PolicyDecision(
                allowed=False,
                reason="Target branch is required for push evaluation.",
            )

        # Protected branch
        if policy.is_protected_branch(target_branch):
            if not policy.branch.allow_default_branch_push:
                return PolicyDecision(
                    allowed=False,
                    reason=(
                        f"Branch '{target_branch}' is protected; direct "
                        "pushes to protected branches are disabled by policy."
                    ),
                )
            requires_approval = True
            warnings.append(
                f"Push targets protected branch '{target_branch}'."
            )

        # Workflow files
        for path in changed_paths:
            if policy.is_workflow_path(path):
                warnings.append(
                    f"Change modifies CI/workflow file: {path}"
                )
                if policy.workflow.require_elevated_approval_for_workflow:
                    requires_approval = True
            if policy.is_secret_path(path):
                warnings.append(
                    f"Change touches a potential secret file: {path}"
                )
                requires_approval = True

        # Base approval requirement
        if policy.approval.require_approval_for_push:
            requires_approval = True

        return PolicyDecision(
            allowed=True,
            requires_approval=requires_approval,
            warnings=tuple(warnings),
        )

    # Change request creation
    if operation in {"create_change_request", "create_pull_request", "create_merge_request"}:
        if not policy.allow_create_change_request:
            return PolicyDecision(
                allowed=False,
                reason="Creating change requests is disabled by policy.",
            )
        requires_approval = policy.approval.require_approval_for_change_request
        return PolicyDecision(
            allowed=True,
            requires_approval=requires_approval,
            warnings=tuple(warnings),
        )

    # Merge
    if operation == "merge_change_request":
        if not policy.allow_merge:
            return PolicyDecision(
                allowed=False,
                reason="Merging is disabled by policy.",
            )
        requires_approval = policy.approval.require_approval_for_merge
        return PolicyDecision(
            allowed=True,
            requires_approval=requires_approval,
            warnings=tuple(warnings),
        )

    # Branch deletion
    if operation == "delete_branch":
        if not policy.allow_delete_branch:
            return PolicyDecision(
                allowed=False,
                reason="Deleting branches is disabled by policy.",
            )
        requires_approval = policy.approval.require_approval_for_branch_delete
        return PolicyDecision(
            allowed=True,
            requires_approval=requires_approval,
            warnings=tuple(warnings),
        )

    # Unknown operation
    return PolicyDecision(
        allowed=False,
        reason=f"Unknown operation '{operation}'.",
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _matches_any(path: str, patterns: Sequence[str]) -> bool:
    """
    Return True if `path` matches any pattern using Git-style globbing.

    Supports `**` for recursive matching, mirroring Git's path
    semantics. Both forward and backward slashes are normalized.
    """
    if not path:
        return False
    normalized = path.replace("\\", "/")
    for pattern in patterns:
        if fnmatch.fnmatchcase(normalized, pattern):
            return True
        # Support `**` semantics by also testing the basename for
        # patterns without slashes.
        if "/" not in pattern and fnmatch.fnmatchcase(
            normalized.rsplit("/", 1)[-1], pattern
        ):
            return True
    return False
