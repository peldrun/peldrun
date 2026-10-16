from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any
import pytest

# Resilient imports for Git engine and path policies
try:
    from ...integrations.source_control.domain.enums import FileChangeType
    from ...integrations.source_control.domain.models import DiffSummary, FileDiff
    from ...integrations.source_control.errors import (
        NotFoundError,
        PermissionDeniedError,
        SourceControlError,
        ValidationError,
    )
    from ...integrations.source_control.git.diff import GitDiffEngine
    from ...integrations.source_control.git.executable import GitExecutable
    from ...integrations.source_control.git.path_policy import (
        secure_resolve_path,
        validate_branch_name,
    )
    from ...integrations.source_control.git.push import GitPushManager, PushResult
    from ...integrations.source_control.git.workspace import GitWorkspaceManager
except (ImportError, ValueError):
    from backend.integrations.source_control.domain.enums import FileChangeType
    from backend.integrations.source_control.domain.models import DiffSummary, FileDiff
    from backend.integrations.source_control.errors import (
        NotFoundError,
        PermissionDeniedError,
        SourceControlError,
        ValidationError,
    )
    from backend.integrations.source_control.git.diff import GitDiffEngine
    from backend.integrations.source_control.git.executable import GitExecutable
    from backend.integrations.source_control.git.path_policy import (
        secure_resolve_path,
        validate_branch_name,
    )
    from backend.integrations.source_control.git.push import GitPushManager, PushResult
    from backend.integrations.source_control.git.workspace import GitWorkspaceManager


# ---------------------------------------------------------------------------
# 1. GitWorkspaceManager Lifecycle Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspace_manager_create_and_get(
    workspace_manager: GitWorkspaceManager,
) -> None:
    """Verifies creating an isolated workspace records valid metadata and tracks directories."""
    ws = await workspace_manager.create_workspace(
        repository_id="repo_101",
        base_sha="a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
        branch_name="feature/task-alpha",
    )

    assert ws.workspace_id is not None
    assert ws.repository_id == "repo_101"
    assert ws.current_branch == "feature/task-alpha"
    assert ws.base_sha == "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
    assert Path(ws.workspace_path).is_dir()

    # Verify retrieval
    retrieved = workspace_manager.get_workspace(ws.workspace_id)
    assert retrieved is not None
    assert retrieved.workspace_id == ws.workspace_id

    # Verify unknown workspace lookup returns None
    assert workspace_manager.get_workspace("non_existent_id") is None


@pytest.mark.asyncio
async def test_workspace_dirty_state_detection(
    workspace_manager: GitWorkspaceManager,
) -> None:
    """Ensures uncommitted changes or untracked files mark the workspace as dirty."""
    ws = await workspace_manager.create_workspace(
        repository_id="repo_dirty_check",
        base_sha="HEAD",
        branch_name="feature/dirty-test",
    )

    ws_dir = Path(ws.workspace_path)
    assert ws.is_dirty is False

    # Create an untracked file
    test_file = ws_dir / "new_scratch.py"
    test_file.write_text("print('dirty')", encoding="utf-8")

    # Re-evaluate workspace status
    updated = await workspace_manager.refresh_workspace_status(ws.workspace_id)
    assert updated.is_dirty is True


@pytest.mark.asyncio
async def test_workspace_cleanup_removes_directory(
    workspace_manager: GitWorkspaceManager,
) -> None:
    """Verifies that cleanup purges workspace directory and internal registry entry."""
    ws = await workspace_manager.create_workspace(
        repository_id="repo_to_clean",
        base_sha="HEAD",
        branch_name="feature/cleanup",
    )
    ws_dir = Path(ws.workspace_path)
    assert ws_dir.exists()

    await workspace_manager.delete_workspace(ws.workspace_id)
    assert not ws_dir.exists()
    assert workspace_manager.get_workspace(ws.workspace_id) is None


# ---------------------------------------------------------------------------
# 2. GitDiffEngine Unified Diff & Cryptographic Fingerprint Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_diff_engine_computes_sha256_and_metrics(
    temp_git_repo: Path,
) -> None:
    """Ensures GitDiffEngine produces valid additions/deletions and SHA-256 diff fingerprint."""
    diff_engine = GitDiffEngine()

    # Initial state should have empty diff against HEAD
    initial_summary = await diff_engine.compute_diff(cwd=temp_git_repo, base_ref="HEAD")
    assert initial_summary.total_files == 0
    assert initial_summary.insertions == 0
    assert initial_summary.deletions == 0

    # Modify existing README.md and add a new module
    readme_path = temp_git_repo / "README.md"
    readme_path.write_text("# Initial Test Repo\n\nAdded second line.\n", encoding="utf-8")

    new_src = temp_git_repo / "main.py"
    new_src.write_text("def hello() -> str:\n    return 'world'\n", encoding="utf-8")

    # Stage changes
    subprocess.run(["git", "add", "."], cwd=temp_git_repo, check=True, capture_output=True)

    summary: DiffSummary = await diff_engine.compute_diff(cwd=temp_git_repo, base_ref="HEAD")

    assert summary.total_files == 2
    assert summary.insertions > 0
    assert len(summary.diff_sha256) == 64
    assert all(c in "0123456789abcdef" for c in summary.diff_sha256.lower())

    # Raw diff should contain unified diff markers
    raw_diff = await diff_engine.get_raw_diff(cwd=temp_git_repo, base_ref="HEAD")
    assert "diff --git" in raw_diff
    assert "main.py" in raw_diff


@pytest.mark.asyncio
async def test_diff_engine_detects_binary_and_workflow_files(
    temp_git_repo: Path,
) -> None:
    """Verifies that modified CI workflow files and binary assets are flagged."""
    diff_engine = GitDiffEngine()

    # 1. Add CI/CD workflow file
    wf_dir = temp_git_repo / ".github" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    ci_file = wf_dir / "build.yml"
    ci_file.write_text("name: CI\non: [push]\njobs:\n  build:\n    runs-on: ubuntu-latest\n", encoding="utf-8")

    # 2. Add binary asset containing null bytes
    binary_file = temp_git_repo / "asset.bin"
    binary_file.write_bytes(b"\x00\x01\x02\x03\xFF\xFE")

    subprocess.run(["git", "add", "."], cwd=temp_git_repo, check=True, capture_output=True)

    summary = await diff_engine.compute_diff(cwd=temp_git_repo, base_ref="HEAD")

    assert diff_engine.contains_workflow_changes(summary) is True
    assert ".github/workflows/build.yml" in list(diff_engine.get_workflow_files(summary))
    assert diff_engine.contains_binary_files(summary) is True


# ---------------------------------------------------------------------------
# 3. GitPushManager Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_manager_push_branch_success(
    temp_git_repo: Path,
    tmp_path: Path,
) -> None:
    """Verifies git push executes with force-with-lease against an isolated bare remote."""
    push_manager = GitPushManager()

    # 1. Create a local bare repository as remote target
    bare_remote = tmp_path / "remote.git"
    bare_remote.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--bare"], cwd=bare_remote, check=True, capture_output=True)

    # 2. Add remote to temp_git_repo
    subprocess.run(
        ["git", "remote", "add", "origin", str(bare_remote)],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
    )

    # 3. Create a feature branch and commit
    subprocess.run(["git", "checkout", "-b", "feature/unit-push"], cwd=temp_git_repo, check=True, capture_output=True)
    patch_file = temp_git_repo / "feature.txt"
    patch_file.write_text("New feature payload\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=temp_git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "feat: test push"], cwd=temp_git_repo, check=True, capture_output=True)

    # 4. Execute push via GitPushManager
    result: PushResult = await push_manager.push_branch(
        cwd=temp_git_repo,
        branch_name="feature/unit-push",
        remote_name="origin",
        target_branch="feature/unit-push",
        token=None,
        force_with_lease=True,
        set_upstream=True,
    )

    assert result.success is True
    assert len(result.pushed_sha) == 40
    assert result.remote_name == "origin"


# ---------------------------------------------------------------------------
# 4. Path Policy & Traversal Prevention Tests
# ---------------------------------------------------------------------------


def test_secure_resolve_path_accepts_valid_subpaths(tmp_path: Path) -> None:
    """Ensures valid files and directories inside workspace root resolve safely."""
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(parents=True, exist_ok=True)

    target_file = workspace_root / "src" / "index.ts"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("// test", encoding="utf-8")

    resolved = secure_resolve_path("src/index.ts", base_dir=workspace_root)
    assert resolved == target_file.resolve()


@pytest.mark.parametrize(
    "malicious_path",
    [
        "../outside.txt",
        "../../etc/passwd",
        "/etc/shadow",
        "C:\\Windows\\System32\\cmd.exe",
        "nested/../../../../root.key",
        "sub/../../../secret",
    ],
)
def test_secure_resolve_path_rejects_traversal(
    tmp_path: Path, malicious_path: str
) -> None:
    """Ensures attempts to escape the designated workspace directory raise ValidationError."""
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(parents=True, exist_ok=True)

    with pytest.raises((ValidationError, PermissionDeniedError)):
        secure_resolve_path(malicious_path, base_dir=workspace_root)


@pytest.mark.parametrize(
    "valid_branch",
    [
        "feature/add-auth",
        "fix/issue-42",
        "refactor/engine-v2",
        "task-123",
        "users/alice/work",
    ],
)
def test_validate_branch_name_valid(valid_branch: str) -> None:
    """Verifies that legitimate branch naming patterns conforming to git rules succeed."""
    assert validate_branch_name(valid_branch) == valid_branch


@pytest.mark.parametrize(
    "invalid_branch",
    [
        "-invalid-leading-dash",
        "branch..double-dot",
        "branch with spaces",
        "branch~tilde",
        "branch^caret",
        "branch:colon",
        "branch?question",
        "branch*asterisk",
        "branch[bracket",
        "branch\\backslash",
        "branch/trailing-slash/",
        "branch.lock",
        "@/*",
        "",
        "   ",
    ],
)
def test_validate_branch_name_invalid(invalid_branch: str) -> None:
    """Verifies that illegal git ref characters and argument injection patterns are rejected."""
    with pytest.raises(ValidationError):
        validate_branch_name(invalid_branch)
