# backend/tests/source_control/test_sc_router.py
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

try:
    from ...omweb.routers.source_control import router, handle_source_control_error
    from ...omweb.services.source_control_facade import SourceControlFacade, get_source_control_facade
    from ...integrations.source_control.domain.enums import OperationStatus
    from ...integrations.source_control.domain.models import OperationRecord
    from ...integrations.source_control.errors import NotFoundError, ValidationError, AuthRequiredError
    from ...integrations.source_control.schemas import (
        OperationResponse,
        ApproveOperationRequest,
        ActionSuccessResponse,
    )
except (ImportError, ValueError):
    from backend.omweb.routers.source_control import router, handle_source_control_error
    from backend.omweb.services.source_control_facade import SourceControlFacade, get_source_control_facade
    from backend.integrations.source_control.domain.enums import OperationStatus
    from backend.integrations.source_control.domain.models import OperationRecord
    from backend.integrations.source_control.errors import NotFoundError, ValidationError, AuthRequiredError
    from backend.integrations.source_control.schemas import (
        OperationResponse,
        ApproveOperationRequest,
        ActionSuccessResponse,
    )


@pytest.fixture
def mock_facade() -> AsyncMock:
    facade = AsyncMock(spec=SourceControlFacade)
    facade.create_operation = AsyncMock()
    facade.get_operation = AsyncMock()
    facade.approve_operation = AsyncMock()
    facade.reject_operation = AsyncMock()
    facade.request_operation_approval = AsyncMock()
    return facade


@pytest.fixture
def client(mock_facade: AsyncMock) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_source_control_facade] = lambda: mock_facade
    return TestClient(app)


def test_create_operation_success(client: TestClient, mock_facade: AsyncMock) -> None:
    mock_response = OperationResponse(
        operation_id="op_123",
        operation_type="push_branch",
        provider="github",
        repository_id="repo_1",
        target_branch="main",
        current_state="created",
        events=[],
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    mock_facade.create_operation.return_value = mock_response

    payload = {
        "provider": "github",
        "repository_id": "repo_1",
        "operation_type": "push_branch",
        "target_branch": "main",
    }

    response = client.post("/api/source-control/operations", json=payload)

    assert response.status_code == 200
    data = response.json()
    assert data["operation_id"] == "op_123"
    assert data["current_state"] == "created"
    mock_facade.create_operation.assert_awaited_once()


def test_create_operation_validation_error(client: TestClient, mock_facade: AsyncMock) -> None:
    mock_facade.create_operation.side_effect = ValidationError("Invalid repository ID")

    payload = {
        "provider": "github",
        "repository_id": "repo_invalid_999",
        "operation_type": "push_branch",
        "target_branch": "main",
    }

    response = client.post("/api/source-control/operations", json=payload)

    assert response.status_code == 422
    assert "Invalid repository ID" in response.json()["detail"]


def test_get_operation_success(client: TestClient, mock_facade: AsyncMock) -> None:
    mock_response = OperationResponse(
        operation_id="op_456",
        operation_type="push_branch",
        provider="github",
        repository_id="repo_1",
        target_branch="main",
        current_state="pushed",
        events=[],
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    mock_facade.get_operation.return_value = mock_response

    response = client.get("/api/source-control/operations/op_456")

    assert response.status_code == 200
    data = response.json()
    assert data["operation_id"] == "op_456"
    assert data["current_state"] == "pushed"
    mock_facade.get_operation.assert_awaited_once_with("op_456")


def test_get_operation_not_found(client: TestClient, mock_facade: AsyncMock) -> None:
    mock_facade.get_operation.side_effect = NotFoundError("Operation 'unknown_op' was not found.")

    response = client.get("/api/source-control/operations/unknown_op")

    assert response.status_code == 404
    assert "was not found" in response.json()["detail"]


def test_approve_operation_success(client: TestClient, mock_facade: AsyncMock) -> None:
    mock_response = {
        "approval_id": "ticket_123",
        "operation_id": "op_789",
        "operation_type": "push_branch",
        "repository_id": "repo_1",
        "target_branch": "main",
        "base_sha": "a" * 40,
        "diff_sha256": "b" * 64,
        "total_files": 1,
        "approved_by_user": True,
        "expires_at": datetime.now(timezone.utc).isoformat(),
    }
    mock_facade.approve_operation.return_value = mock_response

    payload = {
        "approval_id": "ticket_123",
        "user_id": "test_user",
        "notes": "Looks good to me",
    }

    response = client.post("/api/source-control/operations/op_789/approve", json=payload)

    assert response.status_code == 200
    data = response.json()
    assert data["approval_id"] == "ticket_123"
    assert data["approved_by_user"] is True

    call_args = mock_facade.approve_operation.call_args
    assert call_args[0][0] == "op_789"
    assert isinstance(call_args[0][1], ApproveOperationRequest)
    assert call_args[0][1].user_id == "test_user"


def test_reject_operation_success(client: TestClient, mock_facade: AsyncMock) -> None:
    mock_facade.reject_operation.return_value = ActionSuccessResponse(
        message="Operation 'op_999' was rejected: Needs refactoring"
    )

    payload = {
        "approval_id": "ticket_999",
        "user_id": "reviewer",
        "reason": "Needs refactoring",
    }

    response = client.post("/api/source-control/operations/op_999/reject", json=payload)

    assert response.status_code == 200
    data = response.json()
    assert "was rejected" in data["message"]
    mock_facade.reject_operation.assert_awaited_once()


def test_handle_source_control_error_auth_required() -> None:
    with pytest.raises(Exception) as exc_info:
        handle_source_control_error(AuthRequiredError("Token missing"))

    assert exc_info.value.status_code == 401
    assert "Token missing" in str(exc_info.value.detail)
