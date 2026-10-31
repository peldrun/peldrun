"""
backend/peldrun/engine/state.py

PELDRUN Core Execution State and Checkpointing Subsystem.
Implements a strict Finite State Machine (FSM) enforcing transition boundaries.
Provides strictly typed, immutable-friendly execution state models with Pydantic v2.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import logging
import time
import uuid
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("peldrun.engine.state")


# ---------------------------------------------------------------------------
# Multimodal image stripping helpers for snapshot persistence
# ---------------------------------------------------------------------------
# Checkpoints and DB state dumps must not embed base64 image payloads.
# A single checkpoint would otherwise be megabytes; 30 steps would be
# hundreds of megabytes per run, and every persisted run would carry a
# duplicate of the same image data. See:
#
#   _strip_image_data_from_snapshot()  — data: URI → artifact-cache://<hash>
#   _reinflate_image_data_from_snapshot()  — reverse, with cache miss fallback
#
# Cache layout:
#   <storage_root>/artifacts/checkpoint_cache/<sha256_16>.png
#
# The 16-hex-digit prefix of the SHA-256 hash provides 64 bits of collision
# resistance — sufficient for deduplicating images across hundreds of runs.
# Files are content-addressed, so two uploads of the same screenshot share
# one cache file regardless of fileId.
# ---------------------------------------------------------------------------


def _get_checkpoint_cache_dir() -> Path:
    """
    Return the checkpoint image cache directory, creating it if needed.

    Lazily resolved so importing ``peldrun.engine.state`` remains cheap
    and side-effect-free (matters for CLI tools that only touch
    ``ExecutionStatus``).
    """
    try:
        from peldrun.storage_paths import get_storage_root

        cache_dir = get_storage_root() / "artifacts" / "checkpoint_cache"
    except Exception:
        # Fallback — should never happen in production, but a broken
        # storage_paths import must not break the agent loop.
        cache_dir = Path(".peldrun") / "artifacts" / "checkpoint_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _hash_image_bytes(raw_bytes: bytes) -> str:
    """Return a 16-hex-digit SHA-256 prefix for content-addressing."""
    return hashlib.sha256(raw_bytes).hexdigest()[:16]


def _mime_to_ext(mime: str) -> str:
    """Map an image MIME type to a cache-friendly file extension."""
    return {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/webp": "webp",
        "image/gif": "gif",
    }.get(mime.lower(), "png")


def _ext_to_mime(ext: str) -> str:
    """Inverse of ``_mime_to_ext``."""
    return {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
        "gif": "image/gif",
    }.get(ext.lower(), "image/png")


def _strip_image_data_from_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """
    Replace inline ``data:image/...;base64,...`` URLs in every message
    with short ``artifact-cache://<hash>.<ext>`` references, writing the
    raw image bytes to the content-addressed cache.

    Non-``data:`` URLs (e.g. ``http(s)://``, already-stripped
    ``artifact-cache://``) are left untouched, so the function is
    idempotent and safe to call on partially-stripped snapshots.

    Mutates ``snapshot`` in place for efficiency but returns it for
    chaining convenience.
    """
    messages = snapshot.get("messages")
    if not isinstance(messages, list):
        return snapshot

    cache_dir = _get_checkpoint_cache_dir()

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") != "image_url":
                continue
            img_payload = part.get("image_url")
            if not isinstance(img_payload, dict):
                continue
            url = img_payload.get("url")
            if not isinstance(url, str) or not url.startswith("data:image/"):
                continue
            if ";base64," not in url:
                continue

            # Parse the mime out of "data:image/png;base64,...."
            try:
                header, b64_data = url.split(";base64,", 1)
                mime = header.split(":", 1)[1] if ":" in header else "image/png"
                raw_bytes = base64.b64decode(b64_data)
            except Exception:
                # Corrupt payload — leave as-is so the checkpoint still
                # reflects the data, just bigger. Better than dropping it.
                continue

            hash_id = _hash_image_bytes(raw_bytes)
            ext = _mime_to_ext(mime)
            cache_path = cache_dir / f"{hash_id}.{ext}"

            # Content-addressed: existing file is authoritative, do not
            # overwrite (concurrent writers produce identical bytes by
            # definition of the hash, so a race is benign).
            if not cache_path.exists():
                try:
                    cache_path.write_bytes(raw_bytes)
                except Exception as exc:
                    logger.debug(
                        "checkpoint cache write failed for %s: %s",
                        cache_path,
                        exc,
                    )
                    # Continue anyway — the reference will fail at
                    # inflate time and produce a cache-miss placeholder.

            img_payload["url"] = f"artifact-cache://{hash_id}.{ext}"

    return snapshot


def _reinflate_image_data_from_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """
    Reverse ``_strip_image_data_from_snapshot``: convert every
    ``artifact-cache://<hash>.<ext>`` URL back into a full data URI by
    reading the cached bytes.

    Cache misses (e.g. cache cleaned up, or written on a different
    machine) are replaced with a placeholder text URL. The LLM provider
    rejects invalid ``image_url`` payloads with a 400 error, so we swap
    the entire part for a text placeholder rather than sending a broken
    URL. This keeps the agent functional (it simply loses the historical
    image) instead of aborting the resumed run.

    Non-``artifact-cache://`` URLs are left untouched.
    """
    messages = snapshot.get("messages")
    if not isinstance(messages, list):
        return snapshot

    cache_dir = _get_checkpoint_cache_dir()

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue

        # Rebuild content in place so cache-miss placeholders can be
        # swapped from image_url parts to text parts.
        new_content: List[Any] = []
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "image_url":
                new_content.append(part)
                continue

            img_payload = part.get("image_url")
            if not isinstance(img_payload, dict):
                new_content.append(part)
                continue

            url = img_payload.get("url")
            if not isinstance(url, str) or not url.startswith("artifact-cache://"):
                new_content.append(part)
                continue

            filename = url.replace("artifact-cache://", "", 1)
            # Reject path traversal in a persisted snapshot — the hash
            # is hex, so any separator is a corruption/tampering signal.
            if "/" in filename or "\\" in filename or ".." in filename:
                new_content.append(
                    {"type": "text", "text": "[image evicted: corrupt cache ref]"}
                )
                continue

            cache_path = cache_dir / filename
            if not cache_path.is_file():
                new_content.append(
                    {"type": "text", "text": "[image evicted: cache miss]"}
                )
                continue

            try:
                raw_bytes = cache_path.read_bytes()
                b64 = base64.b64encode(raw_bytes).decode("ascii")
                ext = filename.rsplit(".", 1)[-1] if "." in filename else "png"
                mime = _ext_to_mime(ext)
                img_payload["url"] = f"data:{mime};base64,{b64}"
                new_content.append(part)
            except Exception as exc:
                logger.debug(
                    "checkpoint cache read failed for %s: %s", cache_path, exc
                )
                new_content.append(
                    {"type": "text", "text": "[image evicted: unreadable cache]"}
                )

        msg["content"] = new_content

    return snapshot


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
    """Represents an atomic message within the agent reasoning context.

    Multimodal support
    ------------------
    ``content`` accepts either:

      * a plain text string (the historical behavior — unchanged);
      * a list of OpenAI-style multimodal parts, e.g.::

            [
                {"type": "text", "text": "What's in this image?"},
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64,...",
                               "detail": "auto"}},
            ]

    The list is preserved verbatim by ``to_llm_dict`` so that the LLM
    provider receives the exact multimodal payload assembled by
    ``CoreRuntimeExecutor`` — the agent loop no longer needs to
    serialise or flatten image parts when persisting state.

    ``ExecutionState.add_message`` transparently accepts both shapes;
    any caller that previously passed a string continues to work
    without modification.
    """
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    role: MessageRole = Field(..., description="Role of the message sender")
    content: Optional[Union[str, List[Dict[str, Any]]]] = Field(
        default=None,
        description=(
            "Textual content (``str``) for text-only turns, or a list of "
            "OpenAI-style multimodal content parts (``list`` of dicts) "
            "for turns that carry images. Both shapes are forwarded "
            "verbatim to the provider."
        ),
    )
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
        """Export clean payload suitable for LLM provider APIs.

        Multimodal content lists are forwarded unchanged — no
        serialisation, no flattening. Text content is forwarded as-is.
        Missing/empty content is omitted, matching the OpenAI contract.
        """
        payload: Dict[str, Any] = {"role": self.role.value}
        if self.content is not None:
            # Both ``str`` and ``List[Dict]`` pass through verbatim.
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
        """Create and append an immutable snapshot of the current state.

        Multimodal image data (base64 payloads) is stripped from the
        checkpoint's ``state_dump`` and replaced with content-addressed
        ``artifact-cache://<hash>`` references. This bounds the size of
        every checkpoint to kilobytes regardless of how many images the
        turn carries.

        The live state (``self.messages``) is **not** modified — the
        full data URIs remain available for the next LLM call. Only the
        on-disk / in-memory ``checkpoints`` list is compacted, because
        checkpoints are historical records, not live payloads.
        """
        raw_dump = self.model_dump(mode="json", exclude={"checkpoints"})
        stripped_dump = _strip_image_data_from_snapshot(raw_dump)
        checkpoint = Checkpoint(
            step=self.current_step,
            state_dump=copy.deepcopy(stripped_dump),
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
    def to_snapshot_dict(self, strip_images: bool = False) -> Dict[str, Any]:
        """
        Serialize current state to a plain JSON-compatible dict.

        Parameters
        ----------
        strip_images : bool
            When ``True``, every inline ``data:image/...;base64,...`` URL
            inside message content is replaced with a compact
            ``artifact-cache://<hash>.<ext>`` reference, and the raw
            bytes are written to the content-addressed checkpoint cache.
            This shrinks the serialized snapshot from megabytes to
            kilobytes for any turn that contains images.

            Callers persisting the snapshot to disk (checkpoint JSON,
            SQLite ``runs.state_json``) MUST pass ``strip_images=True``.
            Callers that need the full payload (rare — usually only
            tests and in-memory diagnostics) keep the default ``False``.

        Returns
        -------
        Dict[str, Any]
            JSON-serializable snapshot, ready for ``json.dumps``.
        """
        data = self.model_dump(mode="json", exclude={"checkpoints"})
        if strip_images:
            data = _strip_image_data_from_snapshot(data)
        return data

    @classmethod
    def from_snapshot_dict(
        cls,
        data: Dict[str, Any],
        inflate_images: bool = False,
    ) -> "ExecutionState":
        """
        Reconstruct an ExecutionState from a dumped snapshot.

        Parameters
        ----------
        data : Dict[str, Any]
            Snapshot produced by ``to_snapshot_dict``.

        inflate_images : bool
            When ``True``, every ``artifact-cache://<hash>.<ext>``
            reference inside message content is resolved back into a
            full ``data:image/...;base64,...`` URI by reading the
            cached bytes. Cache misses are replaced with a text
            placeholder so the resumed run can continue with the
            historical image missing rather than aborting.

            Callers loading a snapshot from disk (checkpoint JSON,
            SQLite ``runs.state_json``) MUST pass ``inflate_images=True``
            so the restored state matches what the LLM expects.
        """
        if inflate_images:
            # Deep-copy so we never mutate the caller's dict — some
            # callers (e.g. ``checkpoint.arestore_state``) cache the
            # original snapshot for later re-use.
            data = copy.deepcopy(data)
            _reinflate_image_data_from_snapshot(data)
        return cls(**data)

    # -------------------------------------------------------------------- llm
    def get_llm_messages(self) -> List[Dict[str, Any]]:
        """Extract clean message list formatted directly for LLM provider API requests."""
        return [msg.to_llm_dict() for msg in self.messages]
