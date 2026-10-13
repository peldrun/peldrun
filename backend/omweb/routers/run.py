"""
Run router - compatibility facade.

This module is now a thin facade over the :mod:`omweb.routers.run_routes`
package, which contains the actual endpoint implementations split across
focused submodules. The public surface (the exported ``router`` and the
legacy module-level symbols) is preserved so that existing imports such as::

    from omweb.routers.run import router

continue to work without modification.

The legacy module-level symbols (``JOB_TO_CHAT_ID``, ``ACTIVE_JOB_TASKS``,
``ACTIVE_JOB_EVENTS``, ``JOB_PRE_SNAPSHOTS``, ``JOB_DELTA_ARTIFACTS``,
``RunRequest``, ``FileContentPayload``, ``HumanRespondRequest``,
``compute_job_delta_files``, and ``resolve_chat``) are re-exported from
their new homes so that any external code referring to them by the old
module path keeps functioning.

For new code, prefer importing directly from the submodules, e.g.::

    from omweb.routers.run_routes.state import ACTIVE_JOB_TASKS
    from omweb.routers.run_routes.utils import resolve_chat
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Public router (aggregated across all endpoint submodules).
# ---------------------------------------------------------------------------
from .run_routes import router


# ---------------------------------------------------------------------------
# Backwards-compatible re-exports.
#
# These names used to live directly in this module. They are re-exported here
# so that existing imports continue to resolve against the same identifiers,
# including the same underlying mutable objects (the containers are shared by
# reference, not copied).
# ---------------------------------------------------------------------------
from .run_routes.schemas import (  # noqa: F401
    FileContentPayload,
    HumanRespondRequest,
    RunRequest,
)
from .run_routes.state import (  # noqa: F401
    ACTIVE_JOB_EVENTS,
    ACTIVE_JOB_TASKS,
    JOB_DELTA_ARTIFACTS,
    JOB_PRE_SNAPSHOTS,
    JOB_TO_CHAT_ID,
)
from .run_routes.utils import (  # noqa: F401
    compute_job_delta_files,
    resolve_chat,
)


__all__ = [
    # Aggregated router mounted by the application.
    "router",
    # Request schemas.
    "RunRequest",
    "FileContentPayload",
    "HumanRespondRequest",
    # Shared runtime state.
    "JOB_TO_CHAT_ID",
    "ACTIVE_JOB_TASKS",
    "ACTIVE_JOB_EVENTS",
    "JOB_PRE_SNAPSHOTS",
    "JOB_DELTA_ARTIFACTS",
    # Helper utilities.
    "compute_job_delta_files",
    "resolve_chat",
]
