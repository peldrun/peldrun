# backend/integrations/source_control/tools/__init__.py
"""
Built-in source-control tool registration.

This module is the single place where the built-in tools are
registered with the `ToolRegistry`. Application startup calls
`register_builtin_tools()` once, after the registry has been
constructed and before the agent runtime starts.

Design rules:
    - The module MUST NOT import any concrete tool at import time.
      Tools are imported lazily inside factory functions. This lets
      the module be imported even when some tools are not yet
      implemented, and it avoids circular imports (concrete tools
      depend on the base module, not on this one).
    - Registration MUST be idempotent by default. Calling
      `register_builtin_tools()` twice MUST NOT raise and MUST NOT
      duplicate tools. The optional `overwrite=True` flag replaces
      existing registrations, which is useful for tests.
    - A failure to import or construct a single tool MUST NOT
      prevent the application from starting. The failure is logged
      and the remaining tools are registered.
    - The set of built-in tool names is exposed as
      `BUILTIN_TOOL_NAMES` for callers that need to enumerate them
      without instantiating the tools.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..errors import ValidationError
from .base import (
    TOOL_NAME_PREFIX,
    SourceControlTool,
    ToolRegistry,
    get_default_tool_registry,
)

__all__ = [
    "register_builtin_tools",
    "unregister_builtin_tools",
    "register_tool",
    "unregister_tool",
    "list_builtin_tool_names",
    "list_registered_tools",
    "describe_tools",
    "BUILTIN_TOOL_NAMES",
]


logger = logging.getLogger(__name__)


#: Names of every tool that ships with PELDRUN. The list mirrors the
#: tool table in the source-control integration reference. It is
#: exposed as a tuple so that callers cannot mutate the canonical
#: list.
BUILTIN_TOOL_NAMES: tuple[str, ...] = (
    "source_control.list_providers",
    "source_control.list_repositories",
    "source_control.get_repository",
    "source_control.read_file",
    "source_control.list_branches",
    "source_control.create_workspace",
    "source_control.create_branch",
    "source_control.get_diff",
    "source_control.run_checks",
    "source_control.request_approval",
    "source_control.push_branch",
    "source_control.create_change_request",
)


# ---------------------------------------------------------------------------
# Lazy factory functions
# ---------------------------------------------------------------------------
#
# Each factory performs its import inside the function body and
# returns a fully constructed tool. When the target module does not
# exist yet, the factory raises `NotImplementedError` with a clear
# message; `register_builtin_tools` catches this and records the tool
# as skipped. Subclasses that are introduced later MUST replace the
# corresponding factory body; the rest of the file remains unchanged.


def _build_list_providers() -> SourceControlTool:
    try:
        from .list_providers import ListProvidersTool  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NotImplementedError(
            "The 'list_providers' tool is not implemented yet."
        ) from exc
    return ListProvidersTool()


def _build_list_repositories() -> SourceControlTool:
    try:
        from .list_repositories import ListRepositoriesTool  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NotImplementedError(
            "The 'list_repositories' tool is not implemented yet."
        ) from exc
    return ListRepositoriesTool()


def _build_get_repository() -> SourceControlTool:
    try:
        from .get_repository import GetRepositoryTool  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NotImplementedError(
            "The 'get_repository' tool is not implemented yet."
        ) from exc
    return GetRepositoryTool()


def _build_read_file() -> SourceControlTool:
    try:
        from .read_file import ReadFileTool  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NotImplementedError(
            "The 'read_file' tool is not implemented yet."
        ) from exc
    return ReadFileTool()


def _build_list_branches() -> SourceControlTool:
    try:
        from .list_branches import ListBranchesTool  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NotImplementedError(
            "The 'list_branches' tool is not implemented yet."
        ) from exc
    return ListBranchesTool()


def _build_create_workspace() -> SourceControlTool:
    try:
        from .create_workspace import CreateWorkspaceTool  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NotImplementedError(
            "The 'create_workspace' tool is not implemented yet."
        ) from exc
    return CreateWorkspaceTool()


def _build_create_branch() -> SourceControlTool:
    try:
        from .create_branch import CreateBranchTool  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NotImplementedError(
            "The 'create_branch' tool is not implemented yet."
        ) from exc
    return CreateBranchTool()


def _build_get_diff() -> SourceControlTool:
    try:
        from .inspect_diff import InspectDiffTool  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NotImplementedError(
            "The 'get_diff' tool is not implemented yet."
        ) from exc
    return InspectDiffTool()


def _build_run_checks() -> SourceControlTool:
    try:
        from .run_checks import RunChecksTool  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NotImplementedError(
            "The 'run_checks' tool is not implemented yet."
        ) from exc
    return RunChecksTool()


def _build_request_approval() -> SourceControlTool:
    try:
        from .request_approval import RequestApprovalTool  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NotImplementedError(
            "The 'request_approval' tool is not implemented yet."
        ) from exc
    return RequestApprovalTool()


def _build_push_branch() -> SourceControlTool:
    try:
        from .push_branch import PushBranchTool  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NotImplementedError(
            "The 'push_branch' tool is not implemented yet."
        ) from exc
    return PushBranchTool()


def _build_create_change_request() -> SourceControlTool:
    try:
        from .create_change_request import (  # type: ignore[import-not-found]
            CreateChangeRequestTool,
        )
    except ImportError as exc:
        raise NotImplementedError(
            "The 'create_change_request' tool is not implemented yet."
        ) from exc
    return CreateChangeRequestTool()


#: Ordered mapping from canonical tool name to a zero-argument
#: factory. The insertion order defines the registration order.
_TOOL_FACTORIES: Dict[str, Callable[[], SourceControlTool]] = {
    "source_control.list_providers": _build_list_providers,
    "source_control.list_repositories": _build_list_repositories,
    "source_control.get_repository": _build_get_repository,
    "source_control.read_file": _build_read_file,
    "source_control.list_branches": _build_list_branches,
    "source_control.create_workspace": _build_create_workspace,
    "source_control.create_branch": _build_create_branch,
    "source_control.get_diff": _build_get_diff,
    "source_control.run_checks": _build_run_checks,
    "source_control.request_approval": _build_request_approval,
    "source_control.push_branch": _build_push_branch,
    "source_control.create_change_request": _build_create_change_request,
}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_tool(
    tool: SourceControlTool,
    *,
    registry: Optional[ToolRegistry] = None,
    overwrite: bool = False,
) -> bool:
    """
    Register a single tool instance.

    Returns True when the tool was registered or replaced, False
    when registration was skipped because the name already existed.

    Raises:
        TypeError: If `tool` is not a `SourceControlTool`.
        ValidationError: If the tool's name is malformed.
    """
    target = registry or get_default_tool_registry()
    if not isinstance(tool, SourceControlTool):
        raise TypeError(
            "register_tool expects a SourceControlTool instance."
        )
    name = tool.name
    if not name or not name.startswith(TOOL_NAME_PREFIX):
        raise ValidationError(
            f"Tool name must start with '{TOOL_NAME_PREFIX}'.",
            details={"tool_name": name},
        )

    if target.get(name) is not None and not overwrite:
        logger.debug(
            "Tool '%s' is already registered; skipping.", name
        )
        return False

    target.register(tool, overwrite=overwrite)
    logger.info("Registered source-control tool '%s'.", name)
    return True


def unregister_tool(
    name: str,
    *,
    registry: Optional[ToolRegistry] = None,
) -> bool:
    """
    Unregister a single tool by name.

    Returns True when a tool was removed, False when nothing was
    registered under that name.
    """
    target = registry or get_default_tool_registry()
    return target.unregister(name)


def register_builtin_tools(
    *,
    registry: Optional[ToolRegistry] = None,
    overwrite: bool = False,
    tools: Optional[Sequence[str]] = None,
) -> Dict[str, List[str]]:
    """
    Register every built-in tool.

    The function is idempotent by default: calling it twice has the
    same effect as calling it once. Tools whose module is not yet
    implemented are skipped with a log entry; the remaining tools
    are registered normally.

    Args:
        registry: The target registry. When `None`, the process-wide
            default registry is used.
        overwrite: When True, existing registrations are replaced.
            Intended for tests.
        tools: Optional subset of tool names to register. When
            `None`, every tool in `BUILTIN_TOOL_NAMES` is processed.

    Returns:
        A mapping with three keys:

            - `"registered"`: names of tools that were registered.
            - `"skipped"`   : names of tools that were already
                              registered and not overwritten.
            - `"failed"`    : names of tools whose factory raised.
    """
    target = registry or get_default_tool_registry()
    selected = list(tools) if tools else list(_TOOL_FACTORIES.keys())

    registered: List[str] = []
    skipped: List[str] = []
    failed: List[str] = []

    for tool_name in selected:
        factory = _TOOL_FACTORIES.get(tool_name)
        if factory is None:
            logger.warning(
                "No built-in factory is registered for tool '%s'.",
                tool_name,
            )
            failed.append(tool_name)
            continue

        already_registered = target.get(tool_name) is not None

        try:
            tool = factory()
        except NotImplementedError as exc:
            logger.info(
                "Skipping tool '%s': %s", tool_name, exc
            )
            failed.append(tool_name)
            continue
        except Exception as exc:  # noqa: BLE001
            # Any other failure is also non-fatal; the remaining
            # tools must still be registered.
            logger.warning(
                "Failed to build tool '%s': %s",
                tool_name,
                type(exc).__name__,
            )
            failed.append(tool_name)
            continue

        try:
            target.register(tool, overwrite=overwrite)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to register tool '%s': %s",
                tool_name,
                type(exc).__name__,
            )
            failed.append(tool_name)
            continue

        if already_registered:
            if overwrite:
                logger.info(
                    "Replaced source-control tool '%s'.", tool_name
                )
                registered.append(tool_name)
            else:
                logger.debug(
                    "Tool '%s' was already registered.", tool_name
                )
                skipped.append(tool_name)
        else:
            logger.info(
                "Registered source-control tool '%s'.", tool_name
            )
            registered.append(tool_name)

    return {
        "registered": registered,
        "skipped": skipped,
        "failed": failed,
    }


def unregister_builtin_tools(
    *,
    registry: Optional[ToolRegistry] = None,
) -> Dict[str, bool]:
    """
    Unregister every built-in tool.

    Returns a mapping from tool name to True when a tool was
    removed, False when nothing was registered under that name.

    Intended for tests that want to start from a clean slate.
    """
    target = registry or get_default_tool_registry()
    results: Dict[str, bool] = {}
    for tool_name in _TOOL_FACTORIES.keys():
        results[tool_name] = target.unregister(tool_name)
    return results


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------


def list_builtin_tool_names() -> Sequence[str]:
    """
    Return the canonical list of built-in tool names, in
    registration order.
    """
    return tuple(_TOOL_FACTORIES.keys())


def list_registered_tools(
    *,
    registry: Optional[ToolRegistry] = None,
) -> Sequence[str]:
    """
    Return the names of every tool currently registered.
    """
    target = registry or get_default_tool_registry()
    return target.list_names()


def describe_tools(
    *,
    registry: Optional[ToolRegistry] = None,
    include_schemas: bool = True,
) -> Dict[str, Dict[str, Any]]:
    """
    Return a description of every registered tool.

    The description is intended for the agent runtime and for the
    UI. It contains the tool's name, description, and (optionally)
    the input schema. It NEVER contains the tool's runtime state.

    Args:
        registry: The registry to inspect.
        include_schemas: When True (default), include the input
            schema. When False, only the name and description are
            returned, which produces a compact prompt.
    """
    target = registry or get_default_tool_registry()
    result: Dict[str, Dict[str, Any]] = {}
    for tool in target.list_tools():
        entry: Dict[str, Any] = {
            "name": tool.name,
            "description": tool.description,
        }
        if include_schemas:
            try:
                entry["input_schema"] = dict(tool.input_schema or {})
            except Exception:  # noqa: BLE001
                entry["input_schema"] = {}
        result[tool.name] = entry
    return result
