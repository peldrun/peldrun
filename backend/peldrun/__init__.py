"""
PELDRUN Core - Standalone Modular AI Agent Engine & Embedded Runtime.

Embedded directly inside PELDRUN Web to serve as the native execution engine.
Maintains architectural parity with the upstream peldrun-core repository.

Upstream Repository: https://github.com/peldrun/peldrun-core
Upstream Commit Baseline: 075566975f608d6de149ec0d5456c34c012bc362
"""

__version__ = "0.2.0"
__upstream_commit__ = "075566975f608d6de149ec0d5456c34c012bc362"
__is_embedded__ = True

from peldrun.runtime.contract import AgentSpec, RunRequest, WorkspaceContext
from peldrun.tools.contract import ToolResult, ToolRuntime
from peldrun.tools.registry import ToolRegistry

__all__ = [
    "__version__",
    "__upstream_commit__",
    "__is_embedded__",
    "AgentSpec",
    "RunRequest",
    "WorkspaceContext",
    "ToolResult",
    "ToolRuntime",
    "ToolRegistry",
]