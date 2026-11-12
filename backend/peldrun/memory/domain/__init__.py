# backend/peldrun/memory/domain/__init__.py
"""
PELDRUN Memory Platform - Domain Layer Public API.

This is the SINGLE PUBLIC IMPORT SURFACE for the memory domain
package. Every consumer outside ``peldrun.memory.domain`` MUST
import from here, NOT from the individual submodules:

    CORRECT:
        from peldrun.memory.domain import (
            MemoryEntry, MemoryContext, MemoryPort, MemoryMode,
        )

    WRONG (fragile, couples consumers to internal layout):
        from peldrun.memory.domain.entry import MemoryEntry

Consumers that depend on this file include:

    * ``omweb.memory.storage.repository``  (P1.2)
    * ``omweb.memory.manager.memory_service`` (P1.7)
    * ``omweb.memory.providers.*``         (P1.3)
    * ``peldrun.memory.services.*``        (P1.4)
    * ``omweb.agent_bridge_parts.dispatcher`` (P1.5)
    * ``omweb.engines.peldrun_engine``     (P1.5)
    * tests                                 (P1.1 - P1.9)

The re-export policy
--------------------

Only PUBLIC types and helpers are re-exported here. Specifically:

    * EXCLUDED -- internal helpers, private constants, functions
      whose name starts with ``_`` (e.g. ``_MAX_CONTENT_CHARS``,
      ``_utc_now``). These are implementation details of their
      owning submodule and must not be relied upon by external
      callers, even when Python's import machinery technically
      allows it.

    * EXCLUDED -- the concrete backend classes
      (``SqliteMemoryRepository``, ``SqliteFtsProvider``,
      ``BaseMemoryProvider``). Those live in ``omweb.memory.*``
      (P1.2 / P1.3) and their public API is exposed there. This
      domain package deliberately has NO knowledge of the
      storage backends.

    * EXCLUDED -- return types of ``MemoryImportAdapter.preview``
      and ``apply`` (``ImportPreview``, ``ImportApplyResult``).
      Those are defined in P1.6 (``omweb.memory.imports``). This
      domain package only declares the PROTOCOL, not the
      concrete result shapes -- keeping the import subsystem
      swappable without touching the domain contract.

Why the ordering matters
------------------------

The imports below follow the internal dependency graph:

    enums      (no domain dependencies)
    entry      (depends on: enums)
    context    (depends on: entry, enums)
    scope      (depends on: enums)
    settings   (depends on: enums)
    usage      (depends on: context -- lazy inside from_context)
    errors     (no domain dependencies)
    contracts  (depends on: entry, context, errors, scope)

Python evaluates ``from ... import ...`` statements in source
order, so putting ``enums`` first guarantees that every
downstream module's ``from peldrun.memory.domain.enums import ...``
call finds an already-fully-initialized module. Any reordering
risks a partial-initialization error at import time when a
submodule imports from a sibling that has not yet run its own
imports.

What "public" means here
------------------------

A symbol is public iff BOTH conditions hold:

    1. It is listed in ``__all__`` below.
    2. Its name does not start with ``_``.

Symbols satisfying (1) but not (2) would be a bug in this file --
Python's ``__all__`` mechanism does not enforce the underscore
convention, but our project convention does. Keeping them in
sync manually is cheap and catches copy-paste mistakes during
review.

See also
--------

Each submodule's own docstring for the detailed contract of the
types it defines. This module's job is just "re-export the
stable public surface from one place".
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 1. Enumerations (no domain dependencies -- import first)
# ---------------------------------------------------------------------------

from peldrun.memory.domain.enums import (
    MEMORY_MODES,
    MEMORY_SCOPES,
    MEMORY_SOURCE_TYPES,
    MEMORY_STATUSES,
    MEMORY_TYPES,
    MemoryMode,
    MemoryScope,
    MemorySourceType,
    MemoryStatus,
    MemoryType,
)

# ---------------------------------------------------------------------------
# 2. The persistent entity (depends on: enums)
# ---------------------------------------------------------------------------

from peldrun.memory.domain.entry import (
    MemoryEntry,
    MemoryProvenance,
)

# ---------------------------------------------------------------------------
# 3. The transient retrieval context (depends on: entry)
# ---------------------------------------------------------------------------

from peldrun.memory.domain.context import MemoryContext

# ---------------------------------------------------------------------------
# 4. Scope resolution (depends on: enums)
# ---------------------------------------------------------------------------

from peldrun.memory.domain.scope import (
    MemoryScopeRef,
    parse_scope_ref,
    resolve_scope,
    resolve_scope_refs,
)

# ---------------------------------------------------------------------------
# 5. Configuration & token budget (depends on: enums)
# ---------------------------------------------------------------------------

from peldrun.memory.domain.settings import (
    MemorySettings,
    MemoryTokenBudget,
    default_settings,
)

# ---------------------------------------------------------------------------
# 6. Per-run usage telemetry (depends on: context lazily)
# ---------------------------------------------------------------------------

from peldrun.memory.domain.usage import MemoryUsage

# ---------------------------------------------------------------------------
# 7. Exception hierarchy (no domain dependencies)
# ---------------------------------------------------------------------------

from peldrun.memory.domain.errors import (
    MemoryError,
    MemoryNotFoundError,
    MemoryOperationError,
    MemoryUnavailableError,
    MemoryValidationError,
    ScopeViolationError,
)

# ---------------------------------------------------------------------------
# 8. Structural protocols + fail-safe implementations
# ---------------------------------------------------------------------------

from peldrun.memory.domain.contracts import (
    MemoryImportAdapter,
    MemoryPort,
    MemoryProvider,
    NullMemoryPort,
    NullMemoryProvider,
    empty_memory_context,
)


# ---------------------------------------------------------------------------
# Public API declaration
# ---------------------------------------------------------------------------
#
# Every symbol below is importable via:
#
#     from peldrun.memory.domain import <symbol>
#
# AND via:
#
#     from peldrun.memory.domain import *
#
# (the latter is discouraged in application code but useful in
# test files and REPL sessions).
#
# Symbols NOT listed here are considered internal to their
# submodule, even if Python technically allows importing them.

__all__ = [
    # -------- enums --------
    "MemoryType",
    "MemoryScope",
    "MemorySourceType",
    "MemoryStatus",
    "MemoryMode",
    # canonical frozen sets (used by validators & API layer)
    "MEMORY_TYPES",
    "MEMORY_SCOPES",
    "MEMORY_SOURCE_TYPES",
    "MEMORY_STATUSES",
    "MEMORY_MODES",
    # -------- entity --------
    "MemoryEntry",
    "MemoryProvenance",
    # -------- context --------
    "MemoryContext",
    # -------- scope --------
    "MemoryScopeRef",
    "parse_scope_ref",
    "resolve_scope",
    "resolve_scope_refs",
    # -------- settings --------
    "MemorySettings",
    "MemoryTokenBudget",
    "default_settings",
    # -------- usage --------
    "MemoryUsage",
    # -------- errors --------
    "MemoryError",
    "MemoryOperationError",
    "MemoryValidationError",
    "MemoryNotFoundError",
    "MemoryUnavailableError",
    "ScopeViolationError",
    # -------- contracts --------
    "MemoryPort",
    "MemoryProvider",
    "MemoryImportAdapter",
    "NullMemoryPort",
    "NullMemoryProvider",
    "empty_memory_context",
]
