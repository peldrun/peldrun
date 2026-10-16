"""
Pydantic request models for the run router.

These schemas are shared by several endpoint submodules, so they live in a
dedicated module to avoid circular imports and to keep a single source of
truth for the public request contract.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# [FIX-3.1] Structured reference to a workspace file the user explicitly
# mentioned in the prompt. Produced by the frontend composer when the user
# selects a file from the `@` typeahead menu.
#
# Design notes:
#   - `rel_path` is the canonical workspace-relative path (forward slashes).
#     This is what the backend needs to resolve, validate, and inject.
#   - `file_name` and `origin` are optional metadata for diagnostics and for
#     future UI features (e.g. showing a chip in the message header).
#   - The alias `relPath` is accepted so the frontend can keep its camelCase
#     convention without a translation layer.
class ContextRef(BaseModel):
    """A single workspace file reference attached to a run request."""

    rel_path: str = Field(
        ...,
        alias="relPath",
        description="Workspace-relative path (forward slashes).",
    )
    file_name: Optional[str] = Field(
        default=None,
        alias="fileName",
        description="Display name of the file (basename).",
    )
    origin: Optional[str] = Field(
        default=None,
        description="Source tier: 'workspace' | 'chats' | 'projects'.",
    )

    class Config:
        populate_by_name = True


class RunRequest(BaseModel):
    """Payload accepted by the run-creation endpoint."""

    prompt: str
    project_id: Optional[str] = "default_project"
    max_steps: Optional[int] = 30
    agent_id: Optional[str] = "peldrun"
    chat_id: Optional[str] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    provider_name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    api_type: Optional[str] = None
    mode: Optional[str] = "agent"
    reasoning_effort: Optional[str] = None
    is_reasoning_model: Optional[bool] = None
    generative_ui: Optional[Dict[str, Any]] = None

    # Optional web search for the next direct-chat message only.
    # Ignored entirely in agent mode; never part of llm_override.
    web_search_enabled: Optional[bool] = False

    # [FIX-3.1] Structured file references selected by the user via the `@`
    # typeahead menu. The dispatcher resolves and injects their contents as
    # trusted context before invoking the agent engine.
    context_refs: Optional[List[ContextRef]] = None

    # Extensibility for Pause / Resume and Checkpoint Interruption Resumption
    checkpoint_id: Optional[str] = None
    resume_from_checkpoint: Optional[bool] = False
    regenerate_turn_id: Optional[str] = None


class FileContentPayload(BaseModel):
    """Payload used to persist edited file content back to the workspace."""

    path: str
    content: str


class HumanRespondRequest(BaseModel):
    """Payload carrying a human answer back to a suspended run."""

    answer: str
    request_id: Optional[str] = None


class JobControlResponse(BaseModel):
    """Unified response payload for job lifecycle mutation operations."""

    success: bool
    job_id: str
    chat_id: Optional[str] = None
    status: str
    message: str
    task_cancelled: Optional[bool] = None
