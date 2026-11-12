# backend/peldrun/memory/services/__init__.py
"""
PELDRUN Memory Platform - Retrieval Services Public API.

SINGLE PUBLIC IMPORT SURFACE for the memory services package.
Every consumer outside ``peldrun.memory.services`` MUST import
from here, NOT from the individual submodules:

    CORRECT:
        from peldrun.memory.services import (
            classify_route, build_memory_context,
            extract_candidates, decide_write_action,
        )

    WRONG (fragile, couples consumers to internal layout):
        from peldrun.memory.services.router import classify_route

Consumers that depend on this file include:

    * ``omweb.agent_bridge_parts.dispatcher``        (P1.5)
    * ``omweb.engines.peldrun_engine``               (P1.5)
    * ``omweb.memory.manager.memory_service``        (P1.7)
    * ``omweb.memory.manager.write_pipeline``        (P1.7)
    * ``omweb.memory.tools.builtins.memory_save``    (P1.4 tools)
    * ``omweb.memory.tools.builtins.memory_search``  (P1.4 tools)
    * tests                                           (P1.4 - P1.9)

Package structure
-----------------

::

    services/
    ├── router.py            Retrieval intent classification.
    ├── ranker.py            5-signal weighted ranking.
    ├── context_builder.py   End-to-end pipeline + budget fitter.
    ├── policies.py          Write-time decision functions.
    └── extractor.py         Heuristic memory extraction.

Each submodule is PURE (no I/O except context_builder's provider
call, no storage). They compose into two pipelines:

    **RETRIEVAL pipeline** (query -> context to inject)

        router.classify_route
            -> (if NO_NEED, empty context)
            -> context_builder.build_memory_context
                 -> provider.search_ranked
                 -> ranker.rank_memories
                 -> budget fitter (with user reservation)
                 -> MemoryContext

    **WRITE pipeline** (candidate -> decision -> persistence)

        extractor.extract_candidates    (when auto-extraction on)
        OR
        memory_save tool direct call    (explicit user save)
            -> policies.decide_write_action
                 -> CREATE / SKIP / SUPERSEDE / REACTIVATE
                 -> (execution handled by WritePipeline, P1.7)

The two pipelines are SEPARATE by design: they can be enabled /
disabled independently via ``MemorySettings``, and they have
different failure semantics (retrieval failures degrade to empty
context; write failures raise to the user).

Re-export policy
----------------

Only PUBLIC, STABLE interfaces are re-exported here. Specifically:

    * EXCLUDED -- internal helpers, private constants, functions
      whose name starts with ``_`` (``_count_tokens``,
      ``_fit_with_reservation``, ``_REMEMBER_MARKER``, ...).
      These are implementation details and may change without
      notice.

    * EXCLUDED -- the weight constants
      (``WEIGHT_RELEVANCE``, ``WEIGHT_IMPORTANCE``,
      ``WEIGHT_RECENCY``, ``WEIGHT_SCOPE``). Callers who need
      them can import from ``peldrun.memory.services.ranker``
      directly -- but they are TUNING parameters, not stable
      contracts. Changing their values is a behavioral change
      for every deployment. Re-exporting them here would
      suggest they are safe to reference from external code.

    * EXCLUDED -- the regex constants. Same rationale.

    * INCLUDED -- everything a caller of the two pipelines
      needs to:
        - classify a query (router),
        - decide whether to retrieve (RouteDecision),
        - build a context (build_memory_context),
        - format a context (format_memory_context),
        - decide what to do with a candidate (decide_write_action),
        - extract candidates from turns (extract_candidates).

Ordering
--------

Imports follow the internal dependency graph:

    router       (no sibling deps)
    ranker       (no sibling deps)
    policies     (no sibling deps)
    context_builder (depends on: router, ranker)
    extractor    (depends on: policies)

Python evaluates the import statements in source order, so
putting the leaf modules first ensures that when
``context_builder`` imports from ``router``, the router module
is already fully initialized. This mirrors the same principle
used in ``peldrun.memory.domain.__init__``.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# 1. Router -- retrieval intent classification
# ---------------------------------------------------------------------------

from peldrun.memory.services.router import (
    Route,
    RouteDecision,
    classify_route,
    route_for_tool_invocation,
)

# ---------------------------------------------------------------------------
# 2. Ranker -- 5-signal weighted ranking
# ---------------------------------------------------------------------------

from peldrun.memory.services.ranker import (
    RANKER_VERSION,
    RankedEntry,
    rank_memories,
)

# ---------------------------------------------------------------------------
# 3. Policies -- write-time decisions
# ---------------------------------------------------------------------------

from peldrun.memory.services.policies import (
    WriteAction,
    WriteDecision,
    classify_memory_type,
    decide_write_action,
    detect_conflict,
    detect_duplicate,
    is_worth_saving,
)

# ---------------------------------------------------------------------------
# 4. Context builder -- retrieval pipeline
# ---------------------------------------------------------------------------

from peldrun.memory.services.context_builder import (
    build_empty_context,
    build_memory_context,
    format_memory_context,
)

# ---------------------------------------------------------------------------
# 5. Extractor -- heuristic candidate extraction
# ---------------------------------------------------------------------------

from peldrun.memory.services.extractor import extract_candidates


# ---------------------------------------------------------------------------
# Public API declaration
# ---------------------------------------------------------------------------

__all__ = [
    # -------- router --------
    "Route",
    "RouteDecision",
    "classify_route",
    "route_for_tool_invocation",
    # -------- ranker --------
    "RANKER_VERSION",
    "RankedEntry",
    "rank_memories",
    # -------- policies --------
    "WriteAction",
    "WriteDecision",
    "classify_memory_type",
    "decide_write_action",
    "detect_conflict",
    "detect_duplicate",
    "is_worth_saving",
    # -------- context builder --------
    "build_empty_context",
    "build_memory_context",
    "format_memory_context",
    # -------- extractor --------
    "extract_candidates",
]
