# backend/tests/source_control/conftest.py
"""
Shared fixtures for the Source Control test suite.

Contract:
    Every fixture in this module returns a *concrete* instance. The
    abstract base classes `AuditLogger`, `OperationJournal`, and
    `CredentialVault` are never instantiated directly, because Python
    forbids instantiating classes that still have unresolved abstract
    methods.

History:
    A previous version of this file instantiated the abstract bases
    directly inside the `audit_logger` and `operation_service`
    fixtures. Those fixtures were never used by the current tests, so
    the defect stayed latent. They are corrected here so that future
    tests (in particular the agent-tool tests) can rely on them.

Fixtures provided:
    - in_memory_vault       : isolated CredentialVault for token tests.
    - token_service         : TokenService backed by `in_memory_vault`.
    - policy_engine         : default PolicyEngine.
    - approval_gate         : isolated ApprovalGate.
    - audit_logger          : InMemoryAuditLogger (concrete).
    - mock_provider         : deterministic SourceControlProvider.
    - provider_registry     : registry pre-populated with `mock_provider`.
    - sample_repo_ref       : canonical RepositoryRef.
    - sample_branch_ref     : canonical BranchRef.
    - sample_file_diff      : canonical FileDiff.
    - sample_diff_summary   : canonical DiffSummary wrapping one file.
    - temp_git_repo         : real on-disk git repository.
    - mock_git_executable   : GitExecutable-shaped MagicMock.
    - workspace_manager     : GitWorkspaceManager wired to the mock.
    - operation_service     : OperationService backed by an in-memory
                              journal (concrete).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional, Sequence
from unittest.mock import AsyncMock, MagicMock

import pytest

# Resilient imports: the relative form works when pytest is run with
# `rootdir=backend`, the absolute form when it is run with the
# repository root on sys.path. Both resolve to the same modules.
try:
    from ...integrations.source_control.capabilities import ProviderCapabilities
    from ...integrations.source_control.domain.enums import (
        FileChangeType,
        ProviderId,
    )
    from ...integrations.source_control.domain.models import (
        BranchRef,
        ChangeRequestRef,
        DiffSummary,
        FileDiff,
        ProviderAccount,
        RepositoryRef,
    )
    from ...integrations.source_control.git.executable import GitExecutable
    from ...integrations.source_control.git.path_policy import WorkspacePathPolicy
    from ...integrations.source_control.git.workspace import GitWorkspaceManager
    from ...integrations.source_control.operations.journal import (
        InMemoryOperationJournal,
    )
    from ...integrations.source_control.operations.service import OperationService
    from ...integrations.source_control.registry import ProviderRegistry
    from ...integrations.source_control.security.approval import ApprovalGate
    from ...integrations.source_control.security.audit import (
        InMemoryAuditLogger,
    )
    from ...integrations.source_control.security.policy_engine import PolicyEngine
    from ...integrations.source_control.security.token_service import TokenService
    from ...integrations.source_control.security.vault import CredentialVault
except (ImportError, ValueError):
    from backend.integrations.source_control.capabilities import ProviderCapabilities
    from backend.integrations.source_control.domain.enums import (
        FileChangeType,
        ProviderId,
    )
    from backend.integrations.source_control.domain.models import (
        BranchRef,
        ChangeRequestRef,
        DiffSummary,
        FileDiff,
        ProviderAccount,
        RepositoryRef,
    )
    from backend.integrations.source_control.git.executable import GitExecutable
    from backend.integrations.source_control.git.path_policy import WorkspacePathPolicy
    from backend.integrations.source_control.git.workspace import GitWorkspaceManager
    from backend.integrations.source_control.operations.journal import (
        InMemoryOperationJournal,
    )
    from backend.integrations.source_control.operations.service import OperationService
    from backend.integrations.source_control.registry import ProviderRegistry
    from backend.integrations.source_control.security.approval import ApprovalGate
    from backend.integrations.source_control.security.audit import (
        InMemoryAuditLogger,
    )
    from backend.integrations.source_control.security.policy_engine import PolicyEngine
    from backend.integrations.source_control.security.token_service import TokenService
    from backend.integrations.source_control.security.vault import CredentialVault

from . import (
    DUMMY_BASE_SHA,
    DUMMY_DEFAULT_BRANCH,
    DUMMY_FEATURE_BRANCH,
    DUMMY_HEAD_SHA,
    DUMMY_REPO_ID,
)


# ---------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------


class InMemoryVault(CredentialVault):
    """
    Minimal in-memory CredentialVault for tests.

    The four abstract operations are implemented as straightforward
    dictionary reads/writes. No key-prefix validation is applied at
    this layer, which lets tests exercise the underlying behaviour
    without tripping over the `peldrun.sc.` policy enforced by
    `CredentialVault.put()`.
    """

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def _put(self, key: str, secret: str) -> None:
        self._store[key] = secret

    def _get(self, key: str) -> Optional[str]:
        return self._store.get(key)

    def _delete(self, key: str) -> None:
        self._store.pop(key, None)

    def _exists(self, key: str) -> bool:
        return key in self._store

    def clear(self) -> None:
        self._store.clear()


class MockSourceControlProvider:
    """
    Deterministic in-memory SourceControlProvider for tests.

    Every method returns fixed, recognisable data so that assertions
    can compare against the constants in `tests/source_control/__init__.py`.
    """

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

    async def get_authenticated_account(
        self, token: Optional[str] = None
    ) -> ProviderAccount:
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
            )
        ]

    async def get_repository(
        self, repository_id: str, token: Optional[str] = None
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
        return BranchRef(name=branch_name, head_sha=from_sha, is_default=False)

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

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------------
# Security / credential fixtures
# ---------------------------------------------------------------------


@pytest.fixture
def in_memory_vault() -> InMemoryVault:
    """Isolated CredentialVault for token lifecycle tests."""
    return InMemoryVault()


@pytest.fixture
def token_service(in_memory_vault: InMemoryVault) -> TokenService:
    """TokenService backed by a private in-memory vault."""
    return TokenService(vault=in_memory_vault)


@pytest.fixture
def policy_engine() -> PolicyEngine:
    """Default PolicyEngine with a fresh default policy."""
    return PolicyEngine()


@pytest.fixture
def approval_gate() -> ApprovalGate:
    """Isolated ApprovalGate."""
    return ApprovalGate()


@pytest.fixture
def audit_logger() -> InMemoryAuditLogger:
    """
    Concrete in-memory AuditLogger.

    Previous versions of this fixture instantiated the abstract
    `AuditLogger` class, which raises `TypeError` at construction
    time. The concrete `InMemoryAuditLogger` is used instead so that
    tests can exercise audit-dependent code paths.
    """
    return InMemoryAuditLogger()


# ---------------------------------------------------------------------
# Provider fixtures
# ---------------------------------------------------------------------


@pytest.fixture
def mock_provider() -> MockSourceControlProvider:
    """Deterministic SourceControlProvider."""
    return MockSourceControlProvider()


@pytest.fixture
def provider_registry(
    mock_provider: MockSourceControlProvider,
) -> ProviderRegistry:
    """ProviderRegistry pre-populated with `mock_provider`."""
    registry = ProviderRegistry()
    registry.register(mock_provider)
    return registry


# ---------------------------------------------------------------------
# Domain-value fixtures
# ---------------------------------------------------------------------


@pytest.fixture
def sample_repo_ref() -> RepositoryRef:
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
    )


@pytest.fixture
def sample_branch_ref() -> BranchRef:
    return BranchRef(
        name=DUMMY_FEATURE_BRANCH,
        head_sha=DUMMY_HEAD_SHA,
        is_default=False,
    )


@pytest.fixture
def sample_file_diff() -> FileDiff:
    return FileDiff(
        path="src/main.py",
        change_type=FileChangeType.MODIFIED,
        additions=10,
        deletions=2,
        old_path=None,
        is_binary=False,
    )


@pytest.fixture
def sample_diff_summary(sample_file_diff: FileDiff) -> DiffSummary:
    return DiffSummary(
        base_sha=DUMMY_BASE_SHA,
        head_sha=DUMMY_HEAD_SHA,
        total_additions=10,
        total_deletions=2,
        files=(sample_file_diff,),
    )


# ---------------------------------------------------------------------
# Git fixtures
# ---------------------------------------------------------------------


@pytest.fixture
def temp_git_repo(tmp_path: Path) -> Path:
    """
    Real on-disk git repository with one initial commit.

    Used by tests that need the actual git binary to run against a
    real workspace. The repository uses a dedicated author identity
    so that commits are reproducible and do not depend on the user's
    global git config.
    """
    repo_dir = tmp_path / "test_repo"
    repo_dir.mkdir(parents=True, exist_ok=True)

    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Peldrun Test",
        "GIT_AUTHOR_EMAIL": "test@peldrun.local",
        "GIT_COMMITTER_NAME": "Peldrun Test",
        "GIT_COMMITTER_EMAIL": "test@peldrun.local",
    }
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=repo_dir, check=True, capture_output=True, env=git_env,
    )
    subprocess.run(
        ["git", "config", "user.name", "Peldrun Test"],
        cwd=repo_dir, check=True, capture_output=True, env=git_env,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@peldrun.local"],
        cwd=repo_dir, check=True, capture_output=True, env=git_env,
    )
    (repo_dir / "README.md").write_text("# Initial Test Repo\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=repo_dir, check=True, capture_output=True, env=git_env,
    )
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo_dir, check=True, capture_output=True, env=git_env,
    )
    return repo_dir


@pytest.fixture
def mock_git_executable() -> MagicMock:
    """
    MagicMock shaped like a GitExecutable.

    The mock returns success for every command. It also handles
    `git status --porcelain` specially so that tests can create a
    file inside the workspace and observe the mock report it as
    dirty.
    """

    async def mock_git_run(cmd, cwd=None, **kwargs):
        if cwd:
            Path(cwd).mkdir(parents=True, exist_ok=True)
            (Path(cwd) / ".git").mkdir(exist_ok=True)

        if (
            isinstance(cmd, list)
            and "status" in cmd
            and "--porcelain" in cmd
            and cwd
        ):
            files = [
                f.name for f in Path(cwd).iterdir() if f.name != ".git"
            ]
            if files:
                stdout = "\n".join([f"?? {f}" for f in files]) + "\n"
                return MagicMock(stdout=stdout, returncode=0)

        return MagicMock(stdout="", returncode=0)

    mock_git = MagicMock(spec=GitExecutable)
    mock_git.run = AsyncMock(side_effect=mock_git_run)
    return mock_git


@pytest.fixture
def workspace_manager(
    tmp_path: Path,
    mock_git_executable: MagicMock,
) -> GitWorkspaceManager:
    """GitWorkspaceManager wired to the mock Git executable."""
    workspaces_root = tmp_path / "workspaces"
    workspaces_root.mkdir(parents=True, exist_ok=True)
    policy = WorkspacePathPolicy(root=workspaces_root)
    return GitWorkspaceManager(
        path_policy=policy,
        git=mock_git_executable,
    )


@pytest.fixture
def operation_service() -> OperationService:
    """
    OperationService backed by an isolated in-memory journal.

    Previous versions of this fixture instantiated the abstract
    `OperationJournal` class, which raises `TypeError` at
    construction time. The concrete `InMemoryOperationJournal` is
    used instead so that tests can exercise the full operation state
    machine without touching the durable journal on disk.
    """
    journal = InMemoryOperationJournal()
    return OperationService(journal=journal)
