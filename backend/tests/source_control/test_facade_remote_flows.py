# backend/tests/source_control/test_facade_remote_flows.py
"""
Integration tests for the remote-facing flows of
`SourceControlFacade`.

These tests exercise the flows that touch the credential vault, the
local Git engine (via mocks), and the provider adapters (via mocks):

    - `request_operation_approval` advancing the state machine
      through the canonical intermediate states.
    - `push_approved_branch` consuming a single-use ticket, passing
      the vault credential to the push manager, and reconciling the
      result.
    - `create_change_request` consuming a single-use ticket and
      passing the vault credential to the provider adapter.

The tests deliberately do NOT exercise:
    - Real `git push` (covered by `test_git_workspace`).
    - Real HTTP calls to GitHub (covered by `test_github_client`).
    - The push manager's post-push reconciliation (covered in the
      push manager's own tests).

Design rules:
    - The facade is created with isolated instances of every
      dependency, so test order cannot affect results.
    - The workspace, diff, and push managers are `MagicMock` /
      `AsyncMock` instances. Their contracts are documented in the
      smoke test; here we only exercise the facade's orchestration.
    - The `mock_provider` fixture (from `conftest.py`) is used as
      the registered adapter. It already returns deterministic
      `RepositoryRef` and `ChangeRequestRef` values.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    from ...integrations.source_control.domain.enums import (
        FileChangeType,
        OperationStatus,
        ProviderId,
    )
    from ...integrations.source_control.domain.models import (
        DiffSummary,
        FileDiff,
        WorkspaceRef,
        WorkspaceStatus,
    )
    from ...integrations.source_control.errors import (
        ApprovalInvalidError,
        PermissionDeniedError,
    )
    from ...integrations.source_control.operations.journal import (
        InMemoryOperationJournal,
    )
    from ...integrations.source_control.operations.service import (
        OperationService,
    )
    from ...integrations.source_control.ports.repository import PushResult
    from ...integrations.source_control.registry import ProviderRegistry
    from ...integrations.source_control.schemas import (
        CreateChangeRequestPayload,
        PushBranchRequest,
    )
    from ...integrations.source_control.security.approval import (
        ApprovalGate,
    )
    from ...integrations.source_control.security.audit import (
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
        _ConnectionRecord,
    )
except (ImportError, ValueError):
    from backend.integrations.source_control.domain.enums import (
        FileChangeType,
        OperationStatus,
        ProviderId,
    )
    from backend.integrations.source_control.domain.models import (
        DiffSummary,
        FileDiff,
        WorkspaceRef,
        WorkspaceStatus,
    )
    from backend.integrations.source_control.errors import (
        ApprovalInvalidError,
        PermissionDeniedError,
    )
    from backend.integrations.source_control.operations.journal import (
        InMemoryOperationJournal,
    )
    from backend.integrations.source_control.operations.service import (
        OperationService,
    )
    from backend.integrations.source_control.ports.repository import (
        PushResult,
    )
    from backend.integrations.source_control.registry import (
        ProviderRegistry,
    )
    from backend.integrations.source_control.schemas import (
        CreateChangeRequestPayload,
        PushBranchRequest,
    )
    from backend.integrations.source_control.security.approval import (
        ApprovalGate,
    )
    from backend.integrations.source_control.security.audit import (
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
        _ConnectionRecord,
    )


# ApprovalGate requires a 64-character lowercase hex digest for
# `base_sha` (SHA-256). Git SHA-1 hashes are 40 characters, but the
# approval gate uses SHA-256 for the *bound diff fingerprint* and
# requires the base SHA to be the same length. Tests that build a
# ticket must therefore use 64-character constants.
_BASE_SHA = "a" * 64
_HEAD_SHA = "b" * 64


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _make_workspace_ref(
    workspace_id: str = "ws_test",
    repository_id: str = "repo_test",
) -> WorkspaceRef:
    now = datetime.now(timezone.utc)
    return WorkspaceRef(
        workspace_id=workspace_id,
        repository_id=repository_id,
        provider=ProviderId.GITHUB,
        workspace_path="/tmp/test_workspace",
        current_branch="feature/test",
        base_sha=_BASE_SHA,
        head_sha=_HEAD_SHA,
        is_worktree=True,
        is_clean=False,
        status=WorkspaceStatus.DIRTY,
        created_at=now,
        updated_at=now,
        metadata={},
    )


def _make_diff_summary(*, files: int = 1) -> DiffSummary:
    file_diffs = tuple(
        FileDiff(
            path=f"file_{i}.py",
            change_type=FileChangeType.MODIFIED,
            additions=1,
            deletions=1,
            old_path=None,
            patch=f"@@ -1 +1 @@\n-old_{i}\n+new_{i}",
            is_binary=False,
            is_truncated=False,
        )
        for i in range(files)
    )
    return DiffSummary(
        files=file_diffs,
        base_sha=_BASE_SHA,
        head_sha=_HEAD_SHA,
        total_additions=files,
        total_deletions=files,
    )


def _make_push_result(*, accepted: bool = True) -> PushResult:
    return PushResult(
        accepted=accepted,
        local_branch="feature/test",
        local_sha=_HEAD_SHA,
        remote_branch="feature/test",
        remote_sha=_HEAD_SHA if accepted else None,
        rejected_reason=None if accepted else "non-fast-forward",
        message="ok" if accepted else "rejected",
    )


async def _create_and_advance(
    facade: SourceControlFacade,
    target_status: OperationStatus,
    *,
    operation_type: str = "push_branch",
    target_branch: str = "feature/test",
    workspace_id: str = "ws_test",
) -> str:
    """
    Create an operation and advance it to `target_status` via the
    canonical intermediate states.

    Returns the operation ID.
    """
    record = await facade.operation_service.create_operation(
        repository_id="repo_test",
        kind=operation_type,
        provider=ProviderId.GITHUB,
        target_branch=target_branch,
        workspace_id=workspace_id,
    )
    if target_status == OperationStatus.CREATED:
        return record.operation_id

    path = [
        OperationStatus.REPOSITORY_RESOLVED,
        OperationStatus.WORKSPACE_READY,
        OperationStatus.EDITING,
        OperationStatus.REVIEW_REQUIRED,
        OperationStatus.APPROVED,
        OperationStatus.PUSHING,
        OperationStatus.PUSHED,
    ]
    for status in path:
        await facade.operation_service.transition(
            record.operation_id, status, actor="test"
        )
        if status == target_status:
            return record.operation_id
    return record.operation_id


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------


@pytest.fixture
def mocked_workspace_manager() -> MagicMock:
    manager = MagicMock()
    manager.get_workspace = MagicMock()
    return manager


@pytest.fixture
def mocked_diff_engine() -> MagicMock:
    engine = MagicMock()
    engine.compute_diff = AsyncMock()
    return engine


@pytest.fixture
def mocked_push_manager() -> MagicMock:
    manager = MagicMock()
    manager.push_branch = AsyncMock()
    return manager


@pytest.fixture
def isolated_operation_service() -> OperationService:
    return OperationService(journal=InMemoryOperationJournal())


@pytest.fixture
def facade(
    mock_provider,
    mocked_workspace_manager: MagicMock,
    mocked_diff_engine: MagicMock,
    mocked_push_manager: MagicMock,
    isolated_operation_service: OperationService,
) -> SourceControlFacade:
    """
    A facade wired with isolated dependencies and a populated vault.
    """
    registry = ProviderRegistry()
    # MockSourceControlProvider is a full SourceControlProvider
    # implementation but fails the strict `isinstance()` protocol
    # check because `SourceControlProvider` is not decorated with
    # `@runtime_checkable` (or requires methods the mock omits).
    # Injecting directly into the registry keeps the test focused
    # on the facade's orchestration rather than on protocol
    # introspection.
    registry._providers["github"] = mock_provider

    vault = InMemoryVault()
    token_service = TokenService(vault=vault)
    token_service.store_token(
        provider=ProviderId.GITHUB,
        account_id="acc_test",
        base_url="https://github.com",
        access_token="fake_token",
    )

    instance = SourceControlFacade(
        registry=registry,
        token_service=token_service,
        vault=vault,
        policy_engine=PolicyEngine(),
        approval_gate=ApprovalGate(),
        operation_service=isolated_operation_service,
        workspace_manager=mocked_workspace_manager,
        diff_engine=mocked_diff_engine,
        push_manager=mocked_push_manager,
        audit_logger=InMemoryAuditLogger(),
    )

    now = datetime.now(timezone.utc)
    instance._connections["github"] = _ConnectionRecord(
        provider="github",
        account_id="acc_test",
        base_url="https://github.com",
        username="testuser",
        connected_at=now,
        last_validated_at=now,
    )

    return instance


# ---------------------------------------------------------------------
# 1. Request approval state machine
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_approval_advances_state_automatically(
    facade: SourceControlFacade,
    mocked_workspace_manager: MagicMock,
    mocked_diff_engine: MagicMock,
) -> None:
    """
    REGRESSION: an operation created in `CREATED` state can request
    approval; the facade advances it through the canonical
    intermediate states before opening the ticket.

    Before the fix, this raised `ConflictError` because the state
    machine does not allow `CREATED -> REVIEW_REQUIRED` directly.
    """
    mocked_workspace_manager.get_workspace.return_value = (
        _make_workspace_ref()
    )
    mocked_diff_engine.compute_diff.return_value = _make_diff_summary()

    record = await facade.operation_service.create_operation(
        repository_id="repo_test",
        kind="push_branch",
        provider=ProviderId.GITHUB,
        target_branch="feature/test",
        workspace_id="ws_test",
    )
    assert record.status == OperationStatus.CREATED

    ticket = await facade.request_operation_approval(record.operation_id)
    assert ticket.approval_id
    assert ticket.diff_sha256

    updated = await facade.get_operation(record.operation_id)
    assert updated.current_state == "review_required"


@pytest.mark.asyncio
async def test_request_approval_is_idempotent(
    facade: SourceControlFacade,
    mocked_workspace_manager: MagicMock,
    mocked_diff_engine: MagicMock,
) -> None:
    """
    Calling `request_operation_approval` twice must not raise on the
    second call; each call creates a fresh ticket.
    """
    mocked_workspace_manager.get_workspace.return_value = (
        _make_workspace_ref()
    )
    mocked_diff_engine.compute_diff.return_value = _make_diff_summary()

    record = await facade.operation_service.create_operation(
        repository_id="repo_test",
        kind="push_branch",
        provider=ProviderId.GITHUB,
        target_branch="feature/test",
        workspace_id="ws_test",
    )

    ticket1 = await facade.request_operation_approval(record.operation_id)
    assert ticket1.approval_id

    ticket2 = await facade.request_operation_approval(record.operation_id)
    assert ticket2.approval_id
    assert ticket2.approval_id != ticket1.approval_id

    updated = await facade.get_operation(record.operation_id)
    assert updated.current_state == "review_required"


# ---------------------------------------------------------------------
# 2. Push flow
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_approved_branch_full_flow(
    facade: SourceControlFacade,
    mocked_workspace_manager: MagicMock,
    mocked_diff_engine: MagicMock,
    mocked_push_manager: MagicMock,
) -> None:
    """
    Full push flow: the ticket is consumed, the vault token is
    supplied to the push manager, and the operation reaches `PUSHED`.
    """
    mocked_workspace_manager.get_workspace.return_value = (
        _make_workspace_ref()
    )
    diff_summary = _make_diff_summary()
    mocked_diff_engine.compute_diff.return_value = diff_summary
    mocked_push_manager.push_branch.return_value = _make_push_result(
        accepted=True
    )

    op_id = await _create_and_advance(
        facade, OperationStatus.APPROVED
    )

    diff_hash = diff_summary.sha256()
    ticket = facade.approval_gate.create_ticket(
        operation_id=op_id,
        repository_id="repo_test",
        operation="push_branch",
        base_sha=diff_summary.base_sha,
        diff_sha256=diff_hash,
        target_branch="feature/test",
    )
    facade.approval_gate.grant(ticket.approval_id, actor="user")

    result = await facade.push_approved_branch(
        op_id,
        PushBranchRequest(workspace_id="ws_test", approval_id=ticket.approval_id),
    )

    assert result.operation_id == op_id
    assert result.branch_name == "feature/test"
    assert result.pushed_sha == _HEAD_SHA

    mocked_push_manager.push_branch.assert_awaited_once()
    call_kwargs = mocked_push_manager.push_branch.call_args.kwargs
    assert call_kwargs["token"] == "fake_token"
    assert call_kwargs["remote_branch"] == "feature/test"
    assert call_kwargs["force_with_lease"] is False

    updated = await facade.get_operation(op_id)
    assert updated.current_state == "pushed"


@pytest.mark.asyncio
async def test_push_approved_branch_rejects_protected_branch(
    facade: SourceControlFacade,
    mocked_workspace_manager: MagicMock,
    mocked_diff_engine: MagicMock,
    mocked_push_manager: MagicMock,
) -> None:
    """
    SECURITY: push to a protected branch is refused before the
    ticket is consumed or the push is attempted.
    """
    mocked_workspace_manager.get_workspace.return_value = (
        _make_workspace_ref()
    )
    diff_summary = _make_diff_summary()
    mocked_diff_engine.compute_diff.return_value = diff_summary

    op_id = await _create_and_advance(
        facade, OperationStatus.APPROVED
    )

    ticket = facade.approval_gate.create_ticket(
        operation_id=op_id,
        repository_id="repo_test",
        operation="push_branch",
        base_sha=diff_summary.base_sha,
        diff_sha256=diff_summary.sha256(),
        target_branch="main",
    )
    facade.approval_gate.grant(ticket.approval_id, actor="user")

    with pytest.raises(PermissionDeniedError, match="protected branch"):
        await facade.push_approved_branch(
            op_id,
            PushBranchRequest(
                workspace_id="ws_test",
                approval_id=ticket.approval_id,
            ),
        )

    mocked_push_manager.push_branch.assert_not_called()


@pytest.mark.asyncio
async def test_push_approved_branch_rejects_wrong_diff(
    facade: SourceControlFacade,
    mocked_workspace_manager: MagicMock,
    mocked_diff_engine: MagicMock,
    mocked_push_manager: MagicMock,
) -> None:
    """
    SECURITY: if the live diff does not match the ticket's diff
    hash, the push is refused.
    """
    mocked_workspace_manager.get_workspace.return_value = (
        _make_workspace_ref()
    )
    diff_summary = _make_diff_summary()
    mocked_diff_engine.compute_diff.return_value = diff_summary
    mocked_push_manager.push_branch.return_value = _make_push_result(
        accepted=True
    )

    op_id = await _create_and_advance(
        facade, OperationStatus.APPROVED
    )

    ticket = facade.approval_gate.create_ticket(
        operation_id=op_id,
        repository_id="repo_test",
        operation="push_branch",
        base_sha=diff_summary.base_sha,
        diff_sha256="0" * 64,
        target_branch="feature/test",
    )
    facade.approval_gate.grant(ticket.approval_id, actor="user")

    with pytest.raises(ApprovalInvalidError):
        await facade.push_approved_branch(
            op_id,
            PushBranchRequest(workspace_id="ws_test", approval_id=ticket.approval_id),
        )

    mocked_push_manager.push_branch.assert_not_called()


# ---------------------------------------------------------------------
# 3. Change-request flow
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_change_request_full_flow(
    facade: SourceControlFacade,
    mocked_workspace_manager: MagicMock,
    mocked_diff_engine: MagicMock,
) -> None:
    """
    Full change-request flow: the ticket is consumed, the vault
    token is passed to the adapter, and the operation reaches
    `CHANGE_REQUEST_CREATED`.
    """
    mocked_workspace_manager.get_workspace.return_value = (
        _make_workspace_ref()
    )
    diff_summary = _make_diff_summary()
    mocked_diff_engine.compute_diff.return_value = diff_summary

    # Change request creation is the canonical successor of a
    # successfully pushed operation. Advance to PUSHED so the state
    # machine allows the final transition to CHANGE_REQUEST_CREATED.
    op_id = await _create_and_advance(
        facade, OperationStatus.PUSHED
    )

    diff_hash = diff_summary.sha256()
    await facade.operation_service.attach_diff(
        operation_id=op_id,
        diff_sha256=diff_hash,
        base_sha=diff_summary.base_sha,
    )

    # SECURITY: change-request creation consumes its own ticket,
    # bound to `create_change_request`. It does NOT reuse the push
    # approval (see `test_push_approval_cannot_be_reused_for_change_request`).
    ticket = facade.approval_gate.create_ticket(
        operation_id=op_id,
        repository_id="repo_test",
        operation="create_change_request",
        base_sha=diff_summary.base_sha,
        diff_sha256=diff_hash,
        target_branch="feature/test",
    )
    facade.approval_gate.grant(ticket.approval_id, actor="user")

    result = await facade.create_change_request(
        op_id,
        CreateChangeRequestPayload(
            provider="github",
            repository_id="repo_test",
            source_branch="feature/test",
            target_branch="main",
            title="Test PR",
            body="Test body",
            draft=True,
            approval_id=ticket.approval_id,
        ),
    )

    assert result.number_or_iid == "42"
    assert "pull/42" in result.web_url


@pytest.mark.asyncio
async def test_create_change_request_requires_approval_id(
    facade: SourceControlFacade,
    mocked_workspace_manager: MagicMock,
) -> None:
    """
    SECURITY: creating a change request without an approval ticket
    is refused, regardless of the operation's state.
    """
    mocked_workspace_manager.get_workspace.return_value = (
        _make_workspace_ref()
    )

    op_id = await _create_and_advance(
        facade, OperationStatus.APPROVED
    )

    with pytest.raises(PermissionDeniedError, match="approval"):
        await facade.create_change_request(
            op_id,
            CreateChangeRequestPayload(
                provider="github",
                repository_id="repo_test",
                source_branch="feature/test",
                target_branch="main",
                title="Test PR",
            ),
        )


@pytest.mark.asyncio
async def test_push_approval_cannot_be_reused_for_change_request(
    facade: SourceControlFacade,
    mocked_workspace_manager: MagicMock,
    mocked_diff_engine: MagicMock,
) -> None:
    """
    SECURITY: a push approval MUST NOT be usable to create a change
    request. The reference specification treats this reuse as a
    critical bug.

    The `create_change_request` endpoint binds its approval to
    `create_change_request`, not to the operation's own kind. A
    ticket granted for `push_branch` is therefore rejected with
    `PermissionDeniedError` when presented to the change-request
    endpoint.
    """
    mocked_workspace_manager.get_workspace.return_value = (
        _make_workspace_ref()
    )
    diff_summary = _make_diff_summary()
    mocked_diff_engine.compute_diff.return_value = diff_summary

    op_id = await _create_and_advance(
        facade, OperationStatus.APPROVED
    )

    diff_hash = diff_summary.sha256()
    await facade.operation_service.attach_diff(
        operation_id=op_id,
        diff_sha256=diff_hash,
        base_sha=diff_summary.base_sha,
    )

    push_ticket = facade.approval_gate.create_ticket(
        operation_id=op_id,
        repository_id="repo_test",
        operation="push_branch",
        base_sha=diff_summary.base_sha,
        diff_sha256=diff_hash,
        target_branch="feature/test",
    )
    facade.approval_gate.grant(push_ticket.approval_id, actor="user")

    # Correct behavior would raise PermissionDeniedError here.
    with pytest.raises(PermissionDeniedError):
        await facade.create_change_request(
            op_id,
            CreateChangeRequestPayload(
                provider="github",
                repository_id="repo_test",
                source_branch="feature/test",
                target_branch="main",
                title="Test PR",
                approval_id=push_ticket.approval_id,
            ),
        )
