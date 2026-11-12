# backend/omweb/engines/base.py
"""
backend/omweb/engines/base.py

Core Execution Engine Contracts and Runtime Context Interfaces.

Defines the universal execution protocol and immutable runtime payload
shared across all concrete agent execution engines in PELDRUN Web.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

# [P1.5] Memory context type.
#
# ``peldrun.memory.domain`` is a Pydantic + stdlib module with no
# I/O, no storage coupling, and no dependency on ``omweb`` -- the
# import is cheap and cannot form a runtime cycle. Type-safe
# storage is preferred over ``Any`` so that a future refactor of
# ``EngineRunContext`` cannot silently break the dispatcher's
# contract by passing the wrong shape.
from peldrun.memory.domain import MemoryContext


@dataclass(frozen=True)
class EngineRunContext:
    """Immutable runtime payload delivered to an execution engine for a task."""

    job_id: str
    prompt: str
    agent_id: str
    active_llm: Dict[str, Any]
    model_name: str
    provider_name: str
    project_dir: Path
    chat_id: str
    manifest: Dict[str, Any]

    # [FIX-3.3] Structured file references selected by the user via the `@`
    # typeahead menu. Each entry is a dict with the shape:
    #   {"relPath": str, "fileName": str | None, "origin": str | None}
    #
    # The dispatcher is responsible for resolving and injecting their contents
    # as trusted context before the engine builds LLM messages. Engines that
    # don't consume this field simply ignore it.
    #
    # Defaults to None so that all existing engine instantiations remain
    # backward compatible without any call-site changes.
    context_refs: Optional[List[Dict[str, Any]]] = None

    # [FIX-MULTIMODAL] Structured multimodal attachment references for
    # the current turn. Each entry is a dict produced by the composer /
    # chat-landing pipeline with the canonical shape::
    #
    #   {"fileId": str, "mimeType": str | None, "fileName": str | None,
    #    "width": int | None, "height": int | None}
    #
    # The referenced artifacts live in ``storage/artifacts/temp/`` and are
    # resolved to base64 data URIs by the engine immediately before
    # constructing the ``RunRequest``. Non-consumers ignore this field,
    # keeping all existing engine implementations backwards compatible.
    attachments: Optional[List[Dict[str, Any]]] = None

    # [SKILLS] Explicit ``/<skill-id>`` invocations captured from the chat
    # composer. The engine resolves these against the skill registry,
    # merges them with the agent's assigned skills, and injects the
    # combined instruction block into the system prompt.
    #
    # Populated by the dispatcher from ``llm_override["invoked_skills"]``.
    # Always a list of canonical skill IDs (normalized at the composer
    # boundary, re-normalized defensively in ``assignments``).
    invoked_skills: Optional[List[str]] = None

    # [P1.5] Retrieved memory context for the current turn.
    #
    # Produced by the dispatcher via ``MemoryBootstrap.retrieve()``
    # (P1.5) or ``MemoryService.retrieve()`` (P1.7 -- same signature,
    # same return type). Consumed by ``PeldrunEngine.run`` which
    # forwards it to ``RunRequest(memory_context=...)``.
    #
    # ``None`` is the fast path and the default: memory is DISABLED,
    # the NO_NEED route fired, retrieval returned empty, or the
    # retrieval failed. All four cases produce a prompt that is
    # bit-for-bit identical to the pre-P1.5 case.
    #
    # ``MemoryContext`` is TRANSIENT: never persisted, never
    # serialized into a checkpoint, never restored on resume. See
    # ``peldrun.memory.domain.context``'s module docstring for the
    # full transience contract.
    memory_context: Optional[MemoryContext] = None


@runtime_checkable
class ExecutionEngine(Protocol):
    """Universal execution contract required for all registered runtime engines."""

    engine_id: str

    async def run(self, context: EngineRunContext) -> None:
        """Execute an autonomous task within the engine's isolated lifecycle.

        Args:
            context: The immutable runtime configuration and parameters.
        """
        ...

    async def health(self) -> Dict[str, Any]:
        """Inspect and return runtime engine health, availability, and diagnostics.

        Returns:
            Dictionary containing health status, version metadata, and engine attributes.
        """
        ...
