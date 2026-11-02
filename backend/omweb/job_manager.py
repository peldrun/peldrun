import asyncio
import logging
import time
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical status contract
#
# See ``docs/state-contracts.md`` §3 for the authoritative definitions.
# The ``Literal`` alias below is used as a static type check by IDEs and
# by mypy/pyright. Runtime callers are additionally guarded inside
# ``update_status`` because Python does not enforce Literal at runtime and
# Pydantic v2 does not validate attribute assignment unless
# ``validate_assignment=True`` is set on the model.
#
# The ``TERMINAL_STATUSES`` set lists every state after which a run can
# never transition again. ``recovery.py`` (Phase E.6) and the frontend
# ``stream-store`` use this list to decide when to prune a run from
# memory.
# ---------------------------------------------------------------------------

SessionStatus = Literal[
    "pending",
    "queued",
    "starting",
    "running",
    "waiting_for_input",
    "retrying",
    "paused",
    "cancelling",
    "cancelled",
    "completed",
    "failed",
    "interrupted",
]

_VALID_STATUSES: frozenset[str] = frozenset(
    (
        "pending",
        "queued",
        "starting",
        "running",
        "waiting_for_input",
        "retrying",
        "paused",
        "cancelling",
        "cancelled",
        "completed",
        "failed",
        "interrupted",
    )
)

TERMINAL_STATUSES: frozenset[str] = frozenset(
    ("cancelled", "completed", "failed", "interrupted")
)

# Legacy aliases that predate the canonical contract. Normalized to
# their canonical equivalent at the update_status boundary so the
# on-disk and in-memory value is always canonical. Kept as a mapping
# (not a set) so the normalizer can substitute in place.
_LEGACY_STATUS_MAP: Dict[str, str] = {
    "stopped": "cancelled",
    "active": "pending",
    "done": "completed",
    "error": "failed",
}


class Job(BaseModel):
    id: str
    # Canonical status. Kept as ``str`` (not ``SessionStatus``) because
    # Pydantic v2 does not validate attribute assignment without
    # ``validate_assignment=True`` and enabling it would break every
    # legacy ``job.status = ...`` assignment in the codebase. The
    # authoritative gate lives in ``update_status`` above, which
    # normalizes both inputs and the assignment path.
    status: str = "running"
    prompt: str = ""
    created_at: float = Field(default_factory=time.time)
    events: List[Dict[str, Any]] = Field(default_factory=list)
    result: Optional[str] = None
    error: Optional[str] = None
    is_paused: bool = False
    current_step: Optional[int] = None
    checkpoint_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "prompt": self.prompt,
            "created_at": self.created_at,
            "events": self.events,
            "result": self.result,
            "error": self.error,
            "is_paused": self.is_paused,
            "current_step": self.current_step,
            "checkpoint_id": self.checkpoint_id,
        }


class JobManager:
    def __init__(self):
        self._jobs: Dict[str, Job] = {}
        self._runners: Dict[str, Any] = {}
        self._pause_events: Dict[str, asyncio.Event] = {}

    def create_job(self, job_id: str, prompt: str = "") -> Job:
        job = Job(id=job_id, prompt=prompt)
        self._jobs[job_id] = job
        return job

    def get_job(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def register_runner(self, job_id: str, runner: Any) -> None:
        """Register an active AgentRunner or runtime engine for lifecycle control."""
        self._runners[job_id] = runner

    def unregister_runner(self, job_id: str) -> None:
        """Unregister an AgentRunner when execution concludes."""
        self._runners.pop(job_id, None)
        self._pause_events.pop(job_id, None)

    def get_runner(self, job_id: str) -> Optional[Any]:
        """Fetch the active AgentRunner associated with a job."""
        return self._runners.get(job_id)

    def register_pause_event(self, job_id: str, event: asyncio.Event) -> None:
        """Register an asyncio.Event for pause coordination."""
        self._pause_events[job_id] = event

    def get_pause_event(self, job_id: str) -> Optional[asyncio.Event]:
        """Fetch the pause event associated with a job."""
        return self._pause_events.get(job_id)

    def pause_job(self, job_id: str) -> bool:
        """Mark job status as paused and propagate pause signal to the active runner."""
        job = self._jobs.get(job_id)
        if job and job.status in ("running", "resuming"):
            job.status = "paused"
            job.is_paused = True
            runner = self._runners.get(job_id)
            if runner and hasattr(runner, "pause"):
                runner.pause()
            event = self._pause_events.get(job_id)
            if event:
                event.set()
            return True
        return False

    def resume_job(self, job_id: str) -> bool:
        """Mark job status as running and propagate resume signal to the active runner."""
        job = self._jobs.get(job_id)
        if job and (job.status == "paused" or job.is_paused):
            job.status = "running"
            job.is_paused = False
            runner = self._runners.get(job_id)
            if runner and hasattr(runner, "resume"):
                runner.resume()
            event = self._pause_events.get(job_id)
            if event:
                event.clear()
            return True
        return False

    def append_event(self, job_id: str, event: Dict[str, Any]) -> None:
        job = self._jobs.get(job_id)
        if job:
            job.events.append(event)

    def update_status(
        self,
        job_id: str,
        status: SessionStatus,
        result: Optional[str] = None,
        error: Optional[str] = None,
        checkpoint_id: Optional[str] = None,
        current_step: Optional[int] = None,
    ) -> None:
        """
        Update a job's canonical status.

        Runtime guards (defense in depth):

          1. Legacy aliases (``stopped``, ``active``, ``done``, ``error``)
             are silently normalized to their canonical equivalents. This
             protects older call sites and historic data that may still
             carry pre-contract values.

          2. Unknown values are logged at WARNING and replaced with
             ``failed`` so the job terminates deterministically instead
             of being stuck in an unrecognized state. This is a fail-safe
             behaviour: the caller still observes the update, but the
             on-disk and in-memory status is guaranteed to be one of the
             twelve canonical values.

          3. Terminal statuses are frozen. Once a job reaches a terminal
             state, subsequent ``update_status`` calls are ignored (with
             a DEBUG log). This prevents late-arriving events from
             resurrecting a completed or cancelled run.
        """
        # Layer 1: legacy alias normalization.
        normalized = _LEGACY_STATUS_MAP.get(status, status)  # type: ignore[arg-type]

        # Layer 2: unknown-value guard.
        if normalized not in _VALID_STATUSES:
            logger.warning(
                "update_status called with non-canonical status %r for job "
                "%s; defaulting to 'failed'. Update the caller to use a "
                "value from SessionStatus.",
                status,
                job_id,
            )
            normalized = "failed"

        job = self._jobs.get(job_id)
        if not job:
            return

        # Layer 3: freeze on terminal. A subsequent update is a no-op
        # because a run that has already ended can never re-enter a
        # non-terminal state.
        if job.status in TERMINAL_STATUSES:
            logger.debug(
                "Ignoring update_status(%s -> %s): job is already in "
                "terminal state %r.",
                job.status,
                normalized,
                job.status,
            )
            return

        job.status = normalized
        if normalized == "paused":
            job.is_paused = True
        elif normalized == "running":
            job.is_paused = False
        if result is not None:
            job.result = result
        if error is not None:
            job.error = error
        if checkpoint_id is not None:
            job.checkpoint_id = checkpoint_id
        if current_step is not None:
            job.current_step = current_step

    def complete_job(self, job_id: str, result: Optional[str] = None) -> None:
        """Marks the job as completed with the given result text."""
        self.update_status(job_id, status="completed", result=result)
        self.unregister_runner(job_id)

    def fail_job(self, job_id: str, error: Optional[str] = None) -> None:
        """Marks the job as failed with the given error message."""
        self.update_status(job_id, status="failed", error=error)
        self.unregister_runner(job_id)

    def list_jobs(self) -> List[Dict[str, Any]]:
        return [
            job.to_dict()
            for job in sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        ]


job_manager = JobManager()
