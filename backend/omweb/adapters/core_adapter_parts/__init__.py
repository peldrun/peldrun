"""
PELDRUN Web Platform - Core Adapter Parts Modular Subsystem.
Exports all platform adapters, executors, canonical schemas,
and runtime environment setup factories for Core ToolRegistry integration.
"""

from __future__ import annotations

from .terminal_utils import (
    find_safe_bash_executable,
    decode_terminal_bytes,
)
from .schemas import CANONICAL_TOOL_SCHEMAS
from .executors import RealToolExecutionFactory
from .web_tool_adapter import WebToolAdapter
from .resolve_tool_runtime import resolve_tool_runtime
from .setup_core_environment import (
    setup_core_environment,
    setup_core_registry,
)

__all__ = [
    "find_safe_bash_executable",
    "decode_terminal_bytes",
    "CANONICAL_TOOL_SCHEMAS",
    "RealToolExecutionFactory",
    "WebToolAdapter",
    "resolve_tool_runtime",
    "setup_core_environment",
    "setup_core_registry",
]