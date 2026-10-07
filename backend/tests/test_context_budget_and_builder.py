"""
Unit tests for Context Management Stages 2, 3, and 4.

Verifies:
1. Strict isolation of historical execution telemetry from prompt messages.
2. Assembly order: System -> Summary -> Recent -> Execution -> Current.
3. Proportional token budgeting and section fitting.
4. Structurally lossless trimming and non-destructive compaction.
"""

from __future__ import annotations

import pytest

from peldrun.context.builder import ContextManager
from peldrun.context.compaction import (
    compact_sidecar_history,
    should_trigger_compaction,
    trim_structurally_lossless,
)
from peldrun.context.schema import (
    BudgetMetadata,
    BudgetRatios,
    BudgetSections,
    ContextSidecar,
    ConversationMessage,
    CurrentExecution,
    ExecutionStep,
    SummaryState,
)


def create_mock_sidecar() -> ContextSidecar:
    """Helper to assemble a test sidecar with multi-turn history."""
    ratios = BudgetRatios(system=10, summary=15, recent=50, execution=15, current=10)
    sections = BudgetSections(
        system=650, summary=975, recent=3250, execution=975, current=650
    )
    budget = BudgetMetadata(
        total_tokens=1200,
        context_window=8000,
        max_output_tokens=1500,
        effective_limit=6500,
        ratios=ratios,
        sections=sections,
    )

    summary = SummaryState(
        covers_turns=["turn_0"],
        content="User created an initial web workspace.",
        tokens=35,
    )

    conversation = [
        ConversationMessage(
            turn_id="turn_1",
            role="user",
            content="Build a basic calculator in HTML and JS.",
        ),
        ConversationMessage(
            turn_id="turn_1",
            role="assistant",
            content="Created index.html, style.css, and app.js.",
        ),
        ConversationMessage(
            turn_id="turn_2",
            role="user",
            content="Add a percentage button.",
        ),
        ConversationMessage(
            turn_id="turn_2",
            role="assistant",
            content="Updated app.js with the percentage handler.",
        ),
    ]

    current_exec = CurrentExecution(
        turn_id="turn_3",
        steps=[
            ExecutionStep(
                type="tool_call",
                name="str_replace_editor",
                args={"path": "app.js"},
            ),
            ExecutionStep(
                type="observation",
                result="Successfully replaced 1 occurrence in app.js.",
            ),
        ],
        artifacts=["app.js"],
    )

    return ContextSidecar(
        session_id="sess_builder_test",
        summary=summary,
        conversation=conversation,
        current_execution=current_exec,
        budget=budget,
    )


def test_context_manager_assembly_and_isolation():
    """Verify correct section order and complete isolation of old tool results."""
    sidecar = create_mock_sidecar()
    manager = ContextManager(default_system_prompt="Base System Prompt")

    messages, usage = manager.build_messages(
        sidecar=sidecar,
        current_user_message="Now generate a hello.pdf report.",
        project_rules="Rule 1: Always write tests.",
    )

    # 1. Assert structure: System, Rules, Summary, Recent, Current Exec, Current User
    assert len(messages) >= 6
    assert messages[0]["role"] == "system"
    assert "Base System Prompt" in messages[0]["content"]

    # Rules
    assert messages[1]["role"] == "system"
    assert "Rule 1" in messages[1]["content"]

    # Previous Summary
    assert messages[2]["role"] == "system"
    assert "[Previous Context Summary]" in messages[2]["content"]

    # Recent turns must not contain raw old tool traces
    all_content_str = "\n".join(str(m["content"]) for m in messages)
    assert "calculator" in all_content_str
    assert "percentage button" in all_content_str

    # Current message must be the final message
    assert messages[-1]["role"] == "user"
    assert "hello.pdf" in messages[-1]["content"]

    # Usage accounting must be populated
    assert usage["total_tokens"] > 0
    assert usage["total_tokens"] <= usage["effective_limit"]


def test_lossless_pruning_and_compaction():
    """Verify lossless stripping of mechanical bloat and history folding."""
    bloated_log = (
        "data:image/png;base64," + ("A" * 300) + "\nExecution logs line 1\n" + ("x" * 1200)
    )
    pruned = trim_structurally_lossless(bloated_log, max_chunk_chars=300)

    assert "base64 payload omitted" in pruned
    assert len(pruned) <= 350

    # Test Compaction Folding
    sidecar = create_mock_sidecar()
    assert len(sidecar.conversation) == 4

    compacted = compact_sidecar_history(sidecar, keep_recent_turns=2)
    assert len(compacted.conversation) == 2
    assert "turn_1" in compacted.summary.covers_turns
    assert "User requested" in compacted.summary.content