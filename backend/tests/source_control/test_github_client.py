from __future__ import annotations

import base64
from typing import Any, Mapping
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Resilient imports for GitHub provider and domain contracts
try:
    from ...integrations.source_control.domain.enums import ProviderId
    from ...integrations.source_control.domain.models import (
        BranchRef,
        ChangeRequestRef,
        ProviderAccount,
        RepositoryRef,
    )
    from ...integrations.source_control.errors import (
        AuthExpiredError,
        AuthRequiredError,
        ConflictError,
        NotFoundError,
        PermissionDeniedError,
        RateLimitedError,
        SourceControlError,
        ValidationError,
    )
    from ...integrations.source_control.providers.github import (
        GitHubClient,
        GitHubProviderAdapter,
    )
except (ImportError, ValueError):
    from backend.integrations.source_control.domain.enums import ProviderId
    from backend.integrations.source_control.domain.models import (
        BranchRef,
        ChangeRequestRef,
        ProviderAccount,
        RepositoryRef,
    )
    from backend.integrations.source_control.errors import (
        AuthExpiredError,
        AuthRequiredError,
        ConflictError,
        NotFoundError,
        PermissionDeniedError,
        RateLimitedError,
        SourceControlError,
        ValidationError,
    )
    from backend.integrations.source_control.providers.github import (
        GitHubClient,
        GitHubProviderAdapter,
    )


class MockHTTPResponse:
    """Mock HTTP response matching common httpx/aiohttp interfaces."""

    def __init__(
        self,
        status_code: int,
        json_data: Any = None,
        headers: Mapping[str, str] | None = None,
        content: bytes = b"",
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data or {}
        self.headers = headers or {}
        self.content = content

    def json(self) -> Any:
        return self._json_data

    @property
    def data(self) -> Any:
        """Exposes raw parsed payload matching GitHubResponse contract."""
        return self._json_data

    @property
    def text(self) -> str:
        if isinstance(self._json_data, dict) and "message" in self._json_data:
            return self._json_data["message"]
        return str(self._json_data)


# ---------------------------------------------------------------------------
# 1. Authentication & Protocol Headers Tests
# ---------------------------------------------------------------------------


def test_github_client_default_headers() -> None:
    """Verifies standard GitHub API headers including media type and version."""
    client = GitHubClient(token="ghp_test_token_12345")
    headers = client._build_headers(token="ghp_test_token_12345")

    assert headers.get("Accept") == "application/vnd.github+json"
    assert headers.get("Authorization") == "Bearer ghp_test_token_12345"
    assert "User-Agent" in headers
    assert headers.get("X-GitHub-Api-Version") == "2022-11-28"


# ---------------------------------------------------------------------------
# 2. Account & Repository Mapping Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_authenticated_account_success() -> None:
    """Ensures GitHub user profile is mapped correctly to ProviderAccount domain model."""
    adapter = GitHubProviderAdapter()
    mock_payload = {
        "id": 583231,
        "login": "octocat",
        "name": "The Octocat",
        "html_url": "https://github.com/octocat",
    }

    with patch.object(
        adapter.client,
        "request",
        new_callable=AsyncMock,
        return_value=MockHTTPResponse(status_code=200, json_data=mock_payload),
    ):
        account: ProviderAccount = await adapter.get_authenticated_account(
            token="ghp_mock_token"
        )

        assert account.provider == ProviderId.GITHUB
        assert account.account_id == "583231"
        assert account.username == "octocat"
        assert account.connected is True
        assert account.base_url == "https://github.com"


@pytest.mark.asyncio
async def test_list_repositories_mapping(sample_repo_ref: RepositoryRef) -> None:
    """Ensures repository items returned from GitHub API are cleanly mapped to RepositoryRef."""
    adapter = GitHubProviderAdapter()
    mock_repos_payload = [
        {
            "id": 12345678,
            "name": "sample-repo",
            "owner": {"id": 999, "login": "peldrun"},
            "html_url": "https://github.com/peldrun/sample-repo",
            "clone_url": "https://github.com/peldrun/sample-repo.git",
            "default_branch": "main",
            "private": False,
            "description": "Standard sample repository for unit testing.",
        }
    ]

    with patch.object(
        adapter.client,
        "request",
        new_callable=AsyncMock,
        return_value=MockHTTPResponse(status_code=200, json_data=mock_repos_payload),
    ):
        repos = await adapter.list_repositories(token="ghp_mock_token", limit=10)

        assert len(repos) == 1
        r = repos[0]
        assert r.repository_id == "12345678"
        assert r.namespace == "peldrun"
        assert r.name == "sample-repo"
        assert r.default_branch == "main"
        assert r.is_private is False


# ---------------------------------------------------------------------------
# 3. Branches & File Inspection Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_and_create_branch(sample_repo_ref: RepositoryRef) -> None:
    """Verifies listing branches and creating a new git ref on remote repository."""
    adapter = GitHubProviderAdapter()

    list_payload = [
        {"name": "main", "commit": {"sha": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"}},
        {"name": "feature/task-1", "commit": {"sha": "11223344556677889900aabbccddeeff00112233"}},
    ]
    create_payload = {
        "ref": "refs/heads/feature/new-branch",
        "object": {"sha": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"},
    }

    with patch.object(adapter.client, "request", new_callable=AsyncMock) as mock_req:
        # 1. Test list_branches
        mock_req.return_value = MockHTTPResponse(status_code=200, json_data=list_payload)
        branches = await adapter.list_branches(sample_repo_ref, token="ghp_mock")

        assert len(branches) == 2
        assert branches[0].name == "main"
        assert branches[0].is_default is True

        # 2. Test create_branch
        mock_req.return_value = MockHTTPResponse(status_code=201, json_data=create_payload)
        created = await adapter.create_branch(
            repo=sample_repo_ref,
            branch_name="feature/new-branch",
            from_sha="a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
            token="ghp_mock",
        )

        assert created.name == "feature/new-branch"
        assert created.head_sha == "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


@pytest.mark.asyncio
async def test_read_file_base64_decoding(sample_repo_ref: RepositoryRef) -> None:
    """Verifies remote file reading decodes Base64 payloads correctly."""
    adapter = GitHubProviderAdapter()
    raw_text = "print('Hello from Peldrun CI')\n"
    encoded_bytes = base64.b64encode(raw_text.encode("utf-8")).decode("ascii")

    mock_content_payload = {
        "type": "file",
        "encoding": "base64",
        "size": len(raw_text),
        "name": "script.py",
        "path": "src/script.py",
        "content": encoded_bytes,
        "sha": "3b18e512dba79e4c8300dd08aeb37f8e728b8dad",
    }

    with patch.object(
        adapter.client,
        "request",
        new_callable=AsyncMock,
        return_value=MockHTTPResponse(status_code=200, json_data=mock_content_payload),
    ):
        file_bytes = await adapter.read_file(
            repo=sample_repo_ref,
            file_path="src/script.py",
            ref="main",
            token="ghp_mock",
        )

        assert file_bytes.decode("utf-8") == raw_text


# ---------------------------------------------------------------------------
# 4. Pull Requests & CI Checks Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_change_request_pull_request(sample_repo_ref: RepositoryRef) -> None:
    """Ensures creating a PR sends draft parameter and maps response to ChangeRequestRef."""
    adapter = GitHubProviderAdapter()
    mock_pr_payload = {
        "id": 987654,
        "number": 42,
        "state": "open",
        "title": "feat: add secure auth flow",
        "html_url": "https://github.com/peldrun/sample-repo/pull/42",
        "head": {"ref": "feature/my-task"},
        "base": {"ref": "main"},
        "draft": True,
    }

    with patch.object(
        adapter.client,
        "request",
        new_callable=AsyncMock,
        return_value=MockHTTPResponse(status_code=201, json_data=mock_pr_payload),
    ) as mock_req:
        cr: ChangeRequestRef = await adapter.create_change_request(
            repo=sample_repo_ref,
            source_branch="feature/my-task",
            target_branch="main",
            title="feat: add secure auth flow",
            body="Implements PKCE",
            draft=True,
            token="ghp_mock",
        )

        assert cr.provider == ProviderId.GITHUB
        assert cr.number_or_iid == "42"
        assert cr.web_url == "https://github.com/peldrun/sample-repo/pull/42"
        assert cr.source_branch == "feature/my-task"
        assert cr.target_branch == "main"
        assert cr.state == "open"

        # Verify sent payload contained draft flag
        sent_json = mock_req.call_args[1].get("json") or mock_req.call_args[0][2]
        assert sent_json["draft"] is True


@pytest.mark.asyncio
async def test_get_check_summary_aggregation(sample_repo_ref: RepositoryRef) -> None:
    """Verifies that check-runs and commit statuses are evaluated into overall state."""
    adapter = GitHubProviderAdapter()
    check_runs_payload = {
        "total_count": 2,
        "check_runs": [
            {
                "id": 101,
                "name": "lint",
                "status": "completed",
                "conclusion": "success",
                "html_url": "https://github.com/peldrun/sample-repo/runs/101",
            },
            {
                "id": 102,
                "name": "unit-tests",
                "status": "completed",
                "conclusion": "success",
                "html_url": "https://github.com/peldrun/sample-repo/runs/102",
            },
        ],
    }

    with patch.object(
        adapter.client,
        "request",
        new_callable=AsyncMock,
        return_value=MockHTTPResponse(status_code=200, json_data=check_runs_payload),
    ):
        summary = await adapter.get_check_summary(
            repo=sample_repo_ref,
            ref="a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
            token="ghp_mock",
        )

        assert summary.ref == "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
        assert summary.total_count == 2
        assert summary.success_count == 2
        assert summary.failure_count == 0
        assert summary.overall_state in {"success", "neutral"}


# ---------------------------------------------------------------------------
# 5. Error Normalization & Rate Limiting Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limited_error_translation(sample_repo_ref: RepositoryRef) -> None:
    """Ensures 403 response with zero rate limit remaining raises RateLimitedError."""
    adapter = GitHubProviderAdapter()
    rate_limit_headers = {
        "x-ratelimit-remaining": "0",
        "x-ratelimit-reset": "1700000000",
    }
    error_payload = {
        "message": "API rate limit exceeded for user ID 12345.",
        "documentation_url": "https://docs.github.com/rest/overview/resources-in-the-rest-api#rate-limiting",
    }

    with patch.object(
        adapter.client,
        "request",
        new_callable=AsyncMock,
        return_value=MockHTTPResponse(
            status_code=403, json_data=error_payload, headers=rate_limit_headers
        ),
    ):
        with pytest.raises(RateLimitedError) as exc_info:
            await adapter.get_repository("12345678", token="ghp_mock")

        assert "rate limit exceeded" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_auth_error_translation(sample_repo_ref: RepositoryRef) -> None:
    """Ensures 401 response from GitHub raises AuthRequiredError or AuthExpiredError."""
    adapter = GitHubProviderAdapter()
    error_payload = {"message": "Bad credentials"}

    with patch.object(
        adapter.client,
        "request",
        new_callable=AsyncMock,
        return_value=MockHTTPResponse(status_code=401, json_data=error_payload),
    ):
        with pytest.raises((AuthRequiredError, AuthExpiredError)):
            await adapter.list_repositories(token="ghp_invalid_token")


@pytest.mark.asyncio
async def test_not_found_error_translation(sample_repo_ref: RepositoryRef) -> None:
    """Ensures 404 response raises NotFoundError."""
    adapter = GitHubProviderAdapter()
    error_payload = {"message": "Not Found"}

    with patch.object(
        adapter.client,
        "request",
        new_callable=AsyncMock,
        return_value=MockHTTPResponse(status_code=404, json_data=error_payload),
    ):
        with pytest.raises(NotFoundError):
            await adapter.get_repository("unknown_repo_999", token="ghp_mock")


@pytest.mark.asyncio
async def test_conflict_and_validation_error_translation(
    sample_repo_ref: RepositoryRef,
) -> None:
    """Ensures 409 maps to ConflictError and 422 maps to ValidationError."""
    adapter = GitHubProviderAdapter()

    # 1. Test 409 Conflict
    with patch.object(
        adapter.client,
        "request",
        new_callable=AsyncMock,
        return_value=MockHTTPResponse(
            status_code=409, json_data={"message": "Reference already exists"}
        ),
    ):
        with pytest.raises(ConflictError):
            await adapter.create_branch(
                sample_repo_ref, "main", "abc", token="ghp_mock"
            )

    # 2. Test 422 Validation Error
    with patch.object(
        adapter.client,
        "request",
        new_callable=AsyncMock,
        return_value=MockHTTPResponse(
            status_code=422,
            json_data={
                "message": "Validation Failed",
                "errors": [{"resource": "PullRequest", "code": "custom"}],
            },
        ),
    ):
        with pytest.raises(ValidationError):
            await adapter.create_change_request(
                sample_repo_ref, "feature", "main", "title", token="ghp_mock"
            )
