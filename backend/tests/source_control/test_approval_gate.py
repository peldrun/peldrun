from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
from typing import Any
import pytest

# Resilient imports for security approval and errors
try:
    from ...integrations.source_control.errors import (
        NotFoundError,
        PermissionDeniedError,
        ValidationError,
    )
    from ...integrations.source_control.security.approval import (
        ApprovalGate,
        ApprovalTicket,
        compute_diff_sha256,
    )
except (ImportError, ValueError):
    from backend.integrations.source_control.errors import (
        NotFoundError,
        PermissionDeniedError,
        ValidationError,
    )
    from backend.integrations.source_control.security.approval import (
        ApprovalGate,
        ApprovalTicket,
        compute_diff_sha256,
    )


# ---------------------------------------------------------------------------
# 1. Ticket Creation & Default State Tests
# ---------------------------------------------------------------------------


def test_create_ticket_metadata_and_expiry(approval_gate: ApprovalGate) -> None:
    """Verifies ticket is created with unapproved status, valid hash, and future expiration."""
    diff_hash = compute_diff_sha256("diff --git a/file.py b/file.py\n+new line")
    ticket: ApprovalTicket = approval_gate.create_ticket(
        operation_id="op_12345",
        repository_id="repo_999",
        operation="push_branch",
        base_sha="a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
        diff_sha256=diff_hash,
        target_branch="feature/test-approval",
        ttl_seconds=300,
    )

    assert ticket.approval_id is not None
    assert ticket.operation_id == "op_12345"
    assert ticket.repository_id == "repo_999"
    assert ticket.operation == "push_branch"
    assert ticket.base_sha == "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
    assert ticket.diff_sha256 == diff_hash
    assert ticket.target_branch == "feature/test-approval"
    assert ticket.approved_by_user is False
    assert ticket.expires_at > datetime.utcnow()

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


def test_no_self_approval_consumption_fails_unapproved(
    approval_gate: ApprovalGate,
) -> None:
    """Ensures unapproved tickets cannot be consumed by the agent runtime."""
    diff_hash = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    ticket = approval_gate.create_ticket(
        operation_id="op_guard",
        repository_id="repo_1",
        operation="push_branch",
        base_sha="base_sha_123",
        diff_sha256=diff_hash,
        target_branch="feature/guard",
    )

    assert ticket.approved_by_user is False

    with pytest.raises(PermissionDeniedError) as exc_info:
        approval_gate.consume_approval(ticket.approval_id)

    assert "not approved" in str(exc_info.value).lower() or "missing" in str(exc_info.value).lower()


def test_approve_ticket_by_user(approval_gate: ApprovalGate) -> None:
    """Verifies explicit human action marks ticket as approved with user audit record."""
    diff_hash = "a" * 64
    ticket = approval_gate.create_ticket(
        operation_id="op_human",
        repository_id="repo_1",
        operation="push_branch",
        base_sha="base_sha_123",
        diff_sha256=diff_hash,
        target_branch="feature/human",
    )

    approved = approval_gate.approve_ticket(
        approval_id=ticket.approval_id,
        user_id="lead_developer",
        notes="Code reviewed and unit tests passed.",
    )

    assert approved.approved_by_user is True
    retrieved = approval_gate.get_ticket(ticket.approval_id)
    assert retrieved is not None
    assert retrieved.approved_by_user is True


# ---------------------------------------------------------------------------
# 3. Single-Use Consumption Tests
# ---------------------------------------------------------------------------


def test_single_use_ticket_consumption(approval_gate: ApprovalGate) -> None:
    """Verifies that approved tickets can be consumed exactly once, preventing replay attacks."""
    diff_hash = "b" * 64
    ticket = approval_gate.create_ticket(
        operation_id="op_single_use",
        repository_id="repo_1",
        operation="push_branch",
        base_sha="base_sha_123",
        diff_sha256=diff_hash,
        target_branch="feature/single-use",
    )

    approval_gate.approve_ticket(ticket.approval_id, user_id="user_admin")

    # First consumption must succeed
    consumed = approval_gate.consume_approval(ticket.approval_id)
    assert consumed.approval_id == ticket.approval_id

    # Second consumption of the same ticket must fail
    with pytest.raises(PermissionDeniedError) as exc_info:
        approval_gate.consume_approval(ticket.approval_id)

    assert "already consumed" in str(exc_info.value).lower() or "invalid" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# 4. Expiration & Rejection Tests
# ---------------------------------------------------------------------------


def test_expired_ticket_fails_consumption(approval_gate: ApprovalGate) -> None:
    """Ensures expired tickets cannot be approved or consumed."""
    diff_hash = "c" * 64
    ticket = approval_gate.create_ticket(
        operation_id="op_expire",
        repository_id="repo_1",
        operation="push_branch",
        base_sha="base_sha_123",
        diff_sha256=diff_hash,
        target_branch="feature/expire",
        ttl_seconds=-10,  # Pre-expired ticket
    )

    with pytest.raises(PermissionDeniedError) as exc_info:
        approval_gate.approve_ticket(ticket.approval_id, user_id="user_admin")
    assert "expired" in str(exc_info.value).lower()

    with pytest.raises(PermissionDeniedError) as exc_info:
        approval_gate.consume_approval(ticket.approval_id)
    assert "expired" in str(exc_info.value).lower()


def test_reject_ticket_invalidates_proposal(approval_gate: ApprovalGate) -> None:
    """Verifies human rejection invalidates ticket and prevents any subsequent consumption."""
    diff_hash = "d" * 64
    ticket = approval_gate.create_ticket(
        operation_id="op_reject",
        repository_id="repo_1",
        operation="push_branch",
        base_sha="base_sha_123",
        diff_sha256=diff_hash,
        target_branch="feature/reject",
    )

    approval_gate.reject_ticket(
        approval_id=ticket.approval_id,
        user_id="lead_reviewer",
        reason="Security policy violation: detected potential secret in commit.",
    )

    # Attempting to consume a rejected ticket must fail
    with pytest.raises(PermissionDeniedError) as exc_info:
        approval_gate.consume_approval(ticket.approval_id)

    assert "rejected" in str(exc_info.value).lower() or "not approved" in str(exc_info.value).lower()


def test_consume_unknown_approval_id_fails(approval_gate: ApprovalGate) -> None:
    """Ensures consuming a non-existent approval ID raises PermissionDeniedError."""
    with pytest.raises(PermissionDeniedError):
        approval_gate.consume_approval("non_existent_ticket_id_xyz")


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

    digest_1 = compute_diff_sha256(sample_diff)
    digest_2 = compute_diff_sha256(sample_diff)

    assert digest_1 == digest_2
    assert len(digest_1) == 64
    assert digest_1 == hashlib.sha256(sample_diff.encode("utf-8")).hexdigest()


def test_compute_diff_sha256_mutation_detection() -> None:
    """Ensures any single-character alteration produces a completely distinct hash."""
    diff_original = "diff --git a/test.py b/test.py\n+value = 1\n"
    diff_mutated = "diff --git a/test.py b/test.py\n+value = 2\n"

    hash_original = compute_diff_sha256(diff_original)
    hash_mutated = compute_diff_sha256(diff_mutated)

    assert hash_original != hash_mutated
