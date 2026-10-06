"""
backend/peldrun/engine/state.py

PELDRUN Core Execution State and Checkpointing Subsystem.
Implements a strict Finite State Machine (FSM) enforcing transition boundaries.
Provides strictly typed, immutable-friendly execution state models with Pydantic v2.
"""

from __future__ import annotations

import copy
import logging
import time
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Union
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("peldrun.engine.state")


class ExecutionStatus(str, Enum):
    """
    Lifecycle execution statuses for agent runs.
    Superset supporting canonical PELDRUN Runtime V1 statuses and legacy shims.
    """
    QUEUED = "queued"
    IDLE = "idle"  # Legacy alias for QUEUED
    STARTING = "starting"
    RUNNING = "running"
    WAITING_FOR_INPUT = "waiting_for_input"
    WAITING_FOR_HUMAN = "waiting_for_human"  # Legacy alias for WAITING_FOR_INPUT
    RETRYING = "retrying"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class InvalidStateTransitionError(RuntimeError):
    """Raised when an illegal lifecycle transition is attempted."""
    pass


# Central Finite State Machine Transition Matrix
VALID_TRANSITIONS: Dict[ExecutionStatus, Set[ExecutionStatus]] = {
    ExecutionStatus.QUEUED: {
        ExecutionStatus.STARTING,
        ExecutionStatus.RUNNING,
        ExecutionStatus.CANCELLING,
        ExecutionStatus.CANCELLED,
    },
    ExecutionStatus.IDLE: {
        ExecutionStatus.STARTING,
        ExecutionStatus.RUNNING,
        ExecutionStatus.CANCELLING,
        ExecutionStatus.CANCELLED,
    },
    ExecutionStatus.STARTING: {
        ExecutionStatus.RUNNING,
        ExecutionStatus.FAILED,
        ExecutionStatus.CANCELLING,
        ExecutionStatus.CANCELLED,
    },
    ExecutionStatus.RUNNING: {
        ExecutionStatus.WAITING_FOR_INPUT,
        ExecutionStatus.WAITING_FOR_HUMAN,
        ExecutionStatus.RETRYING,
        ExecutionStatus.PAUSED,
        ExecutionStatus.CANCELLING,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.COMPLETED,
        ExecutionStatus.FAILED,
    },
    ExecutionStatus.WAITING_FOR_INPUT: {
        ExecutionStatus.RUNNING,
        ExecutionStatus.CANCELLING,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.FAILED,
        ExecutionStatus.PAUSED,
    },
    ExecutionStatus.WAITING_FOR_HUMAN: {
        ExecutionStatus.RUNNING,
        ExecutionStatus.CANCELLING,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.FAILED,
        ExecutionStatus.PAUSED,
    },
    ExecutionStatus.RETRYING: {
        ExecutionStatus.RUNNING,
        ExecutionStatus.FAILED,
        ExecutionStatus.CANCELLING,
        ExecutionStatus.CANCELLED,
    },
    ExecutionStatus.PAUSED: {
        ExecutionStatus.RUNNING,
        ExecutionStatus.CANCELLING,
        ExecutionStatus.CANCELLED,
    },
    ExecutionStatus.CANCELLING: {
        ExecutionStatus.CANCELLED,
        ExecutionStatus.FAILED,
    },
    ExecutionStatus.CANCELLED: set(),
    ExecutionStatus.COMPLETED: set(),
    ExecutionStatus.FAILED: set(),
}


class MessageRole(str, Enum):
    """Standard message roles compatible with OpenAI chat completion formats."""
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ChatMessage(BaseModel):
    """Represents an atomic message within the agent reasoning context."""
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    role: MessageRole = Field(..., description="Role of the message sender")
    content: Optional[str] = Field(default=None, description="Textual content or thought trace")
    name: Optional[str] = Field(default=None, description="Optional name identifier for tool or participant")
    tool_calls: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Structured tool call invocations emitted by the assistant",
    )
    tool_call_id: Optional[str] = Field(
        default=None,
        description="Associated tool call identifier when role is TOOL",
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Auxiliary context such as tokens or step index",
    )

    def to_llm_dict(self) -> Dict[str, Any]:
        """Export clean payload suitable for LLM provider APIs."""
        payload: Dict[str, Any] = {"role": self.role.value}
        if self.content is not None:
            payload["content"] = self.content
        if self.name:
            payload["name"] = self.name
        if self.tool_calls is not None:
            payload["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            payload["tool_call_id"] = self.tool_call_id
        return payload


class ToolExecutionRecord(BaseModel):
    """Historical trace of an executed tool action and its observation outcome."""
    model_config = ConfigDict(extra="ignore")

    call_id: str = Field(default="", description="Unique tool invocation identifier")
    tool_name: str = Field(..., description="Name of the invoked tool")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="Arguments supplied to the tool")
    output: Any = Field(default=None, description="Resulting output or observation returned by the tool")
    exit_code: int = Field(default=0, description="Execution status code (0 = success)")
    is_error: bool = Field(default=False, description="Flag indicating if invocation failed")
    attempt: int = Field(default=1, description="Sequential attempt count for this invocation")
    artifacts: List[str] = Field(default_factory=list, description="Files or artifacts produced by the tool")
    timestamp: float = Field(default_factory=time.time, description="Timestamp of execution")


class Checkpoint(BaseModel):
    """Immutable snapshot of the execution state at a discrete step."""
    model_config = ConfigDict(extra="ignore")

    checkpoint_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique identifier of this checkpoint",
    )
    step: int = Field(..., description="Step index when checkpoint was taken")
    timestamp: float = Field(default_factory=time.time, description="Creation epoch timestamp")
    state_dump: Dict[str, Any] = Field(..., description="Serialized representation of the state")


class ExecutionState(BaseModel):
    """
    Central state container tracking agent execution lifecycle, history, and artifacts.
    Enforces Finite State Machine validations, preventing arbitrary illegal status mutation.
    """
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    run_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Globally unique identifier for the execution run",
    )
    task_prompt: str = Field(default="", description="Original user directive or overarching objective")
    status: ExecutionStatus = Field(
        default=ExecutionStatus.IDLE,
        description="Current operational status governed by the state machine",
    )
    current_step: int = Field(default=0, description="Current 1-based execution step index")
    max_steps: int = Field(default=30, description="Configured ceiling for reasoning iterations")
    agent_name: str = Field(default="PrimaryAgent", description="Identifier of the executing agent")
    workspace_root: Optional[str] = Field(
        default=None,
        description="Filesystem root bounding tool operations",
    )
    messages: List[ChatMessage] = Field(
        default_factory=list,
        description="Chronological message dialogue history",
    )
    tool_history: List[ToolExecutionRecord] = Field(
        default_factory=list,
        description="Audit log of external tool executions",
    )
    deliverables: List[str] = Field(
        default_factory=list,
        description="Produced workspace artifact file paths",
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary execution context and telemetry metrics",
    )
    checkpoints: List[Checkpoint] = Field(
        default_factory=list,
        description="Recorded checkpoints across steps",
    )
    final_output: Optional[str] = Field(
        default=None,
        description="Final textual output produced when the task is completed",
    )
    transition_history: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Audit ledger of all lifecycle state transitions",
    )
    updated_at: float = Field(
        default_factory=time.time,
        description="Epoch timestamp of the last state mutation",
    )

    def __setattr__(self, name: str, value: Any) -> None:
        """
        Intercept attribute assignments to enforce state machine validation on status updates.
        Protects against arbitrary mutations while keeping attribute assignment syntax compatible.
        """
        if name == "status":
            current_status = getattr(self, "status", None)
            if current_status is not None:
                target_status = (
                    ExecutionStatus(value)
                    if not isinstance(value, ExecutionStatus)
                    else value
                )
                if target_status != current_status:
                    allowed = VALID_TRANSITIONS.get(current_status, set())
                    if target_status not in allowed:
                        raise InvalidStateTransitionError(
                            f"Illegal execution state transition: Cannot transition from "
                            f"'{current_status.value}' to '{target_status.value}'."
                        )
                    history = getattr(self, "transition_history", None)
                    if history is not None:
                        history.append({
                            "from_status": current_status.value,
                            "to_status": target_status.value,
                            "timestamp": time.time(),
                            "step": getattr(self, "current_step", 0),
                        })
                    super().__setattr__("updated_at", time.time())
                    super().__setattr__("status", target_status)
                    return
        super().__setattr__(name, value)

    # ----------------------------------------------------------------- aliases
    @property
    def step(self) -> int:
        """Backward-compatible alias for `current_step`."""
        return self.current_step

    @step.setter
    def step(self, value: int) -> None:
        self.current_step = value
        self.updated_at = time.time()

    @property
    def output(self) -> Optional[str]:
        """Backward-compatible alias for `final_output`."""
        return self.final_output

    @output.setter
    def output(self, value: Optional[str]) -> None:
        self.final_output = value
        self.updated_at = time.time()

    @property
    def tool_calls(self) -> List[ToolExecutionRecord]:
        """Backward-compatible alias for `tool_history`."""
        return self.tool_history

    @property
    def artifacts(self) -> List[str]:
        """Backward-compatible alias for `deliverables`."""
        return self.deliverables

    @property
    def is_completed(self) -> bool:
        """True when the run has reached the COMPLETED terminal state."""
        return self.status == ExecutionStatus.COMPLETED

    @property
    def is_failed(self) -> bool:
        """True when the run has reached the FAILED terminal state."""
        return self.status == ExecutionStatus.FAILED

    @property
    def is_error(self) -> bool:
        """Backward-compatible alias for `is_failed`."""
        return self.is_failed

    @property
    def is_cancelled(self) -> bool:
        """True when the run has reached the CANCELLED terminal state."""
        return self.status == ExecutionStatus.CANCELLED

    @property
    def is_cancelling(self) -> bool:
        """True when the run is actively halting in response to cancellation."""
        return self.status == ExecutionStatus.CANCELLING

    @property
    def is_terminal(self) -> bool:
        """True when the run has reached an immutable terminal state."""
        return self.status in (
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        )

    # ------------------------------------------------------------- transitions
    def transition_to(
        self,
        target_status: Union[ExecutionStatus, str],
        reason: str = "",
    ) -> None:
        """
        Explicit central transition mechanism with structured reasoning logging.
        Validates transition legality against the FSM graph.
        """
        target = (
            ExecutionStatus(target_status)
            if not isinstance(target_status, ExecutionStatus)
            else target_status
        )
        if target == self.status:
            self.updated_at = time.time()
            return

        allowed = VALID_TRANSITIONS.get(self.status, set())
        if target not in allowed:
            raise InvalidStateTransitionError(
                f"Illegal execution state transition: Cannot transition from "
                f"'{self.status.value}' to '{target.value}'. Reason: {reason or 'unspecified'}"
            )

        self.transition_history.append({
            "from_status": self.status.value,
            "to_status": target.value,
            "reason": reason,
            "timestamp": time.time(),
            "step": self.current_step,
        })
        super().__setattr__("status", target)
        self.updated_at = time.time()

    def mark_starting(self, reason: str = "") -> None:
        """Transition state into STARTING."""
        self.transition_to(ExecutionStatus.STARTING, reason=reason)

    def mark_running(self, reason: str = "") -> None:
        """Transition into RUNNING state."""
        self.transition_to(ExecutionStatus.RUNNING, reason=reason)

    def mark_waiting_for_input(self, reason: str = "") -> None:
        """Transition into WAITING_FOR_INPUT state."""
        self.transition_to(ExecutionStatus.WAITING_FOR_INPUT, reason=reason)

    def mark_waiting_for_human(self, reason: str = "") -> None:
        """Backward-compatible transition into WAITING_FOR_HUMAN."""
        try:
            self.transition_to(ExecutionStatus.WAITING_FOR_HUMAN, reason=reason)
        except InvalidStateTransitionError:
            self.transition_to(ExecutionStatus.WAITING_FOR_INPUT, reason=reason)

    def mark_retrying(self, reason: str = "") -> None:
        """Transition into RETRYING state."""
        self.transition_to(ExecutionStatus.RETRYING, reason=reason)

    def mark_paused(self, reason: str = "") -> None:
        """Transition into PAUSED state."""
        self.transition_to(ExecutionStatus.PAUSED, reason=reason)

    def mark_cancelling(self, reason: str = "") -> None:
        """Transition into CANCELLING state."""
        self.transition_to(ExecutionStatus.CANCELLING, reason=reason)

    def mark_cancelled(self, reason: str = "") -> None:
        """Transition into final CANCELLED terminal state."""
        self.transition_to(ExecutionStatus.CANCELLED, reason=reason)

    def mark_completed(self, output: Optional[str] = None, reason: str = "") -> None:
        """Transition into COMPLETED and attach deliverables/final output."""
        if output is not None:
            self.final_output = output
        self.transition_to(ExecutionStatus.COMPLETED, reason=reason)

    def mark_failed(
        self,
        message: str,
        error_code: Optional[str] = None,
        reason: str = "",
    ) -> None:
        """Transition into FAILED terminal state and record diagnostics."""
        self.metadata["error"] = message
        self.metadata["error_code"] = error_code or "RUNTIME_ERROR"
        self.metadata["error_ts"] = time.time()
        self.transition_to(ExecutionStatus.FAILED, reason=reason or message)

    def mark_error(self, message: str) -> None:
        """Backward-compatible alias for `mark_failed`."""
        self.mark_failed(message=message, error_code="GENERAL_ERROR")

    # ---------------------------------------------------------------- messages
    def add_message(
        self,
        role: MessageRole,
        content: Optional[str] = None,
        name: Optional[str] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        tool_call_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ChatMessage:
        """Append a new message to the conversation history."""
        msg = ChatMessage(
            role=role,
            content=content,
            name=name,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            metadata=metadata or {},
        )
        self.messages.append(msg)
        self.updated_at = time.time()
        return msg

    # ------------------------------------------------------------------ tools
    def record_tool_execution(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        output: Any,
        exit_code: int = 0,
        is_error: bool = False,
        artifacts: Optional[List[str]] = None,
        tool_call_id: str = "",
        call_id: Optional[str] = None,
        attempt: int = 1,
    ) -> ToolExecutionRecord:
        """Log a completed tool execution into the state record."""
        actual_id = tool_call_id or call_id or ""
        record = ToolExecutionRecord(
            call_id=actual_id,
            tool_name=tool_name,
            arguments=arguments,
            output=output,
            exit_code=exit_code,
            is_error=is_error,
            attempt=attempt,
            artifacts=list(artifacts or []),
        )
        self.tool_history.append(record)
        for art in (artifacts or []):
            if art not in self.deliverables:
                self.deliverables.append(art)
        self.updated_at = time.time()
        return record

    # ------------------------------------------------------------ deliverables
    def add_deliverable(self, file_path: str) -> None:
        """Register a newly generated artifact deliverable (idempotent)."""
        if file_path not in self.deliverables:
            self.deliverables.append(file_path)
            self.updated_at = time.time()

    # ----------------------------------------------------------- checkpointing
    def create_checkpoint(self) -> Checkpoint:
        """Create and append an immutable snapshot of the current state."""
        raw_dump = self.model_dump(mode="json", exclude={"checkpoints"})
        checkpoint = Checkpoint(
            step=self.current_step,
            state_dump=copy.deepcopy(raw_dump),
        )
        self.checkpoints.append(checkpoint)
        return checkpoint

    def restore_checkpoint(self, checkpoint_id: str) -> bool:
        """Revert active state to snapshot identified by checkpoint_id."""
        target = next((cp for cp in self.checkpoints if cp.checkpoint_id == checkpoint_id), None)
        if not target:
            return False

        restored_data = copy.deepcopy(target.state_dump)
        for key, value in restored_data.items():
            if hasattr(self, key):
                super().__setattr__(key, value)
        self.updated_at = time.time()
        return True

    # ---------------------------------------------------------- serialization
    def to_snapshot_dict(self) -> Dict[str, Any]:
        """Serialize current state to plain JSON-compatible dict."""
        return self.model_dump(mode="json", exclude={"checkpoints"})

    @classmethod
    def from_snapshot_dict(cls, data: Dict[str, Any]) -> "ExecutionState":
        """Reconstruct an ExecutionState from a dumped snapshot."""
        return cls(**data)

    # -------------------------------------------------------------------- llm
    def get_llm_messages(self) -> List[Dict[str, Any]]:
        """Extract clean message list formatted directly for LLM provider API requests."""
        return [msg.to_llm_dict() for msg in self.messages]