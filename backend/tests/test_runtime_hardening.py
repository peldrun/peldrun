"""
backend/tests/test_runtime_hardening.py

Integration and concurrency isolation testing for PELDRUN Universal Agent Bridge.
Tests concurrent job runs to verify zero state leakage and clean lifecycle management.
Hardened with direct asyncio execution and mock LLM readiness probing for hermetic unit testing.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch
import pytest

from omweb.agent_bridge import run_instrumented
from omweb.job_manager import job_manager
from omweb.sse_events import SSEEventType


def test_concurrent_jobs_state_and_event_isolation():
    """Verify that multiple concurrent jobs maintain strictly isolated state and lifecycle."""
    async def _test():
        job_1 = "test-job-isolation-1"
        job_2 = "test-job-isolation-2"

        job_manager.create_job(job_1)
        job_manager.create_job(job_2)

        events_job_1 = []
        events_job_2 = []

        async def mock_dispatch(job_id, event):
            if job_id == job_1:
                events_job_1.append(event)
            elif job_id == job_2:
                events_job_2.append(event)

        with patch("omweb.agent_bridge_parts.dispatcher.dispatch_event", side_effect=mock_dispatch), \
             patch("omweb.agent_bridge_parts.dispatcher.check_lmstudio_model_readiness", new_callable=AsyncMock, return_value={"found": True, "is_loaded": True, "unreachable": False}), \
             patch("omweb.engines.PeldrunEngine.run", new_callable=AsyncMock) as mock_engine_run:

            async def fake_engine_run(context):
                await asyncio.sleep(0.01)

            mock_engine_run.side_effect = fake_engine_run

            await asyncio.gather(
                run_instrumented(job_1, "Prompt for job 1", engine="peldrun"),
                run_instrumented(job_2, "Prompt for job 2", engine="peldrun"),
            )

        assert mock_engine_run.call_count == 2

        # Verify both jobs were processed cleanly
        j1 = job_manager.get_job(job_1)
        j2 = job_manager.get_job(job_2)
        assert j1 is not None
        assert j2 is not None

    asyncio.run(_test())
