# backend/omweb/provider_packages/__init__.py
"""
PELDRUN Provider Package Subsystem (M9).

Manages the complete lifecycle of external Provider Packages:

    * Manifest schema validation (``schema.py``).
    * ZIP extraction with path-traversal and archive-bomb guards.
    * Dependency installation in an isolated Python virtual environment.
    * Permission model and enforcement.
    * Installed-package registry with per-version rollback.
    * Install / enable / disable / update / rollback / uninstall
      orchestration (``package_manager.py``).
    * Store catalog integration (``catalog.py``).

This package NEVER imports any runtime code from ``peldrun.llm``. It is
concerned with managing package *files on disk* and *environments*;
the actual provider adapter runtime host lives in
``peldrun.llm.plugin_host`` (M9, phase 2).

The split exists because:
    * ``omweb.provider_packages`` is I/O-heavy (disk, pip, subprocess).
      It belongs to the OMWeb layer, which already owns the Store.
    * ``peldrun.llm.plugin_host`` is execution-heavy (IPC, subprocess).
      It belongs to the core runtime layer.

Keeping them separate preserves the architectural boundary established
by ``contracts.py`` — the runtime never imports from OMWeb.
"""

__all__: list = []
