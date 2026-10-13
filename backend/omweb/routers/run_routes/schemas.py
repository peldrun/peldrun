"""
Pydantic request models for the run router.

These schemas are shared by several endpoint submodules, so they live in a
dedicated module to avoid circular imports and to keep a single source of
truth for the public request contract.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel


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


class FileContentPayload(BaseModel):
    """Payload used to persist edited file content back to the workspace."""

    path: str
    content: str


class HumanRespondRequest(BaseModel):
    """Payload carrying a human answer back to a suspended run."""

    answer: str
    request_id: Optional[str] = None
