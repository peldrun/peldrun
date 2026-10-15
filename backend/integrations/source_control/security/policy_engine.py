from __future__ import annotations

import threading
from typing import Optional, Sequence

from ..domain.enums import OperationKind
from ..domain.policies import (
    PolicyEvaluationResult,
    SourceControlPolicy,
    evaluate_operation,
    is_protected_branch,
    is_workflow_path,
)
from ..errors import PermissionDeniedError


class PolicyEngine:
    """Thread-safe engine enforcing runtime source control policies and constraints."""

    def __init__(self, default_policy: Optional[SourceControlPolicy] = None) -> None:
        self._default_policy = default_policy or SourceControlPolicy()
        self._repository_policies: dict[str, SourceControlPolicy] = {}
        self._lock = threading.Lock()

    def get_policy(self, repository_id: Optional[str] = None) -> SourceControlPolicy:
        """Retrieves the effective policy for a repository or the global default."""
        with self._lock:
            if repository_id and repository_id in self._repository_policies:
                return self._repository_policies[repository_id]
            return self._default_policy

    def set_policy(self, repository_id: str, policy: SourceControlPolicy) -> None:
        """Configures a dedicated policy override for a specific repository."""
        if not repository_id:
            raise ValueError("Repository ID cannot be empty.")
        with self._lock:
            self._repository_policies[repository_id] = policy

    def remove_policy(self, repository_id: str) -> bool:
        """Removes a repository-specific policy override, resuming global fallback."""
        with self._lock:
            return self._repository_policies.pop(repository_id, None) is not None

    def evaluate(
        self,
        operation: OperationKind,
        repository_id: Optional[str] = None,
        target_branch: str = "",
        changed_files: Sequence[str] = (),
    ) -> PolicyEvaluationResult:
        """Evaluates an intended operation against the effective repository policy."""
        policy = self.get_policy(repository_id)
        return evaluate_operation(
            operation=operation,
            target_branch=target_branch,
            changed_files=changed_files,
            policy=policy,
        )

    def assert_permitted(
        self,
        operation: OperationKind,
        repository_id: Optional[str] = None,
        target_branch: str = "",
        changed_files: Sequence[str] = (),
    ) -> PolicyEvaluationResult:
        """Evaluates an operation and raises PermissionDeniedError if not permitted."""
        result = self.evaluate(
            operation=operation,
            repository_id=repository_id,
            target_branch=target_branch,
            changed_files=changed_files,
        )
        if not result.allowed:
            reason = result.denial_reason or f"Operation '{operation.value}' is denied by policy."
            raise PermissionDeniedError(
                message=reason,
                details={
                    "operation": operation.value,
                    "target_branch": target_branch,
                    "repository_id": repository_id or "global",
                },
            )
        return result

    def validate_diff_limits(
        self,
        file_count: int,
        diff_bytes: int,
        repository_id: Optional[str] = None,
    ) -> None:
        """Validates that a proposed diff does not exceed configured safety bounds."""
        policy = self.get_policy(repository_id)
        if file_count > policy.max_files_in_diff:
            raise PermissionDeniedError(
                message=(
                    f"Diff exceeds maximum file count limit: {file_count} files "
                    f"(maximum allowed: {policy.max_files_in_diff})."
                ),
                details={
                    "file_count": file_count,
                    "max_files_in_diff": policy.max_files_in_diff,
                },
            )

        if diff_bytes > policy.max_diff_bytes:
            raise PermissionDeniedError(
                message=(
                    f"Diff exceeds maximum payload size: {diff_bytes} bytes "
                    f"(maximum allowed: {policy.max_diff_bytes} bytes)."
                ),
                details={
                    "diff_bytes": diff_bytes,
                    "max_diff_bytes": policy.max_diff_bytes,
                },
            )

    def is_branch_protected(
        self, branch_name: str, repository_id: Optional[str] = None
    ) -> bool:
        """Helper to determine whether a branch is protected under active policy."""
        policy = self.get_policy(repository_id)
        return is_protected_branch(branch_name, policy)

    def is_sensitive_file(
        self, file_path: str, repository_id: Optional[str] = None
    ) -> bool:
        """Helper to determine whether a path points to protected CI/CD automation."""
        policy = self.get_policy(repository_id)
        return is_workflow_path(file_path, policy)

    def clear(self) -> None:
        """Clears all repository policy overrides (isolated test teardown helper)."""
        with self._lock:
            self._repository_policies.clear()


# ============================================================
# PolicyEngine Singleton Accessors
# ============================================================

_DEFAULT_POLICY_ENGINE: Optional[PolicyEngine] = None
_POLICY_LOCK = threading.Lock()


def get_default_policy_engine() -> PolicyEngine:
    """Returns the process-wide default PolicyEngine singleton."""
    global _DEFAULT_POLICY_ENGINE
    if _DEFAULT_POLICY_ENGINE is None:
        with _POLICY_LOCK:
            if _DEFAULT_POLICY_ENGINE is None:
                _DEFAULT_POLICY_ENGINE = PolicyEngine()
    return _DEFAULT_POLICY_ENGINE


def set_default_policy_engine(engine: PolicyEngine) -> None:
    """Explicitly injects a PolicyEngine instance (used during testing)."""
    global _DEFAULT_POLICY_ENGINE
    with _POLICY_LOCK:
        _DEFAULT_POLICY_ENGINE = engine


def reset_default_policy_engine() -> None:
    """Resets the process-wide default PolicyEngine (used during testing)."""
    global _DEFAULT_POLICY_ENGINE
    with _POLICY_LOCK:
        if _DEFAULT_POLICY_ENGINE is not None:
            _DEFAULT_POLICY_ENGINE.clear()
        _DEFAULT_POLICY_ENGINE = None
