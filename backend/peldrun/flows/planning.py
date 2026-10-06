"""
PELDRUN Core Plan State Models.
Defines structured plan representations, individual execution steps,
and dynamic progress tracking.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field


class StepStatus(str, Enum):
    """Status lifecycle of an individual plan step."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class PlanStep(BaseModel):
    """Individual action step within an execution plan."""
    model_config = ConfigDict(extra="allow")

    index: int = Field(..., description="Step sequence index (1-based)")
    title: str = Field(..., description="Short summary of the step objective")
    description: str = Field(default="", description="Detailed step instructions")
    status: StepStatus = Field(default=StepStatus.PENDING, description="Execution status")
    output: Optional[str] = Field(default=None, description="Result or artifact from executing this step")
    error: Optional[str] = Field(default=None, description="Error message if execution failed")
    updated_at: float = Field(default_factory=time.time, description="Last update timestamp")


class PlanState(BaseModel):
    """Active execution plan composed of ordered sequential steps."""
    model_config = ConfigDict(extra="ignore")

    task: str = Field(..., description="Original user prompt or task goal")
    steps: List[PlanStep] = Field(default_factory=list, description="Ordered plan steps")
    current_step_index: int = Field(default=1, description="Index of currently active step")
    is_completed: bool = Field(default=False, description="Flag indicating full plan completion")

    def get_current_step(self) -> Optional[PlanStep]:
        """Retrieve the currently active plan step."""
        for step in self.steps:
            if step.index == self.current_step_index:
                return step
        return None

    def mark_step_in_progress(self, index: int) -> Optional[PlanStep]:
        """Update a step status to in_progress."""
        for step in self.steps:
            if step.index == index:
                step.status = StepStatus.IN_PROGRESS
                step.updated_at = time.time()
                self.current_step_index = index
                return step
        return None

    def mark_step_completed(self, index: int, output: str = "") -> Optional[PlanStep]:
        """Update a step status to completed and advance current_step_index."""
        for step in self.steps:
            if step.index == index:
                step.status = StepStatus.COMPLETED
                step.output = output
                step.updated_at = time.time()
                # Advance to next pending step
                next_pending = [s for s in self.steps if s.index > index and s.status == StepStatus.PENDING]
                if next_pending:
                    self.current_step_index = next_pending[0].index
                else:
                    self.is_completed = True
                return step
        return None

    def mark_step_failed(self, index: int, error: str = "") -> Optional[PlanStep]:
        """Update a step status to failed."""
        for step in self.steps:
            if step.index == index:
                step.status = StepStatus.FAILED
                step.error = error
                step.updated_at = time.time()
                return step
        return None

    def to_markdown(self) -> str:
        """Format the active plan as a Markdown checklist."""
        lines = [f"### Execution Plan: {self.task}"]
        status_icons = {
            StepStatus.PENDING: "[ ]",
            StepStatus.IN_PROGRESS: "[>]",
            StepStatus.COMPLETED: "[x]",
            StepStatus.FAILED: "[!]",
            StepStatus.SKIPPED: "[-]",
        }
        for step in self.steps:
            icon = status_icons.get(step.status, "[ ]")
            lines.append(f"{step.index}. {icon} **{step.title}**: {step.description}")
        return "\n".join(lines)