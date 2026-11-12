# backend/omweb/memory/monitoring/__init__.py
"""
PELDRUN Memory Platform - Monitoring & Usage Public API.

SINGLE PUBLIC IMPORT SURFACE for the memory monitoring
subsystem. Every consumer outside ``omweb.memory.monitoring``
MUST import from here, NOT from the individual submodules:

    CORRECT:
        from omweb.memory.monitoring import (
            get_memory_accounting_bridge,
            get_memory_usage_tracker,
            MemoryUsageTracker,
        )

    WRONG (fragile, couples consumers to internal layout):
        from omweb.memory.monitoring.token_accounting import (
            get_memory_accounting_bridge,
        )

Package structure
-----------------

::

    monitoring/
    ├── memory_usage.py      Per-run ledger + tracker.
    ├── token_accounting.py  Bridge: MemoryContext -> LLMInvocationRecord.
    └── __init__.py          (this file) -- re-exports.

Two responsibilities, two entry points
--------------------------------------

The two submodules serve DIFFERENT callers with DIFFERENT
lifecycles, and consumers MUST NOT conflate them:

    **``memory_usage`` (ledger + tracker)**

        Called by the DISPATCHER (P1.5) after retrieval, and by
        monitoring dashboards (via the API router in P1.8).
        Answers: "how much memory did this run use?"

        Public entry points:

          * ``MemoryUsageTracker`` -- the class.
          * ``get_memory_usage_tracker()`` -- singleton accessor.
          * ``SqliteMemoryUsageStore`` -- the storage layer
            (used by tests that want isolated temp DBs).
          * ``get_memory_usage_store()`` -- singleton accessor.

    **``token_accounting`` (bridge)**

        Called by the DISPATCHER (after retrieval) AND the
        EXECUTOR (when building the first LLM invocation record
        for the run). Answers: "how many of this invocation's
        tokens were spent on memory?"

        Public entry points:

          * ``MemoryAccountingBridge`` -- the class.
          * ``get_memory_accounting_bridge()`` -- singleton
            accessor.

The dispatcher usually needs BOTH (record to the ledger AND
register for one-shot attribution). The convenience wrapper
``get_memory_accounting_bridge()`` handles both internally --
its ``record_retrieval`` method writes the ledger AND
registers the pending value in one call. There is no need for
callers to import both.

Re-export policy
----------------

INCLUDED:

    * ``MemoryUsageTracker`` / ``get_memory_usage_tracker`` --
      the tracker interface.
    * ``MemoryAccountingBridge`` /
      ``get_memory_accounting_bridge`` -- the attribution
      bridge.
    * ``SqliteMemoryUsageStore`` /
      ``get_memory_usage_store`` -- the storage layer (public
      for tests and for the API router that reads directly
      from the store).

EXCLUDED:

    * ``reset_for_tests`` -- test-only helper. Tests that
      need it import directly from
      ``memory_usage.reset_for_tests``.
    * ``reset_accounting_bridge_for_tests`` -- same policy.
    * Internal helpers -- ``_period_to_cutoff``,
      ``_empty_summary``, ``_SCHEMA_VERSION``, every function
      whose name starts with ``_``.

Consumers
---------

The subsystems that import from this package:

    * ``omweb.agent_bridge_parts.dispatcher`` (P1.5) --
      calls ``get_memory_accounting_bridge().record_retrieval()``
      after each retrieval.
    * ``peldrun.runtime.executor`` (P1.9) -- calls
      ``get_memory_accounting_bridge().take_memory_tokens()``
      when building the first invocation record.
    * ``omweb.memory.api.memory_usage`` (P1.9) -- the HTTP
      router that reads from ``get_memory_usage_store()``.
    * ``tests/memory/test_usage_api.py`` -- isolated tests.

Ordering
--------

Imports follow the internal dependency graph:

    1. ``memory_usage``       (depends on peldrun.memory.domain)
    2. ``token_accounting``   (depends on memory_usage)

Python evaluates ``from ... import ...`` statements in source
order, so putting ``memory_usage`` first ensures that when
``token_accounting`` imports ``MemoryUsageTracker``, the
tracker module is already fully initialized.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# 1. Ledger + tracker (no sibling deps)
# ---------------------------------------------------------------------------

from omweb.memory.monitoring.memory_usage import (
    MemoryUsageTracker,
    SqliteMemoryUsageStore,
    get_memory_usage_store,
    get_memory_usage_tracker,
)

# ---------------------------------------------------------------------------
# 2. Bridge (depends on memory_usage)
# ---------------------------------------------------------------------------

from omweb.memory.monitoring.token_accounting import (
    MemoryAccountingBridge,
    get_memory_accounting_bridge,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    # -------- Ledger + tracker --------
    "MemoryUsageTracker",
    "SqliteMemoryUsageStore",
    "get_memory_usage_store",
    "get_memory_usage_tracker",
    # -------- Bridge --------
    "MemoryAccountingBridge",
    "get_memory_accounting_bridge",
]
