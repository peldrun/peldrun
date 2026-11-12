# backend/omweb/memory/imports/__init__.py
"""
PELDRUN Memory Platform - Import & Export Subsystem Public API.

SINGLE PUBLIC IMPORT SURFACE for the ``peldrun-memory-package``
import/export subsystem. Every consumer outside
``omweb.memory.imports`` (P1.7's ``MemoryService``, P1.8's API
routers, tests) MUST import from here, NOT from the individual
submodules:

    CORRECT:
        from omweb.memory.imports import (
            ImportManager, import_manager_from_service,
            JsonAdapter, MarkdownAdapter, SqliteAdapter,
        )

    WRONG (fragile, couples consumers to internal layout):
        from omweb.memory.imports.import_manager import ImportManager

Package structure
-----------------

::

    imports/
    ├── package_validator.py   Parse+validate peldrun-memory-package.
    ├── package_writer.py      Build peldrun-memory-package bytes.
    ├── base_adapter.py        ABC + ImportPreview/ImportApplyResult.
    ├── adapters/
    │   ├── __init__.py        Registry + select_adapter.
    │   ├── json_adapter.py    .zip / .json / dict / bytes.
    │   ├── markdown_adapter.py .md / .markdown / dict.
    │   └── sqlite_adapter.py   .db / .sqlite / bytes.
    └── import_manager.py      Orchestrator facade.

Two pipelines, one subsystem
----------------------------

This module supports TWO orthogonal flows:

    **Import** (external source -> memory store)
        ``ImportManager`` -> ``select_adapter`` -> adapter
        -> ``preview`` (read) -> ``apply`` (write)

    **Export** (memory store -> package bytes)
        ``MemoryService`` reads entries via its provider
        -> ``package_writer.write_package_to_zip_bytes``

The two share the ``peldrun-memory-package`` format (see
``package_validator``), but they are otherwise independent --
``ImportManager`` never calls the writer, and the writer never
calls the manager. A caller that only wants to EXPORT does not
need to instantiate ``ImportManager`` at all.

Re-export policy
----------------

Only PUBLIC, STABLE interfaces are re-exported here. Excluded:

    * **Internal helpers** -- ``_MANIFEST_FILENAME``,
      ``_MEMORIES_FILENAME``, ``_ZIP_MAGIC``, ``_SQLITE_MAGIC``,
      ``_MAX_ROWS``, ``_MAX_ARCHIVE_SIZE``, every function whose
      name starts with ``_``. These are implementation details
      and may change without notice.

    * **Schema internals** -- ``validate_manifest``,
      ``validate_package``'s lower-level pieces, the
      ``_rebuild_entry_from_line`` reconstruction machinery. The
      public surface is the HIGH-LEVEL flow (preview / apply),
      not the pieces.

    * **Registry internals** -- ``_reset_adapters_to_builtins``,
      ``_BUILTIN_ADAPTERS``, ``_RUNTIME_ADAPTERS``. Tests that
      need these import from
      ``omweb.memory.imports.adapters`` directly, with the
      explicit understanding that they are test-only helpers.

Included:

    * ``ImportManager`` -- the orchestrator.
    * ``ImportPreview`` / ``ImportApplyResult`` -- result shapes.
    * The three concrete adapters -- for direct use in tests and
      in callers that want to bypass registry auto-detection.
    * ``select_adapter`` / ``register_adapter`` /
      ``unregister_adapter`` / ``list_adapters`` /
      ``get_adapter_by_id`` / ``is_registered`` -- the registry
      surface, for callers who want to inspect or extend it.
    * ``PACKAGE_FORMAT_VERSION`` -- the format contract, for
      callers that generate packages independently.
    * ``package_writer`` helpers -- ``build_package_payload``,
      ``write_package_to_directory``, ``write_package_to_zip_bytes``.
    * ``validate_package`` / ``validate_manifest`` -- public
      entry points for callers that want to validate a package
      WITHOUT running the full preview flow (e.g. a startup
      sanity check on a bundled default-memory package).

Convenience factory
-------------------

``import_manager_from_service`` builds an ``ImportManager``
wired to the process-wide ``MemoryService`` singleton (P1.7).
This is the entry point P1.8's API routers use -- they should
not have to import ``MemoryService`` AND ``ImportManager``
separately and wire them together themselves.

Tests that need an isolated ``ImportManager`` (against a
temp-path ``SqliteFtsProvider``) construct it directly:

    mgr = ImportManager(port=temp_provider, settings=temp_settings)

Ordering
--------

Imports follow the internal dependency graph:

    1. package_validator  (no sibling deps)
    2. package_writer     (depends on package_validator)
    3. base_adapter       (depends on peldrun.memory.services)
    4. adapters           (depends on base_adapter)
    5. import_manager     (depends on base_adapter + adapters)

Python evaluates ``from ... import ...`` statements in source
order, so putting the leaf modules first ensures that when
``import_manager`` imports from ``adapters``, the adapter
module is already fully initialized.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# 1. Package format -- validator (no sibling deps)
# ---------------------------------------------------------------------------

from omweb.memory.imports.package_validator import (
    PACKAGE_FORMAT_VERSION,
    PeldrunMemoryPackageManifest,
    PackageValidationResult,
    validate_manifest,
    validate_package,
)

# ---------------------------------------------------------------------------
# 2. Package format -- writer (depends on validator)
# ---------------------------------------------------------------------------

from omweb.memory.imports.package_writer import (
    build_package_payload,
    write_package_to_directory,
    write_package_to_zip_bytes,
)

# ---------------------------------------------------------------------------
# 3. Base adapter -- ABC + result types
# ---------------------------------------------------------------------------

from omweb.memory.imports.base_adapter import (
    BaseImportAdapter,
    ImportApplyResult,
    ImportPreview,
)

# ---------------------------------------------------------------------------
# 4. Adapters -- registry + concrete implementations
# ---------------------------------------------------------------------------

from omweb.memory.imports.adapters import (
    JsonAdapter,
    MarkdownAdapter,
    SqliteAdapter,
    get_adapter_by_id,
    is_registered,
    list_adapters,
    register_adapter,
    select_adapter,
    unregister_adapter,
)

# ---------------------------------------------------------------------------
# 5. ImportManager -- the orchestrator facade
# ---------------------------------------------------------------------------

from omweb.memory.imports.import_manager import ImportManager


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------


def import_manager_from_service() -> ImportManager:
    """Build an ``ImportManager`` wired to the process-wide
    ``MemoryService`` singleton (P1.7).

    Convenience factory for the API layer (P1.8) and any other
    consumer that needs an ``ImportManager`` operating on the
    canonical production provider and settings. The service is
    assumed to have been initialized via the FastAPI lifespan
    hook -- this function does NOT call ``ainitialize()``.

    Raises
    ------
    RuntimeError
        When ``MemoryService`` has not been initialized. This is
        a programmer error (missing lifespan wiring); the caller
        should fix its startup sequence rather than catching
        this in a hot path.
    """
    # Lazy import -- ``omweb.memory.__init__`` imports this
    # module at package load time, so a top-level import here
    # would form a cycle during initial import resolution.
    from omweb.memory import get_memory_service

    service = get_memory_service()
    return ImportManager(
        port=service.provider,
        settings=service.settings,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    # -------- Orchestrator --------
    "ImportManager",
    "import_manager_from_service",
    # -------- Result types --------
    "ImportPreview",
    "ImportApplyResult",
    # -------- Adapters (concrete + base) --------
    "BaseImportAdapter",
    "JsonAdapter",
    "MarkdownAdapter",
    "SqliteAdapter",
    # -------- Registry --------
    "select_adapter",
    "get_adapter_by_id",
    "list_adapters",
    "is_registered",
    "register_adapter",
    "unregister_adapter",
    # -------- Package format --------
    "PACKAGE_FORMAT_VERSION",
    "PeldrunMemoryPackageManifest",
    "PackageValidationResult",
    "validate_manifest",
    "validate_package",
    # -------- Package writer --------
    "build_package_payload",
    "write_package_to_directory",
    "write_package_to_zip_bytes",
]
