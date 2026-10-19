# backend/integrations/source_control/tools/__init__.py
"""
Built-in source-control tool registration.

This module is the single entry point for the source-control tool
suite. It exposes:

    - `BUILTIN_TOOL_NAMES` and the registration helpers
      (`register_builtin_tools`, `unregister_builtin_tools`) used by
      the application startup code.
    - The concrete `SourceControlTool` subclasses for callers that
      need to instantiate them directly (for example the HTTP facade
      and the test suite).
    - Lightweight `Args` dataclasses that describe each tool's
      accepted arguments. The `Args` classes are simple containers;
      they are NOT Pydantic models and they do NOT perform validation
      beyond basic type coercion. Validation lives in the tool's own
      `validate_input` method.

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
    - The `Args` dataclasses exist so that callers can pass
      structured arguments without reaching into private tool
      attributes. They are intentionally minimal: they carry the
      argument values and nothing else.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..errors import ValidationError
from .base import (
    TOOL_NAME_PREFIX,
    SourceControlTool,
    ToolRegistry,
    get_default_tool_registry,
)

__all__ = [
    # Registry helpers
    "register_builtin_tools",
    "unregister_builtin_tools",
    "register_tool",
    "unregister_tool",
    "register_source_control_tools",
    "get_all_source_control_tools",
    "list_builtin_tool_names",
    "list_registered_tools",
    "describe_tools",
    "BUILTIN_TOOL_NAMES",
    # Args dataclasses
    "CreateBranchArgs",
    "CreateChangeRequestArgs",
    "InspectDiffArgs",
    "ListRepositoriesArgs",
    "PushBranchArgs",
    "ReadFileArgs",
    "RequestApprovalArgs",
    "RunChecksArgs",
    # Concrete tool classes (available via lazy import; may be missing
    # in partial installations, in which case the symbol is absent)
    "CreateBranchTool",
    "CreateChangeRequestTool",
    "InspectDiffTool",
    "ListRepositoriesTool",
    "PushBranchTool",
    "ReadFileTool",
    "RequestApprovalTool",
    "RunChecksTool",
    "GetOperationTool",
    "ListOperationsTool",
]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Args dataclasses
# ---------------------------------------------------------------------------
#
# These are lightweight, immutable containers for a tool's arguments.
# They deliberately do NOT perform validation; the tool is responsible
# for validating its own inputs in `validate_input` or `execute`. The
# classes exist so that callers can construct, serialize, and inspect
# arguments without depending on the tool's internal implementation.


@dataclass(frozen=True)
class CreateBranchArgs:
    """
    Arguments for the `source_control.create_branch` tool.

    The tool creates a local branch inside an existing workspace. It
    does not touch the remote; use `source_control.push_branch` after
    the local branch is committed.
    """

    workspace_id: str
    branch_name: str
    from_sha: Optional[str] = None
    force: bool = False


@dataclass(frozen=True)
class CreateChangeRequestArgs:
    """
    Arguments for the `source_control.create_change_request` tool.

    The tool creates a pull request or merge request for a branch
    that has already been pushed. An approval ticket is required.
    """

    operation_id: str
    approval_id: str
    title: str
    body: str = ""
    draft: bool = True


@dataclass(frozen=True)
class InspectDiffArgs:
    """
    Arguments for the `source_control.get_diff` tool.

    The tool computes the diff of a workspace against a base commit
    and returns the canonical diff hash used for approvals.
    """

    workspace_id: str
    base_sha: Optional[str] = None
    include_patch: bool = True


@dataclass(frozen=True)
class ListRepositoriesArgs:
    """
    Arguments for the `source_control.list_repositories` tool.
    """

    provider: str = "github"
    account_id: Optional[str] = None
    query: Optional[str] = None
    page: int = 1
    page_size: int = 30


@dataclass(frozen=True)
class PushBranchArgs:
    """
    Arguments for the `source_control.push_branch` tool.

    The tool pushes a reviewed branch to a remote. A valid approval
    ticket is required; the ticket is consumed before the push runs.
    """

    operation_id: str
    approval_id: str
    local_branch: Optional[str] = None
    remote: str = "origin"


@dataclass(frozen=True)
class ReadFileArgs:
    """
    Arguments for the `source_control.read_file` tool.
    """

    repository_id: str
    file_path: str
    ref: Optional[str] = None
    provider: str = "github"
    account_id: Optional[str] = None


@dataclass(frozen=True)
class RequestApprovalArgs:
    """
    Arguments for the `source_control.request_approval` tool.

    The tool creates a pending approval ticket. It does NOT grant the
    approval; the user must grant it through the UI.
    """

    operation_id: str
    operation: str
    target_branch: str
    ttl_seconds: Optional[int] = None


@dataclass(frozen=True)
class RunChecksArgs:
    """
    Arguments for the `source_control.run_checks` tool.

    The tool runs one or more pre-registered check commands inside a
    workspace. The commands are configured at application startup;
    the agent never supplies them.
    """

    workspace_id: str
    checks: Sequence[str] = field(default_factory=tuple)
    fail_fast: bool = True


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


def _build_get_operation() -> SourceControlTool:
    try:
        from .get_operation import GetOperationTool  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NotImplementedError(
            "The 'get_operation' tool is not implemented yet."
        ) from exc
    return GetOperationTool()


def _build_list_operations() -> SourceControlTool:
    try:
        from .get_operation import ListOperationsTool  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NotImplementedError(
            "The 'list_operations' tool is not implemented yet."
        ) from exc
    return ListOperationsTool()


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
    "source_control.get_operation": _build_get_operation,
    "source_control.list_operations": _build_list_operations,
}


#: Names of every tool that ships with PELDRUN. The list mirrors the
#: tool table in the source-control integration reference. It is
#: exposed as a tuple so that callers cannot mutate the canonical
#: list.
BUILTIN_TOOL_NAMES: tuple[str, ...] = tuple(_TOOL_FACTORIES.keys())


# ---------------------------------------------------------------------------
# Lazy re-exports for the concrete tool classes
# ---------------------------------------------------------------------------
#
# The package `__init__` imports the tool classes by name. The imports
# below are performed lazily so that a missing or partially-implemented
# tool does not break the import of this module. Each class is
# resolved on first access via module-level `__getattr__`, which
# Python calls for names not found in the module's namespace.
#
# Callers that need the class object SHOULD use `from
# .tools import XxxTool`. This triggers `__getattr__` and either
# returns the class or raises `AttributeError` with a clear message.


_TOOL_CLASS_IMPORTS: Dict[str, tuple[str, str]] = {
    "ListProvidersTool": (".list_providers", "ListProvidersTool"),
    "ListRepositoriesTool": (".list_repositories", "ListRepositoriesTool"),
    "GetRepositoryTool": (".get_repository", "GetRepositoryTool"),
    "ReadFileTool": (".read_file", "ReadFileTool"),
    "ListBranchesTool": (".list_branches", "ListBranchesTool"),
    "CreateWorkspaceTool": (".create_workspace", "CreateWorkspaceTool"),
    "CreateBranchTool": (".create_branch", "CreateBranchTool"),
    "InspectDiffTool": (".inspect_diff", "InspectDiffTool"),
    "RunChecksTool": (".run_checks", "RunChecksTool"),
    "RequestApprovalTool": (".request_approval", "RequestApprovalTool"),
    "PushBranchTool": (".push_branch", "PushBranchTool"),
    "CreateChangeRequestTool": (
        ".create_change_request",
        "CreateChangeRequestTool",
    ),
    "GetOperationTool": (".get_operation", "GetOperationTool"),
    "ListOperationsTool": (".get_operation", "ListOperationsTool"),
}


def __getattr__(name: str) -> Any:
    """
    Lazily resolve concrete tool classes.

    Python calls this function when an attribute that is not defined
    in the module's namespace is accessed. We use it to import the
    concrete tool classes on demand, so that a missing tool module
    does not prevent the rest of the package from importing.

    When the target class is missing (module not yet implemented, or
    the class name inside the module has changed), we return a
    **placeholder class** instead of raising `AttributeError`. The
    placeholder raises `NotImplementedError` only when someone tries
    to instantiate it, which gives a clear and late failure instead
    of breaking the whole package import.

    This behavior is intentional: the top-level
    `integrations.source_control.__init__` imports the tool classes
    eagerly, so a missing class MUST NOT abort the entire import.
    Callers that want a strict existence check SHOULD inspect
    `_TOOL_CLASS_IMPORTS` or import the concrete module directly.
    """
    spec = _TOOL_CLASS_IMPORTS.get(name)
    if spec is None:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        )

    module_name, class_name = spec
    try:
        import importlib

        module = importlib.import_module(module_name, package=__name__)
        return getattr(module, class_name)
    except (ImportError, AttributeError) as exc:
        # Return a placeholder that raises only when instantiated.
        return _make_placeholder_tool(
            name, module_name=module_name, cause=exc
        )


def _make_placeholder_tool(
    class_name: str,
    *,
    module_name: str,
    cause: Optional[BaseException] = None,
) -> type:
    """
    Build a placeholder class for a missing tool.

    The returned class has the same name as the missing tool, so
    `isinstance` and `type()` checks that reference the class by
    name still work. Instantiating it raises `NotImplementedError`
    with a clear message that names the missing module.
    """

    class _PlaceholderTool:
        """Placeholder for an unimplemented or renamed tool."""

        #: Set so that `ToolRegistry.register` and other tools that
        #: inspect `provider_id` do not crash on attribute lookup.
        provider_id: str = "placeholder"

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise NotImplementedError(
                f"Tool class '{class_name}' could not be imported "
                f"from '{module_name}'. The tool is either not yet "
                f"implemented or has been renamed. "
                f"Underlying error: {type(cause).__name__ if cause else 'unknown'}."
            )

    _PlaceholderTool.__name__ = class_name
    _PlaceholderTool.__qualname__ = class_name
    _PlaceholderTool.__doc__ = (
        f"Placeholder for the missing tool '{class_name}' from "
        f"module '{module_name}'."
    )
    return _PlaceholderTool

def __dir__() -> Sequence[str]:
    """Include the lazily-exported names in `dir(tools)`."""
    return sorted(set(globals()) | set(_TOOL_CLASS_IMPORTS) | set(__all__))


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


def register_source_control_tools(
    *,
    registry: Optional[ToolRegistry] = None,
    overwrite: bool = False,
    tools: Optional[Sequence[str]] = None,
) -> Dict[str, List[str]]:
    """
    Alias for `register_builtin_tools`.

    The HTTP facade and the package `__init__` were written against
    this name. It refers to the SAME registration logic; only the
    name differs.
    """
    return register_builtin_tools(
        registry=registry, overwrite=overwrite, tools=tools
    )


def get_all_source_control_tools(
    *,
    registry: Optional[ToolRegistry] = None,
) -> Sequence[SourceControlTool]:
    """
    Return every registered source-control tool.

    The function returns the current contents of the registry as a
    tuple, sorted by tool name. When `registry` is `None`, the
    process-wide default registry is used.

    This is a read-only helper for callers that need to enumerate
    the tools the agent runtime can invoke. It does NOT attempt to
    construct tools that are not registered; use
    `register_builtin_tools` before calling this function if the
    application has not yet populated the registry.
    """
    target = registry or get_default_tool_registry()
    return target.list_tools()


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
