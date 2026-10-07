"""
Context Management Data Contracts and Schemas for peldrun-core.

Defines Pydantic models for the Sidecar pattern (session.context.json),
representing decoupled conversation history, execution state, and token budget allocations.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field


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
    steps: List[ExecutionStep] = Field(
        default_factory=list,
        description="Tool calls and observations for the active turn.",
    )
    artifacts: List[str] = Field(
        default_factory=list,
        description="Generated artifact paths or file names for this turn.",
    )


class BudgetSections(BaseModel):
    """Allocated token caps across the five architectural context slices."""

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


class BudgetRatios(BaseModel):
    """Percentage allocations for the five context slices (normalized to 100%)."""

    system: int = Field(default=10, description="System percentage.")
    summary: int = Field(default=15, description="Summary percentage.")
    recent: int = Field(default=50, description="Recent turns percentage.")
    execution: int = Field(default=15, description="Execution state percentage.")
    current: int = Field(default=10, description="Current message percentage.")


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