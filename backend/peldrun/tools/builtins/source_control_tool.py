from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence
from pydantic import BaseModel, ConfigDict, Field

# Resilient imports for Peldrun tool foundation
try:
    from ..base import BaseTool
except (ImportError, ValueError):
    try:
        from backend.peldrun.tools.base import BaseTool
    except (ImportError, ValueError):
        class BaseTool:  # type: ignore[no-redef]
            name: str = "base_tool"
            description: str = ""
            args_schema: Optional[type[BaseModel]] = None

            async def aexecute(self, *args: Any, **kwargs: Any) -> Any:
                raise NotImplementedError

            def execute(self, *args: Any, **kwargs: Any) -> Any:
                raise NotImplementedError

try:
    from ..contract import ToolResult
except (ImportError, ValueError):
    try:
        from backend.peldrun.tools.contract import ToolResult
    except (ImportError, ValueError):
        @dataclass
        class ToolResult:  # type: ignore[no-redef]
            output: Any
            is_error: bool = False
            exit_code: int = 0
            metadata: dict[str, Any] = field(default_factory=dict)

# Resilient imports for source control specialized tools
try:
    from ...integrations.source_control.errors import (
        AuthExpiredError,
        AuthRequiredError,
        ConflictError,
        NotFoundError,
        PermissionDeniedError,
        RateLimitedError,
        RemoteStateChangedError,
        SourceControlError,
        ValidationError,
    )
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
except (ImportError, ValueError):
    try:
        from backend.integrations.source_control.errors import (
            AuthExpiredError,
            AuthRequiredError,
            ConflictError,
            NotFoundError,
            PermissionDeniedError,
            RateLimitedError,
            RemoteStateChangedError,
            SourceControlError,
            ValidationError,
        )
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
    except (ImportError, ValueError):
        class SourceControlError(Exception):  # type: ignore[no-redef]
            pass

        class PermissionDeniedError(SourceControlError):  # type: ignore[no-redef]
            pass

        class AuthRequiredError(SourceControlError):  # type: ignore[no-redef]
            pass

        class ValidationError(SourceControlError):  # type: ignore[no-redef]
            pass

        class NotFoundError(SourceControlError):  # type: ignore[no-redef]
            pass

        class ConflictError(SourceControlError):  # type: ignore[no-redef]
            pass

        class RateLimitedError(SourceControlError):  # type: ignore[no-redef]
            pass

        class RemoteStateChangedError(SourceControlError):  # type: ignore[no-redef]
            pass

        class AuthExpiredError(SourceControlError):  # type: ignore[no-redef]
            pass

        ListRepositoriesTool = None  # type: ignore[assignment, misc]
        ReadFileTool = None  # type: ignore[assignment, misc]
        CreateBranchTool = None  # type: ignore[assignment, misc]
        InspectDiffTool = None  # type: ignore[assignment, misc]
        RunChecksTool = None  # type: ignore[assignment, misc]
        RequestApprovalTool = None  # type: ignore[assignment, misc]
        PushBranchTool = None  # type: ignore[assignment, misc]
        CreateChangeRequestTool = None  # type: ignore[assignment, misc]


class SourceControlArgs(BaseModel):
    """Input argument schema for the unified source control agent tool."""

    model_config = ConfigDict(
        extra="allow",
        str_strip_whitespace=True,
    )

    action: str = Field(
        ...,
        description=(
            "The source control action to execute. Supported actions: "
            "'list_repositories', 'read_file', 'create_branch', 'inspect_diff', "
            "'run_checks', 'request_approval', 'push_branch', 'create_change_request'."
        ),
    )
    params: Optional[dict[str, Any]] = Field(
        default=None,
        description="Optional dictionary of parameters for the action. Parameters may also be passed directly as top-level fields.",
    )


class SourceControlTool(BaseTool):
    """Builtin core tool exposing governed source control capabilities to Peldrun AI agents."""

    name: str = "source_control"
    description: str = (
        "Unified interface for interacting with version control and remote Git repositories. "
        "Allows listing repositories, reading remote files, creating branches, inspecting diffs, "
        "checking CI status, requesting human approval, pushing approved commits, and creating pull requests. "
        "All remote write operations strictly require explicit human approval via approval tickets."
    )
    args_schema: type[BaseModel] = SourceControlArgs

    # Action alias dictionary for LLM resilience
    ACTION_ALIASES: Mapping[str, str] = {
        "list_repos": "list_repositories",
        "repos": "list_repositories",
        "search_repos": "list_repositories",
        "read": "read_file",
        "cat": "read_file",
        "branch": "create_branch",
        "new_branch": "create_branch",
        "diff": "inspect_diff",
        "status": "inspect_diff",
        "checks": "run_checks",
        "ci_status": "run_checks",
        "approve": "request_approval",
        "ask_approval": "request_approval",
        "push": "push_branch",
        "create_pr": "create_change_request",
        "open_pr": "create_change_request",
        "pr": "create_change_request",
    }

    def __init__(self, **tool_deps: Any) -> None:
        super().__init__()
        self._tool_deps = tool_deps
        self._instantiated_tools: dict[str, Any] = {}

    def _get_subtool(self, canonical_action: str) -> Any:
        """Lazily instantiates and caches the requested subtool."""
        if canonical_action in self._instantiated_tools:
            return self._instantiated_tools[canonical_action]

        tool_instance: Any = None
        if canonical_action == "list_repositories" and ListRepositoriesTool is not None:
            tool_instance = ListRepositoriesTool(**self._tool_deps)
        elif canonical_action == "read_file" and ReadFileTool is not None:
            tool_instance = ReadFileTool(**self._tool_deps)
        elif canonical_action == "create_branch" and CreateBranchTool is not None:
            tool_instance = CreateBranchTool(**self._tool_deps)
        elif canonical_action == "inspect_diff" and InspectDiffTool is not None:
            tool_instance = InspectDiffTool(**self._tool_deps)
        elif canonical_action == "run_checks" and RunChecksTool is not None:
            tool_instance = RunChecksTool(**self._tool_deps)
        elif canonical_action == "request_approval" and RequestApprovalTool is not None:
            tool_instance = RequestApprovalTool(**self._tool_deps)
        elif canonical_action == "push_branch" and PushBranchTool is not None:
            tool_instance = PushBranchTool(**self._tool_deps)
        elif canonical_action == "create_change_request" and CreateChangeRequestTool is not None:
            tool_instance = CreateChangeRequestTool(**self._tool_deps)

        if tool_instance is None:
            raise ValidationError(
                f"Source control subtool for action '{canonical_action}' is not installed or available."
            )

        self._instantiated_tools[canonical_action] = tool_instance
        return tool_instance

    def _normalize_action(self, action_str: str) -> str:
        """Resolves raw action name or alias into a canonical action identifier."""
        clean = action_str.strip().lower()
        return self.ACTION_ALIASES.get(clean, clean)

    async def aexecute(self, *args: Any, **kwargs: Any) -> ToolResult:
        """Asynchronously dispatches the action and returns a structured ToolResult."""
        # 1. Parse and validate arguments
        try:
            validated = self.args_schema(**kwargs)
        except Exception as exc:
            return ToolResult(
                output=f"Invalid arguments for source_control tool: {exc}",
                is_error=True,
                exit_code=1,
                metadata={"error_type": "ValidationError"},
            )

        raw_action = validated.action
        canonical_action = self._normalize_action(raw_action)

        # 2. Merge nested params and top-level extra parameters
        call_params: dict[str, Any] = {}
        for k, v in kwargs.items():
            if k not in {"action", "params"}:
                call_params[k] = v

        if validated.params and isinstance(validated.params, dict):
            call_params.update(validated.params)

        # 3. Resolve and execute subtool
        try:
            subtool = self._get_subtool(canonical_action)
            raw_output = await subtool.aexecute(**call_params)

            # Check if subtool reported an internal non-fatal failure
            if isinstance(raw_output, dict) and raw_output.get("status") == "error":
                return ToolResult(
                    output=raw_output.get("message", "Operation failed."),
                    is_error=True,
                    exit_code=1,
                    metadata={"action": canonical_action, "details": raw_output},
                )

            return ToolResult(
                output=raw_output,
                is_error=False,
                exit_code=0,
                metadata={"action": canonical_action, "status": "success"},
            )

        except (AuthRequiredError, AuthExpiredError) as exc:
            return ToolResult(
                output=f"Authentication Required: {exc}",
                is_error=True,
                exit_code=1,
                metadata={
                    "action": canonical_action,
                    "error_type": exc.__class__.__name__,
                    "details": getattr(exc, "details", None),
                },
            )
        except PermissionDeniedError as exc:
            return ToolResult(
                output=f"Permission Denied: {exc}",
                is_error=True,
                exit_code=1,
                metadata={
                    "action": canonical_action,
                    "error_type": "PermissionDeniedError",
                    "details": getattr(exc, "details", None),
                },
            )
        except RemoteStateChangedError as exc:
            return ToolResult(
                output=f"State Conflict: {exc}",
                is_error=True,
                exit_code=1,
                metadata={
                    "action": canonical_action,
                    "error_type": "RemoteStateChangedError",
                    "details": getattr(exc, "details", None),
                },
            )
        except ValidationError as exc:
            return ToolResult(
                output=f"Validation Error: {exc}",
                is_error=True,
                exit_code=1,
                metadata={
                    "action": canonical_action,
                    "error_type": "ValidationError",
                    "details": getattr(exc, "details", None),
                },
            )
        except NotFoundError as exc:
            return ToolResult(
                output=f"Not Found: {exc}",
                is_error=True,
                exit_code=1,
                metadata={
                    "action": canonical_action,
                    "error_type": "NotFoundError",
                    "details": getattr(exc, "details", None),
                },
            )
        except ConflictError as exc:
            return ToolResult(
                output=f"Conflict: {exc}",
                is_error=True,
                exit_code=1,
                metadata={
                    "action": canonical_action,
                    "error_type": "ConflictError",
                    "details": getattr(exc, "details", None),
                },
            )
        except SourceControlError as exc:
            return ToolResult(
                output=f"Source Control Error: {exc}",
                is_error=True,
                exit_code=1,
                metadata={
                    "action": canonical_action,
                    "error_type": exc.__class__.__name__,
                    "details": getattr(exc, "details", None),
                },
            )
        except Exception as exc:
            return ToolResult(
                output=f"Unexpected error executing '{canonical_action}': {exc}",
                is_error=True,
                exit_code=1,
                metadata={
                    "action": canonical_action,
                    "error_type": "InternalError",
                },
            )

    def execute(self, *args: Any, **kwargs: Any) -> ToolResult:
        """Synchronous invocation raising an error to enforce asynchronous execution."""
        raise NotImplementedError(
            "SourceControlTool only supports non-blocking asynchronous execution via aexecute()."
        )


def get_builtin_source_control_tool(**tool_deps: Any) -> SourceControlTool:
    """Factory creating a configured SourceControlTool instance for Core runtime."""
    return SourceControlTool(**tool_deps)
