# backend/omweb/memory/manager/__init__.py
"""
PELDRUN Memory Platform - Manager Package.

Public surface for the memory subsystem's orchestration layer.
Contains the process-wide ``MemoryService`` facade, its lifecycle
manager, the persistent settings store, and the reusable write
pipeline.

Package structure
-----------------

    manager/
    ├── __init__.py               (this file)
    ├── memory_service.py         -- MemoryService (facade)
    ├── memory_lifecycle.py       -- MemoryLifecycleManager
    ├── memory_settings_manager.py -- MemorySettingsManager
    └── write_pipeline.py         -- MemoryWritePipeline + WriteOutcome

Re-export policy
----------------

INCLUDED:

  * ``MemoryService`` -- the process-wide facade. Consumers call
    ``MemoryService.retrieve(...)``; they NEVER instantiate it
    directly (the singleton lives in ``omweb.memory.__init__``).
  * ``MemoryLifecycleManager`` / ``MemorySettingsManager`` --
    public because tests and integration code construct them
    explicitly with temp paths.
  * ``MemoryWritePipeline`` / ``WriteOutcome`` -- the reusable
    write path. ``memory_save`` (P1.5) has its own inline
    implementation and is NOT migrated to use this pipeline yet;
    new callers (extractor output, import apply, future outbox)
    SHOULD use the pipeline directly.

EXCLUDED:

  * Private attributes of ``MemoryService`` (``_settings_manager``,
    ``_lifecycle``). Accessed via property where needed
    (``service.settings``, ``service.provider``). The API layer
    (``memory_settings.py``) accesses ``_settings_manager``
    directly for the update flow -- documented in that file as a
    P1 compromise.

Singleton pattern
-----------------

This package does NOT expose a ``get_memory_service()`` singleton.
That accessor lives in ``omweb.memory.__init__`` (see P1.7's
lifecycle wiring). Rationale: the singleton needs a module-level
lock and initialization guard; putting it here would force
``omweb.memory.manager`` to be imported eagerly even when a
deployment never enables memory.
"""

from __future__ import annotations

from omweb.memory.manager.memory_lifecycle import MemoryLifecycleManager
from omweb.memory.manager.memory_service import MemoryService
from omweb.memory.manager.memory_settings_manager import (
    MemorySettingsManager,
)
from omweb.memory.manager.write_pipeline import (
    MemoryWritePipeline,
    WriteOutcome,
)


__all__ = [
    "MemoryService",
    "MemoryLifecycleManager",
    "MemorySettingsManager",
    "MemoryWritePipeline",
    "WriteOutcome",
]
