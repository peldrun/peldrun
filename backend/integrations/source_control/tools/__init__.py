from __future__ import annotations

from typing import Any, Sequence

from .create_branch import CreateBranchArgs, CreateBranchTool
from .create_change_request import CreateChangeRequestArgs, CreateChangeRequestTool
from .inspect_diff import InspectDiffArgs, InspectDiffTool
from .list_repositories import ListRepositoriesArgs, ListRepositoriesTool
from .push_branch import PushBranchArgs, PushBranchTool
from .read_file import ReadFileArgs, ReadFileTool
from .request_approval import RequestApprovalArgs, RequestApprovalTool
from .run_checks import RunChecksArgs, RunChecksTool


def get_all_source_control_tools(
    **tool_kwargs: Any,
) -> tuple[Any, ...]:
    """Instantiates and returns the complete suite of source control agent tools."""
    return (
        ListRepositoriesTool(**tool_kwargs),
        ReadFileTool(**tool_kwargs),
        CreateBranchTool(**tool_kwargs),
        InspectDiffTool(**tool_kwargs),
        RunChecksTool(**tool_kwargs),
        RequestApprovalTool(**tool_kwargs),
        PushBranchTool(**tool_kwargs),
        CreateChangeRequestTool(**tool_kwargs),
    )


def register_source_control_tools(
    registry: Any,
    **tool_kwargs: Any,
) -> None:
    """Registers all official source control tools into an agent tool registry."""
    tools = get_all_source_control_tools(**tool_kwargs)
    for tool in tools:
        if hasattr(registry, "register"):
            registry.register(tool)
        elif hasattr(registry, "add_tool"):
            registry.add_tool(tool)


__all__ = [
    # Factory & Registration Helpers
    "get_all_source_control_tools",
    "register_source_control_tools",
    # Tools
    "ListRepositoriesTool",
    "ReadFileTool",
    "CreateBranchTool",
    "InspectDiffTool",
    "RunChecksTool",
    "RequestApprovalTool",
    "PushBranchTool",
    "CreateChangeRequestTool",
    # Input Schemas (Args)
    "ListRepositoriesArgs",
    "ReadFileArgs",
    "CreateBranchArgs",
    "InspectDiffArgs",
    "RunChecksArgs",
    "RequestApprovalArgs",
    "PushBranchArgs",
    "CreateChangeRequestArgs",
]
