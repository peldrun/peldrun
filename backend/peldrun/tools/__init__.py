"""
PELDRUN Core Tools Subsystem.

Central aggregation module exposing BaseTool, ToolResult,
ToolExecutionPolicy, and ToolRuntime.

Import style
------------
This module uses RELATIVE imports (``from .base import ...``) rather
than absolute ones (``from peldrun.tools.base import ...``). Absolute
imports would force the top-level ``peldrun.*`` module tree even when
this package is loaded as ``backend.peldrun.*``, creating two
incompatible copies of every class. Relative imports always stay in
the tree that loaded this package, so ``isinstance`` checks and
``except`` clauses work across module boundaries.
"""

from __future__ import annotations

from .base import BaseTool
from .contract import (
    ToolExecutionPolicy,
    ToolResult,
    ToolRuntime,
)

__all__ = [
    "BaseTool",
    "ToolResult",
    "ToolRuntime",
    "ToolExecutionPolicy",
]
