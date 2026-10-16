from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, AsyncGenerator, Generator, Mapping, Optional, Sequence
from unittest.mock import AsyncMock, MagicMock

import pytest

# Resilient imports across root or backend working directory execution
try:
    from ...integrations.source_control.capabilities import ProviderCapabilities
    from ...integrations.source_control.domain.enums import FileChangeType, ProviderId
    from ...integrations.source_control.domain.models import (
        BranchRef,
        ChangeRequestRef,
        CommitRef,
        DiffSummary,
        FileDiff,
        ProviderAccount,
        RepositoryRef,
    )
    from ...integrations.source_control.git.diff import GitDiffEngine
    from ...integrations.source_control.git.executable import GitExecutable
    from ...integrations.source_control.git.push import GitPushManager, PushResult
    from ...integrations.source_control.git.workspace import GitWorkspaceManager
    from ...integrations.source_control.operations.journal import OperationJournal
    from ...integrations.source_control.operations.models import OperationRecord, OperationState
    from ...integrations.source_control.operations.service import OperationService
    from ...integrations.source_control.ports.provider import SourceControlProvider
    from ...integrations.source_control.registry import ProviderRegistry
    from ...integrations.source_control.security.approval import ApprovalGate, ApprovalTicket
    from ...integrations.source_control.security.audit import AuditLogger
    from ...integrations.source_control.security.policy_engine import PolicyEngine
    from ...integrations.source_control.security.token_service import TokenService
    from ...integrations.source_control.security.vault import CredentialVault
except (ImportError, ValueError):
    from backend.integrations.source_control.capabilities import ProviderCapabilities
    from backend.integrations.source_control.domain.enums import FileChangeType, ProviderId
    from backend.integrations.source_control.domain.models import (
        BranchRef,
        ChangeRequestRef,
        CommitRef,
        DiffSummary,
        FileDiff,
        ProviderAccount,
        RepositoryRef,
    )
    from backend.integrations.source_control.git.diff import GitDiffEngine
    from backend.integrations.source_control.git.executable import GitExecutable
    from backend.integrations.source_control.git.push import GitPushManager, PushResult
    from backend.integrations.source_control.git.workspace import GitWorkspaceManager
    from backend.integrations.source_control.operations.journal import OperationJournal
    from backend.integrations.source_control.operations.models import OperationRecord, OperationState
    from backend.integrations.source_control.operations.service import OperationService
    from backend.integrations.source_control.ports.provider import SourceControlProvider
    from backend.integrations.source_control.registry import ProviderRegistry
    from backend.integrations.source_control.security.approval import ApprovalGate, ApprovalTicket
    from backend.integrations.source_control.security.audit import AuditLogger
    from backend.integrations.source_control.security.policy_engine import PolicyEngine
    from backend.integrations.source_control.security.token_service import TokenService
    from backend.integrations.source_control.security.vault import CredentialVault

from . import (
    DUMMY_BASE_SHA,
    DUMMY_DEFAULT_BRANCH,
    DUMMY_DIFF_SHA256,
    DUMMY_FEATURE_BRANCH,
    DUMMY_HEAD_SHA,
    DUMMY_PROVIDER,
    DUMMY_REPO_ID,
    DUMMY_REPO_SLUG,
)


class InMemoryVault(CredentialVault):
    """Hermetic in-memory credential vault implementation for testing."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def put(self, key: str, secret: str) -> None:
        self._store[key] = secret

    def get(self, key: str) -> Optional[str]:
        return self._store.get(key)

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()


class MockSourceControlProvider:
    """Mock source control provider conforming to SourceControlProvider protocol."""

    def __init__(
        self,
        provider_id: ProviderId = ProviderId.GITHUB,
        capabilities: Optional[ProviderCapabilities] = None,
    ) -> None:
        self._provider_id = provider_id
        self._capabilities = capabilities or ProviderCapabilities(
            list_repositories=True,
            read_files=True,
            create_branch=True,
            push_branch=True,
            create_change_request=True,
            list_checks=True,
            comments=True,
            merge_change_request=True,
            self_managed_instance=False,
        )
        self.auth = MagicMock()
        self.auth.exchange_code_for_token = AsyncMock()

    @property
    def provider_id(self) -> ProviderId:
        return self._provider_id

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def get_authenticated_account(self, token: Optional[str] = None) -> ProviderAccount:
        return ProviderAccount(
            provider=self._provider_id,
            account_id="user_999",
            username="testuser",
            base_url="https://github.com",
            connected=True,
        )

    async def list_repositories(
        self,
        query: Optional[str] = None,
        limit: int = 100,
        token: Optional[str] = None,
    ) -> Sequence[RepositoryRef]:
        return [
            RepositoryRef(
                provider=self._provider_id,
                account_id="owner_1",
                repository_id=DUMMY_REPO_ID,
                namespace="peldrun",
                name="sample-repo",
                clone_url="https://github.com/peldrun/sample-repo.git",
                web_url="https://github.com/peldrun/sample-repo",
                default_branch=DUMMY_DEFAULT_BRANCH,
                is_private=False,
                metadata={"description": "Test repository"},
            )
        ]

    async def get_repository(
        self,
        repository_id: str,
        token: Optional[str] = None,
    ) -> RepositoryRef:
        return RepositoryRef(
            provider=self._provider_id,
            account_id="owner_1",
            repository_id=repository_id,
            namespace="peldrun",
            name="sample-repo",
            clone_url="https://github.com/peldrun/sample-repo.git",
            web_url="https://github.com/peldrun/sample-repo",
            default_branch=DUMMY_DEFAULT_BRANCH,
            is_private=False,
            metadata={"description": "Test repository"},
        )

    async def read_file(
        self,
        repo: RepositoryRef,
        file_path: str,
        ref: Optional[str] = None,
        token: Optional[str] = None,
    ) -> bytes:
        if file_path == "binary.dat":
            return b"\x00\x01\x02\x03\x04"
        return f"# Sample file content for {file_path}\n".encode("utf-8")

    async def list_branches(
        self,
        repo: RepositoryRef,
        limit: int = 100,
        token: Optional[str] = None,
    ) -> Sequence[BranchRef]:
        return [
            BranchRef(name=DUMMY_DEFAULT_BRANCH, head_sha=DUMMY_HEAD_SHA, is_default=True),
            BranchRef(name=DUMMY_FEATURE_BRANCH, head_sha=DUMMY_BASE_SHA, is_default=False),
        ]

    async def get_branch(
        self,
        repo: RepositoryRef,
        branch_name: str,
        token: Optional[str] = None,
    ) -> BranchRef:
        return BranchRef(
            name=branch_name,
            head_sha=DUMMY_HEAD_SHA,
            is_default=(branch_name == DUMMY_DEFAULT_BRANCH),
        )

    async def create_branch(
        self,
        repo: RepositoryRef,
        branch_name: str,
        from_sha: str,
        token: Optional[str] = None,
    ) -> BranchRef:
        return BranchRef(
            name=branch_name,
            head_sha=from_sha,
            is_default=False,
        )

    async def list_change_requests(
        self,
        repo: RepositoryRef,
        source_branch: Optional[str] = None,
        target_branch: Optional[str] = None,
        state: Optional[str] = "open",
        limit: int = 100,
        token: Optional[str] = None,
    ) -> Sequence[ChangeRequestRef]:
        return []

    async def get_change_request(
        self,
        repo: RepositoryRef,
        number_or_iid: str | int,
        token: Optional[str] = None,
    ) -> ChangeRequestRef:
        return ChangeRequestRef(
            provider=self._provider_id,
            repository_id=repo.repository_id,
            number_or_iid=str(number_or_iid),
            web_url=f"https://github.com/peldrun/sample-repo/pull/{number_or_iid}",
            state="open",
            source_branch=DUMMY_FEATURE_BRANCH,
            target_branch=DUMMY_DEFAULT_BRANCH,
        )

    async def create_change_request(
        self,
        repo: RepositoryRef,
        source_branch: str,
        target_branch: str,
        title: str,
        body: str = "",
        draft: bool = False,
        token: Optional[str] = None,
    ) -> ChangeRequestRef:
        return ChangeRequestRef(
            provider=self._provider_id,
            repository_id=repo.repository_id,
            number_or_iid="42",
            web_url="https://github.com/peldrun/sample-repo/pull/42",
            state="open",
            source_branch=source_branch,
            target_branch=target_branch,
        )

    async def get_check_summary(
        self,
        repo: RepositoryRef,
        ref: str,
        token: Optional[str] = None,
    ) -> Any:
        try:
            from backend.integrations.source_control.domain.models import CheckRunRef, CheckSummaryRef
        except ImportError:
            from dataclasses import dataclass, field

            @dataclass(frozen=True)
            class CheckRunRef:
                name: str
                status: str
                conclusion: Optional[str] = None
                web_url: Optional[str] = None

            @dataclass(frozen=True)
            class CheckSummaryRef:
                ref: str
                total_count: int
                success_count: int
                failure_count: int
                pending_count: int
                overall_state: str
                runs: tuple[CheckRunRef, ...] = field(default_factory=tuple)

        run = CheckRunRef(
            name="ci/tests",
            status="completed",
            conclusion="success",
            web_url="https://github.com/peldrun/sample-repo/actions/runs/1",
        )
        return CheckSummaryRef(
            ref=ref,
            total_count=1,
            success_count=1,
            failure_count=0,
            pending_count=0,
            overall_state="success",
            runs=(run,),
        )

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Core Pytest Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def in_memory_vault() -> InMemoryVault:
    """Provides a fresh isolated in-memory credential vault."""
    return InMemoryVault()


@pytest.fixture
def token_service(in_memory_vault: InMemoryVault) -> TokenService:
    """Provides a TokenService backed by the in-memory vault."""
    return TokenService(vault=in_memory_vault)


@pytest.fixture
def policy_engine() -> PolicyEngine:
    """Provides a PolicyEngine configured with permissive defaults for test execution."""
    return PolicyEngine()


@pytest.fixture
def approval_gate() -> ApprovalGate:
    """Provides an isolated ApprovalGate instance."""
    return ApprovalGate()


@pytest.fixture
def audit_logger(tmp_path: Path) -> AuditLogger:
    """Provides an AuditLogger writing events to a temporary jsonl file."""
    log_file = tmp_path / "test_audit.jsonl"
    return AuditLogger(log_file_path=log_file)


@pytest.fixture
def mock_provider() -> MockSourceControlProvider:
    """Provides a mock provider implementing SourceControlProvider interface."""
    return MockSourceControlProvider()


@pytest.fixture
def provider_registry(mock_provider: MockSourceControlProvider) -> ProviderRegistry:
    """Provides an isolated ProviderRegistry with MockSourceControlProvider registered."""
    registry = ProviderRegistry()
    registry.register(mock_provider)
    return registry


@pytest.fixture
def sample_repo_ref() -> RepositoryRef:
    """Provides a standard immutable RepositoryRef domain model."""
    return RepositoryRef(
        provider=ProviderId.GITHUB,
        account_id="owner_1",
        repository_id=DUMMY_REPO_ID,
        namespace="peldrun",
        name="sample-repo",
        clone_url="https://github.com/peldrun/sample-repo.git",
        web_url="https://github.com/peldrun/sample-repo",
        default_branch=DUMMY_DEFAULT_BRANCH,
        is_private=False,
        metadata={"description": "Standard sample repository for unit testing."},
    )


@pytest.fixture
def sample_branch_ref() -> BranchRef:
    """Provides a standard immutable BranchRef domain model."""
    return BranchRef(
        name=DUMMY_FEATURE_BRANCH,
        head_sha=DUMMY_HEAD_SHA,
        is_default=False,
    )


@pytest.fixture
def sample_file_diff() -> FileDiff:
    """Provides a standard immutable FileDiff domain model."""
    return FileDiff(
        path="src/main.py",
        change_type=FileChangeType.MODIFIED,
        insertions=10,
        deletions=2,
        old_path=None,
        is_binary=False,
    )


@pytest.fixture
def sample_diff_summary(sample_file_diff: FileDiff) -> DiffSummary:
    """Provides a standard immutable DiffSummary domain model."""
    return DiffSummary(
        base_sha=DUMMY_BASE_SHA,
        head_sha=DUMMY_HEAD_SHA,
        total_files=1,
        insertions=10,
        deletions=2,
        files=(sample_file_diff,),
        diff_sha256=DUMMY_DIFF_SHA256,
    )


@pytest.fixture
def temp_git_repo(tmp_path: Path) -> Path:
    """Initializes and returns an isolated real Git repository for testing local git commands."""
    repo_dir = tmp_path / "test_repo"
    repo_dir.mkdir(parents=True, exist_ok=True)

    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Peldrun Test",
        "GIT_AUTHOR_EMAIL": "test@peldrun.local",
        "GIT_COMMITTER_NAME": "Peldrun Test",
        "GIT_COMMITTER_EMAIL": "test@peldrun.local",
    }

    # Initialize repository on main branch
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        env=git_env,
    )
    subprocess.run(
        ["git", "config", "user.name", "Peldrun Test"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        env=git_env,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@peldrun.local"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        env=git_env,
    )

    # Create and commit an initial README
    readme = repo_dir / "README.md"
    readme.write_text("# Initial Test Repo\n", encoding="utf-8")

    subprocess.run(
        ["git", "add", "README.md"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        env=git_env,
    )
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        env=git_env,
    )

    return repo_dir


@pytest.fixture
def workspace_manager(tmp_path: Path) -> GitWorkspaceManager:
    """Provides a GitWorkspaceManager rooted in a dedicated temporary test directory."""
    workspaces_root = tmp_path / "workspaces"
    workspaces_root.mkdir(parents=True, exist_ok=True)
    return GitWorkspaceManager(base_workspace_dir=workspaces_root)


@pytest.fixture
def operation_service(tmp_path: Path) -> OperationService:
    """Provides a durable OperationService rooted in a temporary directory."""
    journal_dir = tmp_path / "operations_journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    journal = OperationJournal(journal_dir=journal_dir)
    return OperationService(journal=journal)
