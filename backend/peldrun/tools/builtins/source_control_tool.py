# backend/peldrun/tools/builtins/source_control_tool.py
"""
Builtin core tool exposing governed source-control capabilities to
PELDRUN agents.

The tool is a unified dispatcher: the model submits an ``action`` plus
optional ``params`` and the tool routes the call to the appropriate
integration-layer sub-tool. Sub-tools carry the actual business logic
and the security enforcement; this class only does dispatch, error
normalization, and result conversion.

Design invariants:
    - The class NEVER constructs its own sub-tools with a signature
      the sub-tool does not accept. Each action declares the set of
      constructor kwargs its sub-tool is willing to receive, and only
      those kwargs are forwarded.
    - A ``ToolContext`` is constructed from the caller's kwargs and
      passed to every sub-tool invocation. Without it, the sub-tools
      cannot look up the operation they belong to.
    - The sub-tool's ``ToolResult`` (a dataclass with ``success``,
      ``data``, ``error_code``, ``error_message``) is converted to the
      core ``ToolResult`` (a Pydantic model with ``is_error``,
      ``output``, ``metadata``) so the Web layer and the model see a
      single canonical shape.
    - Errors are classified by type, not by inspecting the message
      text, so the caller receives a stable ``error_code``.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Core tool foundation
# ---------------------------------------------------------------------------

try:
    from peldrun.tools.base import BaseTool
    from peldrun.tools.contract import ToolExecutionPolicy, ToolResult
except ImportError:  # repo layout: backend.peldrun.*
    from backend.peldrun.tools.base import BaseTool
    from backend.peldrun.tools.contract import ToolExecutionPolicy, ToolResult


# ---------------------------------------------------------------------------
# Integration-layer errors and sub-tools
# ---------------------------------------------------------------------------

try:
    from integrations.source_control.errors import (
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
    from integrations.source_control.tools import (
        CreateBranchTool,
        CreateChangeRequestTool,
        InspectDiffTool,
        ListRepositoriesTool,
        PushBranchTool,
        ReadFileTool,
        RequestApprovalTool,
        RunChecksTool,
    )
    from integrations.source_control.tools.base import (
        SourceControlTool as IntegrationSourceControlTool,
        ToolContext as IntegrationToolContext,
        ToolResult as IntegrationToolResult,
    )
except ImportError:  # repo layout: backend.integrations.*
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
    from backend.integrations.source_control.tools.base import (
        SourceControlTool as IntegrationSourceControlTool,
        ToolContext as IntegrationToolContext,
        ToolResult as IntegrationToolResult,
    )


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Action map: canonical action -> (tool class, accepted constructor kwargs)
# ---------------------------------------------------------------------------
#
# Each integration sub-tool declares its own set of optional constructor
# kwargs (registry, token_service, workspace_manager, ...). Forwarding the
# union of every dependency to every sub-tool raises TypeError because a
# sub-tool rejects kwargs it does not recognize. The mapping below makes
# the accepted surface explicit and lets the dispatcher filter cleanly.

_ACTION_MAP: dict[str, tuple[type, frozenset[str]]] = {
    "list_repositories": (
        ListRepositoriesTool,
        frozenset({"registry", "token_service", "audit_logger"}),
    ),
    "read_file": (
        ReadFileTool,
        frozenset({"registry", "token_service", "audit_logger"}),
    ),
    "create_branch": (
        CreateBranchTool,
        frozenset({"workspace_manager", "branch_manager", "audit_logger"}),
    ),
    "inspect_diff": (
        InspectDiffTool,
        frozenset({"workspace_manager", "diff_engine", "audit_logger"}),
    ),
    "run_checks": (
        RunChecksTool,
        frozenset(
            {
                "workspace_manager",
                "check_registry",
                "path_policy",
                "audit_logger",
            }
        ),
    ),
    "request_approval": (
        RequestApprovalTool,
        frozenset({"operation_service", "approval_gate", "audit_logger"}),
    ),
    "push_branch": (
        PushBranchTool,
        frozenset(
            {
                "operation_service",
                "approval_gate",
                "push_manager",
                "workspace_manager",
                "token_service",
                "audit_logger",
            }
        ),
    ),
    "create_change_request": (
        CreateChangeRequestTool,
        frozenset(
            {
                "operation_service",
                "approval_gate",
                "registry",
                "token_service",
                "audit_logger",
            }
        ),
    ),
}

#: Aliases the model may emit; each maps to a canonical action name.
_ACTION_ALIASES: Mapping[str, str] = {
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


# ---------------------------------------------------------------------------
# Input schema
# ---------------------------------------------------------------------------

class SourceControlArgs(BaseModel):
    """Input argument schema for the unified source-control tool."""

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    action: str = Field(
        ...,
        description=(
            "The source-control action to execute. Canonical actions: "
            "'list_repositories', 'read_file', 'create_branch', "
            "'inspect_diff', 'run_checks', 'request_approval', "
            "'push_branch', 'create_change_request'."
        ),
    )
    params: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "Optional dictionary of parameters for the action. Parameters "
            "may also be passed directly as top-level fields."
        ),
    )


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------

class SourceControlTool(BaseTool):
    """Unified dispatcher exposing governed source-control actions."""

    name: str = "source_control"
    description: str = (
        "Unified interface for interacting with version control and remote "
        "Git repositories. Allows listing repositories, reading remote "
        "files, creating branches, inspecting diffs, checking CI status, "
        "requesting human approval, pushing approved commits, and creating "
        "pull requests. All remote write operations strictly require "
        "explicit human approval via approval tickets."
    )
    args_schema: type[BaseModel] = SourceControlArgs

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(
        self,
        *,
        workspace_root: Optional[str] = None,
        execution_policy: Optional[ToolExecutionPolicy] = None,
        **tool_deps: Any,
    ) -> None:
        # Forward BaseTool-specific kwargs; keep the rest for sub-tools.
        super().__init__(
            workspace_root=workspace_root,
            execution_policy=execution_policy,
        )
        self._tool_deps: dict[str, Any] = dict(tool_deps)
        self._instantiated_tools: dict[str, IntegrationSourceControlTool] = {}

    # ------------------------------------------------------------------
    # Sub-tool resolution
    # ------------------------------------------------------------------

    def _get_subtool(
        self, canonical_action: str
    ) -> IntegrationSourceControlTool:
        """
        Instantiate and cache the sub-tool for ``canonical_action``.

        The sub-tool receives only the constructor kwargs it accepts;
        unrelated dependencies are filtered out to avoid ``TypeError``.
        """
        cached = self._instantiated_tools.get(canonical_action)
        if cached is not None:
            return cached

        entry = _ACTION_MAP.get(canonical_action)
        if entry is None:
            raise ValidationError(
                f"Unknown source-control action: '{canonical_action}'.",
                details={
                    "action": canonical_action,
                    "supported": sorted(_ACTION_MAP.keys()),
                },
            )

        tool_class, accepted_deps = entry
        filtered_deps = {
            key: value
            for key, value in self._tool_deps.items()
            if key in accepted_deps
        }

        instance = tool_class(**filtered_deps)
        self._instantiated_tools[canonical_action] = instance
        return instance

    @staticmethod
    def _normalize_action(action_str: str) -> str:
        clean = action_str.strip().lower()
        return _ACTION_ALIASES.get(clean, clean)

    # ------------------------------------------------------------------
    # Result canonicalization
    # ------------------------------------------------------------------

    @staticmethod
    def _to_core_result(
        integration_result: Any,
        *,
        action: str,
    ) -> ToolResult:
        """
        Convert an integration-layer ``ToolResult`` (dataclass) into the
        core ``ToolResult`` (Pydantic). If the value is already a core
        result, return it unchanged.
        """
        if isinstance(integration_result, ToolResult):
            return integration_result

        # Integration result: success / data / error_code / error_message
        success = bool(getattr(integration_result, "success", False))
        data = getattr(integration_result, "data", None)
        error_code = getattr(integration_result, "error_code", None)
        error_message = getattr(integration_result, "error_message", None)
        extra_meta = dict(getattr(integration_result, "metadata", {}) or {})

        if success:
            metadata = {"action": action, "status": "success"}
            metadata.update(extra_meta)
            return ToolResult(
                output=data,
                exit_code=0,
                is_error=False,
                metadata=metadata,
            )

        metadata = {"action": action, "status": "error"}
        if error_code:
            metadata.setdefault("error_code", error_code)
        metadata.update(extra_meta)

        return ToolResult(
            output=error_message or "Source-control operation failed.",
            exit_code=1,
            is_error=True,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def _arun(self, **kwargs: Any) -> ToolResult:
        """
        Core dispatch logic.

        ``BaseTool.aexecute`` is the public entry point. It validates
        the outer envelope against ``args_schema`` (which rejects a
        missing ``action`` and any additionalProperties violation),
        converts the validated model to a dict via ``model_dump()``,
        and calls this method. Therefore ``kwargs`` here contains:

            - ``action``: the canonical or aliased action name.
            - ``params``: an optional dict of nested parameters.
            - any extra top-level keys the caller supplied, because
              the schema declares ``extra="allow"``.

        This method MUST NOT raise. Every failure becomes a
        ``ToolResult`` with a stable ``error_code`` so the caller can
        branch on the code without parsing free text.
        """
        # ``BaseTool.aexecute`` guarantees ``action`` is present; we
        # still normalize defensively.
        raw_action = str(kwargs.get("action") or "").strip()
        canonical_action = self._normalize_action(raw_action)

        # Merge nested params with top-level extras. Explicit params
        # take precedence over same-named top-level extras so callers
        # can always override via the structured form.
        call_params: dict[str, Any] = {
            key: value
            for key, value in kwargs.items()
            if key not in {"action", "params"}
        }
        nested_params = kwargs.get("params")
        if isinstance(nested_params, dict):
            call_params.update(nested_params)

        # 3. Build the ToolContext the sub-tools require.
        context = IntegrationToolContext(
            operation_id=call_params.pop("operation_id", None),
            run_id=call_params.pop("run_id", None),
            actor=call_params.pop("actor", "agent"),
            metadata=call_params.pop("context_metadata", {}) or {},
        )

        # 4. Dispatch.
        try:
            subtool = self._get_subtool(canonical_action)
            integration_result = await subtool.run(context, **call_params)
            return self._to_core_result(
                integration_result, action=canonical_action
            )

        except (AuthRequiredError, AuthExpiredError) as exc:
            return ToolResult(
                output=f"Authentication Required: {exc}",
                is_error=True,
                exit_code=1,
                metadata={
                    "action": canonical_action,
                    "error_type": type(exc).__name__,
                    "error_code": "AUTH_REQUIRED",
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
                    "error_code": "PERMISSION_DENIED",
                },
            )
        except RemoteStateChangedError as exc:
            return ToolResult(
                output=f"Remote State Changed: {exc}",
                is_error=True,
                exit_code=1,
                metadata={
                    "action": canonical_action,
                    "error_type": "RemoteStateChangedError",
                    "error_code": "REMOTE_STATE_CHANGED",
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
                    "error_code": "NOT_FOUND",
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
                    "error_code": "CONFLICT",
                },
            )
        except RateLimitedError as exc:
            return ToolResult(
                output=f"Rate Limited: {exc}",
                is_error=True,
                exit_code=1,
                metadata={
                    "action": canonical_action,
                    "error_type": "RateLimitedError",
                    "error_code": "RATE_LIMITED",
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
                    "error_code": "VALIDATION_ERROR",
                },
            )
        except SourceControlError as exc:
            return ToolResult(
                output=f"Source Control Error: {exc}",
                is_error=True,
                exit_code=1,
                metadata={
                    "action": canonical_action,
                    "error_type": type(exc).__name__,
                    "error_code": "SOURCE_CONTROL_ERROR",
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Unexpected error executing source-control action '%s'",
                canonical_action,
            )
            return ToolResult(
                output=(
                    f"Unexpected error executing '{canonical_action}': {exc}"
                ),
                is_error=True,
                exit_code=1,
                metadata={
                    "action": canonical_action,
                    "error_type": "InternalError",
                },
            )

    def execute(self, *args: Any, **kwargs: Any) -> ToolResult:
        """Synchronous execution is intentionally unsupported."""
        raise NotImplementedError(
            "SourceControlTool only supports non-blocking asynchronous "
            "execution via aexecute()."
        )


def get_builtin_source_control_tool(**tool_deps: Any) -> SourceControlTool:
    """Factory creating a configured ``SourceControlTool`` for Core runtime."""
    return SourceControlTool(**tool_deps)


__all__ = ["SourceControlTool", "SourceControlArgs", "get_builtin_source_control_tool"]
