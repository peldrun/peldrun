# backend/tests/source_control/test_domain_models.py
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
import pytest

try:
    from ...integrations.source_control.domain.enums import (
        FileChangeType,
        ProviderId,
        SourceControlErrorCode,
    )
    from ...integrations.source_control.domain.models import (
        BranchRef,
        ChangeRequestRef,
        CommitRef,
        DiffSummary,
        FileDiff,
        ProviderAccount,
        RepositoryRef,
    )
    from ...integrations.source_control.domain.policies import SourceControlPolicy
except (ImportError, ValueError):
    from backend.integrations.source_control.domain.enums import (
        FileChangeType,
        ProviderId,
        SourceControlErrorCode,
    )
    from backend.integrations.source_control.domain.models import (
        BranchRef,
        ChangeRequestRef,
        CommitRef,
        DiffSummary,
        FileDiff,
        ProviderAccount,
        RepositoryRef,
    )
    from backend.integrations.source_control.domain.policies import SourceControlPolicy


def test_provider_id_enum_values() -> None:
    assert ProviderId.GITHUB.value == "github"
    assert ProviderId.GITLAB.value == "gitlab"
    assert ProviderId.BITBUCKET.value == "bitbucket"
    assert len(ProviderId) == 3

def test_file_change_type_enum_values() -> None:
    expected = {
        "added", "modified", "deleted", "renamed",
        "copied", "type_changed", "unmerged", "unknown"
    }
    actual = {change.value for change in FileChangeType}
    assert actual == expected

def test_source_control_error_codes() -> None:
    assert SourceControlErrorCode.AUTH_REQUIRED.value == "AUTH_REQUIRED"
    assert SourceControlErrorCode.PERMISSION_DENIED.value == "PERMISSION_DENIED"
    assert SourceControlErrorCode.NOT_FOUND.value == "NOT_FOUND"
    assert SourceControlErrorCode.REMOTE_STATE_CHANGED.value == "REMOTE_STATE_CHANGED"
    assert SourceControlErrorCode.CONFLICT.value == "CONFLICT"

def test_provider_account_immutability() -> None:
    account = ProviderAccount(
        provider=ProviderId.GITHUB,
        account_id="usr_12345",
        username="octocat",
        base_url="https://github.com",
        connected=True,
    )
    assert account.username == "octocat"
    assert account.connected is True

    with pytest.raises(dataclasses.FrozenInstanceError):
        account.connected = False  # type: ignore[misc]

def test_repository_ref_immutability(sample_repo_ref: RepositoryRef) -> None:
    assert sample_repo_ref.default_branch == "main"
    assert sample_repo_ref.is_private is False

    with pytest.raises(dataclasses.FrozenInstanceError):
        sample_repo_ref.default_branch = "develop"  # type: ignore[misc]

def test_branch_ref_immutability(sample_branch_ref: BranchRef) -> None:
    assert sample_branch_ref.is_default is False

    with pytest.raises(dataclasses.FrozenInstanceError):
        sample_branch_ref.head_sha = "0000000000000000000000000000000000000000"  # type: ignore[misc]

def test_commit_ref_immutability() -> None:
    commit = CommitRef(
        sha="abcdef1234567890abcdef1234567890abcdef12",
        message="feat: initial implementation",
        author_name="Alice Dev",
        author_email="alice@peldrun.local",
        committed_at=datetime.now(timezone.utc),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        commit.message = "tampered commit message"  # type: ignore[misc]

def test_change_request_ref_immutability() -> None:
    cr = ChangeRequestRef(
        provider=ProviderId.GITHUB,
        repository_id="repo_999",
        number_or_iid="42",
        web_url="https://github.com/peldrun/peldrun/pull/42",
        state="open",
        source_branch="feature/test",
        target_branch="main",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        cr.state = "closed"  # type: ignore[misc]

def test_file_diff_immutability(sample_file_diff: FileDiff) -> None:
    assert sample_file_diff.change_type == FileChangeType.MODIFIED
    assert sample_file_diff.additions == 10

    with pytest.raises(dataclasses.FrozenInstanceError):
        sample_file_diff.additions = 999  # type: ignore[misc]

def test_diff_summary_immutability(sample_diff_summary: DiffSummary) -> None:
    assert len(sample_diff_summary.files) == 1
    assert sample_diff_summary.total_additions == 10

    with pytest.raises(dataclasses.FrozenInstanceError):
        sample_diff_summary.base_sha = "invalid_hash"  # type: ignore[misc]

@pytest.fixture
def default_policy() -> SourceControlPolicy:
    return SourceControlPolicy()

@pytest.mark.parametrize(
    "branch_name",
    [
        "main", "master", "develop", "release", "release/v1.0",
        "release/2.0.0", "releases/2026", "prod", "production",
        "stage", "staging", "MAIN", " Master ",
    ],
)
def test_is_protected_branch_true(branch_name: str, default_policy: SourceControlPolicy) -> None:
    assert default_policy.is_protected_branch(branch_name) is True

@pytest.mark.parametrize(
    "branch_name",
    [
        "feature/add-search", "feat/user-auth", "fix/issue-123",
        "bugfix/overflow", "chore/bump-deps", "test/local-git",
        "my-sandbox-branch", "patch-1",
    ],
)
def test_is_protected_branch_false(branch_name: str, default_policy: SourceControlPolicy) -> None:
    assert default_policy.is_protected_branch(branch_name) is False

@pytest.mark.parametrize(
    "file_path",
    [
        ".github/workflows/ci.yml", ".github/workflows/release.yaml",
        ".github/workflows/tests.yml", ".gitlab-ci.yml",
        "bitbucket-pipelines.yml", ".circleci/config.yml",
    ],
)
def test_is_workflow_path_true(file_path: str, default_policy: SourceControlPolicy) -> None:
    assert default_policy.is_workflow_path(file_path) is True

@pytest.mark.parametrize(
    "file_path",
    [
        "src/workflows/runner.py", "backend/omweb/routers/workflow.py",
        "README.md", "package.json", ".github/CODEOWNERS",
        ".github/PULL_REQUEST_TEMPLATE.md", "tests/test_ci.py", "docs/workflows.md",
    ],
)
def test_is_workflow_path_false(file_path: str, default_policy: SourceControlPolicy) -> None:
    assert default_policy.is_workflow_path(file_path) is False
