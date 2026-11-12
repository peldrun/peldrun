# backend/peldrun/memory/tools/__init__.py
"""
PELDRUN Memory Platform - Memory Tools Package.

Re-exports the built-in memory tools at the package level so
consumers import from ONE place:

    from peldrun.memory.tools import MemorySaveTool, MemorySearchTool

Package structure
-----------------

    tools/
    ├── __init__.py       (this file)
    └── builtins/
        ├── __init__.py   -- re-exports from the two modules below
        ├── memory_save.py   -- MemorySaveTool
        └── memory_search.py -- MemorySearchTool

Adding a new tool
-----------------

1. Create ``tools/builtins/<name>.py`` with the tool class.
2. Re-export from ``tools/builtins/__init__.py``.
3. Re-export from THIS file.
4. Register in the agent engine's tool collection (P1.5's
   ``peldrun_engine.py``) if the tool is meant to be available
   to agents by default.

Tools not registered in step 4 remain importable but are only
invoked when explicitly constructed (e.g. by tests or by the
memory_search tool's HTTP wrapper in P1.8).
"""

from __future__ import annotations

from peldrun.memory.tools.builtins import (
    MemorySaveTool,
    MemorySearchTool,
)


__all__ = [
    "MemorySaveTool",
    "MemorySearchTool",
]
