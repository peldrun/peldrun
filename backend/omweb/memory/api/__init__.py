# backend/omweb/memory/api/__init__.py
"""
PELDRUN Memory Platform - Memory HTTP API Package.

Hosts the SIX routers that together expose the memory subsystem
over HTTP. Each router is a module-level ``router`` variable; the
aggregator ``omweb.routers.memory_rtr`` mounts five of them, and
``omweb.main`` mounts the sixth (``memory_crud``) directly.

Package structure
-----------------

    api/
    ├── __init__.py          (this file)
    ├── memory_settings.py   -- GET/PATCH /settings, /projects/{id}
    ├── memory_projects.py   -- /projects, /projects/{id}/stats, bulk ops
    ├── memory_import.py     -- POST /import/preview, /import/apply
    ├── memory_export.py     -- GET /export
    ├── memory_usage.py      -- GET /usage, /usage/breakdown, /usage/timeseries
    └── memory_crud.py       -- GET/POST/PATCH/DELETE /, /search, /{id}

IMPORTANT: no eager re-exports
------------------------------

This package deliberately does NOT re-export the routers at the
package level:

    # DO NOT do this in __init__.py:
    # from omweb.memory.api.memory_crud import router

Why: FastAPI routers are STATEFUL (they accumulate routes as
``include_router`` is called). Importing them through
``omweb.memory.api`` would still yield the same module-level
``router`` object, so technically it wouldn't break -- BUT the
re-export creates a second "canonical" import path, and future
maintainers would have to know that
``omweb.memory.api.memory_crud.router`` and
``omweb.memory.api.router`` refer to the same object. That is a
footgun waiting to happen.

Instead, mount sites import each module EXPLICITLY:

    from omweb.memory.api.memory_crud import router as _crud_router
    from omweb.memory.api.memory_settings import router as _settings_router
    ...

This is what ``omweb.routers.memory_rtr`` and ``omweb.main``
already do. Keeping the imports explicit documents the intent
per-router and prevents accidental double-mounts.

Ordering guarantee
------------------

The order in which routers are mounted determines FastAPI's URL
matching (registration order, not specificity). The mounting
contract -- fixed-prefix routers first, ``memory_crud`` last --
lives in:

    * ``omweb.routers.memory_rtr`` (settings/projects/import/
      export/usage)
    * ``omweb.main`` (mounts memory_crud AFTER memory_rtr)

See both files' comments for the rationale.
"""

from __future__ import annotations


__all__: list[str] = []
