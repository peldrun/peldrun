"""
backend/peldrun/events/schema.py

PELDRUN Core Event Protocol Schemas.
Implements the strict Canonical Envelope Pattern using Pydantic v2 for the unified runtime lifecycle.
Includes canonical Artifact lifecycle event models under Phase M2.
Hardened to support flexible run_id identifiers (UUID or canonical job/chat string tokens).
"""

from __future__ import annotations

import json
import time
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union
from uuid import UUID, uuid4
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EventType(str, Enum):
    """Supported protocol event types across agent tasks and direct chat interactions."""
    SNAPSHOT = "snapshot"
    STEP_START = "step_start"
    THOUGHT = "thought"
    AGENT_ACTIVITY = "agent_activity"
    TOOL_CALL = "tool_call"
    OBSERVATION = "observation"
    STEP_END = "step_end"
    FINAL = "final"
    ERROR = "error"
    ASK_HUMAN = "ask_human"

    # Canonical Artifact Lifecycle Events (Phase M2)
    ARTIFACT_CREATED = "artifact_created"
    ARTIFACT_UPDATED = "artifact_updated"
    ARTIFACT_DELETED = "artifact_deleted"
    ARTIFACT_MOVED = "artifact_moved"


# Typed helper payloads for domain modeling and strict event validation
class SnapshotPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    status: str = Field(default="idle")
    agent_name: Optional[str] = None
    step: int = 0
    total_steps: Optional[int] = None
    workspace_files: List[str] = Field(default_factory=list)
    active_tools: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    snapshot: Optional[Dict[str, Any]] = None


class StepStartPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    step: int = 0
    step_number: Optional[int] = None
    agent_name: Optional[str] = ""
    input_prompt: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _unify_step_fields(cls, values: Any) -> Any:
        if isinstance(values, dict):
            if "step_number" in values and "step" not in values:
                values["step"] = values["step_number"]
            elif "step" in values and "step_number" not in values:
                values["step_number"] = values["step"]
        return values


class ThoughtPayload(BaseModel):
    """Payload for internal chain-of-thought and reasoning traces."""
    model_config = ConfigDict(extra="allow")
    thought: str = ""
    is_delta: bool = False


class AgentActivityPayload(BaseModel):
    """Payload for public UX status updates and execution phase transitions."""
    model_config = ConfigDict(extra="allow")
    message: str = ""
    phase: str = "general"
    details: Dict[str, Any] = Field(default_factory=dict)


class ToolCallPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    tool_name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    tool_call_id: str = ""


class ObservationPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    tool_name: str = ""
    output: Any = ""
    exit_code: int = 0
    is_error: bool = False
    artifacts: List[Union[Dict[str, Any], str]] = Field(default_factory=list)
    tool_call_id: str = ""


class StepEndPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    step: int = 0
    step_number: Optional[int] = None
    elapsed_seconds: float = 0.0
    success: bool = True

    @model_validator(mode="before")
    @classmethod
    def _unify_step_fields(cls, values: Any) -> Any:
        if isinstance(values, dict):
            if "step_number" in values and "step" not in values:
                values["step"] = values["step_number"]
            elif "step" in values and "step_number" not in values:
                values["step_number"] = values["step"]
        return values


class FinalPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    output: str = ""
    deliverables: List[str] = Field(default_factory=list)
    total_steps: int = 1
    total_tokens: Optional[int] = None


class ErrorPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    error: str = ""
    error_type: str = "RuntimeError"
    details: Optional[Dict[str, Any]] = None
    recoverable: bool = False


class AskHumanPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    request_id: str = Field(default_factory=lambda: str(uuid4()))
    question: str
    options: List[str] = Field(default_factory=list)
    timeout_seconds: float = 300.0


class ArtifactPayload(BaseModel):
    """Canonical descriptor payload for workspace artifact lifecycle changes."""
    model_config = ConfigDict(extra="allow")
    artifact_id: str = Field(default_factory=lambda: str(uuid4()))
    job_id: Optional[str] = None
    run_id: Optional[str] = None
    name: str = Field(..., description="File name")
    relative_path: str = Field(..., description="Canonical relative path from workspace root")
    operation: str = Field(default="created", description="Operation type: created, updated, deleted, moved")
    mime_type: str = Field(default="application/octet-stream")
    size_bytes: int = Field(default=0)
    revision: int = Field(default=1, ge=1)
    sha256: Optional[str] = None
    source_tool: Optional[str] = None
    timestamp: float = Field(default_factory=time.time)
    metadata: Dict[str, Any] = Field(default_factory=dict)


EVENT_PAYLOAD_MAP: Dict[EventType, type[BaseModel]] = {
    EventType.SNAPSHOT: SnapshotPayload,
    EventType.STEP_START: StepStartPayload,
    EventType.THOUGHT: ThoughtPayload,
    EventType.AGENT_ACTIVITY: AgentActivityPayload,
    EventType.TOOL_CALL: ToolCallPayload,
    EventType.OBSERVATION: ObservationPayload,
    EventType.STEP_END: StepEndPayload,
    EventType.FINAL: FinalPayload,
    EventType.ERROR: ErrorPayload,
    EventType.ASK_HUMAN: AskHumanPayload,
    EventType.ARTIFACT_CREATED: ArtifactPayload,
    EventType.ARTIFACT_UPDATED: ArtifactPayload,
    EventType.ARTIFACT_DELETED: ArtifactPayload,
    EventType.ARTIFACT_MOVED: ArtifactPayload,
}


class PeldrunEvent(BaseModel):
    """
    Unified public event envelope for all PELDRUN runtime event dispatches.
    Enforces strict typing and forbids arbitrary top-level fields on the core envelope.
    """
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )

    version: int = Field(default=1, ge=1, description="Protocol envelope schema version")
    event_id: UUID = Field(default_factory=uuid4, description="Unique identifier for the event instance")
    sequence: int = Field(default=0, ge=0, description="Monotonically increasing sequence number within run")
    run_id: Union[UUID, str] = Field(default_factory=uuid4, description="Unique execution or session identifier")
    timestamp: float = Field(default_factory=time.time, description="Unix epoch timestamp in UTC seconds")
    step: int = Field(default=0, ge=0, description="Execution step index (0 for direct chat or initialization)")
    type: EventType = Field(..., description="Discriminator event type")
    payload: Dict[str, Any] = Field(default_factory=dict, description="Event-specific payload content")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Contextual tracing and producer metadata")

    @field_validator("event_id", mode="before")
    @classmethod
    def _validate_event_id(cls, value: Any) -> UUID:
        """Enforce strict UUID validation for atomic event instance identifier."""
        if value is None:
            return uuid4()
        if isinstance(value, UUID):
            return value
        if isinstance(value, str):
            return UUID(value)
        raise ValueError(f"Value must be a valid UUID or UUID string, got {type(value).__name__}")

    @field_validator("run_id", mode="before")
    @classmethod
    def _validate_run_id(cls, value: Any) -> Union[UUID, str]:
        """Validate run_id allowing UUID instances, UUID strings, or canonical job/chat string tokens."""
        if value is None:
            return uuid4()
        if isinstance(value, UUID):
            return value
        if isinstance(value, str):
            trimmed = value.strip()
            try:
                return UUID(trimmed)
            except ValueError:
                return trimmed
        raise ValueError(f"Value must be a valid UUID or string identifier, got {type(value).__name__}")

    @classmethod
    def _validate_uuid(cls, value: Any) -> UUID:
        """Backward-compatible helper ensuring strict UUID coercion."""
        if value is None:
            return uuid4()
        if isinstance(value, UUID):
            return value
        if isinstance(value, str):
            return UUID(value)
        raise ValueError(f"Value must be a valid UUID or UUID string, got {type(value).__name__}")

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_fields(cls, values: Any) -> Any:
        """Normalize legacy input fields (id -> event_id, data -> payload) on instantiation."""
        if not isinstance(values, dict):
            return values
        mutable = dict(values)
        if "id" in mutable and "event_id" not in mutable:
            mutable["event_id"] = mutable.pop("id")
        if "data" in mutable and "payload" not in mutable:
            mutable["payload"] = mutable.pop("data")
        return mutable

    @model_validator(mode="after")
    def _validate_payload_model(self) -> PeldrunEvent:
        """Validate payload against registered event payload model."""
        payload_cls = EVENT_PAYLOAD_MAP.get(self.type)
        if payload_cls is not None:
            if isinstance(self.payload, dict):
                validated = payload_cls.model_validate(self.payload)
                self.payload = validated.model_dump(mode="python")
            elif isinstance(self.payload, BaseModel):
                if not isinstance(self.payload, payload_cls):
                    raise ValueError(
                        f"Expected payload of type {payload_cls.__name__} for event type '{self.type.value}', "
                        f"got '{type(self.payload).__name__}'"
                    )
                self.payload = self.payload.model_dump(mode="python")
            else:
                raise ValueError(
                    f"Payload for event type '{self.type.value}' must be a dict or {payload_cls.__name__}, "
                    f"got '{type(self.payload).__name__}'"
                )
        return self

    @property
    def id(self) -> str:
        """String representation of event_id for Python backward compatibility."""
        return str(self.event_id)

    @property
    def data(self) -> Dict[str, Any]:
        """Dictionary access alias pointing to payload for Python backward compatibility."""
        return self.payload

    @data.setter
    def data(self, value: Dict[str, Any]) -> None:
        self.payload = value

    def to_sse_payload(self) -> Dict[str, Any]:
        """
        Return serialized JSON-safe dictionary representation of the canonical envelope.
        Strict protocol wire format without redundant alias keys.
        """
        return self.model_dump(mode="json")

    def to_sse(self) -> str:
        """Serialize event into standard SSE wire protocol frame."""
        serialized = self.to_sse_payload()
        event_name = self.type.value if hasattr(self.type, "value") else str(self.type)
        return f"event: {event_name}\ndata: {json.dumps(serialized, ensure_ascii=False)}\n\n"


# Aliases for backward compatibility
BaseEvent = PeldrunEvent
AgentEvent = PeldrunEvent


# Specialized convenience envelope subclasses with strictly enforced literal types
class SnapshotEvent(PeldrunEvent):
    type: Literal[EventType.SNAPSHOT] = EventType.SNAPSHOT


class StepStartEvent(PeldrunEvent):
    type: Literal[EventType.STEP_START] = EventType.STEP_START


class ThoughtEvent(PeldrunEvent):
    type: Literal[EventType.THOUGHT] = EventType.THOUGHT


class AgentActivityEvent(PeldrunEvent):
    type: Literal[EventType.AGENT_ACTIVITY] = EventType.AGENT_ACTIVITY


class ToolCallEvent(PeldrunEvent):
    type: Literal[EventType.TOOL_CALL] = EventType.TOOL_CALL


class ObservationEvent(PeldrunEvent):
    type: Literal[EventType.OBSERVATION] = EventType.OBSERVATION


class StepEndEvent(PeldrunEvent):
    type: Literal[EventType.STEP_END] = EventType.STEP_END


class FinalEvent(PeldrunEvent):
    type: Literal[EventType.FINAL] = EventType.FINAL


class ErrorEvent(PeldrunEvent):
    type: Literal[EventType.ERROR] = EventType.ERROR


class AskHumanEvent(PeldrunEvent):
    type: Literal[EventType.ASK_HUMAN] = EventType.ASK_HUMAN


class ArtifactCreatedEvent(PeldrunEvent):
    type: Literal[EventType.ARTIFACT_CREATED] = EventType.ARTIFACT_CREATED


class ArtifactUpdatedEvent(PeldrunEvent):
    type: Literal[EventType.ARTIFACT_UPDATED] = EventType.ARTIFACT_UPDATED


class ArtifactDeletedEvent(PeldrunEvent):
    type: Literal[EventType.ARTIFACT_DELETED] = EventType.ARTIFACT_DELETED


class ArtifactMovedEvent(PeldrunEvent):
    type: Literal[EventType.ARTIFACT_MOVED] = EventType.ARTIFACT_MOVED


__all__ = [
    "EventType",
    "PeldrunEvent",
    "BaseEvent",
    "AgentEvent",
    "EVENT_PAYLOAD_MAP",
    "SnapshotPayload",
    "StepStartPayload",
    "ThoughtPayload",
    "AgentActivityPayload",
    "ToolCallPayload",
    "ObservationPayload",
    "StepEndPayload",
    "FinalPayload",
    "ErrorPayload",
    "AskHumanPayload",
    "ArtifactPayload",
    "SnapshotEvent",
    "StepStartEvent",
    "ThoughtEvent",
    "AgentActivityEvent",
    "ToolCallEvent",
    "ObservationEvent",
    "StepEndEvent",
    "FinalEvent",
    "ErrorEvent",
    "AskHumanEvent",
    "ArtifactCreatedEvent",
    "ArtifactUpdatedEvent",
    "ArtifactDeletedEvent",
    "ArtifactMovedEvent",
]