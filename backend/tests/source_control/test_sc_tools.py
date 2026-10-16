from __future__ import annotations

from pathlib import Path
from typing import Any
import pytest

# Resilient imports for domain, tools, and Peldrun contract
try:
    from ...integrations.source_control.domain.enums import ProviderId
    from ...integrations.source_control.errors import (
        AuthRequiredError,
        PermissionDeniedError,
        RemoteStateChangedError,
        ValidationError,
    )
    from ...integrations.source_control.git.workspace import GitWorkspaceManager
    from ...integrations.source_control.registry import ProviderRegistry
    from ...integrations.source_control.security.approval import ApprovalGate
    from ...integrations.source_control.security.policy_engine import PolicyEngine
    from ...integrations.source_control.security.token_service import TokenService
    from ...integrations.source_control.tools import (
        CreateBranchTool,
        CreateChangeRequestTool,
        InspectDiffTool,
        ListRepositoriesTool,
        PushBranchTool,
        ReadFileTool,
        RequestApprovalTool,
        RunChecksTool,
    )
    from ...peldrun.tools.builtins.source_control_tool import SourceControlTool
    from ...peldrun.tools.contract import ToolResult
except (ImportError, ValueError):
    from backend.integrations.source_control.domain.enums import ProviderId
    from backend.integrations.source_control.errors import (
        AuthRequiredError,
        PermissionDeniedError,
        RemoteStateChangedError,
        ValidationError,
    )
    from backend.integrations.source_control.git.workspace import GitWorkspaceManager
    from backend.integrations.source_control.registry import ProviderRegistry
    from backend.integrations.source_control.security.approval import ApprovalGate
    from backend.integrations.source_control.security.policy_engine import PolicyEngine
    from backend.integrations.source_control.security.token_service import TokenService
    from backend.integrations.source_control.tools import (
        CreateBranchTool,
        CreateChangeRequestTool,
        InspectDiffTool,
        ListRepositoriesTool,
        PushBranchTool,
        ReadFileTool,
        RequestApprovalTool,
        RunChecksTool,
    )
    from backend.peldrun.tools.builtins.source_control_tool import SourceControlTool
    from backend.peldrun.tools.contract import ToolResult


# ---------------------------------------------------------------------------
# 1. Specialized Source Control Tools Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_repositories_tool(
    provider_registry: ProviderRegistry,
    token_service: TokenService,
) -> None:
    """Verifies ListRepositoriesTool lists accessible repositories when authenticated."""
    tool = ListRepositoriesTool(
        registry=provider_registry,
        token_service=token_service,
    )

    # 1. Without credentials in vault, raises AuthRequiredError
    with pytest.raises(AuthRequiredError):
        await tool.aexecute()

    # 2. Store mock token and execute
    await token_service.save_tokens(
        provider_id=ProviderId.GITHUB,
        access_token="ghp_test_token_valid",
    )
    res = await tool.aexecute(query="sample")

    assert res["status"] == "success"
    assert res["count"] >= 1
    assert len(res["repositories"]) >= 1
    assert res["repositories"][0]["name"] == "sample-repo"


@pytest.mark.asyncio
async def test_read_file_tool(
    provider_registry: ProviderRegistry,
    token_service: TokenService,
) -> None:
    """Verifies ReadFileTool handles both plain text and binary assets correctly."""
    await token_service.save_tokens(
        provider_id=ProviderId.GITHUB,
        access_token="ghp_test_token_valid",
    )
    tool = ReadFileTool(
        registry=provider_registry,
        token_service=token_service,
    )

    # 1. Read normal text file
    text_res = await tool.aexecute(
        repository_id="12345678",
        file_path="src/main.py",
    )
    assert text_res["status"] == "success"
    assert text_res["is_binary"] is False
    assert "Sample file content" in text_res["content"]

    # 2. Read binary file
    bin_res = await tool.aexecute(
        repository_id="12345678",
        file_path="binary.dat",
    )
    assert bin_res["status"] == "success"
    assert bin_res["is_binary"] is True
    assert bin_res["content"] is None


@pytest.mark.asyncio
async def test_create_branch_tool_guards_protected_branches(
    provider_registry: ProviderRegistry,
    token_service: TokenService,
    policy_engine: PolicyEngine,
) -> None:
    """Verifies CreateBranchTool blocks direct targeting of protected branches."""
    await token_service.save_tokens(
        provider_id=ProviderId.GITHUB,
        access_token="ghp_test_token_valid",
    )
    tool = CreateBranchTool(
        registry=provider_registry,
        token_service=token_service,
        policy_engine=policy_engine,
    )

    # Attempting to create protected branch 'main' directly must fail
    with pytest.raises(PermissionDeniedError):
        await tool.aexecute(
            repository_id="12345678",
            branch_name="main",
            from_sha="a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
        )

    # Valid task branch succeeds
    valid_res = await tool.aexecute(
        repository_id="12345678",
        branch_name="feature/my-task",
        from_sha="a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
    )
    assert valid_res["status"] == "success"
    assert valid_res["branch_name"] == "feature/my-task"


@pytest.mark.asyncio
async def test_inspect_diff_and_request_approval_tools(
    workspace_manager: GitWorkspaceManager,
    approval_gate: ApprovalGate,
) -> None:
    """Verifies diff inspection and cryptographic approval proposal generation."""
    ws = await workspace_manager.create_workspace(
        repository_id="repo_diff_test",
        base_sha="HEAD",
        branch_name="feature/approval-flow",
    )
    ws_dir = Path(ws.workspace_path)
    (ws_dir / "new_feature.py").write_text("def run(): pass\n", encoding="utf-8")

    diff_tool = InspectDiffTool(workspace_manager=workspace_manager)
    diff_res = await diff_tool.aexecute(workspace_id=ws.workspace_id)

    assert diff_res["status"] == "success"
    assert diff_res["total_files"] == 1
    assert len(diff_res["diff_sha256"]) == 64

    # Request approval
    approval_tool = RequestApprovalTool(
        workspace_manager=workspace_manager,
        approval_gate=approval_gate,
    )
    appr_res = await approval_tool.aexecute(
        workspace_id=ws.workspace_id,
        operation="push_branch",
        target_branch="feature/approval-flow",
    )

    assert appr_res["status"] == "approval_required"
    assert appr_res["approval_id"] is not None
    assert appr_res["diff_sha256"] == diff_res["diff_sha256"]


@pytest.mark.asyncio
async def test_push_branch_tool_single_use_approval(
    workspace_manager: GitWorkspaceManager,
    approval_gate: ApprovalGate,
    token_service: TokenService,
) -> None:
    """Verifies PushBranchTool consumes single-use approval ticket and rejects replay attempts."""
    await token_service.save_tokens(
        provider_id=ProviderId.GITHUB,
        access_token="ghp_test_token_valid",
    )
    ws = await workspace_manager.create_workspace(
        repository_id="repo_push_test",
        base_sha="HEAD",
        branch_name="feature/push-test",
    )

    ticket = approval_gate.create_ticket(
        operation_id="op_push_001",
        repository_id="repo_push_test",
        operation="push_branch",
        base_sha=ws.base_sha,
        diff_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        target_branch="feature/push-test",
    )
    approval_gate.approve_ticket(ticket.approval_id, user_id="lead_dev")

    push_tool = PushBranchTool(
        approval_gate=approval_gate,
        workspace_manager=workspace_manager,
        token_service=token_service,
    )

    # First execution consumes ticket and proceeds
    res = await push_tool.aexecute(
        approval_id=ticket.approval_id,
        workspace_id=ws.workspace_id,
    )
    assert res["status"] == "success"
    assert res["branch_name"] == "feature/push-test"

    # Second execution of the exact same approval must raise PermissionDeniedError
    with pytest.raises(PermissionDeniedError):
        await push_tool.aexecute(
            approval_id=ticket.approval_id,
            workspace_id=ws.workspace_id,
        )


@pytest.mark.asyncio
async def test_create_change_request_tool_secret_redaction(
    provider_registry: ProviderRegistry,
    token_service: TokenService,
) -> None:
    """Ensures CreateChangeRequestTool redacts inline secrets from titles and descriptions."""
    await token_service.save_tokens(
        provider_id=ProviderId.GITHUB,
        access_token="ghp_test_token_valid",
    )
    tool = CreateChangeRequestTool(
        registry=provider_registry,
        token_service=token_service,
    )

    res = await tool.aexecute(
        repository_id="12345678",
        source_branch="feature/my-task",
        target_branch="main",
        title="feat: add token ghp_secretKey1234567890abcdefghijklmnopqrstuv in UI",
        body="Details mentioning gho_oauthSecretToken1234567890abcdefghij",
        draft=True,
    )

    assert res["status"] == "success"
    assert res["change_request_number"] == "42"
    assert "ghp_secretKey" not in res["message"]


# ---------------------------------------------------------------------------
# 2. Unified SourceControlTool Bridge & Contract Compliance Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_control_tool_result_contract_success(
    provider_registry: ProviderRegistry,
    token_service: TokenService,
) -> None:
    """Verifies that SourceControlTool outputs conforming ToolResult with exit_code 0."""
    await token_service.save_tokens(
        provider_id=ProviderId.GITHUB,
        access_token="ghp_test_token_valid",
    )
    sc_tool = SourceControlTool(
        registry=provider_registry,
        token_service=token_service,
    )

    result: ToolResult = await sc_tool.aexecute(action="list_repositories")

    assert isinstance(result, ToolResult)
    assert result.is_error is False
    assert result.exit_code == 0
    assert result.metadata.get("action") == "list_repositories"
    assert isinstance(result.output, dict)
    assert result.output.get("status") == "success"


@pytest.mark.asyncio
async def test_source_control_tool_action_alias_resolution(
    provider_registry: ProviderRegistry,
    token_service: TokenService,
) -> None:
    """Verifies that common model aliases (list_repos, diff, create_pr) are normalized."""
    await token_service.save_tokens(
        provider_id=ProviderId.GITHUB,
        access_token="ghp_test_token_valid",
    )
    sc_tool = SourceControlTool(
        registry=provider_registry,
        token_service=token_service,
    )

    # 'list_repos' alias -> 'list_repositories'
    result: ToolResult = await sc_tool.aexecute(action="list_repos")
    assert result.is_error is False
    assert result.metadata.get("action") == "list_repositories"


@pytest.mark.asyncio
async def test_source_control_tool_nested_params_and_flat_args(
    provider_registry: ProviderRegistry,
    token_service: TokenService,
) -> None:
    """Ensures tool parameters passed either flat or nested inside params dict resolve cleanly."""
    await token_service.save_tokens(
        provider_id=ProviderId.GITHUB,
        access_token="ghp_test_token_valid",
    )
    sc_tool = SourceControlTool(
        registry=provider_registry,
        token_service=token_service,
    )

    # 1. Flat argument
    res_flat = await sc_tool.aexecute(
        action="read_file",
        repository_id="12345678",
        file_path="src/main.py",
    )
    assert res_flat.is_error is False
    assert "Sample file content" in res_flat.output["content"]

    # 2. Nested dictionary
    res_nested = await sc_tool.aexecute(
        action="read_file",
        params={"repository_id": "12345678", "file_path": "src/main.py"},
    )
    assert res_nested.is_error is False
    assert "Sample file content" in res_nested.output["content"]


@pytest.mark.asyncio
async def test_source_control_tool_error_encapsulation_no_exception_raised(
    provider_registry: ProviderRegistry,
    token_service: TokenService,
) -> None:
    """Verifies that domain errors are encapsulated in ToolResult without crashing agent runtime."""
    # Ensure no token exists to provoke AuthRequiredError
    await token_service.revoke_tokens(ProviderId.GITHUB)

    sc_tool = SourceControlTool(
        registry=provider_registry,
        token_service=token_service,
    )

    result: ToolResult = await sc_tool.aexecute(action="list_repositories")

    assert isinstance(result, ToolResult)
    assert result.is_error is True
    assert result.exit_code == 1
    assert "Authentication Required" in str(result.output)
    assert result.metadata.get("error_type") == "AuthRequiredError"


@pytest.mark.asyncio
async def test_source_control_tool_zero_credential_exposure(
    provider_registry: ProviderRegistry,
    token_service: TokenService,
) -> None:
    """Ensures token strings do not leak into ToolResult output or metadata."""
    secret_token = "ghp_superSecretToken998877665544"
    await token_service.save_tokens(
        provider_id=ProviderId.GITHUB,
        access_token=secret_token,
    )
    sc_tool = SourceControlTool(
        registry=provider_registry,
        token_service=token_service,
    )

    result = await sc_tool.aexecute(action="list_repositories")

    serialized_result = str(result.output) + str(result.metadata)
    assert secret_token not in serialized_result


def test_source_control_tool_sync_execute_raises_not_implemented() -> None:
    """Ensures synchronous execution is strictly disallowed to prevent blocking event loop."""
    tool = SourceControlTool()
    with pytest.raises(NotImplementedError):
        tool.execute(action="list_repositories")
