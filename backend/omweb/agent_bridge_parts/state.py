"""
Global shared runtime state for the PELDRUN Universal Agent Bridge.

These dictionaries are module-level singletons that are read and
mutated by both the core engine runner and the legacy engine runner,
as well as the public dispatcher entry points.

Because dict objects are mutable, importing them across modules keeps
a single shared instance (no rebinding is ever performed anywhere).
"""

from __future__ import annotations

import asyncio
from typing import Dict, List


# Maps a job_id to an asyncio.Event that is set when the human operator
# supplies an answer to an `ask_human` tool call.
human_answers: Dict[str, asyncio.Event] = {}

# Maps a job_id to the raw text of the human operator's answer.
human_data: Dict[str, str] = {}

# Maps a job_id to the asyncio.Task currently executing it (for cancellation).
active_tasks: Dict[str, asyncio.Task] = {}

# Single-entry dict mapping the literal key "current" to the active job id.
current_active_job_id: Dict[str, str] = {}

# Maps a job_id or chat_id to the list of artifact filenames produced by the job.
job_scoped_artifacts: Dict[str, List[str]] = {}