"""
Shared runtime state for the run router.

This module centralizes every mutable module-level dictionary used by the
run endpoints. By isolating the state in a dedicated module we ensure that
all split submodules operate on the exact same dictionary instances, which
is essential for the task registry, event buffers, and artifact tracking.

Note:
    These containers are intentionally mutated in place (never reassigned)
    so that any `from .state import X` binding continues to observe updates
    performed by other submodules.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Tuple


# ---------------------------------------------------------------------------
# Job / chat correlation
# ---------------------------------------------------------------------------

# Maps a job identifier to the chat identifier that owns it.
JOB_TO_CHAT_ID: Dict[str, str] = {}


# ---------------------------------------------------------------------------
# Active execution registry
# ---------------------------------------------------------------------------

# Currently running asyncio tasks, keyed by job id (and mirrored by chat id).
ACTIVE_JOB_TASKS: Dict[str, asyncio.Task] = {}


# ---------------------------------------------------------------------------
# Live event buffers
# ---------------------------------------------------------------------------

# Accumulated live events per job, mirrored by chat id when available.
ACTIVE_JOB_EVENTS: Dict[str, List[Dict[str, Any]]] = {}


# ---------------------------------------------------------------------------
# Filesystem snapshot tracking
# ---------------------------------------------------------------------------

# Pre-execution snapshot of the chat files directory.
# Shape: job_id -> { relative_path -> (mtime, size) }
JOB_PRE_SNAPSHOTS: Dict[str, Dict[str, Tuple[float, int]]] = {}

# Post-execution delta artifacts derived from the pre-execution snapshot.
# Shape: job_id -> [ { name, path, size, action, status }, ... ]
JOB_DELTA_ARTIFACTS: Dict[str, List[Dict[str, Any]]] = {}
