"""
Unit tests for Context Management Stage 1: Sidecar Pattern.

Verifies:
1. Non-destructive Sidecar creation from session data.
2. Separation of clean conversation dialogue from raw execution telemetry.
3. Accurate per-model token budget calculation and ratio distribution.
4. Atomic persistence of session.context.json without mutating session.json.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import pytest

from peldrun.context.schema import (
    BudgetRatios,
    ContextSidecar,
    ConversationMessage,
    CurrentExecution,
)
from peldrun.context.sidecar import (
    build_context_sidecar,
    get_sidecar_path,
    load_context_sidecar,
    save_context_sidecar,
)


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
    assert sidecar.current_execution.steps[0].name == "str_replace_editor"
    assert sidecar.current_execution.artifacts == ["app.js"]

    # 4. Assert token budget slices are mathematically valid
    budget = sidecar.budget
    assert budget.context_window > 0
    assert budget.effective_limit == budget.context_window - budget.max_output_tokens
    assert budget.sections.recent > 0
    assert budget.sections.system > 0


def test_sidecar_atomic_persistence():
    """Verify atomic write and round-trip load of session.context.json."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        session_dir = Path(tmp_dir)

        # Create dummy session.json to simulate frontend storage
        original_session = {
            "id": "sess_atomic_01",
            "messages": [
                {"role": "user", "content": "Hello PELDRUN"},
                {"role": "assistant", "content": "Hello! How can I help?"},
            ],
        }
        session_json_path = session_dir / "session.json"
        with open(session_json_path, "w", encoding="utf-8") as f:
            json.dump(original_session, f, indent=2)

        # Build and save sidecar
        sidecar = build_context_sidecar(
            session_id="sess_atomic_01",
            session_data=original_session,
            model_id="gemini-2.0-flash",
        )
        saved_path = save_context_sidecar(session_dir, sidecar)

        assert saved_path.exists()
        assert saved_path.name == "session.context.json"

        # Verify session.json remained unchanged
        with open(session_json_path, "r", encoding="utf-8") as f:
            persisted_session = json.load(f)
        assert persisted_session == original_session

        # Verify sidecar round-trip load
        loaded_sidecar = load_context_sidecar(session_dir)
        assert loaded_sidecar is not None
        assert loaded_sidecar.session_id == "sess_atomic_01"
        assert len(loaded_sidecar.conversation) == 2
        assert loaded_sidecar.budget.context_window == 1048576  # Gemini 1M spec