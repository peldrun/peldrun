# backend/integrations/source_control/security/policy_engine.py
"""
Policy engine for the Source Control Integration subsystem.

The policy engine is the coordinator between three orthogonal
concerns:

    1. Provider capabilities (`capabilities.py`) -- which operations a
       given provider adapter supports.
    2. Domain policies (`domain.policies`) -- which operations a given
       repository or connection is allowed to perform.
    3. Audit logging (`security.audit`) -- what decision was made and
       why.

The engine is deliberately thin. It does NOT re-implement policy
logic; it delegates to `evaluate_policy` for the actual decision and
augments the result with capability information and audit events.

Design rules:
    - The engine never raises for a "deny" decision. Denials are
      returned as `PolicyCheckResult` with `allowed=False`. Callers
      that want to fail fast use `require()`, which raises the
      appropriate error.
    - Capability checks run BEFORE policy checks. If a provider does
      not support an operation, the engine returns a result whose
      `reason` reflects the missing capability, and no policy is
      consulted.
    - Every decision is recorded in the audit log. The payload does
      NOT include secrets; only operation names, paths, and hashes.
    - The engine is thread-safe. Policy registration and lookup are
      protected by a reentrant lock.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field, replace
from typing import Dict, Mapping, Optional, Sequence

from ..capabilities import CapabilityChecker, ProviderCapabilities
from ..domain.enums import ProviderId
from ..domain.models import BranchRef, RepositoryRef
from ..domain.policies import (
    PolicyDecision,
    SourceControlPolicy,
    evaluate_policy,
)
from ..errors import (
    PolicyDeniedError,
    UnsupportedCapabilityError,
    ValidationError,
)
from .audit import (
    AuditEventType,
    AuditLogger,
    AuditSeverity,
    get_default_audit_logger,
)

__all__ = [
    "PolicyEngine",
    "PolicyCheckResult",
    "PolicyResolutionContext",
    "get_default_policy_engine",
    "set_default_policy_engine",
    "reset_default_policy_engine",
    "build_default_policy",
]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyResolutionContext:
    """
    Input context for a policy evaluation.

    Every field is optional except `operation`. Callers SHOULD provide
    as much context as possible so that the engine can make a precise
    decision. Missing context degrades to the default policy.

    Fields:
        operation:        The operation being requested (e.g.
                          `"push_branch"`).
        provider:         The provider the operation targets.
        repository:       The repository being operated on.
        source_branch:    The source branch, if applicable.
        target_branch:    The target branch, if applicable.
        target_branch_ref: The target branch reference with protection
                          flags, when known.
        changed_paths:    Paths modified by the operation.
        actor:            Who is requesting the operation
                          (`"user"` or `"agent"`).
    """

    operation: str
    provider: Optional[ProviderId] = None
    repository: Optional[RepositoryRef] = None
    source_branch: Optional[str] = None
    target_branch: Optional[str] = None
    target_branch_ref: Optional[BranchRef] = None
    changed_paths: Sequence[str] = field(default_factory=tuple)
    actor: str = "agent"


@dataclass(frozen=True)
class PolicyCheckResult:
    """
    Result of a policy evaluation.

    Fields:
        allowed:            Whether the operation is permitted by
                            policy.
        requires_approval:  Whether human approval is required before
                            the operation may run.
        warnings:           Non-fatal messages that MUST be shown to
                            the user (e.g. workflow file changed,
                            potential secret touched).
        reason:             Human-readable explanation when the
                            operation is denied or requires approval.
        policy:             The `SourceControlPolicy` used in the
                            decision.
        decision:           The raw `PolicyDecision` returned by the
                            pure evaluator.
        capability_gap:     When set, the name of the missing
                            capability that blocked the operation.
    """

    allowed: bool
    requires_approval: bool = False
    warnings: Sequence[str] = field(default_factory=tuple)
    reason: Optional[str] = None
    policy: Optional[SourceControlPolicy] = None
    decision: Optional[PolicyDecision] = None
    capability_gap: Optional[str] = None

    def is_denied(self) -> bool:
        """Return True if the operation was denied."""
        return not self.allowed

    def needs_human_approval(self) -> bool:
        """Return True if the operation requires explicit approval."""
        return self.allowed and self.requires_approval

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-safe mapping (no secrets)."""
        return {
            "allowed": self.allowed,
            "requires_approval": self.requires_approval,
            "warnings": list(self.warnings),
            "reason": self.reason,
            "capability_gap": self.capability_gap,
        }


# ---------------------------------------------------------------------------
# Policy engine
# ---------------------------------------------------------------------------


class PolicyEngine:
    """
    Coordinator for policy evaluation, capability checks, and audit.

    The engine holds a registry of per-repository policies and a
    default policy that applies when no specific policy is registered.
    """

    def __init__(
        self,
        *,
        default_policy: Optional[SourceControlPolicy] = None,
        audit_logger: Optional[AuditLogger] = None,
    ) -> None:
        self._default_policy = default_policy or build_default_policy()
        self._policies: Dict[str, SourceControlPolicy] = {}
        self._lock = threading.RLock()
        self._audit = audit_logger or get_default_audit_logger()

    # --- Registration ---------------------------------------------------

    def register_policy(
        self,
        provider: ProviderId | str,
        repository_id: str,
        policy: SourceControlPolicy,
        *,
        overwrite: bool = False,
    ) -> None:
        """
        Register a policy for a specific repository.

        `provider` may be a `ProviderId` enum member or a plain
        string. `repository_id` MUST be a non-empty string.

        Raises:
            ValidationError: If any input is invalid, or if a policy
                is already registered and `overwrite` is False.
        """
        key = self._make_key(provider, repository_id)
        if not isinstance(policy, SourceControlPolicy):
            raise ValidationError(
                "register_policy expects a SourceControlPolicy instance.",
                details={"type": type(policy).__name__},
            )

        with self._lock:
            if key in self._policies and not overwrite:
                raise ValidationError(
                    f"A policy is already registered for '{key}'.",
                    details={"key": key},
                )
            self._policies[key] = policy

    def unregister_policy(
        self, provider: ProviderId | str, repository_id: str
    ) -> bool:
        """
        Remove a repository-specific policy.

        Returns True if a policy was present and removed, False
        otherwise. Never raises for a missing key.
        """
        try:
            key = self._make_key(provider, repository_id)
        except ValidationError:
            return False
        with self._lock:
            return self._policies.pop(key, None) is not None

    def list_policies(self) -> Mapping[str, SourceControlPolicy]:
        """
        Return a snapshot of all registered policies.

        The mapping is a shallow copy; modifying it does not affect
        the engine's internal state.
        """
        with self._lock:
            return dict(self._policies)

    def clear_policies(self) -> None:
        """Remove every registered policy. Intended for tests."""
        with self._lock:
            self._policies.clear()

    @property
    def default_policy(self) -> SourceControlPolicy:
        """Return the default policy used when no specific one matches."""
        return self._default_policy

    # --- Resolution -----------------------------------------------------

    def resolve_policy(
        self,
        provider: Optional[ProviderId | str],
        repository: Optional[RepositoryRef],
    ) -> SourceControlPolicy:
        """
        Return the policy that applies to the given provider and
        repository.

        Resolution order:
            1. Exact match on `provider:repository_id`.
            2. The engine's default policy.
        """
        if provider is None or repository is None:
            return self._default_policy
        try:
            key = self._make_key(provider, repository.repository_id)
        except ValidationError:
            return self._default_policy
        with self._lock:
            return self._policies.get(key, self._default_policy)

    # --- Evaluation -----------------------------------------------------

    def check(
        self,
        context: PolicyResolutionContext,
        *,
        capabilities: Optional[ProviderCapabilities] = None,
    ) -> PolicyCheckResult:
        """
        Evaluate `context` and return a `PolicyCheckResult`.

        The evaluation order is:
            1. Capability check (if `capabilities` is provided).
            2. Domain policy evaluation via `evaluate_policy`.
            3. Audit logging.

        This method NEVER raises. Callers that want strict behavior
        SHOULD use `require()`.

        Args:
            context: The resolution context.
            capabilities: The provider capabilities, if known. When
                provided, unsupported operations are rejected before
                policy evaluation.
        """
        policy = self.resolve_policy(context.provider, context.repository)

        # Step 1: capability check.
        capability_gap: Optional[str] = None
        if capabilities is not None:
            checker = CapabilityChecker(capabilities)
            if not checker.supports(context.operation):
                from ..capabilities import operation_to_capability

                capability_gap = operation_to_capability(context.operation)
                reason = (
                    f"Operation '{context.operation}' is not supported "
                    "by the selected provider."
                )
                result = PolicyCheckResult(
                    allowed=False,
                    reason=reason,
                    policy=policy,
                    capability_gap=capability_gap,
                )
                self._audit_decision(context, result)
                return result

        # Step 2: policy evaluation.
        decision = evaluate_policy(
            policy,
            operation=context.operation,
            repository=context.repository,
            target_branch=context.target_branch,
            source_branch=context.source_branch,
            target_branch_ref=context.target_branch_ref,
            changed_paths=context.changed_paths,
        )

        result = PolicyCheckResult(
            allowed=decision.allowed,
            requires_approval=decision.requires_approval,
            warnings=tuple(decision.warnings),
            reason=decision.reason,
            policy=policy,
            decision=decision,
        )

        # Step 3: audit.
        self._audit_decision(context, result)
        return result

    def require(
        self,
        context: PolicyResolutionContext,
        *,
        capabilities: Optional[ProviderCapabilities] = None,
    ) -> PolicyCheckResult:
        """
        Evaluate `context` and raise on denial.

        Returns the `PolicyCheckResult` when the operation is allowed
        (whether or not approval is required). The caller is expected
        to handle the approval requirement separately.

        Raises:
            UnsupportedCapabilityError: If the provider does not
                support the operation.
            PolicyDeniedError: If the policy denies the operation.
        """
        result = self.check(context, capabilities=capabilities)

        if result.allowed:
            return result

        # Capability gap -> raise the specific error.
        if result.capability_gap:
            raise UnsupportedCapabilityError(
                result.reason or "Operation is not supported.",
                details={
                    "operation": context.operation,
                    "capability": result.capability_gap,
                },
            )

        raise PolicyDeniedError(
            result.reason or "Operation is not permitted by policy.",
            details={
                "operation": context.operation,
                "warnings": list(result.warnings),
            },
        )

    # --- Internal helpers ----------------------------------------------

    def _audit_decision(
        self,
        context: PolicyResolutionContext,
        result: PolicyCheckResult,
    ) -> None:
        """
        Record a policy decision in the audit log.

        Failures are swallowed by the audit logger; the policy engine
        must not raise because the audit sink is unavailable.
        """
        severity = AuditSeverity.INFO
        event_type = AuditEventType.POLICY_DECISION
        if result.is_denied():
            severity = AuditSeverity.WARNING
            event_type = AuditEventType.POLICY_DENIED

        payload = {
            "operation": context.operation,
            "actor": context.actor,
            "allowed": result.allowed,
            "requires_approval": result.requires_approval,
            "warnings": list(result.warnings),
            "reason": result.reason,
            "capability_gap": result.capability_gap,
            "changed_paths": list(context.changed_paths),
        }

        try:
            self._audit.record(
                event_type,
                severity=severity,
                action="policy_check",
                message=result.reason
                or (
                    "Policy allowed the operation."
                    if result.allowed
                    else "Policy denied the operation."
                ),
                payload=payload,
                provider=(
                    context.provider.value
                    if isinstance(context.provider, ProviderId)
                    else (
                        str(context.provider)
                        if context.provider is not None
                        else None
                    )
                ),
                repository_id=(
                    context.repository.repository_id
                    if context.repository is not None
                    else None
                ),
                actor=context.actor,
            )
        except Exception:  # noqa: BLE001
            # The audit logger already swallows its own failures; this
            # outer guard protects against programmer errors in the
            # event construction itself.
            logger.debug("Policy audit logging failed", exc_info=True)

    @staticmethod
    def _make_key(
        provider: ProviderId | str, repository_id: str
    ) -> str:
        """
        Build a stable lookup key for the policy registry.

        Accepts `ProviderId` enum members or plain strings. Raises
        `ValidationError` for empty inputs.
        """
        if provider is None:
            raise ValidationError("provider must not be None.")
        provider_value = (
            provider.value if isinstance(provider, ProviderId) else str(provider)
        ).strip().lower()
        if not provider_value:
            raise ValidationError("provider must not be empty.")
        if not repository_id or not isinstance(repository_id, str):
            raise ValidationError(
                "repository_id must be a non-empty string."
            )
        trimmed = repository_id.strip()
        if not trimmed:
            raise ValidationError("repository_id must not be blank.")
        return f"{provider_value}:{trimmed}"


# ---------------------------------------------------------------------------
# Default policy
# ---------------------------------------------------------------------------


def build_default_policy() -> SourceControlPolicy:
    """
    Return the default `SourceControlPolicy` used by the subsystem.

    The default is intentionally restrictive: dangerous operations
    (merge, delete branch, direct default-branch push) are disabled,
    and every push or change-request creation requires approval.

    Applications MAY override this policy per repository via
    `PolicyEngine.register_policy`.
    """
    return SourceControlPolicy()


# ---------------------------------------------------------------------------
# Default engine singleton
# ---------------------------------------------------------------------------


_DEFAULT_ENGINE: Optional[PolicyEngine] = None
_DEFAULT_LOCK = threading.RLock()


def get_default_policy_engine() -> PolicyEngine:
    """
    Return the process-wide default policy engine, creating it on
    first use.
    """
    global _DEFAULT_ENGINE
    with _DEFAULT_LOCK:
        if _DEFAULT_ENGINE is None:
            _DEFAULT_ENGINE = PolicyEngine()
        return _DEFAULT_ENGINE


def set_default_policy_engine(engine: PolicyEngine) -> None:
    """
    Replace the process-wide default policy engine.

    Intended for application startup and for tests.
    """
    if not isinstance(engine, PolicyEngine):
        raise TypeError(
            "set_default_policy_engine expects a PolicyEngine instance."
        )
    global _DEFAULT_ENGINE
    with _DEFAULT_LOCK:
        _DEFAULT_ENGINE = engine


def reset_default_policy_engine() -> PolicyEngine:
    """
    Replace the default engine with a fresh one.

    Used by tests to guarantee isolation between test cases.
    """
    global _DEFAULT_ENGINE
    with _DEFAULT_LOCK:
        _DEFAULT_ENGINE = PolicyEngine()
        return _DEFAULT_ENGINE
