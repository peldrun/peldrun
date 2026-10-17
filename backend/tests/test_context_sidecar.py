# Path: backend/tests/test_context_sidecar.py

import json
from pathlib import Path
import tempfile
import pytest

from peldrun.context.sidecar import (
    build_context_sidecar,
    load_context_sidecar,
    save_context_sidecar,
    select_current_run_events,
    is_active_turn,
)
from peldrun.context.schema import ContextSidecar


def test_build_context_sidecar_basic():
    """Verify standard extraction of conversational turns and budget assignment."""
    session_data = {
        "id": "sess_test_101",
        "model": "qwen3-vl-8b-instruct",
        "turns": [
            {
                "id": "t1",
                "user": "Create a calculator app in vanilla JS",
                "assistant": "I have created index.html, style.css, and app.js.",
            },
            {
                "id": "t2",
                "user": "Now add a clear button",
                "assistant": "Clear button has been added.",
                "steps": [
                    {
                        "type": "tool_call",
                        "name": "str_replace_editor",
                        "args": {"path": "app.js"},
                    },
                    {
                        "type": "observation",
                        "result": "File edited successfully.",
                    },
                ],
                "artifacts": ["app.js"],
            },
        ],
    }

    sidecar = build_context_sidecar(
        session_id="sess_test_101",
        session_data=session_data,
        model_id="qwen3-vl-8b-instruct",
    )

    # 1. Assert schema structure
    assert sidecar.session_id == "sess_test_101"
    assert sidecar.schema_version == 1
    assert len(sidecar.conversation) == 4

    # 2. Assert clean conversation messages
    assert sidecar.conversation[0].role == "user"
    assert "calculator app" in sidecar.conversation[0].content
    assert sidecar.conversation[1].role == "assistant"

    # 3. Assert active execution state captured for latest turn only
    assert sidecar.current_execution is not None
    assert sidecar.current_execution.turn_id == "t2"
    assert len(sidecar.current_execution.steps) == 2
    assert sidecar.current_execution.artifacts == ["app.js"]


def test_sidecar_atomic_persistence():
    """Verify safe atomic persistence to session.context.json."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        dir_path = Path(tmp_dir)
        session_data = {"id": "s_persist", "turns": []}
        sidecar = build_context_sidecar("s_persist", session_data)

        saved_path = save_context_sidecar(dir_path, sidecar)
        assert saved_path.exists()
        assert saved_path.name == "session.context.json"

        loaded = load_context_sidecar(dir_path)
        assert loaded is not None
        assert loaded.session_id == "s_persist"


# ---------------------------------------------------------------------------
# P0-02 Acceptance Tests Suite
# ---------------------------------------------------------------------------

def test_p0_02_case1_completed_turn_steps_isolation():
    """Case 1: Older completed turns never leak their historical steps into CurrentExecution."""
    session_data = {
        "id": "sess_iso_1",
        "turns": [
            {
                "id": "t1",
                "status": "succeeded",
                "user": "Old task",
                "assistant": "Old done",
                "steps": [{"type": "tool_call", "name": "bash"}],
            },
            {
                "id": "t2",
                "status": "succeeded",
                "user": "Newer task",
                "assistant": "Newer done",
            },
        ],
    }
    sidecar = build_context_sidecar("sess_iso_1", session_data)
    # Both turns completed -> no active execution
    assert sidecar.current_execution is None


def test_p0_02_case2_running_turn_captured():
    """Case 2: Turn with explicit 'running' status captures active execution steps."""
    session_data = {
        "id": "sess_iso_2",
        "turns": [
            {
                "id": "t1",
                "status": "succeeded",
                "user": "First turn",
                "assistant": "Done",
            },
            {
                "id": "t2",
                "status": "running",
                "user": "Active turn",
                "steps": [{"type": "tool_call", "name": "http_fetch"}],
            },
        ],
    }
    sidecar = build_context_sidecar("sess_iso_2", session_data)
    assert sidecar.current_execution is not None
    assert sidecar.current_execution.turn_id == "t2"
    assert sidecar.current_execution.status == "running"
    assert sidecar.current_execution.steps[0].name == "http_fetch"


def test_p0_02_case3_waiting_for_user_resumable():
    """Case 3: Turn in 'waiting_for_user' preserves execution state for resumption."""
    session_data = {
        "id": "sess_iso_3",
        "turns": [
            {
                "id": "t1",
                "status": "waiting_for_user",
                "user": "Ask question",
                "steps": [
                    {
                        "type": "tool_call",
                        "name": "ask_human",
                        "args": {"prompt": "Confirm deletion?"},
                    }
                ],
            }
        ],
    }
    sidecar = build_context_sidecar("sess_iso_3", session_data)
    assert sidecar.current_execution is not None
    assert sidecar.current_execution.status == "waiting_for_user"
    assert sidecar.current_execution.steps[0].name == "ask_human"


def test_p0_02_case4_terminal_statuses_not_active():
    """Case 4: Latest turn with terminal statuses (succeeded, failed, cancelled) is never active."""
    for term_status in ("succeeded", "failed", "cancelled"):
        session_data = {
            "id": f"sess_term_{term_status}",
            "turns": [
                {
                    "id": "t1",
                    "status": term_status,
                    "user": "Task",
                    "steps": [{"type": "tool_call", "name": "bash"}],
                }
            ],
        }
        sidecar = build_context_sidecar(f"sess_term_{term_status}", session_data)
        assert sidecar.current_execution is None


def test_p0_02_case5_old_final_event_does_not_mask_new_run():
    """Case 5: Old terminal event in events.json does not mask a newer ongoing run."""
    events = [
        # Old run (job_1) finished
        {"type": "step_start", "run_id": "job_1"},
        {"type": "final", "run_id": "job_1"},
        # New run (job_2) currently active
        {"type": "step_start", "run_id": "job_2"},
        {"type": "tool_call", "run_id": "job_2", "toolName": "bash", "args": {"cmd": "pwd"}},
    ]
    active = select_current_run_events(events)
    assert len(active) == 2
    assert active[1]["toolName"] == "bash"


def test_p0_02_case6_active_run_with_terminal_event_cleared():
    """Case 6: Active run that concluded with a terminal event returns no active execution."""
    events = [
        {"type": "step_start", "run_id": "job_active"},
        {"type": "tool_call", "run_id": "job_active", "toolName": "bash"},
        {"type": "final", "run_id": "job_active"},
    ]
    active = select_current_run_events(events)
    assert active == []


def test_p0_02_case7_mismatched_run_id_isolation():
    """Case 7: Events belonging to an older run_id are never mixed into the current active run."""
    events = [
        {"type": "tool_call", "run_id": "job_old", "toolName": "str_replace_editor"},
        {"type": "final", "run_id": "job_old"},
        {"type": "tool_call", "run_id": "job_new", "toolName": "http_fetch"},
    ]
    active = select_current_run_events(events)
    assert len(active) == 1
    assert active[0]["toolName"] == "http_fetch"
