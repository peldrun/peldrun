"""
Adapter layer connecting PELDRUN Web Platform constructs to PELDRUN Core Runtime.
Provides real runtime tool bindings for shell, python, editor, resilient web search,
real browser inspection, and interactive human suspension loops.

=======================================================================
  MIGRATION NOTICE  --  This file is a clean facade package.
=======================================================================
"""

from __future__ import annotations

# --- Explicit imports from modular package parts to guarantee load order ---
from .core_adapter_parts.terminal_utils import (
    find_safe_bash_executable,
    decode_terminal_bytes,
)
from .core_adapter_parts.schemas import CANONICAL_TOOL_SCHEMAS
from .core_adapter_parts.executors import RealToolExecutionFactory
from .core_adapter_parts.web_tool_adapter import WebToolAdapter
from .core_adapter_parts.resolve_tool_runtime import resolve_tool_runtime
from .core_adapter_parts.setup_core_environment import (
    setup_core_environment,
    setup_core_registry,
)

# --- Core Runtime Registry and Client Exports -----------------------------
from peldrun.llm.client import AsyncLLMClient

try:
    from peldrun.tools.registry import ToolRegistry as CoreToolRegistry
except ImportError:
    CoreToolRegistry = None  # type: ignore[assignment, misc]

# --- Preserve auxiliary Web store registry imports -----------------------
try:
    from omweb.tools.registry import ToolRegistry, tool_registry
except ImportError:
    from backend.omweb.tools.registry import ToolRegistry, tool_registry

try:
    from omweb.agent_bridge import human_answers, human_data, current_active_job_id
except ImportError:
    human_answers: dict = {}
    human_data: dict = {}
    current_active_job_id: dict = {}


__all__ = [
    # Public API
    "find_safe_bash_executable",
    "decode_terminal_bytes",
    "CANONICAL_TOOL_SCHEMAS",
    "RealToolExecutionFactory",
    "WebToolAdapter",
    "resolve_tool_runtime",
    "setup_core_environment",
    "setup_core_registry",
    # Core Runtime Registry
    "CoreToolRegistry",
    # Re-exported auxiliaries (for backward compatibility)
    "AsyncLLMClient",
    "ToolRegistry",
    "tool_registry",
    "human_answers",
    "human_data",
    "current_active_job_id",
]