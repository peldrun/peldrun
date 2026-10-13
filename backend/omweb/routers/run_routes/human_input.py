"""
Human-in-the-loop response endpoint.

Implements ``POST /jobs/{job_id}/respond`` which authoritatively records a
human answer, unblocks the suspended tool call, and — when the underlying
task is no longer alive — resumes execution from the persisted run state.

The endpoint enforces two guarantees:

* Idempotency: an already-answered request returns a success response with
  ``already_resolved=True`` instead of failing.
* Ownership: the request id is validated against the run store before the
  answer is dispatched to the core registry.
"""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, HTTPException

from omweb.agent_bridge import (
    human_answers,
    human_data,
    run_instrumented,
)
from omweb.job_manager import job_manager
from peldrun.runtime.contract import HumanInputStatus
from peldrun.runtime.store import get_run_store
from peldrun.tools.builtins.human_input import HumanInputRegistry

from .schemas import HumanRespondRequest
from .state import ACTIVE_JOB_TASKS
from .utils import resolve_chat


router = APIRouter()


@router.post("/jobs/{job_id}/respond")
async def respond_to_human_prompt(job_id: str, req: HumanRespondRequest):
    """
    Receive a human input response and unblock the suspended run.

    The endpoint resolves the pending request id (either from the payload or
    by querying the run store), validates idempotency, dispatches the answer
    to the core registry, and resumes execution when needed.
    """
    chat, chat_id = resolve_chat(job_id)

    # The canonical job id may differ from the caller-supplied identifier.
    target_job_id = job_id
    if chat and chat.get("job_id"):
        target_job_id = chat.get("job_id")

    answer = req.answer.strip()
    store = get_run_store()

    # ------------------------------------------------------------------
    # Resolve the request id.
    # ------------------------------------------------------------------
    resolved_req_id = req.request_id
    if not resolved_req_id:
        # Prefer the most recent pending request for this run.
        pending_requests = await store.list_pending_human_requests(
            run_id=target_job_id
        )
        if not pending_requests and chat_id:
            pending_requests = await store.list_pending_human_requests(run_id=chat_id)

        if pending_requests:
            resolved_req_id = pending_requests[-1].request_id

    # Fall back to the in-process registry when the store has nothing.
    if (
        not resolved_req_id
        and hasattr(HumanInputRegistry, "_pending_requests")
        and HumanInputRegistry._pending_requests
    ):
        resolved_req_id = list(HumanInputRegistry._pending_requests.keys())[-1]

    if not resolved_req_id:
        raise HTTPException(
            status_code=404,
            detail=f"No pending human input request found for job '{target_job_id}'.",
        )

    # ------------------------------------------------------------------
    # Idempotency: an already-answered request is a no-op success.
    # ------------------------------------------------------------------
    existing_req = await store.get_human_request(resolved_req_id)
    if existing_req and existing_req.status == HumanInputStatus.ANSWERED:
        return {
            "success": True,
            "job_id": target_job_id,
            "request_id": resolved_req_id,
            "already_resolved": True,
            "message": "Request was already resolved previously.",
        }

    # ------------------------------------------------------------------
    # Dispatch the answer to the core registry and to the legacy bridge.
    # ------------------------------------------------------------------
    core_resolved = HumanInputRegistry.resolve_request(resolved_req_id, answer)

    legacy_event = human_answers.get(target_job_id) or human_answers.get(job_id)
    if legacy_event and not legacy_event.is_set():
        human_data[target_job_id] = answer
        human_data[job_id] = answer
        legacy_event.set()

    job_manager.append_event(target_job_id, {
        "type": "human_response",
        "answer": answer,
        "request_id": resolved_req_id,
        "timestamp": time.time(),
    })

    # ------------------------------------------------------------------
    # Resume execution if the task is no longer alive.
    # ------------------------------------------------------------------
    is_actively_running = (
        target_job_id in ACTIVE_JOB_TASKS
        or job_id in ACTIVE_JOB_TASKS
        or (chat_id and chat_id in ACTIVE_JOB_TASKS)
    )

    resumed_run = False
    if not is_actively_running:
        saved_state = await store.load_state(target_job_id)
        if saved_state is not None:
            # Rehydrate the state with the human answer recorded as a tool
            # result, then schedule the continuation task.
            saved_state.mark_running(
                reason="Resuming execution following human response"
            )
            saved_state.add_message(
                role="tool",
                content=answer,
                name="ask_human",
                tool_call_id=resolved_req_id,
            )
            saved_state.record_tool_execution(
                tool_name="ask_human",
                arguments={"answer": answer},
                output=answer,
                tool_call_id=resolved_req_id,
            )
            await store.save_state(target_job_id, saved_state)

            resumed_task = asyncio.create_task(
                run_instrumented(target_job_id, saved_state.task_prompt)
            )
            ACTIVE_JOB_TASKS[target_job_id] = resumed_task
            resumed_run = True

    return {
        "success": True,
        "job_id": target_job_id,
        "chat_id": chat_id,
        "request_id": resolved_req_id,
        "answer": answer,
        "core_resolved": core_resolved,
        "resumed_run": resumed_run,
        "message": "Response recorded and agent execution progressed successfully",
    }
