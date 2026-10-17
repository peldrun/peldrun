# backend/tests/source_control/test_policy_engine.py
"""
Tests for the policy engine.

The policy engine combines three sources of truth:

    - The default `SourceControlPolicy` (defined in
      `domain.policies`), which declares the safe-by-default rules.
    - The provider's `ProviderCapabilities`, which gate operations
      that a provider may not support.
    - The audit log, which records every decision for later review.

These tests verify the behavior a policy change would be expected
to alter:

    - Direct pushes to default/protected branches are denied.
    - Pushes to feature branches are allowed with approval.
    - Merges are denied by default.
    - Deletions are denied by default.
    - Workflow-file edits surface a warning.
    - `require()` raises when the policy denies.
    - Capability gaps short-circuit the policy check.
    - Registered per-repository policies override the default.
    - Every decision is recorded in the audit log.

The tests deliberately avoid asserting on the exact `reason` text:
the reason is user-facing and may change for localization or UX
reasons. They DO assert on the observable result (`allowed`,
`requires_approval`, `warnings`, `capability_gap`) and on the audit
side effect.
"""

from __future__ import annotations

import pytest

try:
    from ...integrations.source_control.capabilities import (
        ProviderCapabilities,
    )
    from ...integrations.source_control.domain.enums import ProviderId
    from ...integrations.source_control.domain.models import (
        RepositoryRef,
    )
    from ...integrations.source_control.domain.policies import (
        SourceControlPolicy,
    )
    from ...integrations.source_control.errors import (
        PolicyDeniedError,
        UnsupportedCapabilityError,
    )
    from ...integrations.source_control.security.audit import (
        AuditEventType,
        InMemoryAuditLogger,
    )
    from ...integrations.source_control.security.policy_engine import (
        PolicyEngine,
        PolicyResolutionContext,
    )
except (ImportError, ValueError):
    from backend.integrations.source_control.capabilities import (
        ProviderCapabilities,
    )
    from backend.integrations.source_control.domain.enums import ProviderId
    from backend.integrations.source_control.domain.models import (
        RepositoryRef,
    )
    from backend.integrations.source_control.domain.policies import (
        SourceControlPolicy,
    )
    from backend.integrations.source_control.errors import (
        PolicyDeniedError,
        UnsupportedCapabilityError,
    )
    from backend.integrations.source_control.security.audit import (
        AuditEventType,
        InMemoryAuditLogger,
    )
    from backend.integrations.source_control.security.policy_engine import (
        PolicyEngine,
        PolicyResolutionContext,
    )


def _make_repo(
    *,
    repository_id: str = "repo_test",
    provider: ProviderId = ProviderId.GITHUB,
) -> RepositoryRef:
    """Build a minimal RepositoryRef for tests."""
    return RepositoryRef(
        provider=provider,
        account_id="acc_test",
        repository_id=repository_id,
        namespace="peldrun",
        name="sample-repo",
        clone_url="https://github.com/peldrun/sample-repo.git",
        web_url="https://github.com/peldrun/sample-repo",
        default_branch="main",
        is_private=False,
    )


# ---------------------------------------------------------------------
# 1. Default policy: protected-branch pushes
# ---------------------------------------------------------------------


def test_default_policy_blocks_default_branch_push(
    policy_engine: PolicyEngine,
) -> None:
    """
    Direct pushes to the default branch are denied by default.
    """
    context = PolicyResolutionContext(
        operation="push_branch",
        target_branch="main",
        actor="agent",
    )
    result = policy_engine.check(context)
    assert result.allowed is False, (
        "Default policy unexpectedly allowed a push to 'main'; "
        f"reason={result.reason!r}, warnings={list(result.warnings)!r}"
    )


def test_default_policy_requires_approval_for_feature_branch_push(
    policy_engine: PolicyEngine,
) -> None:
    """
    Pushes to a non-protected branch are allowed, but MUST require
    user approval.
    """
    context = PolicyResolutionContext(
        operation="push_branch",
        target_branch="feature/safe-branch",
        actor="agent",
    )
    result = policy_engine.check(context)
    assert result.allowed is True, (
        "Default policy unexpectedly denied a push to a feature "
        f"branch; reason={result.reason!r}"
    )
    assert result.requires_approval is True, (
        "A push to a feature branch MUST require explicit approval; "
        "the default policy did not set requires_approval=True."
    )


# ---------------------------------------------------------------------
# 2. Default policy: merge and delete are denied
# ---------------------------------------------------------------------


def test_default_policy_blocks_merge_change_request(
    policy_engine: PolicyEngine,
) -> None:
    """
    Merging a change request is denied by default, even on a feature
    target branch.
    """
    context = PolicyResolutionContext(
        operation="merge_change_request",
        target_branch="main",
        actor="agent",
    )
    result = policy_engine.check(context)
    assert result.allowed is False, (
        "Default policy unexpectedly allowed a merge; "
        f"reason={result.reason!r}"
    )


def test_default_policy_blocks_delete_remote_branch(
    policy_engine: PolicyEngine,
) -> None:
    """
    Deleting a remote branch is denied by default, regardless of the
    target branch name.
    """
    context = PolicyResolutionContext(
        operation="delete_remote_branch",
        target_branch="feature/abandoned",
        actor="agent",
    )
    result = policy_engine.check(context)
    assert result.allowed is False, (
        "Default policy unexpectedly allowed a remote branch "
        f"deletion; reason={result.reason!r}"
    )


# ---------------------------------------------------------------------
# 3. Workflow files must surface a warning
# ---------------------------------------------------------------------


def test_workflow_file_change_emits_warning(
    policy_engine: PolicyEngine,
) -> None:
    """
    Editing a CI workflow file MUST produce at least one warning,
    even when the operation is otherwise allowed.
    """
    context = PolicyResolutionContext(
        operation="push_branch",
        target_branch="feature/ci-update",
        changed_paths=[".github/workflows/ci.yml"],
        actor="agent",
    )
    result = policy_engine.check(context)

    assert result.allowed is True, (
        "Editing a workflow file on a feature branch should be "
        "allowed with a warning; got allowed=False "
        f"(reason={result.reason!r})"
    )
    assert len(result.warnings) >= 1, (
        "Editing a workflow file MUST emit at least one warning; "
        f"got warnings={list(result.warnings)!r}"
    )


# ---------------------------------------------------------------------
# 4. require(): raises on denial, returns on allow
# ---------------------------------------------------------------------


def test_require_raises_on_denial(
    policy_engine: PolicyEngine,
) -> None:
    """
    `require()` raises when the policy denies the operation.
    """
    context = PolicyResolutionContext(
        operation="push_branch",
        target_branch="main",
        actor="agent",
    )
    with pytest.raises(PolicyDeniedError):
        policy_engine.require(context)


def test_require_returns_result_on_allow(
    policy_engine: PolicyEngine,
) -> None:
    """
    `require()` returns the `PolicyCheckResult` when the operation is
    allowed, even if it requires approval.
    """
    context = PolicyResolutionContext(
        operation="push_branch",
        target_branch="feature/require-allow",
        actor="agent",
    )
    result = policy_engine.require(context)
    assert result.allowed is True


# ---------------------------------------------------------------------
# 5. Capability gaps short-circuit the policy check
# ---------------------------------------------------------------------


def test_capability_gap_returns_denied_result(
    policy_engine: PolicyEngine,
) -> None:
    """
    When the provider does not declare support for an operation,
    the engine MUST return `allowed=False` and set
    `capability_gap`, without consulting the policy.
    """
    capabilities = ProviderCapabilities(
        list_repositories=True,
        read_files=True,
        create_branch=True,
        push_branch=True,
        create_change_request=True,
        list_checks=True,
        comments=False,
        merge_change_request=False,  # unsupported
        self_managed_instance=False,
    )
    context = PolicyResolutionContext(
        operation="merge_change_request",
        target_branch="main",
        actor="agent",
    )
    result = policy_engine.check(context, capabilities=capabilities)

    assert result.allowed is False
    assert result.capability_gap is not None, (
        "Capability gap was expected for 'merge_change_request' but "
        "was not set."
    )


def test_require_raises_unsupported_capability(
    policy_engine: PolicyEngine,
) -> None:
    """
    `require()` with unsupported capabilities raises
    `UnsupportedCapabilityError`, not `PolicyDeniedError`.
    """
    capabilities = ProviderCapabilities(
        list_repositories=True,
        read_files=True,
        create_branch=True,
        push_branch=True,
        create_change_request=True,
        list_checks=True,
        comments=False,
        merge_change_request=False,
        self_managed_instance=False,
    )
    context = PolicyResolutionContext(
        operation="merge_change_request",
        target_branch="main",
        actor="agent",
    )
    with pytest.raises(UnsupportedCapabilityError):
        policy_engine.require(context, capabilities=capabilities)


# ---------------------------------------------------------------------
# 6. Policy registry: per-repository overrides
# ---------------------------------------------------------------------


def test_registered_policy_overrides_default(
    policy_engine: PolicyEngine,
) -> None:
    """
    A policy registered for a specific (provider, repository) MUST be
    returned by `resolve_policy` instead of the default policy.
    """
    custom_policy = SourceControlPolicy()
    policy_engine.register_policy(
        provider=ProviderId.GITHUB,
        repository_id="custom-repo-id",
        policy=custom_policy,
    )

    resolved = policy_engine.resolve_policy(
        ProviderId.GITHUB,
        _make_repo(repository_id="custom-repo-id"),
    )
    assert resolved is custom_policy


def test_unregister_policy_is_idempotent(
    policy_engine: PolicyEngine,
) -> None:
    """
    `unregister_policy` returns True the first time and False on
    subsequent calls, and never raises.
    """
    custom_policy = SourceControlPolicy()
    policy_engine.register_policy(
        provider=ProviderId.GITHUB,
        repository_id="ephemeral-repo",
        policy=custom_policy,
    )

    first = policy_engine.unregister_policy(
        ProviderId.GITHUB, "ephemeral-repo"
    )
    second = policy_engine.unregister_policy(
        ProviderId.GITHUB, "ephemeral-repo"
    )

    assert first is True
    assert second is False


# ---------------------------------------------------------------------
# 7. Audit logging
# ---------------------------------------------------------------------


def test_policy_decision_is_audited(
    audit_logger: InMemoryAuditLogger,
) -> None:
    """
    Every policy decision MUST be recorded in the audit log.
    """
    engine = PolicyEngine(audit_logger=audit_logger)

    context = PolicyResolutionContext(
        operation="push_branch",
        target_branch="feature/audited",
        actor="agent",
    )
    engine.check(context)

    events = list(audit_logger.query(limit=50))
    assert len(events) >= 1, (
        "Policy engine did not record any audit event for a "
        "policy check."
    )

    relevant_types = {
        AuditEventType.POLICY_DECISION.value,
        AuditEventType.POLICY_DENIED.value,
    }
    matching = [
        e
        for e in events
        if getattr(e.event_type, "value", str(e.event_type))
        in relevant_types
    ]
    assert matching, (
        "Audit log did not contain a policy decision event; "
        f"got event types: "
        f"{[getattr(e.event_type, 'value', str(e.event_type)) for e in events]!r}"
    )
