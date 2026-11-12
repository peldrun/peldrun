# backend/omweb/memory/storage/__init__.py
"""
PELDRUN Memory Platform - Storage Package.

Public surface for the memory storage layer. Contains the SQLite
implementation of ``MemoryPort``, its migrations, and the
multilingual text normalizer.

Package structure
-----------------

    storage/
    ├── __init__.py       (this file)
    ├── repository.py     -- SqliteMemoryRepository (implements MemoryPort)
    ├── migrations.py     -- schema DDL, version tracking, FTS rebuild
    └── normalizer.py     -- Arabic/Russian/English text normalization

Re-export policy
----------------

INCLUDED:

  * ``SqliteMemoryRepository`` -- the concrete storage class.
  * ``normalize_for_storage`` / ``normalize_query`` -- the two
    normalizer entry points. Both are called by the repository
    AND may be called by tests that need to compare expected vs.
    actual normalized content.
  * ``apply_schema`` / ``rebuild_fts`` / ``schema_status`` /
    ``CURRENT_VERSION`` -- migration helpers. Public because
    operators (via a future health endpoint in P2) and tests
    (schema verification) need them.

EXCLUDED:

  * Internal DDL constants (``_V1_MEMORIES_TABLE_DDL`` etc.).
    Implementation details.
  * Internal normalizer regex constants (``_ARABIC_DIACRITICS``
    etc.). Implementation details.
  * Internal migration helpers (``_get_recorded_version`` etc.).
    Implementation details.

Architectural note
------------------

The ``SqliteFtsProvider`` (``peldrun/memory/providers/sqlite_fts.py``)
imports from this package LAZILY inside ``open()`` -- so importing
``peldrun.memory.providers`` does NOT pull in ``omweb.memory.storage``.
That decoupling is what lets the provider package be imported in
isolation (unit tests, headless CLI tools). The lazy import is
the reason this package's re-exports exist at all: when the
provider DOES need them (at ``open()`` time), it can import from
``omweb.memory.storage`` -- ONE import statement instead of three.
"""

from __future__ import annotations

from omweb.memory.storage.migrations import (
    CURRENT_VERSION,
    apply_schema,
    rebuild_fts,
    schema_status,
)
from omweb.memory.storage.normalizer import (
    normalize_for_storage,
    normalize_query,
)
from omweb.memory.storage.repository import SqliteMemoryRepository


__all__ = [
    # Repository
    "SqliteMemoryRepository",
    # Normalizer
    "normalize_for_storage",
    "normalize_query",
    # Migrations
    "CURRENT_VERSION",
    "apply_schema",
    "rebuild_fts",
    "schema_status",
]
