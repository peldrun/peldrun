# backend/tests/source_control/security/test_approval_bypass.py
"""
Security regression tests for the approval gate.

These tests attempt every bypass an agent, a tool, or a compromised
component might try against the approval gate, and verify that the
gate rejects the attempt with the correct error.

Design rules for these tests:
    - Every test states the ATTACK explicitly in its docstring.
    - Every test asserts on observable behavior, not internal state.
    - Every test is a regression test: if the gate ever allows the
      bypass, the test fails loudly, and CI stops the change.

The attacks covered are:

     1. Self-approval: an agent tries to consume a PENDING ticket
        without the user granting it.
     2. Content swap: after a legitimate approval, the diff hash is
        mutated before consumption.
     3. Base swap: after a legitimate approval, the base SHA is
        mutated before consumption.
     4. Target swap: after a legitimate approval, the target branch
        is mutated before consumption.
     5. Operation swap: after a legitimate approval, the operation
        kind is mutated before consumption.
     6. Repository swap: after a legitimate approval, the repository
        is mutated before consumption.
     7. Operation ID swap: after a legitimate approval, the operation
        ID is mutated before consumption.
     8. Replay: after a legitimate consumption, the same ticket is
        presented again.
     9. Legacy-path abuse: the legacy `consume_approval` (which did
        not validate bound fields) MUST raise.
    10. Structural forgery: constructing a fake `ApprovalTicket` via
        `dataclasses.replace` MUST NOT affect the gate's internal
        ticket.
    11. Frozen-dataclass mutation: attempting to mutate an
        `ApprovalTicket` in place MUST fail.
    12. Expired-ticket reuse: after expiry, the ticket cannot be
        granted or consumed.
"""

from __future__ import annotations

import dataclasses

import pytest

# Resilient imports: relative form works when pytest is run with
# `rootdir=backend`, the absolute form when the repository root is on
# sys.path. Both resolve to the same modules.
try:
    from ....integrations.source_control.domain.enums import ApprovalStatus
    from ....integrations.source_control.domain.models import ApprovalTicket
    from ....integrations.source_control.errors import (
        ApprovalConsumedError,
        ApprovalExpiredError,
        ApprovalInvalidError,
        ApprovalRequiredError,
    )
    from ....integrations.source_control.security.approval import (
        ApprovalGate,
    )
except (ImportError, ValueError):
    from backend.integrations.source_control.domain.enums import (
        ApprovalStatus,
    )
    from backend.integrations.source_control.domain.models import (
        ApprovalTicket,
    )
    from backend.integrations.source_control.errors import (
        ApprovalConsumedError,
        ApprovalExpiredError,
        ApprovalInvalidError,
        ApprovalRequiredError,
    )
    from backend.integrations.source_control.security.approval import (
        ApprovalGate,
    )


# A valid, deterministic 64-character lowercase hex value. These
# stand in for a base commit and a reviewed diff. They are NOT real
# hashes; the gate only validates the format.
_BASE_SHA = "a" * 64
_DIFF_SHA = "b" * 64
_TARGET = "feature/secure-branch"
_REPO = "repo_secure"
_OPERATION = "push_branch"
_OP_ID = "op_secure"


def _create_granted_ticket(gate: ApprovalGate) -> ApprovalTicket:
    """Create and grant a ticket for use in the bypass tests."""
    ticket = gate.create_ticket(
        operation_id=_OP_ID,
        repository_id=_REPO,
        operation=_OPERATION,
        base_sha=_BASE_SHA,
        diff_sha256=_DIFF_SHA,
        target_branch=_TARGET,
    )
    gate.grant(ticket.approval_id, actor="user")
    return ticket


def _consume_kwargs(**overrides):
    """
    Build the full kwargs for `ApprovalGate.consume`, then apply the
    caller's overrides. Used to make each test's deviation from the
    canonical call explicit.
    """
    kwargs = dict(
        expected_operation_id=_OP_ID,
        expected_repository_id=_REPO,
        expected_operation=_OPERATION,
        expected_base_sha=_BASE_SHA,
        expected_diff_sha256=_DIFF_SHA,
        expected_target_branch=_TARGET,
    )
    kwargs.update(overrides)
    return kwargs


# ---------------------------------------------------------------------
# Attack 1: Self-approval
# ---------------------------------------------------------------------


def test_agent_cannot_consume_pending_ticket(
    approval_gate: ApprovalGate,
) -> None:
    """
    ATTACK: An agent tries to consume a PENDING ticket without the
    user granting it.

    EXPECTED: `ApprovalRequiredError` is raised. The ticket remains
    PENDING.
    """
    ticket = approval_gate.create_ticket(
        operation_id=_OP_ID,
        repository_id=_REPO,
        operation=_OPERATION,
        base_sha=_BASE_SHA,
        diff_sha256=_DIFF_SHA,
        target_branch=_TARGET,
    )

    with pytest.raises(ApprovalRequiredError):
        approval_gate.consume(ticket.approval_id, **_consume_kwargs())

    still_pending = approval_gate.get_ticket(ticket.approval_id)
    assert still_pending is not None
    assert still_pending.status == ApprovalStatus.PENDING
    assert still_pending.approved_by_user is False


# ---------------------------------------------------------------------
# Attacks 2-7: Bound-field swap after approval
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "field_name,override_value",
    [
        ("expected_operation_id", "op_different"),
        ("expected_repository_id", "repo_different"),
        ("expected_operation", "create_change_request"),
        ("expected_base_sha", "c" * 64),
        ("expected_diff_sha256", "d" * 64),
        ("expected_target_branch", "feature/different"),
    ],
)
def test_bound_field_mutation_is_rejected(
    approval_gate: ApprovalGate,
    field_name: str,
    override_value: str,
) -> None:
    """
    ATTACK: After a legitimate approval, the caller supplies a
    different value for a bound field.

    EXPECTED: `ApprovalInvalidError` is raised. The ticket is NOT
    consumed.
    """
    ticket = _create_granted_ticket(approval_gate)

    with pytest.raises(ApprovalInvalidError):
        approval_gate.consume(
            ticket.approval_id,
            **_consume_kwargs(**{field_name: override_value}),
        )

    # The ticket must remain in the GRANTED state; the failed
    # attempt MUST NOT consume it.
    unchanged = approval_gate.get_ticket(ticket.approval_id)
    assert unchanged is not None
    assert unchanged.status == ApprovalStatus.GRANTED
    assert unchanged.consumed_at is None


# ---------------------------------------------------------------------
# Attack 8: Replay
# ---------------------------------------------------------------------


def test_consumed_ticket_cannot_be_replayed(
    approval_gate: ApprovalGate,
) -> None:
    """
    ATTACK: After a legitimate consumption, the same ticket is
    presented again.

    EXPECTED: `ApprovalConsumedError` is raised.
    """
    ticket = _create_granted_ticket(approval_gate)

    # First consumption succeeds.
    consumed = approval_gate.consume(
        ticket.approval_id, **_consume_kwargs()
    )
    assert consumed.status == ApprovalStatus.CONSUMED

    # Second consumption must fail.
    with pytest.raises(ApprovalConsumedError):
        approval_gate.consume(ticket.approval_id, **_consume_kwargs())


# ---------------------------------------------------------------------
# Attack 9: Legacy-path abuse
# ---------------------------------------------------------------------


def test_legacy_consume_approval_is_disabled(
    approval_gate: ApprovalGate,
) -> None:
    """
    ATTACK: Use the legacy `consume_approval` (which did not compare
    bound fields) to bypass validation.

    EXPECTED: `NotImplementedError` is raised. The ticket remains
    GRANTED and consumable via the canonical path.
    """
    ticket = _create_granted_ticket(approval_gate)

    with pytest.raises(NotImplementedError):
        approval_gate.consume_approval(ticket.approval_id)

    # The ticket must still be GRANTED — the legacy call must not
    # have consumed it.
    still_granted = approval_gate.get_ticket(ticket.approval_id)
    assert still_granted is not None
    assert still_granted.status == ApprovalStatus.GRANTED
    assert still_granted.consumed_at is None

    # Canonical consume still works.
    consumed = approval_gate.consume(
        ticket.approval_id, **_consume_kwargs()
    )
    assert consumed.status == ApprovalStatus.CONSUMED


# ---------------------------------------------------------------------
# Attack 10: Structural forgery
# ---------------------------------------------------------------------


def test_forged_ticket_does_not_affect_gate_state(
    approval_gate: ApprovalGate,
) -> None:
    """
    ATTACK: An agent constructs a fake `ApprovalTicket` with
    `approved_by_user=True` (via `dataclasses.replace`) and hopes the
    gate honors it.

    EXPECTED: The gate uses its OWN internal ticket (which is still
    PENDING). Consumption fails with `ApprovalRequiredError`.
    """
    pending = approval_gate.create_ticket(
        operation_id=_OP_ID,
        repository_id=_REPO,
        operation=_OPERATION,
        base_sha=_BASE_SHA,
        diff_sha256=_DIFF_SHA,
        target_branch=_TARGET,
    )

    forged = dataclasses.replace(
        pending,
        approved_by_user=True,
        status=ApprovalStatus.GRANTED,
    )

    # The forged ticket looks approved to any code that inspects
    # it directly.
    assert forged.approved_by_user is True
    assert forged.status == ApprovalStatus.GRANTED

    # But the gate ignores the forged object. Consumption via the
    # original approval_id reads the gate's internal (still
    # PENDING) ticket.
    with pytest.raises(ApprovalRequiredError):
        approval_gate.consume(
            pending.approval_id, **_consume_kwargs()
        )

    # The gate's internal ticket must remain PENDING.
    still_pending = approval_gate.get_ticket(pending.approval_id)
    assert still_pending is not None
    assert still_pending.status == ApprovalStatus.PENDING
    assert still_pending.approved_by_user is False


# ---------------------------------------------------------------------
# Attack 11: Frozen-dataclass mutation
# ---------------------------------------------------------------------


def test_ticket_is_immutable(approval_gate: ApprovalGate) -> None:
    """
    ATTACK: Try to mutate an `ApprovalTicket` in place to flip
    `approved_by_user` or any bound field.

    EXPECTED: `dataclasses.FrozenInstanceError` is raised for every
    attempt.
    """
    ticket = approval_gate.create_ticket(
        operation_id=_OP_ID,
        repository_id=_REPO,
        operation=_OPERATION,
        base_sha=_BASE_SHA,
        diff_sha256=_DIFF_SHA,
        target_branch=_TARGET,
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        ticket.approved_by_user = True  # type: ignore[misc]

    with pytest.raises(dataclasses.FrozenInstanceError):
        ticket.status = ApprovalStatus.GRANTED  # type: ignore[misc]

    with pytest.raises(dataclasses.FrozenInstanceError):
        ticket.diff_sha256 = "e" * 64  # type: ignore[misc]

    with pytest.raises(dataclasses.FrozenInstanceError):
        ticket.base_sha = "f" * 64  # type: ignore[misc]


# ---------------------------------------------------------------------
# Attack 12: Expired-ticket reuse
# ---------------------------------------------------------------------


def test_expired_ticket_cannot_be_granted_or_consumed(
    approval_gate: ApprovalGate,
) -> None:
    """
    ATTACK: Create a ticket with a TTL in the past, then try to use
    it.

    EXPECTED: Both `grant` and `consume` raise
    `ApprovalExpiredError`.
    """
    expired = approval_gate.create_ticket(
        operation_id=_OP_ID,
        repository_id=_REPO,
        operation=_OPERATION,
        base_sha=_BASE_SHA,
        diff_sha256=_DIFF_SHA,
        target_branch=_TARGET,
        ttl_seconds=-1,
    )

    with pytest.raises(ApprovalExpiredError):
        approval_gate.grant(expired.approval_id, actor="user")

    with pytest.raises(ApprovalExpiredError):
        approval_gate.consume(
            expired.approval_id, **_consume_kwargs()
        )


# ---------------------------------------------------------------------
# Attack 13: Cross-operation confusion
# ---------------------------------------------------------------------


def test_ticket_from_different_operation_is_rejected(
    approval_gate: ApprovalGate,
) -> None:
    """
    ATTACK: Present a valid ticket issued for operation A, but try to
    use it for operation B.

    EXPECTED: `ApprovalInvalidError` is raised because the operation
    ID bound to the ticket does not match the expected value.
    """
    ticket = _create_granted_ticket(approval_gate)

    with pytest.raises(ApprovalInvalidError):
        approval_gate.consume(
            ticket.approval_id,
            **_consume_kwargs(expected_operation_id="op_different"),
        )

    # The ticket must remain GRANTED — the failed attempt must not
    # have consumed it.
    unchanged = approval_gate.get_ticket(ticket.approval_id)
    assert unchanged is not None
    assert unchanged.status == ApprovalStatus.GRANTED
