"""
backend/omweb/agent_bridge_parts/state.py

Runtime state registry for the PELDRUN Universal Agent Bridge.

Maintains job-isolated state containers (human interactions, active tasks,
and produced artifacts) strictly mapped by explicit job_id.

Hardened under PR 3 (Durable HITL & State Persistence):
- Deprecates in-memory global state singletons in favor of RunStore.
- Provides unified task registration and safe memory purging.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# [DEPRECATED - M0 & PR 3 HARDENING]
# Retained for backward-compatibility; primary authority is now RunStore.
human_answers: Dict[str, asyncio.Event] = {}
human_data: Dict[str, str] = {}
current_active_job_id: Dict[str, str] = {}

# Active execution tasks mapped by job_id
active_tasks: Dict[str, asyncio.Task] = {}

# Canonical workspace artifact relative paths mapped by job_id
job_scoped_artifacts: Dict[str, List[str]] = {}

# Dedicated FIFO event queues mapped by job_id
job_event_queues: Dict[str, asyncio.Queue[Any]] = {}


def register_job_task(job_id: str, task: asyncio.Task) -> None:
    """Register an active execution task for a specific job."""
    active_tasks[job_id] = task


def get_job_task(job_id: str) -> Optional[asyncio.Task]:
    """Retrieve the active execution task for a job."""
    return active_tasks.get(job_id)


def cleanup_job_state(job_id: str) -> None:
    """
    Safely purge all runtime memory, pending futures, and queues for a job.
    Called upon task completion, cancellation, or error.
    """
    human_answers.pop(job_id, None)
    human_data.pop(job_id, None)
    active_tasks.pop(job_id, None)
    job_event_queues.pop(job_id, None)

    if current_active_job_id.get("current") == job_id:
        current_active_job_id.pop("current", None)

    # Clean any associated pending requests in HumanInputRegistry
    try:
        from peldrun.tools.builtins.human_input import HumanInputRegistry

        pending_keys = [
            k for k, v in list(HumanInputRegistry._request_payloads.items())
            if v.get("run_id") == job_id or v.get("job_id") == job_id or k == job_id
        ]
        for pk in pending_keys:
            HumanInputRegistry.cancel_request(pk)
    except Exception as ex:
        logger.debug("Failed cleaning HumanInputRegistry for job %s: %s", job_id, ex)