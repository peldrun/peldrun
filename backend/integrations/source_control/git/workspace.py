from __future__ import annotations

import base64
import dataclasses
import os
import re
import secrets
import shutil
import stat
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from ..domain.enums import FileChangeType, SourceControlErrorCode
from ..domain.models import BranchRef, DiffSummary, FileDiff, RepositoryRef, WorkspaceRef
from ..errors import ConflictError, SourceControlError, ValidationError
from ..security.approval import compute_diff_sha256
from ..security.redaction import redact_url
from .executable import GitExecutable, GitExecutionError
from .path_policy import secure_resolve_path, validate_branch_name


class WorkspaceError(SourceControlError):
    """Raised when an isolated git workspace operation fails."""

    def __init__(
        self,
        message: str,
        *,
        details: Optional[dict[str, str]] = None,
    ) -> None:
        super().__init__(
            message=message,
            code=SourceControlErrorCode.PROVIDER_ERROR,
            retryable=False,
            details=details,
        )


class GitWorkspaceManager:
    """Thread-safe manager coordinating local Git repository cloning, branch management, and isolated diff inspections."""

    def __init__(
        self,
        base_workspace_dir: str | Path,
        git: Optional[GitExecutable] = None,
    ) -> None:
        self.base_workspace_dir = Path(base_workspace_dir).resolve()
        self.base_workspace_dir.mkdir(parents=True, exist_ok=True)
        self.git = git or GitExecutable()
        self._workspaces: dict[str, WorkspaceRef] = {}
        self._lock = threading.Lock()

    def _get_auth_header_args(self, token: Optional[str]) -> list[str]:
        """Generates transient CLI configuration arguments to inject HTTP auth without writing secrets to .git/config."""
        if not token:
            return []
        # Support generic HTTP basic auth header for OAuth / PAT tokens
        encoded_token = base64.b64encode(f"x-access-token:{token}".encode("ascii")).decode("ascii")
        return ["-c", f"http.extraHeader=Authorization: Basic {encoded_token}"]

    async def create_workspace(
        self,
        repo: RepositoryRef,
        source_branch: Optional[str] = None,
        base_sha: Optional[str] = None,
        token: Optional[str] = None,
    ) -> WorkspaceRef:
        """Clones a remote repository into an isolated local directory with sanitized configuration."""
        workspace_id = f"ws_{secrets.token_hex(12)}"
        workspace_folder_name = f"{repo.repository_id}_{workspace_id}"
        workspace_path = secure_resolve_path(self.base_workspace_dir, workspace_folder_name)

        if workspace_path.exists():
            raise ConflictError(f"Workspace path '{workspace_path}' already exists.")

        workspace_path.mkdir(parents=True, exist_ok=True)

        clean_clone_url = redact_url(repo.clone_url)
        auth_args = self._get_auth_header_args(token)

        target_branch = source_branch or repo.default_branch

        # 1. Initialize local repository and set sanitized remote URL
        await self.git.run(["init", "--initial-branch", target_branch], cwd=str(workspace_path))
        await self.git.run(["remote", "add", "origin", clean_clone_url], cwd=str(workspace_path))

        # 2. Fetch target branch using transient credentials
        fetch_cmd = [*auth_args, "fetch", "--depth=50", "origin", target_branch]
        await self.git.run(fetch_cmd, cwd=str(workspace_path))

        # 3. Checkout target branch or explicit base SHA
        checkout_target = base_sha if base_sha else f"origin/{target_branch}"
        await self.git.run(["checkout", "-B", target_branch, checkout_target], cwd=str(workspace_path))

        head_res = await self.git.run(["rev-parse", "HEAD"], cwd=str(workspace_path))
        current_head = head_res.stdout.strip()
        effective_base = base_sha or current_head

        now = datetime.utcnow()
        workspace = WorkspaceRef(
            workspace_id=workspace_id,
            repository_id=repo.repository_id,
            workspace_path=str(workspace_path),
            current_branch=target_branch,
            base_sha=effective_base,
            head_sha=current_head,
            is_worktree=False,
            is_clean=True,
            created_at=now,
            metadata={"clone_url": clean_clone_url},
        )

        with self._lock:
            self._workspaces[workspace_id] = workspace

        return workspace

    def get_workspace(self, workspace_id: str) -> Optional[WorkspaceRef]:
        """Retrieves a tracked workspace reference by ID."""
        with self._lock:
            return self._workspaces.get(workspace_id)

    async def is_dirty(self, workspace_id: str) -> bool:
        """Checks if the local workspace contains uncommitted changes or untracked files."""
        ws = self.get_workspace(workspace_id)
        if ws is None:
            raise ValidationError(f"Workspace '{workspace_id}' is not recognized.")

        res = await self.git.run(["status", "--porcelain"], cwd=ws.workspace_path)
        return bool(res.stdout.strip())

    async def get_current_branch(self, workspace_id: str) -> str:
        """Retrieves the active branch name in the workspace."""
        ws = self.get_workspace(workspace_id)
        if ws is None:
            raise ValidationError(f"Workspace '{workspace_id}' is not recognized.")

        res = await self.git.run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=ws.workspace_path)
        return res.stdout.strip()

    async def get_head_sha(self, workspace_id: str) -> str:
        """Retrieves the latest commit SHA of HEAD."""
        ws = self.get_workspace(workspace_id)
        if ws is None:
            raise ValidationError(f"Workspace '{workspace_id}' is not recognized.")

        res = await self.git.run(["rev-parse", "HEAD"], cwd=ws.workspace_path)
        return res.stdout.strip()

    async def create_branch(
        self,
        workspace_id: str,
        branch_name: str,
        start_point: Optional[str] = None,
    ) -> BranchRef:
        """Creates and switches to a new validated Git branch."""
        clean_name = validate_branch_name(branch_name)
        ws = self.get_workspace(workspace_id)
        if ws is None:
            raise ValidationError(f"Workspace '{workspace_id}' is not recognized.")

        cmd = ["checkout", "-b", clean_name]
        if start_point:
            cmd.append(start_point)

        await self.git.run(cmd, cwd=ws.workspace_path)
        head_sha = await self.get_head_sha(workspace_id)

        with self._lock:
            self._workspaces[workspace_id] = dataclasses.replace(
                ws, current_branch=clean_name, head_sha=head_sha
            )

        return BranchRef(name=clean_name, head_sha=head_sha, is_default=False)

    async def get_diff(
        self,
        workspace_id: str,
        base_sha: Optional[str] = None,
    ) -> DiffSummary:
        """Extracts and summarizes the unified diff and file metrics against base SHA or HEAD."""
        ws = self.get_workspace(workspace_id)
        if ws is None:
            raise ValidationError(f"Workspace '{workspace_id}' is not recognized.")

        effective_base = base_sha or ws.base_sha
        current_head = await self.get_head_sha(workspace_id)

        # 1. Fetch raw unified patch diff
        diff_res = await self.git.run(
            ["diff", "-U3", effective_base], cwd=ws.workspace_path, check=False
        )
        raw_diff = diff_res.stdout

        # 2. Extract numstat for exact file insertion and deletion metrics
        numstat_res = await self.git.run(
            ["diff", "--numstat", effective_base], cwd=ws.workspace_path, check=False
        )

        file_diffs: list[FileDiff] = []
        total_insertions = 0
        total_deletions = 0

        for line in numstat_res.stdout.splitlines():
            line_str = line.strip()
            if not line_str:
                continue
            parts = line_str.split("\t")
            if len(parts) >= 3:
                ins_str, del_str, file_path = parts[0], parts[1], parts[2]
                ins = int(ins_str) if ins_str.isdigit() else 0
                dels = int(del_str) if del_str.isdigit() else 0

                total_insertions += ins
                total_deletions += dels

                change_type = FileChangeType.MODIFIED
                if ins > 0 and dels == 0:
                    change_type = FileChangeType.ADDED
                elif ins == 0 and dels > 0:
                    change_type = FileChangeType.DELETED

                file_diffs.append(
                    FileDiff(
                        path=file_path,
                        change_type=change_type,
                        insertions=ins,
                        deletions=dels,
                    )
                )

        diff_sha256 = compute_diff_sha256(raw_diff)

        return DiffSummary(
            base_sha=effective_base,
            head_sha=current_head,
            total_files=len(file_diffs),
            insertions=total_insertions,
            deletions=total_deletions,
            files=tuple(file_diffs),
            diff_sha256=diff_sha256,
        )

    async def cleanup_workspace(self, workspace_id: str, force: bool = False) -> bool:
        """Safely removes an isolated workspace directory and deregisters it."""
        ws = self.get_workspace(workspace_id)
        if ws is None:
            return False

        if not force and await self.is_dirty(workspace_id):
            raise ConflictError(
                f"Cannot delete workspace '{workspace_id}': local uncommitted changes exist. "
                "Explicit confirmation required."
            )

        workspace_path = Path(ws.workspace_path)

        def _remove_readonly(func: Any, path: str, _: Any) -> None:
            """Handles Windows read-only file lock attribute clearing during deletion."""
            try:
                os.chmod(path, stat.S_IWRITE)
                func(path)
            except Exception:
                pass

        if workspace_path.exists():
            shutil.rmtree(str(workspace_path), onerror=_remove_readonly)

        with self._lock:
            self._workspaces.pop(workspace_id, None)

        return True

    def clear(self) -> None:
        """Clears in-memory workspace references (isolated test teardown helper)."""
        with self._lock:
            self._workspaces.clear()
