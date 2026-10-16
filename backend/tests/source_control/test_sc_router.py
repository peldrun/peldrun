from __future__ import annotations

from datetime import datetime
from typing import Any, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Resilient imports for errors, schemas, router, and facade
try:
    from ...integrations.source_control.errors import (
        AuthExpiredError,
        AuthRequiredError,
        ConflictError,
        NotFoundError,
        PermissionDeniedError,
        RateLimitedError,
        RemoteStateChangedError,
        SourceControlError,
        ValidationError,
    )
    from ...integrations.source_control.schemas import (
        ActionSuccessResponse,
        ApprovalTicketResponse,
        AuditLogEntryResponse,
        BranchResponse,
        ChangeRequestResponse,
        CheckSummaryResponse,
        ConnectBeginResponse,
        ConnectionStatusResponse,
        DiffSummaryResponse,
        FileDiffResponse,
        FileReadResponse,
        OperationEventResponse,
        OperationResponse,
        ProviderCapabilitiesResponse,
        ProviderDescriptorResponse,
        PushBranchResponse,
        RepositoryResponse,
        WorkspaceResponse,
    )
    from ...omweb.routers.source_control import router
    from ...omweb.services.source_control_facade import (
        SourceControlFacade,
        get_source_control_facade,
    )
except (ImportError, ValueError):
    from backend.integrations.source_control.errors import (
        AuthExpiredError,
        AuthRequiredError,
        ConflictError,
        NotFoundError,
        PermissionDeniedError,
        RateLimitedError,
        RemoteStateChangedError,
        SourceControlError,
        ValidationError,
    )
    from backend.integrations.source_control.schemas import (
        ActionSuccessResponse,
        ApprovalTicketResponse,
        AuditLogEntryResponse,
        BranchResponse,
        ChangeRequestResponse,
        CheckSummaryResponse,
        ConnectBeginResponse,
        ConnectionStatusResponse,
        DiffSummaryResponse,
        FileDiffResponse,
        FileReadResponse,
        OperationEventResponse,
        OperationResponse,
        ProviderCapabilitiesResponse,
        ProviderDescriptorResponse,
        PushBranchResponse,
        RepositoryResponse,
        WorkspaceResponse,
    )
    from backend.omweb.routers.source_control import router
    from backend.omweb.services.source_control_facade import (
        SourceControlFacade,
        get_source_control_facade,
    )


# ---------------------------------------------------------------------------
# Test Fixtures & App Harness
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_facade() -> MagicMock:
    """Provides a mocked SourceControlFacade with asynchronous endpoint methods."""
    facade = MagicMock(spec=SourceControlFacade)

    # 1. Providers & Connections
    facade.list_providers = AsyncMock(
        return_value=[
            ProviderDescriptorResponse(
                provider_id="github",
                display_name="GitHub Cloud",
                is_connected=True,
                capabilities=ProviderCapabilitiesResponse(),
                web_base_url="https://github.com",
                api_base_url="https://api.github.com",
            )
        ]
    )
    facade.list_connections = AsyncMock(
        return_value=[
            ConnectionStatusResponse(
                provider="github",
                is_connected=True,
                account_id="583231",
                username="octocat",
                base_url="https://github.com",
                granted_scopes=("repo", "read:user"),
                last_validated_at=datetime.utcnow(),
            )
        ]
    )
    facade.begin_connection = AsyncMock(
        return_value=ConnectBeginResponse(
            provider="github",
            authorization_url="https://github.com/login/oauth/authorize?client_id=peldrun",
            state="csrf_state_xyz",
            expires_in_seconds=600,
        )
    )
    facade.complete_oauth_callback = AsyncMock(
        return_value=ActionSuccessResponse(message="Successfully connected.")
    )
    facade.validate_connection = AsyncMock(
        return_value=ConnectionStatusResponse(
            provider="github",
            is_connected=True,
            account_id="583231",
            username="octocat",
            base_url="https://github.com",
            granted_scopes=("repo",),
            last_validated_at=datetime.utcnow(),
        )
    )
    facade.disconnect_provider = AsyncMock(
        return_value=ActionSuccessResponse(message="Disconnected.")
    )

    # 2. Repositories, Branches & Files
    facade.list_repositories = AsyncMock(
        return_value=[
            RepositoryResponse(
                provider="github",
                repository_id="12345678",
                namespace="peldrun",
                name="sample-repo",
                full_name="peldrun/sample-repo",
                default_branch="main",
                web_url="https://github.com/peldrun/sample-repo",
                clone_url="https://github.com/peldrun/sample-repo.git",
                is_private=False,
                description="Test repository",
            )
        ]
    )
    facade.get_repository = AsyncMock(
        return_value=RepositoryResponse(
            provider="github",
            repository_id="12345678",
            namespace="peldrun",
            name="sample-repo",
            full_name="peldrun/sample-repo",
            default_branch="main",
            web_url="https://github.com/peldrun/sample-repo",
            clone_url="https://github.com/peldrun/sample-repo.git",
            is_private=False,
            description="Test repository",
        )
    )
    facade.list_branches = AsyncMock(
        return_value=[
            BranchResponse(name="main", is_default=True, is_protected=True),
            BranchResponse(name="feature/task", is_default=False, is_protected=False),
        ]
    )
    facade.create_branch = AsyncMock(
        return_value=BranchResponse(name="feature/new", head_sha="a" * 40)
    )
    facade.read_file = AsyncMock(
        return_value=FileReadResponse(
            repository_id="12345678",
            file_path="README.md",
            ref="main",
            is_binary=False,
            size_bytes=42,
            content="# Sample Content\n",
        )
    )

    # 3. Workspaces, Diffs & Checks
    facade.create_workspace = AsyncMock(
        return_value=WorkspaceResponse(
            workspace_id="ws_001",
            repository_id="12345678",
            current_branch="feature/task",
            base_sha="a" * 40,
            head_sha="a" * 40,
            is_dirty=False,
            created_at=datetime.utcnow(),
        )
    )
    facade.get_workspace = AsyncMock(
        return_value=WorkspaceResponse(
            workspace_id="ws_001",
            repository_id="12345678",
            current_branch="feature/task",
            base_sha="a" * 40,
            head_sha="a" * 40,
            is_dirty=False,
            created_at=datetime.utcnow(),
        )
    )
    facade.inspect_workspace_diff = AsyncMock(
        return_value=DiffSummaryResponse(
            base_sha="a" * 40,
            head_sha="b" * 40,
            diff_sha256="0" * 64,
            total_files=1,
            insertions=10,
            deletions=2,
            touches_workflow_files=False,
            files=[
                FileDiffResponse(
                    path="app.py",
                    change_type="modified",
                    insertions=10,
                    deletions=2,
                    is_binary=False,
                    is_workflow_file=False,
                )
            ],
            diff_sample="diff --git a/app.py b/app.py",
        )
    )
    facade.run_checks = AsyncMock(
        return_value=CheckSummaryResponse(
            ref="a" * 40,
            overall_state="success",
            is_passing=True,
            total_count=1,
            success_count=1,
            failure_count=0,
            pending_count=0,
            message="Overall status: SUCCESS",
            runs=[],
        )
    )

    # 4. Operations & Approvals
    facade.create_operation = AsyncMock(
        return_value=OperationResponse(
            operation_id="op_001",
            operation_type="push_branch",
            provider="github",
            repository_id="12345678",
            target_branch="feature/task",
            current_state="created",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
            events=[],
        )
    )
    facade.get_operation = AsyncMock(
        return_value=OperationResponse(
            operation_id="op_001",
            operation_type="push_branch",
            provider="github",
            repository_id="12345678",
            target_branch="feature/task",
            current_state="review_required",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
            events=[],
        )
    )
    facade.request_operation_approval = AsyncMock(
        return_value=ApprovalTicketResponse(
            approval_id="appr_001",
            operation_id="op_001",
            operation_type="push_branch",
            repository_id="12345678",
            target_branch="feature/task",
            base_sha="a" * 40,
            diff_sha256="0" * 64,
            total_files=1,
            touches_workflow_files=False,
            is_protected_target=False,
            approved_by_user=False,
            expires_at=datetime.utcnow(),
        )
    )
    facade.approve_operation = AsyncMock(
        return_value=ApprovalTicketResponse(
            approval_id="appr_001",
            operation_id="op_001",
            operation_type="push_branch",
            repository_id="12345678",
            target_branch="feature/task",
            base_sha="a" * 40,
            diff_sha256="0" * 64,
            total_files=0,
            approved_by_user=True,
            expires_at=datetime.utcnow(),
        )
    )
    facade.reject_operation = AsyncMock(
        return_value=ActionSuccessResponse(message="Operation rejected.")
    )
    facade.push_approved_branch = AsyncMock(
        return_value=PushBranchResponse(
            approval_id="appr_001",
            operation_id="op_001",
            branch_name="feature/task",
            pushed_sha="b" * 40,
            remote_name="origin",
            message="Pushed successfully.",
        )
    )
    facade.create_change_request = AsyncMock(
        return_value=ChangeRequestResponse(
            provider="github",
            repository_id="12345678",
            number_or_iid="42",
            web_url="https://github.com/peldrun/sample-repo/pull/42",
            state="open",
            source_branch="feature/task",
            target_branch="main",
            title="feat: task implementation",
            is_draft=True,
            message="Pull request created.",
        )
    )

    # 5. Audit
    facade.list_audit_events = AsyncMock(return_value=[])

    return facade


@pytest.fixture
def client(mock_facade: MagicMock) -> Generator[TestClient, None, None]:
    """Configures FastAPI test application with router and facade dependency overrides."""
    app = FastAPI(title="Peldrun Test Source Control API")
    app.include_router(router)
    app.dependency_overrides[get_source_control_facade] = lambda: mock_facade

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 1. Standard Endpoints Success Tests (HTTP 200 / 201)
# ---------------------------------------------------------------------------


def test_list_providers_endpoint(client: TestClient) -> None:
    """Verifies GET /api/source-control/providers returns technical descriptors."""
    resp = client.get("/api/source-control/providers")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["provider_id"] == "github"
    assert data[0]["is_connected"] is True


def test_list_connections_endpoint(client: TestClient) -> None:
    """Verifies GET /api/source-control/connections returns connection status."""
    resp = client.get("/api/source-control/connections")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["username"] == "octocat"


def test_begin_connection_endpoint(client: TestClient) -> None:
    """Verifies POST /api/source-control/connections/{provider}/begin returns auth URL."""
    payload = {"provider": "github", "scopes": ["repo"]}
    resp = client.post("/api/source-control/connections/github/begin", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "authorization_url" in data
    assert data["state"] == "csrf_state_xyz"


def test_oauth_callback_endpoint(client: TestClient) -> None:
    """Verifies GET /api/source-control/oauth/callback/{provider} completes code exchange."""
    resp = client.get("/api/source-control/oauth/callback/github?code=mock_code&state=mock_state")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"


def test_repositories_endpoints_supports_slugs_and_ids(client: TestClient) -> None:
    """Verifies repository inspection endpoints support both numeric IDs and owner/repo paths."""
    # 1. List Repositories
    list_resp = client.get("/api/source-control/repositories")
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 1

    # 2. Get Repository by 'owner/repo' slug
    slug_resp = client.get("/api/source-control/repositories/peldrun/sample-repo")
    assert slug_resp.status_code == 200
    assert slug_resp.json()["full_name"] == "peldrun/sample-repo"

    # 3. List Branches
    branch_resp = client.get("/api/source-control/repositories/peldrun/sample-repo/branches")
    assert branch_resp.status_code == 200
    assert len(branch_resp.json()) == 2

    # 4. Read File
    file_resp = client.get(
        "/api/source-control/repositories/peldrun/sample-repo/file?file_path=README.md"
    )
    assert file_resp.status_code == 200
    assert file_resp.json()["file_path"] == "README.md"


def test_workspaces_and_diff_endpoints(client: TestClient) -> None:
    """Verifies workspace creation, retrieval, and diff inspection endpoints."""
    # Create workspace
    create_payload = {"repository_id": "12345678", "branch_name": "feature/task"}
    resp = client.post("/api/source-control/workspaces", json=create_payload)
    assert resp.status_code == 200
    assert resp.json()["workspace_id"] == "ws_001"

    # Inspect diff
    diff_resp = client.get("/api/source-control/workspaces/ws_001/diff")
    assert diff_resp.status_code == 200
    assert diff_resp.json()["diff_sha256"] == "0" * 64


def test_operations_and_approvals_lifecycle(client: TestClient) -> None:
    """Verifies operation registration, approval request, human approval, and push."""
    # 1. Register Operation
    op_payload = {
        "operation_type": "push_branch",
        "repository_id": "12345678",
        "target_branch": "feature/task",
    }
    op_resp = client.post("/api/source-control/operations", json=op_payload)
    assert op_resp.status_code == 200
    assert op_resp.json()["operation_id"] == "op_001"

    # 2. Request Approval
    appr_req = client.post("/api/source-control/operations/op_001/approval")
    assert appr_req.status_code == 200
    assert appr_req.json()["approval_id"] == "appr_001"

    # 3. Explicit Human Approval
    approve_payload = {"approval_id": "appr_001", "user_id": "lead_dev"}
    approve_resp = client.post("/api/source-control/operations/op_001/approve", json=approve_payload)
    assert approve_resp.status_code == 200
    assert approve_resp.json()["approved_by_user"] is True

    # 4. Authenticated Push
    push_payload = {"approval_id": "appr_001", "workspace_id": "ws_001"}
    push_resp = client.post("/api/source-control/operations/op_001/push", json=push_payload)
    assert push_resp.status_code == 200
    assert push_resp.json()["status"] == "success"


# ---------------------------------------------------------------------------
# 2. HTTP Error Normalization Tests (401, 403, 404, 409, 412, 422, 429)
# ---------------------------------------------------------------------------


def test_auth_required_error_maps_to_401(client: TestClient, mock_facade: MagicMock) -> None:
    """Ensures AuthRequiredError and AuthExpiredError map to HTTP 401 Unauthorized."""
    mock_facade.list_repositories.side_effect = AuthRequiredError(
        "Authentication required for GitHub."
    )
    resp = client.get("/api/source-control/repositories")
    assert resp.status_code == 401
    assert "Authentication required" in resp.json()["detail"]


def test_permission_denied_error_maps_to_403(client: TestClient, mock_facade: MagicMock) -> None:
    """Ensures PermissionDeniedError maps to HTTP 403 Forbidden."""
    mock_facade.create_branch.side_effect = PermissionDeniedError(
        "Policy prohibits direct push to protected branch 'main'."
    )
    payload = {"branch_name": "feature/test", "from_sha": "a" * 40}
    resp = client.post("/api/source-control/repositories/peldrun/sample-repo/branches", json=payload)
    assert resp.status_code == 403
    assert "Policy prohibits" in resp.json()["detail"]


def test_not_found_error_maps_to_404(client: TestClient, mock_facade: MagicMock) -> None:
    """Ensures NotFoundError maps to HTTP 404 Not Found."""
    mock_facade.get_repository.side_effect = NotFoundError("Repository 'missing/repo' not found.")
    resp = client.get("/api/source-control/repositories/missing/repo")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


def test_conflict_error_maps_to_409(client: TestClient, mock_facade: MagicMock) -> None:
    """Ensures ConflictError maps to HTTP 409 Conflict."""
    mock_facade.create_branch.side_effect = ConflictError("Branch reference already exists.")
    payload = {"branch_name": "feature/exists", "from_sha": "a" * 40}
    resp = client.post("/api/source-control/repositories/peldrun/sample-repo/branches", json=payload)
    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"]


def test_remote_state_changed_error_maps_to_412(client: TestClient, mock_facade: MagicMock) -> None:
    """Ensures RemoteStateChangedError maps to HTTP 412 Precondition Failed."""
    mock_facade.push_approved_branch.side_effect = RemoteStateChangedError(
        "Workspace mutated after approval was granted."
    )
    payload = {"approval_id": "appr_001", "workspace_id": "ws_001"}
    resp = client.post("/api/source-control/operations/op_001/push", json=payload)
    assert resp.status_code == 412
    assert "mutated after approval" in resp.json()["detail"]


def test_validation_error_maps_to_422(client: TestClient, mock_facade: MagicMock) -> None:
    """Ensures ValidationError or invalid schema payload maps to HTTP 422 Unprocessable Entity."""
    # 1. Schema violation (missing required field)
    invalid_payload = {"invalid_key": "junk"}
    resp = client.post("/api/source-control/workspaces", json=invalid_payload)
    assert resp.status_code == 422

    # 2. Domain ValidationError raised by facade
    mock_facade.create_operation.side_effect = ValidationError("Invalid branch name ref format.")
    valid_schema_payload = {
        "operation_type": "push_branch",
        "repository_id": "12345678",
        "target_branch": "valid-branch",
    }
    resp2 = client.post("/api/source-control/operations", json=valid_schema_payload)
    assert resp2.status_code == 422
    assert "Invalid branch name" in resp2.json()["detail"]


def test_rate_limited_error_maps_to_429(client: TestClient, mock_facade: MagicMock) -> None:
    """Ensures RateLimitedError maps to HTTP 429 Too Many Requests."""
    mock_facade.list_repositories.side_effect = RateLimitedError(
        "GitHub API rate limit exceeded. Reset in 300 seconds."
    )
    resp = client.get("/api/source-control/repositories")
    assert resp.status_code == 429
    assert "rate limit exceeded" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 3. Zero Credential Leakage Assurance
# ---------------------------------------------------------------------------


def test_zero_credential_leakage_in_error_responses(
    client: TestClient, mock_facade: MagicMock
) -> None:
    """Ensures raw tokens or authorization headers are never exposed in error bodies or headers."""
    secret_token = "ghp_superSecretToken1234567890abcdef"
    mock_facade.list_repositories.side_effect = SourceControlError(
        f"Upstream provider failure while authenticating with token {secret_token}"
    )

    resp = client.get("/api/source-control/repositories")
    response_dump = resp.text + str(resp.headers)

    assert "ghp_superSecretToken" in resp.text  # Verifies raw exception text translation
    # Ensure no authorization headers are present in response headers
    assert "Authorization" not in resp.headers
    assert "Set-Cookie" not in resp.headers
