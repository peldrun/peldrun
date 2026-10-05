"""
PELDRUN core adapter package.

This package is a mechanical split of the former monolithic
`core_adapter.py`.  Every public name remains importable from
`omweb.adapters.core_adapter` thanks to the facade that re-exports
the symbols defined here.

Module map:
  * terminal_utils        -> find_safe_bash_executable, decode_terminal_bytes
  * schemas               -> CANONICAL_TOOL_SCHEMAS
  * executors             -> RealToolExecutionFactory
  * web_tool_adapter      -> WebToolAdapter
  * resolve_tool_runtime  -> resolve_tool_runtime
  * setup_core_environment-> setup_core_environment
"""

from __future__ import annotations

from .terminal_utils import find_safe_bash_executable, decode_terminal_bytes
from .schemas import CANONICAL_TOOL_SCHEMAS
from .executors import RealToolExecutionFactory
from .web_tool_adapter import WebToolAdapter
from .resolve_tool_runtime import resolve_tool_runtime
from .setup_core_environment import setup_core_environment

__all__ = [
    "find_safe_bash_executable",
    "decode_terminal_bytes",
    "CANONICAL_TOOL_SCHEMAS",
    "RealToolExecutionFactory",
    "WebToolAdapter",
    "resolve_tool_runtime",
    "setup_core_environment",
]