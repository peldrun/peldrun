# backend/omweb/routers/__init__.py
"""
Aggregated router package for the PELDRUN web API.

Importing a router module through this package keeps the import
surface stable and makes it easy to discover which routers are
available to `omweb.main`. The list is intentionally explicit;
routers are added here only once they are wired into the application
entrypoint.
"""

from omweb.routers import (
    config_rtr,
    files,
    mcp,
    run,
    search_rtr,
    setup,
    source_control,
    status,
)

__all__ = [
    "config_rtr",
    "files",
    "mcp",
    "run",
    "search_rtr",
    "setup",
    "source_control",
    "status",
]
