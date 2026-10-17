# backend/tests/source_control/test_github_client.py
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
import pytest

try:
    from ...integrations.source_control.domain.enums import ProviderId
    from ...integrations.source_control.domain.models import (
        BranchRef,
        ChangeRequestRef,
        RepositoryRef,
    )
    from ...integrations.source_control.errors import (
        AuthRequiredError,
        NotFoundError,
        RateLimitError,
    )
    from ...integrations.source_control.providers.github import GitHubProviderAdapter
except (ImportError, ValueError):
    from backend.integrations.source_control.domain.enums import ProviderId
    from backend.integrations.source_control.domain.models import (
        BranchRef,
        ChangeRequestRef,
        RepositoryRef,
    )
    from backend.integrations.source_control.errors import (
        AuthRequiredError,
        NotFoundError,
        RateLimitError,
    )
    from backend.integrations.source_control.providers.github import GitHubProviderAdapter


@pytest.fixture
def mock_http_client():
    client = AsyncMock()
    client.get_json = AsyncMock()
    client.post_json = AsyncMock()
    return client


@pytest.fixture
def github_adapter(mock_http_client):
    return GitHubProviderAdapter(client=mock_http_client, base_url="https://api.github.com")


@pytest.fixture
def sample_repo() -> RepositoryRef:
    return RepositoryRef(
        provider=ProviderId.GITHUB,
        account_id="owner_1",
        repository_id="12345",
        namespace="peldrun",
        name="sample-repo",
        clone_url="https://github.com/peldrun/sample-repo.git",
        web_url="https://github.com/peldrun/sample-repo",
        default_branch="main",
        is_private=False,
    )


@pytest.mark.asyncio
async def test_get_repository_mapping(github_adapter, mock_http_client, sample_repo) -> None:
    mock_http_client.get_json.return_value = {
        "id": 12345, "name": "sample-repo", "full_name": "peldrun/sample-repo",
        "owner": {"login": "peldrun", "id": 999}, "private": False,
        "html_url": "https://github.com/peldrun/sample-repo",
        "clone_url": "https://github.com/peldrun/sample-repo.git",
        "default_branch": "main", "description": "A sample repository",
    }

    repo = await github_adapter.get_repository(repo=sample_repo, token="fake_token")
    assert isinstance(repo, RepositoryRef)
    assert repo.name == "sample-repo"


@pytest.mark.asyncio
async def test_get_repository_not_found(github_adapter, mock_http_client, sample_repo) -> None:
    mock_http_client.get_json.side_effect = NotFoundError("Not Found")
    with pytest.raises(NotFoundError):
        await github_adapter.get_repository(repo=sample_repo, token="fake_token")


@pytest.mark.asyncio
async def test_get_repository_auth_error(github_adapter, mock_http_client, sample_repo) -> None:
    mock_http_client.get_json.side_effect = AuthRequiredError("Bad credentials")
    with pytest.raises(AuthRequiredError):
        await github_adapter.get_repository(repo=sample_repo, token="bad_token")


@pytest.mark.asyncio
async def test_list_branches_mapping(github_adapter, mock_http_client, sample_repo) -> None:
    mock_http_client.get_json.return_value = [
        {"name": "main", "commit": {"sha": "a" * 40}, "protected": True},
        {"name": "feature/test", "commit": {"sha": "b" * 40}, "protected": False},
    ]

    branches = await github_adapter.list_branches(repo=sample_repo, token="fake_token")
    assert len(branches) == 2
    assert branches[0].name == "main"
    assert branches[0].is_protected is True


@pytest.mark.asyncio
async def test_create_change_request_mapping(github_adapter, mock_http_client, sample_repo) -> None:
    mock_http_client.post_json.return_value = {
        "number": 42, "html_url": "https://github.com/peldrun/sample-repo/pull/42",
        "state": "open", "title": "Add new feature",
        "head": {"ref": "feature/test"}, "base": {"ref": "main"}, "draft": False,
    }

    cr = await github_adapter.create_change_request(
        repo=sample_repo, source_branch="feature/test", target_branch="main",
        title="Add new feature", body="This is a test PR", draft=False, token="fake_token",
    )
    assert isinstance(cr, ChangeRequestRef)
    assert cr.number_or_iid == "42"


@pytest.mark.asyncio
async def test_rate_limit_error(github_adapter, mock_http_client, sample_repo) -> None:
    mock_http_client.get_json.side_effect = RateLimitError("API rate limit exceeded")
    with pytest.raises(RateLimitError):
        await github_adapter.get_repository(repo=sample_repo, token="fake_token")
