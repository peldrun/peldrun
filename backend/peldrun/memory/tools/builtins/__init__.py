# backend/peldrun/memory/tools/builtins/__init__.py
"""
PELDRUN Memory Platform - Built-in Memory Tools.

Public surface for the two agent-facing memory tools. Both are
stateless (no per-execution state on the instance), so concurrent
invocations against a shared instance are safe.

Consumers
---------
* ``peldrun.memory.tools``         -- re-exports both tools.
* ``omweb.engines.peldrun_engine`` -- instantiates them when
  building the agent's ``ToolCollection``.
* ``peldrun.memory.tools.builtins.memory_save`` /
  ``peldrun.memory.tools.builtins.memory_search`` -- the
  underlying modules (direct imports discouraged; use this
  package surface).
"""

from __future__ import annotations

from peldrun.memory.tools.builtins.memory_save import MemorySaveTool
from peldrun.memory.tools.builtins.memory_search import MemorySearchTool


__all__ = [
    "MemorySaveTool",
    "MemorySearchTool",
]
