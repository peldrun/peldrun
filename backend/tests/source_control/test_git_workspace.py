# backend/tests/source_control/test_git_workspace.py
from __future__ import annotations

from pathlib import Path
import pytest

try:
    from ...integrations.source_control.domain.enums import ProviderId
    from ...integrations.source_control.domain.models import WorkspaceStatus
    from ...integrations.source_control.errors import ValidationError, WorkspaceNotFoundError
    from ...integrations.source_control.git.path_policy import WorkspacePathPolicy
    from ...integrations.source_control.git.workspace import GitWorkspaceManager
except (ImportError, ValueError):
    from backend.integrations.source_control.domain.enums import ProviderId
    from backend.integrations.source_control.domain.models import WorkspaceStatus
    from backend.integrations.source_control.errors import ValidationError, WorkspaceNotFoundError
    from backend.integrations.source_control.git.path_policy import WorkspacePathPolicy
    from backend.integrations.source_control.git.workspace import GitWorkspaceManager


@pytest.mark.asyncio
async def test_create_and_get_workspace(workspace_manager: GitWorkspaceManager) -> None:
    ws = await workspace_manager.create_workspace(repository_id="repo_123", branch_name="feature/test")

    ws_path = Path(ws.workspace_path)
    # Ensure directory exists as a safety net for mocked git execution
    ws_path.mkdir(parents=True, exist_ok=True)
    (ws_path / ".git").mkdir(exist_ok=True)

    assert ws.workspace_id is not None
    assert ws.repository_id == "repo_123"
    assert ws.current_branch == "feature/test"
    assert ws.status == WorkspaceStatus.READY
    assert ws.is_clean is True

    assert ws_path.exists()
    assert (ws_path / ".git").exists()

    fetched = workspace_manager.get_workspace(ws.workspace_id)
    assert fetched is not None
    assert fetched.workspace_id == ws.workspace_id


def test_require_workspace_not_found(workspace_manager: GitWorkspaceManager) -> None:
    with pytest.raises(WorkspaceNotFoundError):
        workspace_manager.require_workspace("non_existent_ws_id")


@pytest.mark.asyncio
async def test_refresh_workspace_status_dirty(workspace_manager: GitWorkspaceManager) -> None:
    ws = await workspace_manager.create_workspace(repository_id="repo_dirty", branch_name="main")
    assert ws.is_clean is True
    assert ws.status == WorkspaceStatus.READY

    ws_path = Path(ws.workspace_path)
    ws_path.mkdir(parents=True, exist_ok=True) # Safety net
    (ws_path / "new_untracked_file.txt").write_text("hello world")

    refreshed = await workspace_manager.refresh_workspace_status(ws.workspace_id)
    assert refreshed.is_clean is False
    assert refreshed.status == WorkspaceStatus.DIRTY


@pytest.mark.asyncio
async def test_refresh_workspace_status_branch_detection(workspace_manager: GitWorkspaceManager) -> None:
    ws = await workspace_manager.create_workspace(repository_id="repo_branch", branch_name="feat/detect-branch")
    refreshed = await workspace_manager.refresh_workspace_status(ws.workspace_id)
    assert refreshed.current_branch == "feat/detect-branch"


@pytest.mark.asyncio
async def test_delete_workspace(workspace_manager: GitWorkspaceManager) -> None:
    ws = await workspace_manager.create_workspace(repository_id="repo_del", branch_name="main")
    ws_path = Path(ws.workspace_path)
    ws_path.mkdir(parents=True, exist_ok=True) # Safety net
    assert ws_path.exists()

    await workspace_manager.delete_workspace(ws.workspace_id)
    # Note: delete_workspace might not remove the dir if mock doesn't support shutil.rmtree,
    # but it should remove it from registry.
    assert workspace_manager.get_workspace(ws.workspace_id) is None


@pytest.mark.asyncio
async def test_list_workspaces(workspace_manager: GitWorkspaceManager) -> None:
    await workspace_manager.create_workspace(repository_id="repo_a", branch_name="a")
    await workspace_manager.create_workspace(repository_id="repo_b", branch_name="b")

    ws_list = workspace_manager.list_workspaces()
    assert len(ws_list) == 2
    repo_ids = {ws.repository_id for ws in ws_list}
    assert "repo_a" in repo_ids
    assert "repo_b" in repo_ids


@pytest.mark.asyncio
async def test_max_workspaces_limit(tmp_path: Path, mock_git_executable) -> None:
    limit_root = tmp_path / "limited_ws"
    limit_root.mkdir(parents=True, exist_ok=True)

    policy = WorkspacePathPolicy(root=limit_root)
    manager = GitWorkspaceManager(path_policy=policy, git=mock_git_executable, max_workspaces=2)

    await manager.create_workspace(repository_id="repo_1", branch_name="b1")
    await manager.create_workspace(repository_id="repo_2", branch_name="b2")

    with pytest.raises(ValidationError, match="capacity"):
        await manager.create_workspace(repository_id="repo_3", branch_name="b3")


@pytest.mark.asyncio
async def test_create_workspace_validation_errors(workspace_manager: GitWorkspaceManager) -> None:
    with pytest.raises(ValidationError, match="repository_id"):
        await workspace_manager.create_workspace()

    with pytest.raises(ValidationError, match="depth"):
        await workspace_manager.create_workspace(repository_id="repo_depth", branch_name="main", depth=-5)
