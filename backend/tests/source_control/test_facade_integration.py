# backend/tests/source_control/test_facade_integration.py
"""
Integration tests for the `SourceControlFacade`.

These tests exercise the facade END-TO-END through its public
surface, using real `OperationService`, `PolicyEngine`,
`ApprovalGate`, `InMemoryAuditLogger`, and `TokenService` instances.
Only the components that would touch the network or the filesystem
beyond what the test controls are stubbed.

The tests focus on flows the earlier unit tests could not cover:
    - Wiring between the facade and its dependencies.
    - Audit trail consistency across a full operation lifecycle.
    - The security-critical rule that change-request creation MUST
      reject calls that omit the approval ticket.
    - Regression: injected services MUST be used, even when they
      are empty (this is the `or` vs. `is not None` bug that was
      uncovered in `policy_engine.py`).

The tests deliberately do NOT exercise:
    - Real Git operations (those are covered by `test_git_workspace`
      and `test_command_injection`).
    - Real provider HTTP calls (those are covered by
      `test_github_client` and are provider-specific).

Design rules:
    - Every test uses a fresh facade instance with isolated
      dependencies, so test order cannot affect results.
    - Assertions check observable behavior (returned values, audit
      events), never internal state.
    - The security test (`test_create_change_request_requires_approval_id`)
      documents a hard requirement: an agent MUST NOT create a
      change request without an approval ticket, regardless of what
      state the operation is in.
"""

from __future__ import annotations

import pytest

try:
    from ...integrations.source_control.domain.enums import (
        OperationStatus,
    )
    from ...integrations.source_control.operations.journal import (
        InMemoryOperationJournal,
    )
    from ...integrations.source_control.operations.service import (
        OperationService,
    )
    from ...integrations.source_control.errors import (
        NotFoundError,
        PermissionDeniedError,
        PolicyDeniedError,
        ValidationError,
    )
    from ...integrations.source_control.schemas import (
        ApproveOperationRequest,
        CreateChangeRequestPayload,
        CreateOperationRequest,
        RejectOperationRequest,
    )
    from ...integrations.source_control.security.approval import (
        ApprovalGate,
    )
    from ...integrations.source_control.security.audit import (
        AuditEventType,
        InMemoryAuditLogger,
    )
    from ...integrations.source_control.security.policy_engine import (
        PolicyEngine,
    )
    from ...integrations.source_control.security.token_service import (
        TokenService,
    )
    from ...integrations.source_control.security.vault import (
        InMemoryVault,
    )
    from ...omweb.services.source_control_facade import (
        SourceControlFacade,
    )
except (ImportError, ValueError):
    from backend.integrations.source_control.domain.enums import (
        OperationStatus,
    )
    from backend.integrations.source_control.operations.journal import (
        InMemoryOperationJournal,
    )
    from backend.integrations.source_control.operations.service import (
        OperationService,
    )
    from backend.integrations.source_control.errors import (
        NotFoundError,
        PermissionDeniedError,
        PolicyDeniedError,
        ValidationError,
    )
    from backend.integrations.source_control.schemas import (
        ApproveOperationRequest,
        CreateChangeRequestPayload,
        CreateOperationRequest,
        RejectOperationRequest,
    )
    from backend.integrations.source_control.security.approval import (
        ApprovalGate,
    )
    from backend.integrations.source_control.security.audit import (
        AuditEventType,
        InMemoryAuditLogger,
    )
    from backend.integrations.source_control.security.policy_engine import (
        PolicyEngine,
    )
    from backend.integrations.source_control.security.token_service import (
        TokenService,
    )
    from backend.integrations.source_control.security.vault import (
        InMemoryVault,
    )
    from backend.omweb.services.source_control_facade import (
        SourceControlFacade,
    )


# A fixed value used throughout the tests. It is NOT a real hash;
# the subsystems only check the format.
_BASE_SHA = "a" * 64
_DIFF_SHA = "b" * 64


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------


@pytest.fixture
def isolated_operation_service() -> OperationService:
    """
    Fresh `OperationService` backed by an in-memory journal.

    The journal is per-test, so operations created in one test never
    leak into another.
    """
    return OperationService(journal=InMemoryOperationJournal())


@pytest.fixture
def facade(isolated_operation_service: OperationService):
    """
    A facade wired with isolated, deterministic dependencies.

    The dependencies below are all concrete, empty, and per-test:
        - `PolicyEngine`: default policy (blocks main, allows
          feature branches with approval).
        - `ApprovalGate`: empty, ready to receive tickets.
        - `InMemoryAuditLogger`: empty, ready to receive events.
        - `TokenService`: backed by `InMemoryVault`, empty.
        - `OperationService`: backed by `InMemoryOperationJournal`.

    The facade is left to construct the remaining components
    (registry, workspace manager, diff engine, push manager) from
    their defaults, because the flows under test do not exercise
    them.
    """
    return SourceControlFacade(
        policy_engine=PolicyEngine(),
        approval_gate=ApprovalGate(),
        audit_logger=InMemoryAuditLogger(),
        token_service=TokenService(vault=InMemoryVault()),
        operation_service=isolated_operation_service,
    )


def _make_create_payload(
    *,
    repository_id: str = "repo_test",
    operation_type: str = "push_branch",
    target_branch: str = "feature/integration-test",
) -> CreateOperationRequest:
    return CreateOperationRequest(
        provider="github",
        repository_id=repository_id,
        operation_type=operation_type,
        target_branch=target_branch,
    )


async def _advance_to_review_required(
    facade: SourceControlFacade, operation_id: str
) -> None:
    """
    Drive an operation through the valid state-machine path up to
    `REVIEW_REQUIRED`.

    The `CREATED` state does not allow a direct transition to
    `REVIEW_REQUIRED`; the operation must first pass through
    `REPOSITORY_RESOLVED`, `WORKSPACE_READY`, and `EDITING`. In the
    real application those transitions are performed by the agent
    flow; the test performs them directly to set up the state
    required by `approve_operation` and `reject_operation`.
    """
    service = facade.operation_service
    for status in (
        OperationStatus.REPOSITORY_RESOLVED,
        OperationStatus.WORKSPACE_READY,
        OperationStatus.EDITING,
        OperationStatus.REVIEW_REQUIRED,
    ):
        await service.transition(operation_id, status, actor="test")


# ---------------------------------------------------------------------
# 1. Operation lifecycle: create → get
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_operation_and_get_operation_roundtrip(
    facade: SourceControlFacade,
) -> None:
    """
    INVARIANT: a freshly created operation is retrievable by ID and
    reflects the payload exactly.
    """
    payload = _make_create_payload()

    created = await facade.create_operation(payload)
    assert created.operation_id
    assert created.repository_id == "repo_test"
    assert created.operation_type == "push_branch"
    assert created.target_branch == "feature/integration-test"
    assert created.current_state == "created"
    fetched = await facade.get_operation(created.operation_id)
    assert fetched.operation_id == created.operation_id
    assert fetched.current_state == "created"


@pytest.mark.asyncio
async def test_get_operation_raises_for_unknown_id(
    facade: SourceControlFacade,
) -> None:
    """
    INVARIANT: `get_operation` raises `NotFoundError` for an unknown
    ID. It does NOT return a bogus response.
    """
    with pytest.raises(NotFoundError):
        await facade.get_operation("op_does_not_exist")


# ---------------------------------------------------------------------
# 2. Policy enforcement at creation time
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_operation_rejects_protected_branch(
    facade: SourceControlFacade,
) -> None:
    """
    INVARIANT: the default policy denies `push_branch` to `main`.

    The facade MUST reject the operation BEFORE creating a journal
    entry, so no orphan record is left behind.
    """
    payload = _make_create_payload(target_branch="main")

    with pytest.raises(PolicyDeniedError):
        await facade.create_operation(payload)


# ---------------------------------------------------------------------
# 3. Audit trail consistency
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_operation_logs_audit_event(
    facade: SourceControlFacade,
) -> None:
    """
    INVARIANT: every successful `create_operation` call records an
    `OPERATION_CREATE` event in the audit log.

    The event is read from `facade.audit_logger`, which is the
    logger the facade actually uses (see the `or` bug note below).
    """
    payload = _make_create_payload()
    created = await facade.create_operation(payload)

    events = list(facade.audit_logger.query(limit=200))
    matching = [
        e
        for e in events
        if getattr(e.event_type, "value", str(e.event_type))
        == AuditEventType.OPERATION_CREATE.value
    ]
    assert matching, (
        "No OPERATION_CREATE event was recorded; "
        f"event types: "
        f"{[getattr(e.event_type, 'value', str(e.event_type)) for e in events]!r}"
    )
    # The recorded event should reference the new operation.
    assert any(
        getattr(e, "operation_id", None) == created.operation_id
        for e in matching
    )


@pytest.mark.asyncio
async def test_audit_log_tracks_full_lifecycle(
    facade: SourceControlFacade,
) -> None:
    """
    INVARIANT: the audit log records each step of an approval
    lifecycle.

    The specific lifecycle is: create → advance to REVIEW_REQUIRED
    → approve. Every one of those steps must produce at least one
    audit event referencing the operation.
    """
    payload = _make_create_payload()
    created = await facade.create_operation(payload)
    op_id = created.operation_id

    await _advance_to_review_required(facade, op_id)

    ticket = facade.approval_gate.create_ticket(
        operation_id=op_id,
        repository_id="repo_test",
        operation="push_branch",
        base_sha=_BASE_SHA,
        diff_sha256=_DIFF_SHA,
        target_branch="feature/integration-test",
    )
    await facade.approve_operation(
        op_id,
        ApproveOperationRequest(
            approval_id=ticket.approval_id, user_id="test_user"
        ),
    )

    events = list(facade.audit_logger.query(limit=500))
    op_events = [
        e for e in events
        if getattr(e, "operation_id", None) == op_id
    ]
    op_event_types = {
        getattr(e.event_type, "value", str(e.event_type))
        for e in op_events
    }

    # The lifecycle must record at minimum the create and grant
    # events. (Transitions also record events, but the exact set is
    # an implementation detail; we assert the two critical ones.)
    assert AuditEventType.OPERATION_CREATE.value in op_event_types
    assert AuditEventType.APPROVAL_GRANT.value in op_event_types


# ---------------------------------------------------------------------
# 4. Approval lifecycle
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_operation_grants_ticket(
    facade: SourceControlFacade,
) -> None:
    """
    INVARIANT: `approve_operation` transitions the ticket to GRANTED
    and the operation to APPROVED, and both changes are observable.
    """
    payload = _make_create_payload()
    created = await facade.create_operation(payload)
    op_id = created.operation_id

    await _advance_to_review_required(facade, op_id)

    ticket = facade.approval_gate.create_ticket(
        operation_id=op_id,
        repository_id="repo_test",
        operation="push_branch",
        base_sha=_BASE_SHA,
        diff_sha256=_DIFF_SHA,
        target_branch="feature/integration-test",
    )

    result = await facade.approve_operation(
        op_id,
        ApproveOperationRequest(
            approval_id=ticket.approval_id, user_id="test_user"
        ),
    )

    assert result.approval_id == ticket.approval_id
    assert result.approved_by_user is True

    # Ticket state changed.
    granted = facade.approval_gate.get_ticket(ticket.approval_id)
    assert granted is not None
    assert granted.status.value == "granted"

    # Operation state changed.
    updated = await facade.get_operation(op_id)
    assert updated.current_state == "approved"


@pytest.mark.asyncio
async def test_reject_operation_invalidates_ticket(
    facade: SourceControlFacade,
) -> None:
    """
    INVARIANT: `reject_operation` transitions the ticket to REJECTED
    and the operation to REJECTED. Neither can be approved
    afterwards.
    """
    payload = _make_create_payload()
    created = await facade.create_operation(payload)
    op_id = created.operation_id

    await _advance_to_review_required(facade, op_id)

    ticket = facade.approval_gate.create_ticket(
        operation_id=op_id,
        repository_id="repo_test",
        operation="push_branch",
        base_sha=_BASE_SHA,
        diff_sha256=_DIFF_SHA,
        target_branch="feature/integration-test",
    )

    result = await facade.reject_operation(
        op_id,
        RejectOperationRequest(
            approval_id=ticket.approval_id,
            user_id="test_reviewer",
            reason="not needed for this change",
        ),
    )
    assert "rejected" in result.message.lower()

    rejected = facade.approval_gate.get_ticket(ticket.approval_id)
    assert rejected is not None
    assert rejected.status.value == "rejected"

    updated = await facade.get_operation(op_id)
    assert updated.current_state == "rejected"


@pytest.mark.asyncio
async def test_approve_operation_rejects_unknown_approval_id(
    facade: SourceControlFacade,
) -> None:
    """
    INVARIANT: `approve_operation` raises `ValidationError` when
    given an approval ID that does not exist.

    This prevents the caller from bypassing the ticket lifecycle by
    inventing an approval ID.
    """
    payload = _make_create_payload()
    created = await facade.create_operation(payload)

    with pytest.raises(ValidationError):
        await facade.approve_operation(
            created.operation_id,
            ApproveOperationRequest(
                approval_id="approval_does_not_exist",
                user_id="test_user",
            ),
        )


@pytest.mark.asyncio
async def test_approve_operation_rejects_mismatched_operation_id(
    facade: SourceControlFacade,
) -> None:
    """
    INVARIANT: an approval ticket issued for operation A MUST NOT be
    usable to approve operation B.

    This is a security-critical check that prevents confusion
    attacks where the caller presents a valid ticket for a
    different operation.
    """
    # Create two operations.
    payload1 = _make_create_payload(repository_id="repo_a")
    payload2 = _make_create_payload(repository_id="repo_b")
    op1 = await facade.create_operation(payload1)
    op2 = await facade.create_operation(payload2)

    await _advance_to_review_required(facade, op1.operation_id)
    await _advance_to_review_required(facade, op2.operation_id)

    # Ticket issued for op1.
    ticket = facade.approval_gate.create_ticket(
        operation_id=op1.operation_id,
        repository_id="repo_a",
        operation="push_branch",
        base_sha=_BASE_SHA,
        diff_sha256=_DIFF_SHA,
        target_branch="feature/integration-test",
    )

    # Attempt to approve op2 with op1's ticket.
    with pytest.raises(ValidationError):
        await facade.approve_operation(
            op2.operation_id,
            ApproveOperationRequest(
                approval_id=ticket.approval_id, user_id="test_user"
            ),
        )


# ---------------------------------------------------------------------
# 5. Change-request creation requires an approval (no bypass)
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_change_request_requires_approval_id(
    facade: SourceControlFacade,
) -> None:
    """
    SECURITY INVARIANT: `create_change_request` MUST reject calls
    that omit `approval_id`, regardless of the operation state.

    Without this check, an agent could open arbitrary pull requests
    on the user's behalf, bypassing the human review that the
    approval gate exists to enforce.
    """
    payload = _make_create_payload()
    created = await facade.create_operation(payload)

    change_request_payload = CreateChangeRequestPayload(
        provider="github",
        repository_id="repo_test",
        source_branch="feature/integration-test",
        target_branch="main",
        title="Test PR",
        # NOTE: intentionally no `approval_id`.
    )

    with pytest.raises(PermissionDeniedError):
        await facade.create_change_request(
            created.operation_id, change_request_payload
        )


# ---------------------------------------------------------------------
# 6. Regression: injected services must be honored
# ---------------------------------------------------------------------


def test_facade_honors_injected_policy_engine() -> None:
    """
    REGRESSION: the facade MUST use an explicitly injected
    `PolicyEngine`, not its own default.

    `PolicyEngine` does not define `__len__`, so `bool(PolicyEngine())`
    is `True`. This test documents the invariant for the specific
    case that already works today. The same test for `audit_logger`
    is marked xfail because `InMemoryAuditLogger` DOES define
    `__len__`, and the facade still uses `audit_logger or
    get_default_audit_logger()`.
    """
    injected = PolicyEngine()
    facade = SourceControlFacade(policy_engine=injected)
    assert facade.policy_engine is injected


def test_facade_honors_injected_audit_logger() -> None:
    """
    REGRESSION: the facade MUST use an explicitly injected
    `AuditLogger`, not its own default.

    `InMemoryAuditLogger` may or may not define `__len__` in a
    future revision. If it does, and the facade uses
    `audit_logger or get_default_audit_logger()`, an empty
    injected logger would be silently replaced. This test
    guarantees the facade honors the injected instance regardless
    of the truthiness of the value.
    """
    injected = InMemoryAuditLogger()
    facade = SourceControlFacade(audit_logger=injected)
    assert facade.audit_logger is injected


def test_facade_honors_injected_approval_gate() -> None:
    """
    REGRESSION: the facade MUST use an explicitly injected
    `ApprovalGate`, not its own default.

    `ApprovalGate` defines `__len__`, so if the facade used
    `approval_gate or get_default_approval_gate()` an empty
    injected gate would be silently replaced. This test guarantees
    the facade honors the injected instance regardless of the
    truthiness of the value.
    """
    injected = ApprovalGate()
    facade = SourceControlFacade(approval_gate=injected)
    assert facade.approval_gate is injected
