# backend/tests/source_control/test_sc_tools.py
from __future__ import annotations

import pytest

# Safely skip this entire test module if the tools module does not exist yet
pytest.importorskip("backend.omweb.tools.source_control_tools")

# Resilient imports for source control tools and related components
try:
    from ...omweb.tools.source_control_tools import (
        read_file_tool,
        create_branch_tool,
        request_approval_tool,
    )
    from ...omweb.services.source_control_facade import SourceControlFacade
    from ...integrations.source_control.errors import NotFoundError, ValidationError
except (ImportError, ValueError):
    from backend.omweb.tools.source_control_tools import (
        read_file_tool,
        create_branch_tool,
        request_approval_tool,
    )
    from backend.omweb.services.source_control_facade import SourceControlFacade
    from backend.integrations.source_control.errors import NotFoundError, ValidationError


@pytest.fixture
def mock_facade() -> pytest.AsyncMock:
    """Provides a mocked SourceControlFacade for tool testing."""
    from unittest.mock import AsyncMock
    return AsyncMock(spec=SourceControlFacade)


# ---------------------------------------------------------------------------
# 1. Read File Tool Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_file_tool_success(mock_facade: pytest.AsyncMock) -> None:
    """Verifies that read_file_tool correctly calls the facade and formats the response."""
    mock_facade.read_file.return_value = {
        "repository_id": "repo_1",
        "file_path": "src/main.py",
        "ref": "main",
        "is_binary": False,
        "size_bytes": 1024,
        "is_truncated": False,
        "content": "print('hello world')",
    }

    result = await read_file_tool(
        repository_id="repo_1",
        file_path="src/main.py",
        ref="main",
        provider="github",
        facade=mock_facade,
    )

    mock_facade.read_file.assert_awaited_once_with(
        repository_id="repo_1",
        file_path="src/main.py",
        ref="main",
        provider="github",
    )
    assert "print('hello world')" in str(result)


@pytest.mark.asyncio
async def test_read_file_tool_not_found(mock_facade: pytest.AsyncMock) -> None:
    """Ensures read_file_tool gracefully handles NotFoundError from the facade."""
    mock_facade.read_file.side_effect = NotFoundError("File not found")

    result = await read_file_tool(
        repository_id="repo_1",
        file_path="missing.py",
        facade=mock_facade,
    )

    assert "Error" in str(result) or "not found" in str(result).lower()


# ---------------------------------------------------------------------------
# 2. Create Branch Tool Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_branch_tool_success(mock_facade: pytest.AsyncMock) -> None:
    """Verifies that create_branch_tool correctly invokes the facade."""
    mock_facade.create_branch.return_value = {
        "name": "feature/new-tool",
        "head_sha": "a" * 40,
        "is_default": False,
        "is_protected": False,
    }

    result = await create_branch_tool(
        repository_id="repo_1",
        branch_name="feature/new-tool",
        from_sha="b" * 40,
        provider="github",
        facade=mock_facade,
    )

    mock_facade.create_branch.assert_awaited_once()
    assert "feature/new-tool" in str(result) or "branch" in str(result).lower()


@pytest.mark.asyncio
async def test_create_branch_tool_validation_error(mock_facade: pytest.AsyncMock) -> None:
    """Ensures create_branch_tool handles invalid branch names gracefully."""
    mock_facade.create_branch.side_effect = ValidationError("Invalid branch name")

    result = await create_branch_tool(
        repository_id="repo_1",
        branch_name="invalid branch name!",
        facade=mock_facade,
    )

    assert "Error" in str(result) or "invalid" in str(result).lower()


# ---------------------------------------------------------------------------
# 3. Request Approval Tool Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_request_approval_tool_success(mock_facade: pytest.AsyncMock) -> None:
    """Verifies that request_approval_tool correctly triggers the approval workflow."""
    mock_facade.request_operation_approval.return_value = {
        "approval_id": "ticket_abc",
        "operation_id": "op_123",
        "operation_type": "push_branch",
        "repository_id": "repo_1",
        "target_branch": "main",
        "base_sha": "a" * 40,
        "diff_sha256": "c" * 64,
        "total_files": 2,
        "touches_workflow_files": False,
        "is_protected_target": True,
        "approved_by_user": False,
    }

    result = await request_approval_tool(
        operation_id="op_123",
        facade=mock_facade,
    )

    mock_facade.request_operation_approval.assert_awaited_once_with("op_123")
    assert "ticket_abc" in str(result)


@pytest.mark.asyncio
async def test_request_approval_tool_missing_workspace(mock_facade: pytest.AsyncMock) -> None:
    """Ensures request_approval_tool handles operations without bound workspaces."""
    mock_facade.request_operation_approval.side_effect = ValidationError(
        "Operation has no bound workspace"
    )

    result = await request_approval_tool(
        operation_id="op_bad",
        facade=mock_facade,
    )

    assert "Error" in str(result) or "workspace" in str(result).lower()
