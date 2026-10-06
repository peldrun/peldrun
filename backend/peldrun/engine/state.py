"""
PELDRUN Core Execution State and Checkpointing Subsystem.
Provides strictly typed, immutable-friendly execution state models with Pydantic v2.
"""

from __future__ import annotations

import copy
import time
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field


class ExecutionStatus(str, Enum):
    """Lifecycle execution statuses for agent runs."""
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_FOR_HUMAN = "waiting_for_human"
    COMPLETED = "completed"
    FAILED = "failed"


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
    Provides checkpointing, restoration, and scoped context tracking.
    """
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    run_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Globally unique identifier for the execution run",
    )
    task_prompt: str = Field(default="", description="Original user directive or overarching objective")
    status: ExecutionStatus = Field(
        default=ExecutionStatus.IDLE,
        description="Current operational status of the agent",
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
    updated_at: float = Field(
        default_factory=time.time,
        description="Epoch timestamp of the last state mutation",
    )

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
    def is_error(self) -> bool:
        """True when the run has reached the FAILED terminal state."""
        return self.status == ExecutionStatus.FAILED

    # ------------------------------------------------------------- transitions
    def mark_running(self) -> None:
        """Transition into RUNNING and refresh the update timestamp."""
        self.status = ExecutionStatus.RUNNING
        self.updated_at = time.time()

    def mark_completed(self, output: Optional[str] = None) -> None:
        """Transition into COMPLETED and record the final output (if any)."""
        self.status = ExecutionStatus.COMPLETED
        if output is not None:
            self.final_output = output
        self.updated_at = time.time()

    def mark_error(self, message: str) -> None:
        """Transition into FAILED and record the diagnostic message."""
        self.status = ExecutionStatus.FAILED
        self.metadata["error"] = message
        self.metadata["error_ts"] = time.time()
        self.updated_at = time.time()

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
    ) -> ToolExecutionRecord:
        """
        Log a completed tool execution into the state record.

        Accepts both `tool_call_id` (preferred, used by agents) and the legacy
        `call_id` keyword for backwards compatibility with older call sites.
        """
        actual_id = tool_call_id or call_id or ""
        record = ToolExecutionRecord(
            call_id=actual_id,
            tool_name=tool_name,
            arguments=arguments,
            output=output,
            exit_code=exit_code,
            is_error=is_error,
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
        """
        Create and append an immutable snapshot of the current state.
        Omits past checkpoints recursively to prevent exponential bloat.
        """
        raw_dump = self.model_dump(mode="json", exclude={"checkpoints"})
        checkpoint = Checkpoint(
            step=self.current_step,
            state_dump=copy.deepcopy(raw_dump),
        )
        self.checkpoints.append(checkpoint)
        return checkpoint

    def restore_checkpoint(self, checkpoint_id: str) -> bool:
        """
        Revert the active state to the snapshot identified by checkpoint_id.
        Returns True if successful, False if the checkpoint was not found.
        """
        target = next((cp for cp in self.checkpoints if cp.checkpoint_id == checkpoint_id), None)
        if not target:
            return False

        restored_data = copy.deepcopy(target.state_dump)
        for key, value in restored_data.items():
            if hasattr(self, key):
                setattr(self, key, value)
        self.updated_at = time.time()
        return True

    # ---------------------------------------------------------- serialization
    def to_snapshot_dict(self) -> Dict[str, Any]:
        """Serialize the current state to a plain JSON-compatible dict."""
        return self.model_dump(mode="json", exclude={"checkpoints"})

    @classmethod
    def from_snapshot_dict(cls, data: Dict[str, Any]) -> "ExecutionState":
        """Reconstruct an ExecutionState from a previously dumped snapshot."""
        return cls(**data)

    # -------------------------------------------------------------------- llm
    def get_llm_messages(self) -> List[Dict[str, Any]]:
        """Extract clean message list formatted directly for LLM provider API requests."""
        return [msg.to_llm_dict() for msg in self.messages]