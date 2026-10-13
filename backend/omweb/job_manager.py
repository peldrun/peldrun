import asyncio
import time
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class Job(BaseModel):
    id: str
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
        status: str,
        result: Optional[str] = None,
        error: Optional[str] = None,
        checkpoint_id: Optional[str] = None,
        current_step: Optional[int] = None,
    ) -> None:
        job = self._jobs.get(job_id)
        if job:
            job.status = status
            if status == "paused":
                job.is_paused = True
            elif status == "running":
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
