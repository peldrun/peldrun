# Path: backend/peldrun/context/schema.py

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field

ExecutionStatus = Literal[
    "queued",
    "running",
    "waiting_for_user",
    "succeeded",
    "failed",
    "cancelled",
]


class TurnExecutionState(BaseModel):
    """Explicit turn execution lifecycle state model."""

    turn_id: str = Field(..., description="Unique turn identifier.")
    status: ExecutionStatus = Field(
        default="running", description="Current execution lifecycle status."
    )


class SummaryState(BaseModel):
    """Summarized historical context for turns that have been compacted."""

    covers_turns: List[str] = Field(
        default_factory=list,
        description="IDs of historical conversation turns condensed in this summary.",
    )
    content: str = Field(
        default="",
        description="Dense textual summary of compacted historical turns.",
    )
    tokens: int = Field(
        default=0,
        description="Estimated token count of the summary content.",
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="ISO-8601 timestamp of the most recent compaction.",
    )


class ConversationMessage(BaseModel):
    """Sanitized conversational turn sent to LLM (free of raw execution traces)."""

    turn_id: str = Field(..., description="Unique turn identifier.")
    role: Literal["user", "assistant", "system"] = Field(
        ..., description="Role of the message author."
    )
    content: str = Field(
        default="", description="Sanitized conversational text content."
    )
    timestamp: Optional[str] = Field(
        default=None, description="ISO-8601 creation timestamp."
    )


class ExecutionStep(BaseModel):
    """Single tool call, observation, or execution status entry."""

    type: Literal["tool_call", "observation", "status"] = Field(
        ..., description="Type of execution telemetry event."
    )
    name: Optional[str] = Field(
        default=None, description="Name of the tool being executed."
    )
    args: Optional[Dict[str, Any]] = Field(
        default=None, description="Arguments provided to the tool."
    )
    result: Optional[str] = Field(
        default=None, description="Observation or execution output text."
    )
    error: Optional[str] = Field(
        default=None, description="Error message if execution failed."
    )


class CurrentExecution(BaseModel):
    """Active execution state belonging exclusively to the ongoing/latest turn."""

    turn_id: str = Field(
        default="", description="Turn identifier currently executing."
    )
    status: ExecutionStatus = Field(
        default="running",
        description="Lifecycle status of this active execution.",
    )
    steps: List[ExecutionStep] = Field(
        default_factory=list,
        description="Tool calls and observations for the active turn.",
    )
    artifacts: List[str] = Field(
        default_factory=list,
        description="Generated artifact paths or file names for this turn.",
    )


class BudgetSections(BaseModel):
    """Allocated token caps across the six architectural context slices.

    The ``memory`` slice was added under P1.5 to give the retrieved
    memory block its own accounting bucket. Its default is 0 -- see
    ``BudgetRatios``'s docstring for the rationale behind the
    zero-valued default and the Phase 1 / Phase 2 split.
    """

    system: int = Field(
        default=0, description="Tokens reserved for system prompt & core rules."
    )
    summary: int = Field(
        default=0, description="Tokens reserved for compacted history summary."
    )
    recent: int = Field(
        default=0, description="Tokens reserved for recent literal conversation turns."
    )
    execution: int = Field(
        default=0, description="Tokens reserved for active execution state."
    )
    current: int = Field(
        default=0, description="Tokens reserved for the current user message."
    )
    # [P1.5] Token cap for the injected [RETRIEVED MEMORY] block.
    #
    # Populated by ``TokenBudgetController.fit_all`` from the
    # ``MemoryContext``'s own token accounting. Unlike the other
    # sections, this one is NOT derived from a static ratio: the
    # memory block's actual size is already bounded upstream by
    # ``peldrun.memory.domain.settings.MemoryTokenBudget.max_tokens``
    # (applied inside ``MemoryContextBuilder`` -- P1.4), so the
    # section here records what was ACTUALLY consumed, not what
    # was allocated.
    #
    # The default 0 means "no dedicated memory section" (Phase 1:
    # memory is fit within the recent-conversation slice's
    # headroom). A future Phase 2 can promote it to a real
    # allocation by setting a positive default here and a
    # matching ratio in ``BudgetRatios.memory``.
    memory: int = Field(
        default=0,
        description=(
            "Tokens consumed by the injected [RETRIEVED MEMORY] "
            "block. Bounded upstream by MemoryTokenBudget.max_tokens; "
            "0 means no memory was injected this turn."
        ),
    )


class BudgetRatios(BaseModel):
    """Percentage allocations for the six context slices (normalized to 100%).

    The ``memory`` slice was added under P1.5. Its Phase 1 default is
    0 -- the retrieved memory block is bounded upstream by
    ``MemoryTokenBudget.max_tokens`` and fit within the existing
    sections' headroom, so it does NOT consume a dedicated
    percentage of the context window.

    Why the default is 0, not 5 or 10
    ---------------------------------
    Every existing ratio below (system=10, summary=15, recent=50,
    execution=15, current=10) is a user-observable behavioral
    contract: a deployment that has run for months on "recent=50"
    must not silently lose half its recent-turn window because a
    new subsystem claimed 5%. Carving a positive ``memory`` slice
    would require reducing exactly one other slice -- breaking that
    contract for every existing user, including those who have NOT
    enabled memory.

    With ``memory=0``, the sum stays at 100. Memory entries fit
    within the existing slices' headroom (typically the ``recent``
    slice already has slack because it only fills with actual
    conversation turns), and the ``fit_all`` safety net (see
    ``peldrun.context.budget.TokenBudgetController``) trims
    ``recent`` further if the total ever exceeds the effective
    limit.

    A future Phase 2 -- once memory injection is proven stable
    across deployments -- can rebalance the ratios by reducing
    ``recent`` to 45 and setting ``memory=5``. That change will be
    a **behavioral** release (documented in the changelog), not a
    silent default flip.
    """

    system: int = Field(default=10, description="System percentage.")
    summary: int = Field(default=15, description="Summary percentage.")
    recent: int = Field(default=50, description="Recent turns percentage.")
    execution: int = Field(default=15, description="Execution state percentage.")
    current: int = Field(default=10, description="Current message percentage.")
    # [P1.5] Percentage of the context window for the memory block.
    #
    # Phase 1 keeps this at 0 -- memory entries fit within the
    # ``recent`` slice's headroom and are bounded upstream by
    # ``MemoryTokenBudget.max_tokens``. See this class's docstring
    # for the full rationale on why a positive default would be a
    # breaking behavioral change.
    memory: int = Field(
        default=0,
        description=(
            "Percentage of the context window allocated to the "
            "[RETRIEVED MEMORY] block. Phase 1: 0 (no dedicated "
            "slice; memory is fit within the recent slice's "
            "headroom and bounded upstream by MemoryTokenBudget)."
        ),
    )


class BudgetMetadata(BaseModel):
    """Token budget accounting, capacity limits, and section breakdowns."""

    total_tokens: int = Field(
        default=0, description="Total active prompt tokens consumed by context."
    )
    context_window: int = Field(
        default=8192, description="Total capacity window of the target model."
    )
    max_output_tokens: int = Field(
        default=1500, description="Tokens reserved exclusively for LLM generation."
    )
    effective_limit: int = Field(
        default=6692,
        description="Effective context input limit (context_window - max_output_tokens).",
    )
    ratios: BudgetRatios = Field(
        default_factory=BudgetRatios,
        description="Active allocation ratios applied to this context.",
    )
    sections: BudgetSections = Field(
        default_factory=BudgetSections,
        description="Calculated token limits for each architectural section.",
    )


class ContextSidecar(BaseModel):
    """
    Sidecar container persisted as session.context.json alongside session.json.

    Fully decoupled from frontend presentation history and rebuildable at any time.
    """

    session_id: str = Field(..., description="Unique chat session identifier.")
    schema_version: int = Field(
        default=1, description="Sidecar contract schema version."
    )
    built_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="ISO-8601 build timestamp.",
    )
    model_id: Optional[str] = Field(
        default=None, description="Active model identifier determining the budget."
    )
    summary: SummaryState = Field(
        default_factory=SummaryState,
        description="Compacted historical summary state.",
    )
    conversation: List[ConversationMessage] = Field(
        default_factory=list,
        description="Sanitized conversation turns for LLM prompt assembly.",
    )
    current_execution: Optional[CurrentExecution] = Field(
        default=None,
        description="Telemetry steps belonging only to the ongoing turn.",
    )
    budget: BudgetMetadata = Field(
        default_factory=BudgetMetadata,
        description="Calculated dynamic token budget.",
    )
