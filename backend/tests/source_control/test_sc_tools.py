# backend/tests/source_control/test_sc_tools.py
"""
Tests for the unified ``SourceControlTool`` Core dispatcher.

The dispatcher is the agent-facing entry point for source-control
operations. It does NOT contain business logic; it validates the
request envelope, routes to the appropriate integration sub-tool, and
converts the sub-tool's result into the canonical Core ``ToolResult``.

Why this file exists and what it tests
--------------------------------------

Earlier iterations of this test module asserted the existence of a
module named ``backend.omweb.tools.source_control_tools`` that exposed
free functions (``read_file_tool``, ``create_branch_tool``, ...).

That module does not exist and does not belong in the architecture:

    * ``omweb/tools/`` (the Web layer) discovers tools via JSON
      manifests under ``omweb/tools/builtins/*.json``. It does NOT
      host Python tool implementations.
    * The real dispatcher lives at
      ``peldrun/tools/builtins/source_control_tool.py`` and is invoked
      by the Core runtime (``peldrun.tools.registry``) through
      ``BaseTool.aexecute``.

This file therefore tests the real dispatcher:

    * Envelope validation (missing fields, malformed ``action``).
    * Action resolution (canonical names and aliases).
    * Parameter merging (``params`` dict + top-level extras).
    * ``ToolContext`` construction (operation_id, run_id, actor).
    * Result conversion (integration ``ToolResult`` → Core ``ToolResult``).
    * Error conversion (``SourceControlError`` subclasses → stable codes).
    * Real sub-tool dispatch (without mocks) for the two simplest
      actions that fail fast without network access.
    * Async-only enforcement.

The integration-layer sub-tools themselves are tested in
``test_facade_integration`` and ``test_facade_remote_flows``. This
module focuses on the dispatcher's orchestration contract.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

# The dispatcher under test.
#
# The dispatcher lives at ``peldrun/tools/builtins/source_control_tool.py``
# and internally imports from ``peldrun.tools.*`` and
# ``integrations.source_control.*``. Those are the canonical trees
# that the production runtime (``backend/`` on sys.path) uses. The
# test imports from the SAME trees so that ``ToolResult``,
# ``ToolContext``, and every exception class are the same objects the
# dispatcher produces and catches.
#
# Importing via ``backend.peldrun.tools.*`` or
# ``backend.integrations.source_control.*`` would create a second,
# incompatible copy of each class, and every ``isinstance`` and
# ``except`` check would silently fail.

from peldrun.tools.builtins.source_control_tool import (
    SourceControlArgs,
    SourceControlTool,
    get_builtin_source_control_tool,
)
from peldrun.tools.contract import ToolResult

from integrations.source_control.errors import (
    AuthExpiredError,
    AuthRequiredError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitedError,
    RemoteStateChangedError,
    SourceControlError,
    ValidationError as IntegrationValidationError,
)
from integrations.source_control.tools.base import (
    ToolContext as IntegrationToolContext,
    ToolResult as IntegrationToolResult,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tool() -> SourceControlTool:
    """A fresh dispatcher with no injected sub-tool dependencies."""
    return SourceControlTool()


@pytest.fixture
def mocked_subtool() -> AsyncMock:
    """
    A mocked integration-layer sub-tool.

    ``run`` is the async method the dispatcher calls; each test sets
    its return value or side effect.
    """
    sub = AsyncMock()
    sub.run = AsyncMock()
    return sub


@pytest.fixture
def mocked_dispatch(tool: SourceControlTool, mocked_subtool: AsyncMock):
    """
    Patch ``tool._get_subtool`` to always return ``mocked_subtool``.

    Yields the ``(tool, subtool)`` pair so tests can assert on the
    mock without repeating the patch boilerplate.
    """
    with patch.object(tool, "_get_subtool", return_value=mocked_subtool):
        yield tool, mocked_subtool


# ---------------------------------------------------------------------------
# 1. Construction & schema
# ---------------------------------------------------------------------------


def test_tool_metadata_is_declared() -> None:
    """The dispatcher declares its name, description, and args schema."""
    tool = SourceControlTool()
    assert tool.name == "source_control"
    assert isinstance(tool.description, str)
    assert len(tool.description) > 0
    assert tool.args_schema is SourceControlArgs


def test_factory_returns_configured_instance() -> None:
    """The public factory wires the dispatcher for Core runtime."""
    instance = get_builtin_source_control_tool()
    assert isinstance(instance, SourceControlTool)
    assert instance.name == "source_control"


def test_args_schema_requires_action() -> None:
    """``action`` is mandatory; the schema rejects an empty payload."""
    with pytest.raises(Exception):
        SourceControlArgs()  # type: ignore[call-arg]

    parsed = SourceControlArgs(action="list_repositories")
    assert parsed.action == "list_repositories"
    assert parsed.params is None


def test_args_schema_accepts_params_and_extras() -> None:
    """``params`` and top-level extras are both accepted."""
    parsed = SourceControlArgs(
        action="read_file",
        params={"file_path": "src/main.py"},
        repository_id="repo_1",
        provider="github",
    )
    assert parsed.action == "read_file"
    assert parsed.params == {"file_path": "src/main.py"}

    extras = getattr(parsed, "__pydantic_extra__", None) or {}
    assert extras.get("repository_id") == "repo_1"
    assert extras.get("provider") == "github"


# ---------------------------------------------------------------------------
# 2. Envelope validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_action_returns_error_result(
    tool: SourceControlTool,
) -> None:
    """
    No ``action`` → the schema raises; the dispatcher converts the
    exception into a failed ToolResult (it never lets the exception
    escape into the model's context).
    """
    result = await tool.aexecute()
    assert isinstance(result, ToolResult)
    assert result.is_error is True
    assert result.metadata.get("error_type") == "ValidationError"


@pytest.mark.asyncio
async def test_unknown_action_returns_error_result(
    tool: SourceControlTool,
) -> None:
    """
    An unknown canonical action fails inside ``_get_subtool`` with an
    integration ``ValidationError``, which the dispatcher converts to
    a failed ToolResult.
    """
    result = await tool.aexecute(action="not_a_real_action")
    assert isinstance(result, ToolResult)
    assert result.is_error is True
    assert result.metadata.get("error_type") == "ValidationError"
    assert result.metadata.get("error_code") == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# 3. Action resolution & aliases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_canonical_action_dispatches(mocked_dispatch) -> None:
    """A canonical action routes to the sub-tool exactly once."""
    tool, subtool = mocked_dispatch
    subtool.run.return_value = IntegrationToolResult(
        success=True, data={"repos": []}
    )

    result = await tool.aexecute(action="list_repositories")

    assert result.is_error is False
    subtool.run.assert_awaited_once()
    ctx = subtool.run.call_args.args[0]
    assert isinstance(ctx, IntegrationToolContext)


@pytest.mark.asyncio
async def test_alias_is_resolved(mocked_dispatch) -> None:
    """``pr`` is an alias for ``create_change_request``."""
    tool, subtool = mocked_dispatch
    subtool.run.return_value = IntegrationToolResult(
        success=True, data={"number_or_iid": "42"}
    )

    result = await tool.aexecute(
        action="pr",
        operation_id="op_test",
        approval_id="approval_test",
        title="Test PR",
    )

    assert result.is_error is False
    # The Core result records the canonical action, not the alias.
    assert result.metadata.get("action") == "create_change_request"
    subtool.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_action_is_normalized_case_and_whitespace(
    mocked_dispatch,
) -> None:
    """Extra whitespace and mixed case in ``action`` are normalized."""
    tool, subtool = mocked_dispatch
    subtool.run.return_value = IntegrationToolResult(
        success=True, data={}
    )

    result = await tool.aexecute(action="  LIST_REPOSITORIES  ")
    assert result.is_error is False
    assert result.metadata.get("action") == "list_repositories"


# ---------------------------------------------------------------------------
# 4. Parameter merging
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_params_dict_is_merged_with_extras(
    mocked_dispatch,
) -> None:
    """Both ``params`` and top-level extras reach the sub-tool call."""
    tool, subtool = mocked_dispatch
    subtool.run.return_value = IntegrationToolResult(
        success=True, data={}
    )

    await tool.aexecute(
        action="read_file",
        params={"file_path": "src/main.py"},
        repository_id="repo_1",
        provider="github",
    )

    subtool.run.assert_awaited_once()
    kwargs = subtool.run.call_args.kwargs
    assert kwargs["file_path"] == "src/main.py"
    assert kwargs["repository_id"] == "repo_1"
    assert kwargs["provider"] == "github"


@pytest.mark.asyncio
async def test_params_override_top_level_extras(
    mocked_dispatch,
) -> None:
    """Values inside ``params`` win over same-named top-level extras."""
    tool, subtool = mocked_dispatch
    subtool.run.return_value = IntegrationToolResult(
        success=True, data={}
    )

    await tool.aexecute(
        action="read_file",
        params={"file_path": "from_params.py"},
        file_path="from_kwargs.py",
    )

    kwargs = subtool.run.call_args.kwargs
    assert kwargs["file_path"] == "from_params.py"


# ---------------------------------------------------------------------------
# 5. ToolContext construction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_fields_are_extracted_from_kwargs(
    mocked_dispatch,
) -> None:
    """
    ``operation_id`` / ``run_id`` / ``actor`` / ``context_metadata``
    are lifted into the ToolContext and are NOT forwarded to the
    sub-tool as plain kwargs.
    """
    tool, subtool = mocked_dispatch
    subtool.run.return_value = IntegrationToolResult(
        success=True, data={}
    )

    await tool.aexecute(
        action="request_approval",
        operation_id="op_abc",
        run_id="run_xyz",
        actor="user",
        context_metadata={"note": "hello"},
    )

    subtool.run.assert_awaited_once()
    ctx = subtool.run.call_args.args[0]
    assert isinstance(ctx, IntegrationToolContext)
    assert ctx.operation_id == "op_abc"
    assert ctx.run_id == "run_xyz"
    assert ctx.actor == "user"
    assert ctx.metadata == {"note": "hello"}

    kwargs = subtool.run.call_args.kwargs
    for field in ("operation_id", "run_id", "actor", "context_metadata"):
        assert field not in kwargs


@pytest.mark.asyncio
async def test_context_defaults_are_applied(mocked_dispatch) -> None:
    """
    When context fields are absent, sensible defaults are used
    (``actor="agent"``, ``metadata={}``).
    """
    tool, subtool = mocked_dispatch
    subtool.run.return_value = IntegrationToolResult(
        success=True, data={}
    )

    await tool.aexecute(action="list_repositories")

    ctx = subtool.run.call_args.args[0]
    assert ctx.operation_id is None
    assert ctx.run_id is None
    assert ctx.actor == "agent"
    assert ctx.metadata == {}


# ---------------------------------------------------------------------------
# 6. Result conversion (integration → core)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_result_is_converted(mocked_dispatch) -> None:
    """A successful integration result becomes a successful Core result."""
    tool, subtool = mocked_dispatch
    subtool.run.return_value = IntegrationToolResult(
        success=True, data={"items": [1, 2, 3]}
    )

    result = await tool.aexecute(action="list_repositories")

    assert result.is_error is False
    assert result.exit_code == 0
    assert result.output == {"items": [1, 2, 3]}
    assert result.metadata.get("status") == "success"
    assert result.metadata.get("action") == "list_repositories"


@pytest.mark.asyncio
async def test_failure_result_is_converted(mocked_dispatch) -> None:
    """A failed integration result becomes a failed Core result."""
    tool, subtool = mocked_dispatch
    subtool.run.return_value = IntegrationToolResult(
        success=False,
        error_code="NOT_FOUND",
        error_message="Repo does not exist.",
    )

    result = await tool.aexecute(
        action="read_file", repository_id="r1", file_path="x.py"
    )

    assert result.is_error is True
    assert result.exit_code == 1
    assert "Repo does not exist" in str(result.output)
    assert result.metadata.get("error_code") == "NOT_FOUND"
    assert result.metadata.get("status") == "error"


# ---------------------------------------------------------------------------
# 7. Exception handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc_factory,expected_code",
    [
        (lambda: AuthRequiredError("no creds"), "AUTH_REQUIRED"),
        (lambda: AuthExpiredError("token expired"), "AUTH_REQUIRED"),
        (lambda: PermissionDeniedError("denied"), "PERMISSION_DENIED"),
        (lambda: NotFoundError("missing"), "NOT_FOUND"),
        (lambda: ConflictError("conflict"), "CONFLICT"),
        (lambda: RemoteStateChangedError("remote moved"), "REMOTE_STATE_CHANGED"),
        (lambda: RateLimitedError("rate limited"), "RATE_LIMITED"),
        (lambda: IntegrationValidationError("bad input"), "VALIDATION_ERROR"),
    ],
)
async def test_source_control_exceptions_are_mapped(
    mocked_dispatch,
    exc_factory,
    expected_code: str,
) -> None:
    """
    Each SourceControlError subclass maps to a stable ``error_code``,
    so callers can branch on the code without parsing free text.
    """
    tool, subtool = mocked_dispatch
    subtool.run.side_effect = exc_factory()

    result = await tool.aexecute(action="read_file")

    assert result.is_error is True
    assert result.metadata.get("error_code") == expected_code
    assert result.metadata.get("action") == "read_file"


@pytest.mark.asyncio
async def test_generic_source_control_error_is_mapped(
    mocked_dispatch,
) -> None:
    """A bare ``SourceControlError`` falls back to a generic code."""
    tool, subtool = mocked_dispatch
    subtool.run.side_effect = SourceControlError("something broke")

    result = await tool.aexecute(action="read_file")

    assert result.is_error is True
    assert result.metadata.get("error_code") == "SOURCE_CONTROL_ERROR"


@pytest.mark.asyncio
async def test_unexpected_exception_is_wrapped(mocked_dispatch) -> None:
    """A non-domain exception is wrapped as ``InternalError``."""
    tool, subtool = mocked_dispatch
    subtool.run.side_effect = RuntimeError("kaboom")

    result = await tool.aexecute(action="read_file")

    assert result.is_error is True
    assert result.metadata.get("error_type") == "InternalError"
    assert "kaboom" in str(result.output)


# ---------------------------------------------------------------------------
# 8. Real sub-tool dispatch (no mock)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_dispatch_create_branch_fails_fast_on_missing_workspace() -> None:
    """
    End-to-end: the dispatcher instantiates the REAL
    ``CreateBranchTool``, which fails fast because the workspace is
    not registered. This proves the sub-tool is wired correctly and
    that failures are converted into a failed ToolResult.
    """
    tool = SourceControlTool()
    result = await tool.aexecute(
        action="create_branch",
        workspace_id="ws_does_not_exist",
        branch_name="feature/x",
    )

    assert result.is_error is True
    # The default workspace manager has no entries.
    assert result.metadata.get("error_code") in {
        "WORKSPACE_NOT_FOUND",
        "VALIDATION_ERROR",
    }


@pytest.mark.asyncio
async def test_real_dispatch_list_repositories_fails_without_auth() -> None:
    """
    End-to-end: the dispatcher instantiates the REAL
    ``ListRepositoriesTool``, which fails because no provider is
    registered in the default registry (or no credential is stored).

    Either failure is a valid ToolResult — the point is that the
    dispatcher does NOT raise and DOES set an error code.
    """
    tool = SourceControlTool()
    result = await tool.aexecute(
        action="list_repositories", provider="github"
    )

    assert result.is_error is True
    assert result.metadata.get("error_code") in {
        "AUTH_REQUIRED",
        "NOT_FOUND",
    }


# ---------------------------------------------------------------------------
# 9. Async-only enforcement
# ---------------------------------------------------------------------------


def test_execute_is_not_supported(tool: SourceControlTool) -> None:
    """Synchronous execution is intentionally unsupported."""
    with pytest.raises(NotImplementedError):
        tool.execute(action="list_repositories")
