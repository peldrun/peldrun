"""
backend/omweb/agent_bridge_parts/state.py

Runtime state registry for the PELDRUN Universal Agent Bridge.

Maintains job-isolated state containers (human interactions, active tasks,
and produced artifacts) strictly mapped by explicit job_id.

All legacy global state singletons have been hardened against concurrency
races. State cleanup utilities are provided to eliminate memory leaks.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional


# Maps a job_id to an asyncio.Event triggered when the human operator
# supplies an answer to an `ask_human` tool invocation.
human_answers: Dict[str, asyncio.Event] = {}

# Maps a job_id to the raw text input supplied by the human operator.
human_data: Dict[str, str] = {}

# Maps a job_id to the executing asyncio.Task (used for graceful cancellation).
active_tasks: Dict[str, asyncio.Task] = {}

# [DEPRECATED - M0 HARDENING]
# Retained strictly for backward compatibility with unmigrated legacy endpoints.
# Modern runtime execution paths MUST NOT read or write to this dictionary.
current_active_job_id: Dict[str, str] = {}

# Maps a job_id or chat_id to the list of canonical artifact relative paths.
job_scoped_artifacts: Dict[str, List[str]] = {}

# Maps a job_id to an optional dedicated asyncio.Queue for strict FIFO event ordering.
job_event_queues: Dict[str, asyncio.Queue[Any]] = {}


def register_job_task(job_id: str, task: asyncio.Task) -> None:
    """
    Register an active execution task for a specific job.
    
    Args:
        job_id: Unique identifier for the job.
        task: Running asyncio.Task instance.
    """
    active_tasks[job_id] = task


def get_job_task(job_id: str) -> Optional[asyncio.Task]:
    """
    Retrieve the active execution task for a job.
    
    Args:
        job_id: Unique identifier for the job.
        
    Returns:
        The running asyncio.Task or None if not found.
    """
    return active_tasks.get(job_id)


def cleanup_job_state(job_id: str) -> None:
    """
    Safely purge all runtime memory and synchronization primitives for a job.
    
    This function should be called upon job completion, failure, or cancellation
    to prevent memory retention in long-running backend processes.
    
    Args:
        job_id: Unique identifier for the job to clean up.
    """
    human_answers.pop(job_id, None)
    human_data.pop(job_id, None)
    active_tasks.pop(job_id, None)
    job_event_queues.pop(job_id, None)

    # Clean legacy pointer if it still referenced this job
    if current_active_job_id.get("current") == job_id:
        current_active_job_id.pop("current", None)