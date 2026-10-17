# backend/tests/source_control/test_approval_gate.py
"""
Tests for the approval gate.

The approval gate is the single place where human consent for a
remote side effect is recorded. These tests exercise:

    - Ticket creation and validation.
    - The canonical consumption path (`consume(expected_*)`).
    - Refusal of unapproved consumption.
    - Single-use semantics.
    - Expiry and rejection.
    - Cryptographic diff binding.
    - The disabled legacy path (`consume_approval`), which MUST
      raise `NotImplementedError` and MUST NOT alter the ticket.
    - The deprecated alias methods (`approve_ticket`,
      `reject_ticket`), which MUST emit `DeprecationWarning` while
      still delegating to the canonical API.
    - The concrete `audit_logger` fixture, which used to crash
      because it instantiated the abstract `AuditLogger` class.
"""

from __future__ import annotations

import hashlib
import warnings
from datetime import datetime, timedelta, timezone
import pytest

# Resilient imports for approval gate and related components
try:
    from ...integrations.source_control.domain.enums import ApprovalStatus
    from ...integrations.source_control.domain.models import ApprovalTicket
    from ...integrations.source_control.errors import (
        ApprovalConsumedError,
        ApprovalExpiredError,
        ApprovalInvalidError,
        ApprovalRequiredError,
        ValidationError,
    )
    from ...integrations.source_control.security.approval import ApprovalGate
except (ImportError, ValueError):
    from backend.integrations.source_control.domain.enums import ApprovalStatus
    from backend.integrations.source_control.domain.models import ApprovalTicket
    from backend.integrations.source_control.errors import (
        ApprovalConsumedError,
        ApprovalExpiredError,
        ApprovalInvalidError,
        ApprovalRequiredError,
        ValidationError,
    )
    from backend.integrations.source_control.security.approval import ApprovalGate


# A valid, deterministic, 64-character lowercase hex SHA used
# throughout the tests. It represents "some base commit" and is not
# meant to be a real one.
_BASE_SHA = (
    "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678901234567890123456789012"
)


def _compute_diff_sha256(diff_content: str) -> str:
    """Helper to compute a valid 64-character lowercase hex SHA-256 digest."""
    return hashlib.sha256(diff_content.encode("utf-8")).hexdigest()


def _make_ticket(
    gate: ApprovalGate,
    *,
    operation_id: str = "op_test",
    repository_id: str = "repo_test",
    operation: str = "push_branch",
    target_branch: str = "feature/test",
    base_sha: str = _BASE_SHA,
    diff_seed: str = "default-diff",
    ttl_seconds: int = 300,
) -> ApprovalTicket:
    """
    Build a deterministic ticket for tests that do not exercise a
    specific creation parameter.
    """
    return gate.create_ticket(
        operation_id=operation_id,
        repository_id=repository_id,
        operation=operation,
        base_sha=base_sha,
        diff_sha256=_compute_diff_sha256(diff_seed),
        target_branch=target_branch,
        ttl_seconds=ttl_seconds,
    )


# ---------------------------------------------------------------------------
# 1. Ticket Creation & Default State Tests
# ---------------------------------------------------------------------------

def test_create_ticket_metadata_and_expiry(approval_gate: ApprovalGate) -> None:
    """Verifies ticket is created with pending status, valid hash, and future expiration."""
    diff_hash = _compute_diff_sha256("diff --git a/file.py b/file.py\n+new line")
    ticket: ApprovalTicket = approval_gate.create_ticket(
        operation_id="op_12345",
        repository_id="repo_999",
        operation="push_branch",
        base_sha=_BASE_SHA,
        diff_sha256=diff_hash,
        target_branch="feature/test-approval",
        ttl_seconds=300,
    )

    assert ticket.approval_id is not None
    assert ticket.operation_id == "op_12345"
    assert ticket.repository_id == "repo_999"
    assert ticket.operation == "push_branch"
    assert ticket.base_sha == _BASE_SHA
    assert ticket.diff_sha256 == diff_hash
    assert ticket.target_branch == "feature/test-approval"
    assert ticket.approved_by_user is False
    assert ticket.status == ApprovalStatus.PENDING
    assert ticket.expires_at > datetime.now(timezone.utc)

    # Verify retrieval
    retrieved = approval_gate.get_ticket(ticket.approval_id)
    assert retrieved is not None
    assert retrieved.approval_id == ticket.approval_id


def test_create_ticket_validates_inputs(approval_gate: ApprovalGate) -> None:
    """Ensures create_ticket rejects empty IDs, invalid operations, or malformed diff hashes."""
    with pytest.raises(ValidationError):
        approval_gate.create_ticket(
            operation_id="",
            repository_id="repo_1",
            operation="push_branch",
            base_sha="abc",
            diff_sha256="1234",
            target_branch="feature/x",
        )

    with pytest.raises(ValidationError):
        approval_gate.create_ticket(
            operation_id="op_1",
            repository_id="repo_1",
            operation="push_branch",
            base_sha="abc",
            diff_sha256="short_hash",  # Not 64 hex characters
            target_branch="feature/x",
        )


# ---------------------------------------------------------------------------
# 2. No Self-Approval & Consumption Enforcement Tests
# ---------------------------------------------------------------------------

def test_no_self_approval_consumption_fails_unapproved(approval_gate: ApprovalGate) -> None:
    """Ensures unapproved (pending) tickets cannot be consumed by the agent runtime."""
    ticket = _make_ticket(approval_gate, operation_id="op_guard")

    assert ticket.status == ApprovalStatus.PENDING
    assert ticket.approved_by_user is False

    with pytest.raises(ApprovalRequiredError) as exc_info:
        approval_gate.consume(
            approval_id=ticket.approval_id,
            expected_operation_id=ticket.operation_id,
            expected_repository_id=ticket.repository_id,
            expected_operation=ticket.operation,
            expected_base_sha=ticket.base_sha,
            expected_diff_sha256=ticket.diff_sha256,
            expected_target_branch=ticket.target_branch,
        )

    assert "not been granted" in str(exc_info.value).lower() or "missing" in str(exc_info.value).lower()


def test_grant_ticket_by_user(approval_gate: ApprovalGate) -> None:
    """Verifies explicit human action marks ticket as granted with user audit record."""
    ticket = _make_ticket(approval_gate, operation_id="op_human")

    approved = approval_gate.grant(
        approval_id=ticket.approval_id,
        actor="lead_developer",
    )

    assert approved.approved_by_user is True
    assert approved.status == ApprovalStatus.GRANTED

    retrieved = approval_gate.get_ticket(ticket.approval_id)
    assert retrieved is not None
    assert retrieved.approved_by_user is True
    assert retrieved.status == ApprovalStatus.GRANTED


# ---------------------------------------------------------------------------
# 3. Single-Use Consumption Tests
# ---------------------------------------------------------------------------

def test_single_use_ticket_consumption(approval_gate: ApprovalGate) -> None:
    """Verifies that approved tickets can be consumed exactly once, preventing replay attacks."""
    ticket = _make_ticket(approval_gate, operation_id="op_single_use")

    approval_gate.grant(ticket.approval_id, actor="user_admin")

    # First consumption must succeed
    consumed = approval_gate.consume(
        approval_id=ticket.approval_id,
        expected_operation_id=ticket.operation_id,
        expected_repository_id=ticket.repository_id,
        expected_operation=ticket.operation,
        expected_base_sha=ticket.base_sha,
        expected_diff_sha256=ticket.diff_sha256,
        expected_target_branch=ticket.target_branch,
    )
    assert consumed.status == ApprovalStatus.CONSUMED
    assert consumed.consumed_at is not None

    # Second consumption of the same ticket must fail
    with pytest.raises(ApprovalConsumedError) as exc_info:
        approval_gate.consume(
            approval_id=ticket.approval_id,
            expected_operation_id=ticket.operation_id,
            expected_repository_id=ticket.repository_id,
            expected_operation=ticket.operation,
            expected_base_sha=ticket.base_sha,
            expected_diff_sha256=ticket.diff_sha256,
            expected_target_branch=ticket.target_branch,
        )

    assert "already been consumed" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# 4. Expiration & Rejection Tests
# ---------------------------------------------------------------------------

def test_expired_ticket_fails_consumption(approval_gate: ApprovalGate) -> None:
    """Ensures expired tickets cannot be granted or consumed."""
    ticket = _make_ticket(
        approval_gate,
        operation_id="op_expire",
        ttl_seconds=-10,  # Pre-expired ticket
    )

    with pytest.raises(ApprovalExpiredError) as exc_info:
        approval_gate.grant(ticket.approval_id, actor="user_admin")
    assert "expired" in str(exc_info.value).lower()

    with pytest.raises(ApprovalExpiredError) as exc_info:
        approval_gate.consume(
            approval_id=ticket.approval_id,
            expected_operation_id=ticket.operation_id,
            expected_repository_id=ticket.repository_id,
            expected_operation=ticket.operation,
            expected_base_sha=ticket.base_sha,
            expected_diff_sha256=ticket.diff_sha256,
            expected_target_branch=ticket.target_branch,
        )
    assert "expired" in str(exc_info.value).lower()


def test_reject_ticket_invalidates_proposal(approval_gate: ApprovalGate) -> None:
    """Verifies human rejection invalidates ticket and prevents any subsequent consumption."""
    ticket = _make_ticket(approval_gate, operation_id="op_reject")

    approval_gate.reject(
        approval_id=ticket.approval_id,
        actor="lead_reviewer",
        reason="Security policy violation: detected potential secret in commit.",
    )

    retrieved = approval_gate.get_ticket(ticket.approval_id)
    assert retrieved is not None
    assert retrieved.status == ApprovalStatus.REJECTED

    # Attempting to consume a rejected ticket must fail
    with pytest.raises(ApprovalInvalidError) as exc_info:
        approval_gate.consume(
            approval_id=ticket.approval_id,
            expected_operation_id=ticket.operation_id,
            expected_repository_id=ticket.repository_id,
            expected_operation=ticket.operation,
            expected_base_sha=ticket.base_sha,
            expected_diff_sha256=ticket.diff_sha256,
            expected_target_branch=ticket.target_branch,
        )

    assert "rejected" in str(exc_info.value).lower()


def test_consume_unknown_approval_id_fails(approval_gate: ApprovalGate) -> None:
    """Ensures consuming a non-existent approval ID raises ApprovalInvalidError."""
    with pytest.raises(ApprovalInvalidError):
        approval_gate.consume(
            approval_id="non_existent_ticket_id_xyz",
            expected_operation_id="op_1",
            expected_repository_id="repo_1",
            expected_operation="push_branch",
            expected_base_sha=_BASE_SHA,
            expected_diff_sha256=_compute_diff_sha256("test"),
            expected_target_branch="main",
        )


# ---------------------------------------------------------------------------
# 5. Cryptographic Diff SHA-256 Tests
# ---------------------------------------------------------------------------

def test_compute_diff_sha256_deterministic() -> None:
    """Verifies SHA-256 fingerprint is exactly 64 lowercase hex characters and deterministic."""
    sample_diff = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,3 +1,4 @@\n"
        "+# Added comment\n"
    )

    digest_1 = _compute_diff_sha256(sample_diff)
    digest_2 = _compute_diff_sha256(sample_diff)

    assert digest_1 == digest_2
    assert len(digest_1) == 64
    assert digest_1 == hashlib.sha256(sample_diff.encode("utf-8")).hexdigest()


def test_compute_diff_sha256_mutation_detection() -> None:
    """Ensures any single-character alteration produces a completely distinct hash."""
    diff_original = "diff --git a/test.py b/test.py\n+value = 1\n"
    diff_mutated = "diff --git a/test.py b/test.py\n+value = 2\n"

    hash_original = _compute_diff_sha256(diff_original)
    hash_mutated = _compute_diff_sha256(diff_mutated)

    assert hash_original != hash_mutated


# ---------------------------------------------------------------------------
# 6. Legacy Path Must Be Disabled (Security Regression Test)
# ---------------------------------------------------------------------------

def test_consume_approval_legacy_path_is_disabled(
    approval_gate: ApprovalGate,
) -> None:
    """
    The legacy `consume_approval(approval_id)` path MUST raise
    `NotImplementedError` and MUST NOT alter the ticket.

    The legacy path used to mark the ticket as consumed without
    comparing its bound fields to the caller's expectation, which
    allowed a diff approved by the user to be swapped for a
    different diff at execution time. The path is disabled until it
    is removed entirely.

    This test guarantees that:
        - The legacy method raises `NotImplementedError`.
        - The ticket remains in the GRANTED state, so the canonical
          `consume(...)` call still works afterwards.
        - The legacy call never leaks the ticket into a CONSUMED
          state silently.
    """
    ticket = _make_ticket(
        approval_gate,
        operation_id="op_legacy",
        repository_id="repo_legacy",
        target_branch="feature/legacy",
        diff_seed="legacy-diff",
    )
    approval_gate.grant(ticket.approval_id, actor="user")

    with pytest.raises(NotImplementedError) as exc_info:
        approval_gate.consume_approval(ticket.approval_id)
    assert "consume_approval" in str(exc_info.value)

    # The ticket must still be GRANTED — the legacy call must not
    # have transitioned it to CONSUMED.
    still_granted = approval_gate.get_ticket(ticket.approval_id)
    assert still_granted is not None
    assert still_granted.status == ApprovalStatus.GRANTED
    assert still_granted.consumed_at is None

    # The canonical path must still work on the same ticket.
    consumed = approval_gate.consume(
        approval_id=ticket.approval_id,
        expected_operation_id=ticket.operation_id,
        expected_repository_id=ticket.repository_id,
        expected_operation=ticket.operation,
        expected_base_sha=ticket.base_sha,
        expected_diff_sha256=ticket.diff_sha256,
        expected_target_branch=ticket.target_branch,
    )
    assert consumed.status == ApprovalStatus.CONSUMED


# ---------------------------------------------------------------------------
# 7. Deprecated Aliases Must Warn and Still Work
# ---------------------------------------------------------------------------

def test_approve_and_reject_ticket_aliases_emit_deprecation_warning(
    approval_gate: ApprovalGate,
) -> None:
    """
    `approve_ticket` and `reject_ticket` MUST emit
    `DeprecationWarning` and MUST delegate to the canonical API.

    These aliases are kept for backward compatibility with older
    callers. The warning is the contract that lets future code
    detect and migrate away from them. Removing the warning without
    removing the alias is a silent regression; this test prevents
    that.
    """
    # --- approve_ticket --------------------------------------------------
    ticket_to_approve = _make_ticket(
        approval_gate,
        operation_id="op_alias_approve",
        diff_seed="alias-approve-diff",
    )

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", DeprecationWarning)
        approved = approval_gate.approve_ticket(
            ticket_to_approve.approval_id, user_id="reviewer"
        )

    assert approved.status == ApprovalStatus.GRANTED
    assert approved.approved_by_user is True
    assert any(
        issubclass(record.category, DeprecationWarning)
        and "approve_ticket" in str(record.message)
        for record in captured
    ), (
        "approve_ticket() did not emit DeprecationWarning; "
        f"captured warnings: {[str(r.message) for r in captured]!r}"
    )

    # --- reject_ticket ---------------------------------------------------
    ticket_to_reject = _make_ticket(
        approval_gate,
        operation_id="op_alias_reject",
        diff_seed="alias-reject-diff",
    )

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", DeprecationWarning)
        rejected = approval_gate.reject_ticket(
            ticket_to_reject.approval_id,
            user_id="reviewer",
            reason="not needed",
        )

    assert rejected.status == ApprovalStatus.REJECTED
    assert rejected.approved_by_user is False
    assert any(
        issubclass(record.category, DeprecationWarning)
        and "reject_ticket" in str(record.message)
        for record in captured
    ), (
        "reject_ticket() did not emit DeprecationWarning; "
        f"captured warnings: {[str(r.message) for r in captured]!r}"
    )


def test_approve_ticket_alias_rejects_secret_in_notes(
    approval_gate: ApprovalGate,
) -> None:
    """
    `approve_ticket(notes=...)` MUST reject notes that appear to
    contain a secret.

    The redaction layer would scrub the secret from the audit log,
    but a hard failure surfaces the caller bug earlier and prevents
    the value from ever entering the gate's internal state.
    """
    ticket = _make_ticket(
        approval_gate,
        operation_id="op_alias_secret_notes",
        diff_seed="alias-secret-diff",
    )

    # Suppress the DeprecationWarning this call will emit; the focus
    # of this test is the secret-in-notes behavior.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        with pytest.raises(ValidationError) as exc_info:
            approval_gate.approve_ticket(
                ticket.approval_id,
                user_id="reviewer",
                notes="token: ghp_1234567890abcdefghijklmnopqrstuvwxyzAB",
            )

    assert "secret" in str(exc_info.value).lower()

    # The ticket must remain PENDING — the alias must not have
    # granted it before the notes check failed.
    unchanged = approval_gate.get_ticket(ticket.approval_id)
    assert unchanged is not None
    assert unchanged.status == ApprovalStatus.PENDING


# ---------------------------------------------------------------------------
# 8. Concrete Audit Logger Fixture (Regression Test)
# ---------------------------------------------------------------------------

def test_audit_logger_fixture_is_concrete(
    audit_logger,
) -> None:
    """
    The `audit_logger` fixture MUST return a concrete `AuditLogger`
    subclass instance.

    A previous version of `conftest.py` instantiated the abstract
    `AuditLogger` class directly, which raised `TypeError` at
    construction time. This test guards against that regression by
    checking that:

        - The fixture returns an object, not a class.
        - The object satisfies the `record` / `query` interface.
        - Recording an event does not raise and the event is
          retrievable via `query`.
    """
    from backend.integrations.source_control.security.audit import (
        AuditEventType,
        AuditLogger,
    )

    # The fixture MUST return an instance, not the abstract class.
    assert audit_logger is not None
    assert not isinstance(audit_logger, type)

    # It MUST be a concrete subclass of the abstract base.
    assert isinstance(audit_logger, AuditLogger)

    # The abstract methods must be resolved — instantiating the
    # abstract class would have failed before we even got here, but
    # this check is explicit for future readers.
    assert not getattr(
        audit_logger, "__abstractmethods__", None
    ), (
        "audit_logger fixture returned an instance that still has "
        "unresolved abstract methods."
    )

    # Smoke test: record an event and read it back through the
    # public API. This catches a class that is concrete but whose
    # storage is broken.
    recorded = audit_logger.record(
        AuditEventType.OTHER,
        action="test_fixture",
        message="audit_logger fixture smoke test",
    )
    assert recorded is not None

    events = audit_logger.query(limit=10)
    assert any(
        getattr(event, "event_id", None) == recorded.event_id
        for event in events
    ), "Recorded event was not retrievable via query()."
