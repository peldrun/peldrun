"""
PELDRUN Core Built-in Tools Subsystem.
Exports foundational tools for file manipulation, shell command execution,
web searching, and interactive human feedback.
"""

from typing import List, Optional

from peldrun.events.emitter import EventEmitter
from peldrun.tools.base import BaseTool
from peldrun.tools.builtins.file_ops import FileOpsArgs, FileOpsTool
from peldrun.tools.builtins.human_input import HumanInputArgs, HumanInputTool
from peldrun.tools.builtins.shell_exec import ShellExecArgs, ShellExecTool
from peldrun.tools.builtins.web_search import WebSearchArgs, WebSearchTool


def get_builtin_tools(
    workspace_root: Optional[str] = None,
    emitter: Optional[EventEmitter] = None,
) -> List[BaseTool]:
    """
    Instantiate and return the suite of standard built-in tools configured for a workspace.
    Injects workspace scoping and attaches the event emitter for interactive tools.
    """
    return [
        FileOpsTool(workspace_root=workspace_root),
        ShellExecTool(workspace_root=workspace_root),
        WebSearchTool(workspace_root=workspace_root),
        HumanInputTool(workspace_root=workspace_root, emitter=emitter),
    ]


__all__ = [
    # Schemas
    "FileOpsArgs",
    "ShellExecArgs",
    "WebSearchArgs",
    "HumanInputArgs",
    # Tools
    "FileOpsTool",
    "ShellExecTool",
    "WebSearchTool",
    "HumanInputTool",
    # Factory
    "get_builtin_tools",
]