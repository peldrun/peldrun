"""
Pydantic request models for the run router.

These schemas are shared by several endpoint submodules, so they live in a
dedicated module to avoid circular imports and to keep a single source of
truth for the public request contract.

Pydantic V2 migration:
    Class-based ``class Config:`` blocks are deprecated and will be removed
    in Pydantic V3. Every model in this module uses the V2 ``model_config``
    attribute with a ``ConfigDict`` instead. The behavior is identical; only
    the syntax changed.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


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

    model_config = ConfigDict(populate_by_name=True)

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


# Multimodal attachment reference.
#
# Unlike ``ContextRef`` (which carries a workspace-relative path for a
# text file selected via the ``@`` menu), ``AttachmentRef`` refers to an
# image that the frontend already uploaded to the temporary artifact
# store via ``POST /api/files/upload-temp``. The backend never receives
# the image bytes inline; it receives only this lightweight reference
# and resolves the actual bytes on demand (see
# ``omweb.routers.files.resolve_temp_artifact_to_data_uri``).
#
# ``width`` and ``height`` are advisory. They are populated by the
# upload endpoint when Pillow is available, and let downstream consumers
# (the vision context optimizer, the provider payload builder) pick a
# sensible ``detail`` hint without re-probing the file.
#
# ``file_name`` preserves the user's original filename purely for
# display; it is never used to resolve the artifact on disk.
class AttachmentRef(BaseModel):
    """A single temporary image artifact attached to a run request."""

    model_config = ConfigDict(populate_by_name=True)

    file_id: str = Field(
        ...,
        alias="fileId",
        description=(
            "Opaque identifier returned by /api/files/upload-temp. "
            "Resolved to a physical path by the backend; never a user "
            "path."
        ),
    )
    mime_type: Optional[str] = Field(
        default=None,
        alias="mimeType",
        description=(
            "MIME type recorded at upload time (e.g. 'image/png'). "
            "Advisory: the backend re-derives it from the file "
            "extension during resolution."
        ),
    )
    file_name: Optional[str] = Field(
        default=None,
        alias="fileName",
        description=(
            "Original client-side filename, for display only. Never "
            "used to resolve the artifact."
        ),
    )
    width: Optional[int] = Field(
        default=None,
        description=(
            "Image width in pixels, when known. Advisory hint used to "
            "select the provider 'detail' level."
        ),
    )
    height: Optional[int] = Field(
        default=None,
        description=(
            "Image height in pixels, when known. Advisory hint used to "
            "select the provider 'detail' level."
        ),
    )


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

    # Multimodal attachments (images uploaded via /api/files/upload-temp).
    # Kept separate from ``context_refs`` because they follow a different
    # resolution path: ``ContextRef`` inlines a file's text content into
    # the prompt, whereas ``AttachmentRef`` is routed to a vision-capable
    # provider (inline) or to the vision worker (text-only active model).
    #
    # Both fields may be populated on the same request; the two flows are
    # independent and do not conflict.
    attachments: Optional[List[AttachmentRef]] = None

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
